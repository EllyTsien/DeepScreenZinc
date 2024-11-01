import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings('ignore')
import paddle as pdl
from paddle import optimizer 
import numpy as np
import json
from rdkit import RDLogger
RDLogger.DisableLog('rdApp.*') # 屏蔽RDKit的warning
from pahelix.datasets.inmemory_dataset import InMemoryDataset
import random
import pandas as pd
from pprint import pprint
import paddle.nn as nn
import paddle.nn.functional as F
import os
import wandb

#
from finetunemodels import mlp
from preprocess import Input_ligand_preprocess,  SMILES_Transfer
from evaluation_train import evaluation_train
from prediction import ModelTester
from dataloader import collate_fn, get_data_loader, sort_and_filter_csv
from pahelix.model_zoo.gem_model import GeoGNNModel

def exempt_parameters(src_list, ref_list):
    """Remove element from src_list that is in ref_list"""
    res = []
    for x in src_list:
        flag = True
        for y in ref_list:
            if x is y:
                flag = False
                break
        if flag:
            res.append(x)
    return res


# def trial(model_version, model, batch_size, criterion, scheduler, opt):
def run_finetune(params):
    finetune_model_layer, lr, head_lr, dropout_rate, ft_time, batch_size, project_name, finetune_dataset, model_version = params
    seed = random.randint(0, 1000000)  # 可以根据需要调整范围
    # finetune_model_layer =json.load(open(finetune_model_layer, 'r'))
    if finetune_model_layer=='mlp4':
        finetune_model = mlp.MLP4()
    elif finetune_model_layer=='mlp6':
        finetune_model = mlp.MLP6()
    
    # Initialize wandb with project name and config
    wandb.init(project=project_name, config={
        "seed": seed,
        "finetunemodel": finetune_model_layer,
        "dataset": finetune_dataset, 
        "batch_size": batch_size,
        "learning_rate": float(lr),
        "head_lr": float(head_lr),
        "finetune time": float(ft_time),
        "dropout rate": float(dropout_rate),
        "model_details": str(finetune_model_layer)
    })
    config = wandb.config  # Use wandb config for consistency
    # Log model architecture
    wandb.config.update({"model_details": str(finetune_model_layer)}, allow_val_change=True)
    np.random.seed(config.seed)
    random.seed(config.seed)
    
    #model construction
    compound_encoder_config = json.load(open('GEM/model_configs/geognn_l8.json', 'r')) 
    compound_encoder = GeoGNNModel(compound_encoder_config)
    encoder_params = compound_encoder.parameters()
    head_params = exempt_parameters(finetune_model.parameters(), encoder_params)
    criterion = nn.CrossEntropyLoss() 
    encoder_scheduler = optimizer.lr.CosineAnnealingDecay(learning_rate=config.learning_rate, T_max=15)
    head_scheduler = optimizer.lr.CosineAnnealingDecay(learning_rate=config.head_lr, T_max=15)
    encoder_opt = optimizer.Adam(encoder_scheduler, parameters=encoder_params, weight_decay=1e-5)
    head_opt = optimizer.Adam(head_scheduler, parameters=head_params, weight_decay=1e-5)

    # 创建dataloader
    train_data_loader, valid_data_loader = get_data_loader(mode='train', batch_size=batch_size, index=0)   
    current_best_metric = -1e10
    max_bearable_epoch = 50    # 设置早停的轮数为50，若连续50轮内验证集的评价指标没有提升，则停止训练
    current_best_epoch = 0
    train_metric_list = []     # 记录训练过程中各指标的变化情况
    valid_metric_list = []
    for epoch in range(800):   # 设置最多训练800轮
        finetune_model.train()
        for (atom_bond_graph, bond_angle_graph, label_true_batch) in train_data_loader:
            label_predict_batch = finetune_model(atom_bond_graph, bond_angle_graph)
            label_true_batch = pdl.to_tensor(label_true_batch, dtype=pdl.int64, place=pdl.CUDAPlace(0))
            loss = criterion(label_predict_batch, label_true_batch)
            loss.backward()   # 反向传播
            encoder_opt.step()   # 更新参数
            head_opt.step()   # 更新参数
            encoder_opt.clear_grad()
            head_opt.clear_grad()
        encoder_scheduler.step()   # 更新学习率
        head_scheduler.step() # 更新学习率
        # 评估模型在训练集、验证集的表现
        evaluator = evaluation_train(finetune_model, train_data_loader, valid_data_loader)
        metric_train = evaluator.evaluate(train_data_loader)
        metric_valid = evaluator.evaluate(valid_data_loader)
        train_metric_list.append(metric_train)
        valid_metric_list.append(metric_valid)
        score = round((metric_valid['ap'] + metric_valid['auc']) / 2, 4)
        if score > current_best_metric:
            # 保存score最大时的模型权重
            current_best_metric = score
            current_best_epoch = epoch
            pdl.save(finetune_model.state_dict(), "weight/" + model_version + ".pkl")

            # save best model config to .json
            best_model_info = {
                "finetune_model_layer": finetune_model_layer,
                "learning_rate": lr,
                "head_learning_rate": head_lr,
                "dropout_rate": dropout_rate,
                "finetune_time": ft_time,
                "batch_size": batch_size,
                "project_name": project_name,
                "finetune_dataset": finetune_dataset,
                "model_version": model_version
            }
            # 确保目标文件夹存在
            os.makedirs("finetunemodels", exist_ok=True)
            # 将配置信息保存为JSON文件
            with open("finetunemodels/best.json", "w") as json_file:
                json.dump(best_model_info, json_file, indent=4)

        print("=========================================================")
        print("Epoch", epoch)
        pprint(("Train", metric_train))
        pprint(("Validate", metric_valid))
        print('current_best_epoch', current_best_epoch, 'current_best_metric', current_best_metric)
        for metric in ['accuracy', 'ap', 'auc', 'f1', 'precision', 'recall']:
            wandb.log({
                f"train_{metric}": metric_train[metric].tolist(),  # Log the last value for simplicity
                f"valid_{metric}": metric_valid[metric].tolist()
            })
        if epoch > current_best_epoch + max_bearable_epoch:
            break

    # Finish the run
    wandb.finish()
    return train_metric_list, valid_metric_list        

