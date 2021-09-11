#!/bin/bash





diff_param=0
filtered_steps=-1
use_constant=1
add_replacement=1
consider_multiple_m0=1
model_folder=math23k_val_acc_replace_mm

TOKENIZERS_PARALLELISM=false \
python3 universal_main.py --device=cuda:1 --model_folder=${model_folder} --height=6 --num_epochs=100 --consider_multiple_m0=${consider_multiple_m0} \
                          --train_file=data/math23k/train23k_processed_all.json --add_replacement=${add_replacement} \
                          --dev_file=data/math23k/test23k_processed_all.json --use_constant=${use_constant} \
                          --diff_param_for_height=${diff_param} --fp16=1 --filtered_steps ${filtered_steps} > logs/${model_folder}.log 2>&1

