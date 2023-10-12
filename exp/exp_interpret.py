import os, torch
from tqdm import tqdm 
import pandas as pd
from utils.explainer import *
from utils.tsr_tunnel import *
from tint.metrics import mae, mse, accuracy, cross_entropy, lipschitz_max, log_odds, sufficiency, comprehensiveness
from utils.auc import auc
from datetime import datetime

from captum.attr import (
    DeepLift,
    GradientShap,
    IntegratedGradients,
    Lime,
    FeaturePermutation
)

from tint.attr import (
    AugmentedOcclusion,
    DynaMask,
    Fit, TimeForwardTunnel,
    Occlusion, 
    FeatureAblation
)

expl_metric_map = {
    'mae': mae, 'mse': mse, 'accuracy': accuracy, 
    'cross_entropy':cross_entropy, 'lipschitz_max': lipschitz_max,
    'log_odds': log_odds, 'auc': auc, 
    # 'comprehensiveness': comprehensiveness, 'sufficiency': sufficiency    
}

explainer_name_map = {
    "deep_lift":DeepLift,
    "gradient_shap":GradientShap,
    "integrated_gradients":IntegratedGradients,
    "lime":Lime,
    "occlusion":Occlusion,
    "augmented_occlusion":AugmentedOcclusion, # requires data when initializing
    "dyna_mask":DynaMask, # needs additional arguments when initializing
    "fit": Fit,
    "feature_ablation":FeatureAblation,
    "feature_permutation":FeaturePermutation
}

