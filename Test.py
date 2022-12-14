# 导入包部分-----------------------------------------------
from tqdm import tqdm  # 显示进度条的工具
import pandas as pd  # 处理数据的
import os
from functools import partial
import numpy as np
import time   # 时间

import torch  # 包括多种用于多维张量的数据结构
import torch.nn as nn  # 为神经网络设计的模块化接口
import torch.nn.functional as F

from torch.utils.data import DataLoader  # 迭代产生训练数据提供给模型
from torch.utils.data.dataset import Dataset  # 需要读取的数据集
from transformers import BertPreTrainedModel, BertTokenizer, BertConfig, BertModel, AutoConfig  # 预训练模型
from functools import partial
from transformers import AdamW, get_linear_schedule_with_warmup  # 优化器

'''
import os
os.environ["CUDA_VISIBLE_DEVICES"] = '3'
'''


# 加载数据部分-------------------------------------------------

with open('data/train_dataset_v2.tsv', 'r', encoding='utf-8') as handler:
    lines = handler.read().split('\n')[1:-1]  # 读取数据按符号\n划分

    data = list()
    for line in tqdm(lines):
        sp = line.split('\t')
        if len(sp) != 4:
            print("Error: ", sp)
            continue
        data.append(sp)

train = pd.DataFrame(data)  # 训练数据集用dataframe来显示数据，列名为:id,剧本内容，角色，情感
train.columns = ['id', 'content', 'character', 'emotions']

test = pd.read_csv('data/test_dataset.tsv', sep='\t')  # 读取测试数据集
submit = pd.read_csv('data/submit_example.tsv', sep='\t')  # 读取赛题给出的提交示范
train = train[train['emotions'] != '']  # 情感列不能为空的


# 数据处理部分，将原本单列的情感值处理为6个不同的情感值列，生成新的csv文件--------------------------------------------------

# astype将dataframe的任何列转化为其他类型的数据
# 令text列内容为：原本的剧本内容加上角色名字
train['text'] = train['content'].astype(str) + '角色: ' + train['character'].astype(str)
test['text'] = test['content'].astype(str) + ' 角色: ' + test['character'].astype(str)
# apply:返回括号内函数的值
# 训练集的情感列内容变为按逗号分开的原来的数据，类似0,0,0,0,0,0表示爱、乐、惊、怒、恐、哀六个维度的值，情感值范围是[0, 1, 2, 3]，0-没有，1-弱，2-中，3-强，
train['emotions'] = train['emotions'].apply(lambda x: [int(_i) for _i in x.split(',')])
# 定义为对应情感值
train[['love', 'joy', 'fright', 'anger', 'fear', 'sorrow']] = train['emotions'].values.tolist()
test[['love', 'joy', 'fright', 'anger', 'fear', 'sorrow']] =[0,0,0,0,0,0]
# 将处理过的数据转换为csv文件，可以进行查看，训练集
train.to_csv('data/train.csv',columns=['id', 'content', 'character','text','love', 'joy', 'fright', 'anger', 'fear', 'sorrow'],
            sep='\t',
            index=False)
# 测试集
test.to_csv('data/test.csv', columns=['id', 'content', 'character','text','love', 'joy', 'fright', 'anger', 'fear', 'sorrow'],
            sep='\t',
            index=False)

# 定义dataset部分--------------------------------------------------------------------------

# 目标列为六个情感值对应的列
target_cols = ['love', 'joy', 'fright', 'anger', 'fear', 'sorrow']