# 将测试集的预测结果保存为result.csv
def test(model_version, index):
    # from best.json import config
    with open("finetunemodels/best.json", "r") as json_file:
        best_model_info = json.load(json_file)
    if best_model_info["finetune_model_layer"] == "mlp4":
        ft_model = mlp.MLP4()
    elif best_model_info["finetune_model_layer"] == "mlp6":
        ft_model = mlp.MLP6()
    else:
        raise ValueError("Unknown model configuration specified in best.json")    
    
    test_data_loader = get_data_loader(mode='test', batch_size=best_model_info["batch_size"], index=index)

    ft_model.set_state_dict(pdl.load("weight/" + model_version + ".pkl"))   # 导入训练好的的模型权重
    ft_model.eval()
    all_result = []
    for (atom_bond_graph, bond_angle_graph, label_true_batch) in test_data_loader:
        label_predict_batch = ft_model(atom_bond_graph, bond_angle_graph)
        label_predict_batch = F.softmax(label_predict_batch)
        result = label_predict_batch[:, 1].cpu().numpy().reshape(-1).tolist()
        all_result.extend(result)
    nolabel_file_path = f'datasets/ZINC20_processed/{index}_ZINC20_nolabel.csv'
    df = pd.read_csv(nolabel_file_path)
    # df = pd.read_csv('datasets/ZINC20_processed/test_nolabel.csv')
    df['pred'] = all_result
    result_file_path = 'datasets/DL_pred/result.csv'
    # 检查文件是否存在
    if index == 1:
        if os.path.exists(result_file_path):
            # 如果文件存在，则覆盖
            df.to_csv(result_file_path, index=False)
        else:
            # 如果文件不存在，则创建文件并写入数据
            df.to_csv(result_file_path, index=False)
    else: 
        if os.path.exists(result_file_path):
            # 如果文件存在，则追加数据
            df.to_csv(result_file_path, mode='a', header=False, index=False)
        else:
            # 如果文件不存在，则创建文件并写入数据
            df.to_csv(result_file_path, index=False)
    print(f'Screen through {index}_ZINC20_nolabel.csv')

    