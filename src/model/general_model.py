from transformers.models.bert.modeling_bert import BertModel, BertPreTrainedModel, BertConfig
import torch.nn as nn
import torch
import torch.utils.checkpoint
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss
from transformers.modeling_outputs import (
    ModelOutput,
)
from dataclasses import dataclass
from typing import Optional, List

from src.model.universal_model import UniversalOutput, get_combination_mask


class GeneralModel(BertPreTrainedModel):
    def __init__(self, config: BertConfig,
                 diff_param_for_height:bool=True,
                 height: int = 4,
                 constant_num: int = 0,
                 add_replacement: bool = False):
        super().__init__(config)
        self.num_labels = config.num_labels ## should be 6
        assert self.num_labels == 6
        self.config = config

        self.bert = BertModel(config)
        self.add_replacement = bool(add_replacement)

        self.label_rep2label = nn.Linear(config.hidden_size, 1) # 0 or 1
        self.diff_param_for_height = diff_param_for_height
        self.max_height = height ## 3 operation
        self.linears = nn.ModuleList()
        if diff_param_for_height:
            for h in range(self.max_height):
                current_linears = nn.ModuleList()
                for i in range(self.num_labels):
                    current_linears.append(nn.Sequential(
                        nn.Linear(3 * config.hidden_size, config.hidden_size),
                        nn.ReLU(),
                        nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps),
                        nn.Dropout(config.hidden_dropout_prob)
                    ))
                self.linears.append(current_linears)
        else:
            for i in range(self.num_labels):
                self.linears.append(nn.Sequential(
                    nn.Linear(3 * config.hidden_size, config.hidden_size),
                    nn.ReLU(),
                    nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps),
                    nn.Dropout(config.hidden_dropout_prob)
                ))
        self.stopper_transformation = nn.Sequential(
                    nn.Linear(config.hidden_size, config.hidden_size),
                    nn.ReLU(),
                    nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps),
                    nn.Dropout(config.hidden_dropout_prob)
                )

        self.stopper = nn.Linear(config.hidden_size, 2) ## whether we need to stop or not.
        self.variable_gru = nn.GRUCell(config.hidden_size, config.hidden_size)
        self.constant_num = constant_num
        self.constant_emb = None
        if self.constant_num > 0:
            self.const_rep = nn.Parameter(torch.randn(self.constant_num, config.hidden_size))
            self.multihead_attention = nn.MultiheadAttention(embed_dim=config.hidden_size, num_heads=6)


        self.init_weights()


    def forward(self,
        input_ids=None, ## batch_size  x max_seq_length
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        variable_indexs_start: torch.Tensor = None, ## batch_size x num_variable
        variable_indexs_end: torch.Tensor = None,  ## batch_size x num_variable
        num_variables: torch.Tensor = None, # batch_size [3,4]
        variable_index_mask:torch.Tensor = None, # batch_size x num_variable
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        ## (batch_size, height, 4). (left_var_index, right_var_index, label_index, stop_label) when height>=1, left_var_index always -1, because left always m0
        label_height_mask = None, #  (batch_size, height)
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        is_eval=False
    ):
        r"""
                labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
                    Labels for computing the sequence classification/regression loss. Indices should be in :obj:`[0, ...,
                    config.num_labels - 1]`. If :obj:`config.num_labels == 1` a regression loss is computed (Mean-Square loss),
                    If :obj:`config.num_labels > 1` a classification loss is computed (Cross-Entropy).
                """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        outputs = self.bert( # batch_size, sent_len, hidden_size,
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        batch_size, sent_len, hidden_size = outputs.last_hidden_state.size()
        if labels is not None and not is_eval:
            # is_train
            _, max_height, _ = labels.size()
        else:
            max_height = self.max_height

        _, max_num_variable = variable_indexs_start.size()

        var_start_hidden_states = torch.gather(outputs.last_hidden_state, 1, variable_indexs_start.unsqueeze(-1).expand(batch_size, max_num_variable,  hidden_size))
        var_end_hidden_states = torch.gather(outputs.last_hidden_state, 1, variable_indexs_end.unsqueeze(-1).expand(batch_size, max_num_variable, hidden_size))
        var_hidden_states = var_start_hidden_states + var_end_hidden_states
        if self.constant_num > 0:
            constant_hidden_states = self.const_rep.unsqueeze(0).expand(batch_size, self.constant_num, hidden_size)
            var_hidden_states = torch.cat([constant_hidden_states, var_hidden_states], dim=1)
            num_variables = num_variables + self.constant_num
            max_num_variable = max_num_variable + self.constant_num
            const_idx_mask = torch.ones((batch_size, self.constant_num), device=variable_indexs_start.device)
            variable_index_mask = torch.cat([const_idx_mask, variable_index_mask], dim = 1)

        best_mi_label_rep = None
        loss = 0
        all_logits = []
        for i in range(max_height):
            linear_modules = self.linears[i] if self.diff_param_for_height else self.linears
            if i == 0:
                ## max_num_variable = 4. -> [0,1,2,3]
                num_var_range = torch.arange(0, max_num_variable, device=variable_indexs_start.device)
                ## 6x2 matrix
                combination = torch.combinations(num_var_range, r=2, with_replacement=self.add_replacement)  ##number_of_combinations x 2
                num_combinations, _ = combination.size()  # number_of_combinations x 2
                # batch_size x num_combinations. 2*6
                batched_combination_mask = get_combination_mask(batched_num_variables=num_variables, combination=combination)  # batch_size, num_combinations

                var_comb_hidden_states = torch.gather(var_hidden_states, 1, combination.view(-1).unsqueeze(0).unsqueeze(-1).expand(batch_size, num_combinations * 2, hidden_size))
                # m0_hidden_states = var_comb_hidden_states.unsqueeze(-2).view(batch_size, num_combinations, 2, hidden_size * 3).sum(dim=-2)
                expanded_var_comb_hidden_states = var_comb_hidden_states.unsqueeze(-2).view(batch_size, num_combinations, 2, hidden_size)
                m0_hidden_states = torch.cat([expanded_var_comb_hidden_states[:, :, 0, :], expanded_var_comb_hidden_states[:, :, 1, :], expanded_var_comb_hidden_states[:, :, 0, :] * expanded_var_comb_hidden_states[:, :, 1, :]], dim=-1)
                # batch_size, num_combinations/num_m0, hidden_size: 2,6,768

                ## batch_size, num_combinations/num_m0, num_labels, hidden_size
                m0_label_rep = torch.stack([layer(m0_hidden_states) for layer in linear_modules], dim=2)
                ## batch_size, num_combinations/num_m0, num_labels
                m0_logits = self.label_rep2label(m0_label_rep).expand(batch_size, num_combinations, self.num_labels, 2)
                m0_logits = m0_logits + batched_combination_mask.unsqueeze(-1).unsqueeze(-1).expand(batch_size, num_combinations, self.num_labels, 2).log()
                ## batch_size, num_combinations/num_m0, num_labels, 2
                m0_stopper_logits = self.stopper(self.stopper_transformation(m0_label_rep))

                ## batch_size, num_combinations/num_m0, num_labels, 2
                m0_combined_logits = m0_logits + m0_stopper_logits

                all_logits.append(m0_combined_logits)
                best_temp_logits, best_stop_label =  m0_combined_logits.max(dim=-1) ## batch_size, num_combinations/num_m0, num_labels
                best_temp_score, best_temp_label = best_temp_logits.max(dim=-1) ## batch_size, num_combinations
                best_m0_score, best_comb = best_temp_score.max(dim=-1) ## batch_size
                best_label = torch.gather(best_temp_label, 1, best_comb.unsqueeze(-1)).squeeze(-1)## batch_size

                b_idxs = [k for k in range(batch_size)]
                # best_m0_label_rep = m0_label_rep[b_idxs, best_comb, best_label] # batch_size x hidden_size
                # best_mi_label_rep = best_m0_label_rep
                ## NOTE: add loosss
                if labels is not None and not is_eval:
                    m0_gold_labels = labels[:, i, :] ## batch_size x 4 (left_var_index, right_var_index, label_index, stop_id)
                    m0_gold_comb = m0_gold_labels[:, :2].unsqueeze(1).expand(batch_size, num_combinations, 2)
                    batched_comb = combination.unsqueeze(0).expand(batch_size, num_combinations, 2)
                    judge = m0_gold_comb == batched_comb
                    judge = judge[:, :, 0] * judge[:, :, 1] #batch_size, num_combinations
                    judge = judge.nonzero()[:,1] #batch_size

                    m0_gold_scores = m0_combined_logits[b_idxs, judge, m0_gold_labels[:, 2], m0_gold_labels[:, 3]] ## batch_size
                    loss = loss +  (best_m0_score - m0_gold_scores).sum()

                    best_mi_label_rep = m0_label_rep[b_idxs, judge, m0_gold_labels[:, 2]] ## teacher-forcing.
                else:
                    best_m0_label_rep = m0_label_rep[b_idxs, best_comb, best_label] # batch_size x hidden_size
                    best_mi_label_rep = best_m0_label_rep
            else:
                ## update hidden_state (gated hidden state)
                init_h = best_mi_label_rep.unsqueeze(1).expand(batch_size, max_num_variable + i - 1, hidden_size).contiguous().view(-1, hidden_size)
                gru_inputs = var_hidden_states.view(-1, hidden_size)
                var_hidden_states = self.variable_gru(gru_inputs, init_h).view(batch_size, max_num_variable + i - 1, hidden_size)

                num_var_range = torch.arange(0, max_num_variable + i, device=variable_indexs_start.device)
                ## 6x2 matrix
                combination = torch.combinations(num_var_range, r=2, with_replacement=self.add_replacement)  ##number_of_combinations x 2
                num_combinations, _ = combination.size()  # number_of_combinations x 2
                batched_combination_mask = get_combination_mask(batched_num_variables=num_variables + i, combination=combination)



                var_hidden_states = torch.cat([best_mi_label_rep.unsqueeze(1), var_hidden_states], dim=1) ## batch_size x (num_var + i) x hidden_size
                var_comb_hidden_states = torch.gather(var_hidden_states, 1, combination.view(-1).unsqueeze(0).unsqueeze(-1).expand(batch_size, num_combinations * 2, hidden_size))
                expanded_var_comb_hidden_states = var_comb_hidden_states.unsqueeze(-2).view(batch_size, num_combinations, 2, hidden_size)
                mi_hidden_states = torch.cat( [expanded_var_comb_hidden_states[:, :, 0, :], expanded_var_comb_hidden_states[:, :, 1, :],
                                        expanded_var_comb_hidden_states[:, :, 0, :] * expanded_var_comb_hidden_states[:, :, 1, :]], dim=-1)
                mi_label_rep = torch.stack([layer(mi_hidden_states) for layer in linear_modules], dim=2)
                mi_logits = self.label_rep2label(mi_label_rep).expand(batch_size, num_combinations, self.num_labels, 2)
                mi_logits = mi_logits + batched_combination_mask.unsqueeze(-1).unsqueeze(-1).expand(batch_size, num_combinations, self.num_labels, 2).log()

                mi_stopper_logits = self.stopper(self.stopper_transformation(mi_label_rep))
                mi_combined_logits = mi_logits + mi_stopper_logits
                all_logits.append(mi_combined_logits)
                best_temp_logits, best_stop_label = mi_combined_logits.max(dim=-1)  ## batch_size, num_combinations/num_m0, num_labels
                best_temp_score, best_temp_label = best_temp_logits.max(dim=-1)  ## batch_size, num_combinations
                best_m0_score, best_comb = best_temp_score.max(dim=-1)  ## batch_size
                best_label = torch.gather(best_temp_label, 1, best_comb.unsqueeze(-1)).squeeze(-1)  ## batch_size

                b_idxs = [k for k in range(batch_size)]
                ## NOTE: add loosss
                if labels is not None and not is_eval:
                    m0_gold_labels = labels[:, i, :]  ## batch_size x 4 (left_var_index, right_var_index, label_index, stop_id)
                    m0_gold_comb = m0_gold_labels[:, :2].unsqueeze(1).expand(batch_size, num_combinations, 2)
                    batched_comb = combination.unsqueeze(0).expand(batch_size, num_combinations, 2)
                    judge = m0_gold_comb == batched_comb
                    judge = judge[:, :, 0] * judge[:, :, 1]  # batch_size, num_combinations
                    judge = judge.nonzero()[:, 1]  # batch_size

                    mi_gold_scores = mi_combined_logits[b_idxs, judge, m0_gold_labels[:, 2], m0_gold_labels[:, 3]]  ## batch_size
                    height_mask = label_height_mask[:, i]  ## batch_size
                    current_loss = (best_m0_score - mi_gold_scores) * height_mask ## avoid compute loss for unnecessary height
                    loss = loss + current_loss.sum()
                    best_mi_label_rep = mi_label_rep[b_idxs, judge, m0_gold_labels[:, 2]]  ## teacher-forcing.
                else:
                    best_mi_label_rep = mi_label_rep[b_idxs, best_comb, best_label]  # batch_size x hidden_size


        return UniversalOutput(loss=loss, all_logits=all_logits)


def test_case_batch_two():
    import random
    import numpy as np
    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    model = GeneralModel.from_pretrained('hfl/chinese-roberta-wwm-ext', num_labels=6, constant_num=0, add_replacement=True, height=4)
    model.eval()
    from transformers import BertTokenizer
    tokenizer = BertTokenizer.from_pretrained('hfl/chinese-roberta-wwm-ext')
    uni_labels = [
        '+', '-', '-_rev', '*', '/', '/_rev'
    ]
    text1 = "一本笔记本 <quant> 元钱, 王小明共带了 <quant> 元, 他一共能买多少本这样的笔记本?"  ## x= temp_b / temp_a
    text2 = "爸爸买来 <quant> 个桃子, 吃了 <quant> 个, 妈妈又买来 <quant> 个桃子, 现在有多少个桃子?"  ##x= temp_a - temp_b + temp_c"
    ## tokens = ['一', '本', '笔', '记', '本', '<', 'q', '##uan', '##t', '>', '元', '钱', ',', '王', '小', '明', '共', '带', '了', '<', 'q', '##uan', '##t', '>', '元', ',', '他', '一', '共', '能', '买', '多', '少', '本', '这', '样', '的', '笔', '记', '本', '?']
    res = tokenizer.batch_encode_plus([text1, text2], return_tensors='pt', padding=True)
    input_ids = res["input_ids"]
    attention_mask = res["attention_mask"]
    token_type_ids = res["token_type_ids"]
    variable_indexs_start = torch.tensor([[6, 20, 0], [5, 16, 28]])
    variable_indexs_end = torch.tensor([[10, 24, 0], [9, 20, 32]])
    num_variables = torch.tensor([2, 3])
    variable_index_mask = torch.tensor([[1, 1, 0], [1, 1, 1]])

    ## batch_size = 2, height=2, 3
    labels = torch.tensor([
        [
            [
                0, 1, uni_labels.index('/_rev'), 1
            ],
            [
                0, 0, 0, 0 ## 3 means, for this one, we directly forward
            ],
            [
                0, 0, 0, 0 ## 3 means, for this one, we directly forward
            ]
        ],
        [
            [
                0, 1, uni_labels.index('-'), 0
            ],
            [
                0, 3, uni_labels.index('+'), 1
            ],
            [
                0, 0, 0, 0 ## 3 means, for this one, we directly forward
            ]
        ]
    ])
    label_height_mask = torch.tensor(
        [
            [
                1, 0, 0
            ],
            [
                1, 1, 0
            ]
        ]
    )
    print(label_height_mask.size())
    print(labels.size())
    print(model(input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
                variable_indexs_start=variable_indexs_start,
                variable_indexs_end=variable_indexs_end,
                num_variables=num_variables,
                variable_index_mask=variable_index_mask,
                label_height_mask = label_height_mask,
                labels=labels))

if __name__ == '__main__':
    test_case_batch_two()