# 数据集的构建，标签一共有6个
class RoleDataset(Dataset):
    def __init__(self, tokenizer, max_len, mode='train'):  # mode模式,默认模式为训练模式；tokenizer为分词器
        super(RoleDataset, self).__init__()
        # 当模式不同时加载不同文件的数据
        if mode == 'train':
            self.data = pd.read_csv('data/train.csv',sep='\t')  # \t代表自动补全为8的整数倍，因为包含了中文；训练
        else:
            self.data = pd.read_csv('data/test.csv',sep='\t')  # 测试
        self.texts=self.data['text'].tolist()  # 获取text列的内容，即之前处理成的角色加剧本内容
        self.labels=self.data[target_cols].to_dict('records')   # 获取情感值列的内容并转换为字典的records类似于[{列名：内容，列名：内容}，{。。}，]形式
        self.tokenizer = tokenizer  # 确定分词器
        self.max_len = max_len  # 最大长度

    def __getitem__(self, index):
        # indec为数据索引，迭代第index条数据
        text=str(self.texts[index])  # 按索引值获取text列的某个具体内容
        label=self.labels[index]    # 获取某行情感值的某个具体内容，含六个值，因为在初始化函数里定义过了
        # 该函数返回所有编码信息
        encoding=self.tokenizer.encode_plus(text,   # 需要encode的文本内容
                                            add_special_tokens=True,    # 添加’【cls】‘and '[SEP]'
                                            max_length=self.max_len,    # 内容最大长度
                                            return_token_type_ids=True,
                                            pad_to_max_length=True,
                                            return_attention_mask=True,
                                            return_tensors='pt',)  # 返回信息
        # 存储编码信息
        sample = {
            'texts': text,
            'input_ids': encoding['input_ids'].flatten(),   # 拉平数组，其中input_ids是单词在词典中的编码
            'attention_mask': encoding['attention_mask'].flatten()   # 拉平，attention_mask 指定对哪些词进行self_attention操作
        }

        # 遍历目标目标情感值的
        for label_col in target_cols:
            sample[label_col] = torch.tensor(label[label_col]/3.0, dtype=torch.float)  # tensor存储和变换数据
        return sample

    def __len__(self):
        return len(self.texts)  # 返回文本内容

# 创建dataloader--------------------------------------------------------------
def create_dataloader(dataset, batch_size, mode='train'):
    shuffle = True if mode == 'train' else False  # shuffle=true代表对batch进行打乱

    if mode == 'train':  # 训练模式时
        # 参数：dataset为传入的数据集，batch_size为每个batch有多少个样本，在后面有定义，shuffle只在每个epoch开始是对数据进行重新打乱，epoch指训练的次数
        data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)
    else:   # 测试模式时
        data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)
    return data_loader
# dataloader将数据集（Dataset）自动形成一个一个的Batch，以用于批处理使用。建立DataLoader需要先建立数据集



# 加载预训练模型-----------------------------------------------------------------
# roberta模型，为BERT模型的改进版，这里注意写一下
# 预训练模型的名字，这些模型在前面导入过，最后生成的文件也包括了这个名字，做PPT的时候要分析这个模型
PRE_TRAINED_MODEL_NAME = 'hfl/chinese-roberta-wwm-ext'  # 'hfl/chinese-roberta-wwm-ext'
# 分词器
tokenizer = BertTokenizer.from_pretrained(PRE_TRAINED_MODEL_NAME)
# 预训练模型
base_model = BertModel.from_pretrained(PRE_TRAINED_MODEL_NAME)  # 加载预训练模型
# model = ppnlp.transformers.BertForSequenceClassification.from_pretrained(MODEL_NAME, num_classes=2)


# 模型构建------------------------------------------------------------------------
def init_params(module_lst):   # 参数为一个列表，在下文***
    for module in module_lst:   # 这里的的每个module为列表的元素如：self.out_love,该值在下面分析
        for param in module.parameters():   # parameters()为获取网络的参数
            if param.dim() > 1:
                torch.nn.init.xavier_uniform_(param)    # 对获取到的参数进行处理初始化
    return

    # 本次爱奇艺剧本模型