class Exp_Interpret:
    def __init__(
        self, exp, dataloader
    ) -> None:
        assert not exp.args.output_attention, 'Model needs to output target only'
        self.exp = exp
        self.args = exp.args
        self.result_folder = exp.output_folder
        self.device = exp.device
        
        print(f'explainers: {exp.args.explainers}\n areas: {exp.args.areas}\n metrics: {exp.args.metrics}\n')
        
        exp.model.eval().zero_grad()
        self.model = exp.model
        
        self.explainers_map = dict()
        for name in exp.args.explainers:
            if name == 'augmented_occlusion':
                add_x_mark = exp.args.task_name != 'classification'
                all_inputs = get_total_data(dataloader, self.device, add_x_mark=add_x_mark)
                self.explainers_map[name] = explainer_name_map[name](self.model, all_inputs)
            else:
                explainer = explainer_name_map[name](self.model)
                if isinstance(explainer, GradientAttribution):
                    explainer = TimeForwardTunnel(explainer)
                self.explainers_map[name] = explainer
                
    def run_classifier(self, dataloader, name):
        # results = [['batch_index', 'metric', 'area', 'value']]
        results = [['batch_index', 'metric', 'area', 'comp', 'suff']]
        progress_bar = tqdm(
            enumerate(dataloader), total=len(dataloader), 
            disable=self.args.disable_progress
        )
        for batch_index, (batch_x, label, padding_mask) in progress_bar:
            batch_x = batch_x.float().to(self.device)
            padding_mask = padding_mask.float().to(self.device)
            # print('label', label)
            # label = label.long() if self.multiclass else label.float()
            # label = label.to(self.device) 
            # output = self.model(batch_x, padding_mask, None, None)
             
            inputs = batch_x
            # baseline must be a scaler or tuple of tensors with same dimension as input
            baselines = get_baseline(inputs, mode=self.args.baseline_mode)
            additional_forward_args = (padding_mask, None, None)

            # get attributions
            batch_results = self.evaluate_classifier(
                name, inputs, baselines, 
                additional_forward_args, batch_index
            )
            results.extend(batch_results)  

        return results
                
    def run_regressor(self, dataloader, name):
        results = [['batch_index', 'metric', 'area', 'comp', 'suff']]
        progress_bar = tqdm(
            enumerate(dataloader), total=len(dataloader), 
            disable=self.args.disable_progress
        )
        for batch_index, (batch_x, batch_y, batch_x_mark, batch_y_mark) in progress_bar:
            batch_x = batch_x.float().to(self.device)
            batch_y = batch_y.float().to(self.device)

            batch_x_mark = batch_x_mark.float().to(self.device)
            batch_y_mark = batch_y_mark.float().to(self.device)
            # decoder input
            dec_inp = torch.zeros_like(batch_y[:, -self.args.pred_len:, :]).float()
            dec_inp = torch.cat([batch_y[:, :self.args.label_len, :], dec_inp], dim=1).float()
            # outputs = model(batch_x, batch_x_mark, dec_inp, batch_y_mark)
            
            inputs = (batch_x, batch_x_mark)
            # baseline must be a scaler or tuple of tensors with same dimension as input
            baselines = get_baseline(inputs, mode=self.args.baseline_mode)
            additional_forward_args = (dec_inp, batch_y_mark)

            # get attributions
            batch_results = self.evaluate_regressor(
                name, inputs, baselines, 
                additional_forward_args, batch_index
            )
            results.extend(batch_results)
        
        return results
    
    def interpret(self, dataloader):
        if self.args.tsr:
            print('Interpreting with TSR enabled.')
            
        task = self.args.task_name
        
        for name in self.args.explainers:
            results = []
            start = datetime.now()
            print(f'Running {name} from {start}')
            
            if task == 'classification':
                results = self.run_classifier(dataloader, name)
            else:
                results = self.run_regressor(dataloader, name)
            
            end = datetime.now()
            print(f'Experiment ended at {end}. Total time taken {end - start}.')
            if self.args.tsr:
                self.dump_results(results, f'tsr_{name}.csv')
            else:
                self.dump_results(results, f'{name}.csv')
                
    def evaluate_classifier(
        self, name, inputs, baselines, 
        additional_forward_args, batch_index
    ):
        explainer = self.explainers_map[name]
        if self.args.tsr:
            explainer = TSRTunnel(explainer)
            if type(inputs) == tuple:
                sliding_window_shapes = tuple([(1,1) for _ in inputs])
                strides = tuple([1 for _ in inputs])
            else:
                sliding_window_shapes = (1, 1)
                strides = 1
                
            attr = compute_classifier_tsr_attr(
                self.args, explainer, inputs=inputs, 
                sliding_window_shapes=sliding_window_shapes, 
                strides=strides, baselines=baselines,
                additional_forward_args=additional_forward_args
            )
        else:
            attr = compute_classifier_attr(
                inputs, baselines, explainer, 
                additional_forward_args, self.args
            )
    
        results = []
        # get scores
        for area in self.args.areas:
            # otherwise it is classification
            # ['accuracy', 'log_odds', 'cross_entropy']
            for metric_name in self.args.metrics:
                metric = expl_metric_map[metric_name]
                # masks top k% features
                error_comp = metric(
                    self.model, inputs=inputs, 
                    attributions=attr, baselines=baselines, 
                    additional_forward_args=additional_forward_args,
                    topk=area
                )
                
                if metric_name in ['comprehensiveness', 'sufficiency', 'log_odds']:
                    # these metrics has not mask_largest parameter
                    error_suff = 0
                else:
                    error_suff = metric(
                        self.model, inputs=inputs, 
                        attributions=attr, baselines=baselines, 
                        additional_forward_args=additional_forward_args,
                        topk=area, mask_largest=False
                    )
        
                result_row = [batch_index, metric_name, area, error_comp, error_suff]
                results.append(result_row)
    
        return results
            
    def evaluate_regressor(
        self, name, inputs, baselines, 
        additional_forward_args, batch_index
    ):
        explainer = self.explainers_map[name]
        if self.args.tsr:
            explainer = TSRTunnel(explainer)
            if type(inputs) == tuple:
                sliding_window_shapes = tuple([(1,1) for _ in inputs])
                strides = tuple([1 for _ in inputs])
            else:
                sliding_window_shapes = (1, 1)
                strides = 1
                
            attr = compute_regressor_tsr_attr(
                self.args, explainer, inputs=inputs, 
                sliding_window_shapes=sliding_window_shapes, 
                strides=strides, baselines=baselines,
                additional_forward_args=additional_forward_args
            )
        else:
            attr = compute_regressor_attr(
                inputs, baselines, explainer, additional_forward_args, self.args
            )
    
        results = []
        # get scores
        for area in self.args.areas:
            for metric_name in self.args.metrics: # ['mae', 'mse']
                metric = expl_metric_map[metric_name]
                error_comp = metric(
                    self.model, inputs=inputs, 
                    attributions=attr, baselines=baselines, 
                    additional_forward_args=additional_forward_args,
                    topk=area, mask_largest=True
                )
                
                error_suff = metric(
                    self.model, inputs=inputs, 
                    attributions=attr, baselines=baselines, 
                    additional_forward_args=additional_forward_args,
                    topk=area, mask_largest=False
                )
        
                result_row = [batch_index, metric_name, area, error_comp, error_suff]
                results.append(result_row)
    
        return results
        
    def dump_results(self, results, filename):
        # if self.args.task_name == 'classification':
        #     results_df = pd.DataFrame(results[1:], columns=results[0])
        #     results_df = results_df.groupby(['metric', 'area'])[
        #         ['value']].aggregate('mean').reset_index()
        # else:
        results_df = pd.DataFrame(results[1:], columns=results[0])
        results_df = results_df.groupby(['metric', 'area'])[
            ['comp', 'suff']
        ].aggregate('mean').reset_index()
        
        filepath = os.path.join(self.result_folder, filename)
        results_df.round(6).to_csv(filepath, index=False)
        print(results_df)