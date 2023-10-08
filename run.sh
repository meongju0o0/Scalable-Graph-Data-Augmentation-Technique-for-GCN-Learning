#!/bin/bash
python -m augmentation.augmentation augmentation.drop
python -m common.calc common.config common.create_batch
python -m training.evaluation training.loss training.model training.node_classification
python /workspace/launch.py --workspace /workspace --num_trainers 1 --num_samplers 0 --num_servers 1 --part_config data/ogbn-products.json --ip_config ip_config.txt "python ./training/node_classification.py --graph_name ogbn-products --ip_config ip_config.txt --num_epochs 30 --batch_size 1000"