class IQIYModelLite(nn.Module):
    def __init__(self, n_classes, model_name):  # 初始化函数传入模型名字

        super(IQIYModelLite, self).__init__()   # 初始化的方法，随机初始化模型权值
        config = AutoConfig.from_pretrained(model_name)     # 该函数可以加载指定模型的config对象，config对象会作为参数传给模型
        config.update({"output_hidden_states": True,
                       "hidden_dropout_prob": 0.0,
                       "layer_norm_eps": 1e-7})  # 更新相关配置

        self.base = BertModel.from_pretrained(model_name, config=config)    # 预训练模型下载，需要获取模型名和配置

        dim = 1024 if 'large' in model_name else 768    # bert有好几种变体，中文的是我们采用的没有large模型的名字里面如果有large的话dim值为1024否则为768

        self.attention = nn.Sequential(  # sequential为一个序列容器，用于搭建神经网络的模块被按照被传入构造器的顺序添加*
            nn.Linear(dim, 512),    # linear函数表示将dim的输入转变为512的输出，代表特征数，与样本数无关*
            nn.Tanh(),  # 激活函数双曲正切函数，正弦除以余弦*
            nn.Linear(512, 1),  # 从512的输入变成1的输出
            nn.Softmax(dim=1)   # 归一化指数函数*
        )
        # self.attention = AttentionHead(h_size=dim, hidden_dim=512, w_drop=0.0, v_drop=0.0)

        self.out_love = nn.Sequential(  # sequential为一个有序容器，用于搭建神经网络的模块被按照被传入构造器的顺序添加
            nn.Linear(dim, n_classes)   # linear代表将dim个输入转变为n_classes的输出，注意与样本数无关
        )
        self.out_joy = nn.Sequential(
            nn.Linear(dim, n_classes)
        )
        self.out_fright = nn.Sequential(
            nn.Linear(dim, n_classes)
        )
        self.out_anger = nn.Sequential(
            nn.Linear(dim, n_classes)
        )
        self.out_fear = nn.Sequential(
            nn.Linear(dim, n_classes)
        )
        self.out_sorrow = nn.Sequential(
            nn.Linear(dim, n_classes)
        )
        # 调用模型构建的方法，参数为上面得到的各个情感值对应的  模块按顺序添加的有序序列
        init_params([self.out_love, self.out_joy, self.out_fright, self.out_anger,  # []即为上文的参数module_list,调用模型构建方法
                     self.out_fear,  self.out_sorrow, self.attention])



    def forward(self, input_ids, attention_mask):
        roberta_output = self.base(input_ids=input_ids,
                                   attention_mask=attention_mask)

        last_layer_hidden_states = roberta_output.hidden_states[-1]
        weights = self.attention(last_layer_hidden_states)
        # print(weights.size())
        context_vector = torch.sum(weights*last_layer_hidden_states, dim=1)
        # context_vector = weights

        love = self.out_love(context_vector)    # 索引值索引到该情感值的不同模块
        joy = self.out_joy(context_vector)
        fright = self.out_fright(context_vector)
        anger = self.out_anger(context_vector)
        fear = self.out_fear(context_vector)
        sorrow = self.out_sorrow(context_vector)

        return {    # 返回不同情感值各模块的结果值，总的来看是一个数组，有输出可以在终端看一下
            'love': love, 'joy': joy, 'fright': fright,
            'anger': anger, 'fear': fear, 'sorrow': sorrow,
        }

# 参数配置-------------------------------------------------------------


# 改成1了
EPOCHS = 1  # 代表将训练集完整跑了一次
weight_decay = 0.0  # 权重衰减
data_path = 'data'  # 数据的路径
warmup_proportion = 0.0  # 预热学习率
batch_size = 16  # 表示单词传递给程序用以训练的样本个数
lr = 1e-5   # 学习率，从前面的0增加到这个值，后面再线性降到0，它控制了权重的更新比率，较大的值（如 0.3）在学习率更新前会有更快的初始学习，而较小的值（如 1.0E-5）会令训练收敛到更好的性能。
max_len = 128   # 每次输入的最大长度，前面有用到
# 换成了自己cpu跑，定义设置，如果可以用cuda就用cuda 没有就用cpu
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
warm_up_ratio = 0   # 预热步数

#  训练集及其加载，用的前面定义过的方法
trainset = RoleDataset(tokenizer, max_len, mode='train')
train_loader = create_dataloader(trainset, batch_size, mode='train')
#  测试集及其加载
valset = RoleDataset(tokenizer, max_len, mode='test')
valid_loader = create_dataloader(valset, batch_size, mode='test')
# 模型为构建的爱奇艺模型
model = IQIYModelLite(n_classes=1, model_name=PRE_TRAINED_MODEL_NAME)
# 该模型用自己的设置即cpu
model.to(device)
#  在这里判断cuda数是否大于1如果大于1就分布着跑，速度更快但我跑的时候没有用到这个
if torch.cuda.device_count() > 1:
    model = nn.DataParallel(model)
optimizer = AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)  # correct_bias=False,
total_steps = len(train_loader) * EPOCHS    # 总步数，训练集加载的长度乘以总共训练的次数

scheduler = get_linear_schedule_with_warmup(    # 元组
  optimizer,    # 优化器
  num_warmup_steps=warm_up_ratio*total_steps,   # 预热步数*训练总步数
  num_training_steps=total_steps    # 总步数
)
criterion = nn.BCEWithLogitsLoss().to(device)   # 二元交叉熵损失函数


# 模型训练---------------------------------------------------------删了一个metric=none,改了一个date_loader
def do_train(model, train_loader, criterion, optimizer, scheduler): # 准则、优化器，调度器
    model.train()   # 执行训练
    global_step = 0     # 全局步数
    tic_train = time.time()     # 时间
    log_steps = 100     # 定义步数，后面每一百步有一次输出表明训练情况
    for epoch in range(EPOCHS):     # epochs定义为1说明会把训练集数据训练一次
        losses = [] # 存放损失值的列表容器
        # sample为字典包括内容有{text，input_ids，attentions_mask}
        for step, sample in enumerate(train_loader):    # 执行每步的操作，enumerate遍历一个集合对象，它在遍历的同时还可以得到当前元素的索引位置
            input_ids = sample["input_ids"].to(device)      # 单词对应的编码
            attention_mask = sample["attention_mask"].to(device)    # attention_mask 指定对哪些词进行self_attention操作
            outputs = model(input_ids=input_ids, attention_mask=attention_mask)     # 编码值和进行操作的词

            loss_love = criterion(outputs['love'], sample['love'].view(-1, 1).to(device))   # love的损失值
            loss_joy = criterion(outputs['joy'], sample['joy'].view(-1, 1).to(device))
            loss_fright = criterion(outputs['fright'], sample['fright'].view(-1, 1).to(device))
            loss_anger = criterion(outputs['anger'], sample['anger'].view(-1, 1).to(device))
            loss_fear = criterion(outputs['fear'], sample['fear'].view(-1, 1).to(device))
            loss_sorrow = criterion(outputs['sorrow'], sample['sorrow'].view(-1, 1).to(device))
            loss = loss_love + loss_joy + loss_fright + loss_anger + loss_fear + loss_sorrow    # 损失值求和

            losses.append(loss.item())  # item以列表返回可遍历的(键, 值) 元组数组，append连接,记录多次的loss值

            loss.backward()     # 自动求梯度，计算小批量随机梯度

#             nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()    # 优化器的步数
            scheduler.step()    # 调度器的步数
            optimizer.zero_grad()   # 是把梯度置零，也就是把loss关于weight的导数变成0

            global_step += 1    # 全局步数加1

            if global_step % log_steps == 0:    # 全局步数为100的整数倍时进行一次输出，打印当前全局步数，当前训练次数，训练到的数据块数，损失值，速度，学习率
                print("global step %d, epoch: %d, batch: %d, loss: %.5f, speed: %.2f step/s, lr: %.10f"
                      % (global_step, epoch, step, np.mean(losses), global_step / (time.time() - tic_train),
                         float(scheduler.get_last_lr()[0])))


do_train(model, train_loader, criterion, optimizer, scheduler)  # 正式调用函数进行训练


# 模型预测------------------------------------------------------------------
from collections import defaultdict

model.eval()    # 预测，上面训练的时候有一个model.train


test_pred = defaultdict(list)   # 当字典里的key不存在但被查找时，返回的不是keyError而是一个默认值[]
for step, batch in tqdm(enumerate(valid_loader)):   # 根据测试集加载情况显示进度条
    b_input_ids = batch['input_ids'].to(device)     #
    attention_mask = batch["attention_mask"].to(device)
    with torch.no_grad():
        logists = model(input_ids=b_input_ids, attention_mask=attention_mask)
        for col in target_cols:
            out2 = logists[col].sigmoid().squeeze(1)*3.0
            test_pred[col].append(out2.cpu().numpy())

    print(test_pred)
    break


# 模型预测--------------------------------------------------------------------
def predict(model, test_loader):
    val_loss = 0
    test_pred = defaultdict(list)
    model.eval()
    model.to(device)
    for  batch in tqdm(test_loader):
        b_input_ids = batch['input_ids'].to(device)
        attention_mask = batch["attention_mask"].to(device)
        with torch.no_grad():
            logists = model(input_ids=b_input_ids, attention_mask=attention_mask)
            for col in target_cols:
                out2 = logists[col].sigmoid().squeeze(1)*3.0
                test_pred[col].extend(out2.cpu().numpy().tolist())

    return test_pred

# 加载submit------------------------------------------------------------------
submit = pd.read_csv('data/submit_example.tsv', sep='\t')
test_pred = predict(model, valid_loader)


# 查看结果---------------------------------------------------------------------
print(test_pred['love'][:10])
print(len(test_pred['love']))


# 预测结果和输出----------------------------------------------------------------
label_preds = []
for col in target_cols:
    preds = test_pred[col]
    label_preds.append(preds)
print(len(label_preds[0]))
sub = submit.copy()
sub['emotion'] = np.stack(label_preds, axis=1).tolist()
sub['emotion'] = sub['emotion'].apply(lambda x: ','.join([str(i) for i in x]))
sub.to_csv('baseline_{}.tsv'.format(PRE_TRAINED_MODEL_NAME.split('/')[-1]), sep='\t', index=False)
sub.head()