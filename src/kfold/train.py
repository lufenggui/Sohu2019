import os

os.chdir("../.")
import h5py
from argparse import ArgumentParser
import torch
from pytorch_pretrained_bert.optimization import BertAdam, warmup_linear
from ignite.engine import Engine, Events, create_supervised_trainer, create_supervised_evaluator
from ignite.handlers import ModelCheckpoint, EarlyStopping
from ignite.contrib.handlers import CustomPeriodicEvent
from ignite.contrib.handlers.tqdm_logger import ProgressBar
# from torch.optim.lr_scheduler import ReduceLROnPlateau
# from ignite.contrib.handlers.param_scheduler import (ConcatScheduler,
#                                                      CosineAnnealingScheduler,
#                                                      LinearCyclicalScheduler,
#                                                      CyclicalScheduler,
#                                                      LRScheduler)
# from torch.optim.lr_scheduler import ExponentialLR

from ignite.metrics import RunningAverage
from ignite.contrib.handlers.tensorboard_logger import *
from metric import FScore
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset
from models import NetY3
from loss import FocalLoss
from utils import load_data


# torch.random.manual_seed(42)


def get_all_data():
    print("get all data...........")
    # -------------------------------read from h5-------------------------
    if not args.lite:
        f = h5py.File("../datasets/full.h5")
    else:
        f = h5py.File("../datasets/lite.h5")
    input_ids_trn = torch.from_numpy(f["train/input_ids"][()])
    myinput_ids_trn = torch.from_numpy(f["train/myinput_ids"][()])
    input_mask_trn = torch.from_numpy(f["train/input_mask"][()])
    segment_ids_trn = torch.from_numpy(f["train/segment_ids"][()])
    label_ent_ids_trn = torch.from_numpy(f["train/label_ent_ids"][()])
    label_emo_ids_trn = torch.from_numpy(f["train/label_emo_ids"][()])
    assert input_ids_trn.size() == segment_ids_trn.size() == label_ent_ids_trn.size() == label_emo_ids_trn.size() == myinput_ids_trn.size()

    input_ids_val = torch.from_numpy(f["val/input_ids"][()])
    myinput_ids_val = torch.from_numpy(f["val/myinput_ids"][()])
    input_mask_val = torch.from_numpy(f["val/input_mask"][()])
    segment_ids_val = torch.from_numpy(f["val/segment_ids"][()])
    label_ent_ids_val = torch.from_numpy(f["val/label_ent_ids"][()])
    label_emo_ids_val = torch.from_numpy(f["val/label_emo_ids"][()])
    assert input_ids_val.size() == segment_ids_val.size() == label_ent_ids_val.size() == label_emo_ids_val.size() == myinput_ids_val.size()
    f.close()
    print("read h5 over!")
    input_ids = torch.cat([input_ids_trn, input_ids_val], dim=0)
    myinput_ids = torch.cat([myinput_ids_trn, myinput_ids_val], dim=0)
    input_mask = torch.cat([input_mask_trn, input_mask_val], dim=0)
    segment_ids = torch.cat([segment_ids_trn, segment_ids_val], dim=0)
    label_ent_ids = torch.cat([label_ent_ids_trn, label_ent_ids_val], dim=0)
    label_emo_ids = torch.cat([label_emo_ids_trn, label_emo_ids_val], dim=0)
    dataset = TensorDataset(input_ids, myinput_ids, input_mask, segment_ids, label_ent_ids, label_emo_ids)

    return dataset


def get_data_loader(dataset, cv):
    print(f"get dataloader {cv}")
    # 从 index 中取出 trn_dataset val_dataset
    if not args.lite:
        index_file = "kfold/5cv_indexs_{}".format(cv)
    else:
        index_file = "kfold/5cv_indexs_{}_lite".format(cv)

    if os.path.exists(index_file):
        trn_index, val_index = load_data(index_file)
        trn_dataset = [dataset[idx] for idx in trn_index]
        val_dataset = [dataset[idx] for idx in val_index]
    else:
        print("Not find index file!")
        os._exit(-1)
    # ---------------------------------------------------------------------
    trn_dataloader = DataLoader(trn_dataset, sampler=RandomSampler(trn_dataset), batch_size=args.batch_size,
                                num_workers=args.nw, pin_memory=True)
    # trn_dataloader = DataLoader(trn_dataset, sampler=RandomSampler(trn_dataset), batch_size=args.batch_size,
    #                              pin_memory=True)
    val_dataloader = DataLoader(val_dataset, sampler=SequentialSampler(val_dataset), batch_size=args.val_batch_size,
                                num_workers=args.nw, pin_memory=True)
    # val_dataloader = DataLoader(val_dataset, sampler=SequentialSampler(val_dataset), batch_size=args.val_batch_size,
    #                             pin_memory=True)
    print("get date loader over!")
    return trn_dataloader, val_dataloader, len(trn_dataset)


def train(dataset, cv):
    print(f"training  cv: {cv}")

    ################################ Model Config ###################################
    if args.lbl_method == "BIO":
        num_labels_emo = 4 # O POS NEG NORM
        num_labels_ent = 3  # O B I
    else:
        num_labels_emo = 4 # O POS NEG NORM
        num_labels_ent = 5  # O B I E S

    model = NetY3.from_pretrained(args.bert_model,
                                  cache_dir="",
                                  num_labels_ent=num_labels_ent,
                                  num_labels_emo=num_labels_emo,
                                  dp=args.dp)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model = torch.nn.DataParallel(model)

    ################################# hyper parameters ###########################
    # alpha = 0.5 # 0.44
    # alpha = 0.6 # 0.42
    # alpha = 0.7
    # alpha = 1.2
    # alpha = 0.8
    # alpha = 0.7
    # alphas = [1, 0.9, 0.8, 0.8, 0.8, 0.8, 0.8]
    alpha = args.alpha
    # alphas = [2,1,1,0.8,0.8,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5]
    # ------------------------------ load model from file -------------------------
    model_file = os.path.join(args.checkpoint_model_dir, args.ckp)
    if os.path.exists(model_file):
        model.load_state_dict(torch.load(model_file))
        print("load checkpoint: {} successfully!".format(model_file))
    # -----------------------------------------------------------------------------

    trn_dataloader, val_dataloader, trn_size = get_data_loader(dataset, cv)

    ############################## Optimizer ###################################
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if (not any(nd in n for nd in no_decay)) and p.requires_grad],
         'weight_decay': args.wd},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay) and p.requires_grad],
         'weight_decay': 0.0}
    ]
    # num_train_optimization_steps = int( trn_size / args.batch_size / args.gradient_accumulation_steps) * args.epochs
    num_train_optimization_steps = int(trn_size / args.batch_size) * args.epochs + 5
    optimizer = BertAdam(optimizer_grouped_parameters, lr=args.lr,
                         warmup=args.warmup_proportion,
                         t_total=num_train_optimization_steps)
    # optimizer = Adam(filter(lambda p:p.requires_grad, model.parameters()), args.lr, weight_decay=5e-3)
    ######################################################################
    if not args.focal:
        criterion = torch.nn.CrossEntropyLoss()
    else:
        criterion = FocalLoss(args.gamma)

    def step(engine, batch):
        model.train()
        batch = tuple(t.to(device) for t in batch)
        input_ids, myinput_ids, input_mask, segment_ids, label_ent_ids, label_emo_ids = batch

        optimizer.zero_grad()
        act_logits_ent, act_y_ent, act_logits_emo, act_y_emo, act_myinput_ids = model(
            input_ids, myinput_ids, segment_ids, input_mask,
            label_ent_ids, label_emo_ids)
        # Only keep active parts of the loss
        loss_ent = criterion(act_logits_ent, act_y_ent)

        loss_emo = criterion(act_logits_emo, act_y_emo)
        # loss = alphas[engine.state.epoch-1] * loss_ent + loss_emo
        if not args.multi:
            loss = alpha * loss_ent + loss_emo
        else:
            alphas = [1e-5, 1e-2, 1e-1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
            print("alphas: ", " ".join(map(str, alpha)))
            loss = loss_ent + alphas[engine.state.epoch - 1] * loss_emo

        if engine.state.metrics.get("total_loss") is None:
            engine.state.metrics["total_loss"] = 0
            engine.state.metrics["ent_loss"] = 0
            engine.state.metrics["emo_loss"] = 0
        else:
            engine.state.metrics["total_loss"] += loss.item()
            engine.state.metrics["ent_loss"] += loss_ent.item()
            engine.state.metrics["emo_loss"] += loss_emo.item() if act_logits_emo.size(0) > 0 else 0

        loss.backward()
        optimizer.step()
        return loss.item(), act_logits_ent, act_y_ent, act_logits_emo, act_y_emo, act_myinput_ids  # [-1, 11]

    def infer(engine, batch):
        model.eval()
        batch = tuple(t.to(device) for t in batch)
        input_ids, myinput_ids, input_mask, segment_ids, label_ent_ids, label_emo_ids = batch

        with torch.no_grad():
            act_logits_ent, act_y_ent, act_logits_emo, act_y_emo, act_myinput_ids = model(
                input_ids, myinput_ids, segment_ids,
                input_mask,
                label_ent_ids, label_emo_ids)
            # Only keep active parts of the loss
            loss_ent = criterion(act_logits_ent, act_y_ent)
            # loss_emo = criterion_fl(act_logits_emo, act_y_emo)
            loss_emo = criterion(act_logits_emo, act_y_emo)
            # loss = alphas[engine.state.epoch-1] * loss_ent + loss_emo
            loss = alpha * loss_ent + loss_emo

            if engine.state.metrics.get("total_loss") is None:
                engine.state.metrics["total_loss"] = 0
                engine.state.metrics["ent_loss"] = 0
                engine.state.metrics["emo_loss"] = 0 if loss_emo else 0
            else:
                engine.state.metrics["total_loss"] += loss.item()
                engine.state.metrics["ent_loss"] += loss_ent.item()
                engine.state.metrics["emo_loss"] += loss_emo.item() if act_logits_emo.size(0) > 0 else 0
        # act_logits = torch.argmax(torch.softmax(act_logits, dim=-1), dim=-1)  # [-1, 1]
        # loss = loss.mean()
        return loss.item(), act_logits_ent, act_y_ent, act_logits_emo, act_y_emo, act_myinput_ids  # [-1, 11]

    trainer = Engine(step)
    trn_evaluator = Engine(infer)
    val_evaluator = Engine(infer)

    ############################## Custom Period Event ###################################
    cpe1 = CustomPeriodicEvent(n_epochs=1)
    cpe1.attach(trainer)
    cpe2 = CustomPeriodicEvent(n_epochs=2)
    cpe2.attach(trainer)
    cpe3 = CustomPeriodicEvent(n_epochs=3)
    cpe3.attach(trainer)
    cpe5 = CustomPeriodicEvent(n_epochs=5)
    cpe5.attach(trainer)

    ############################## My F1 ###################################
    F1 = FScore(output_transform=lambda x: [x[1], x[2], x[3], x[4], x[-1]], lbl_method="BIEOS")
    F1.attach(val_evaluator, "F1")

    #####################################  progress bar #########################
    RunningAverage(output_transform=lambda x: x[0]).attach(trainer, 'batch_loss')
    pbar = ProgressBar(persist=True)
    pbar.attach(trainer, metric_names=["batch_loss"])

    #####################################  Evaluate #########################

    @trainer.on(Events.EPOCH_COMPLETED)
    def compute_val_metric(engine):
        # trainer engine
        engine.state.metrics["total_loss"] /= engine.state.iteration
        engine.state.metrics["ent_loss"] /= engine.state.iteration
        engine.state.metrics["emo_loss"] /= engine.state.iteration
        pbar.log_message(
            "Training - total_loss: {:.4f} ent_loss: {:.4f} emo_loss: {:.4f}".format(engine.state.metrics["total_loss"],
                                                                                     engine.state.metrics["ent_loss"],
                                                                                     engine.state.metrics["emo_loss"]))

        val_evaluator.run(val_dataloader)

        metrics = val_evaluator.state.metrics
        ent_loss = metrics["ent_loss"]
        emo_loss = metrics["emo_loss"]
        f1 = metrics['F1']
        pbar.log_message(
            "Validation Results - Epoch: {}  Ent_loss: {:.4f}, Emo_loss: {:.4f}, F1: {:.4f}"
                .format(engine.state.epoch, ent_loss, emo_loss, f1))

        pbar.n = pbar.last_print_n = 0

    @val_evaluator.on(Events.EPOCH_COMPLETED)
    def reduct_step(engine):
        engine.state.metrics["total_loss"] /= engine.state.iteration
        engine.state.metrics["ent_loss"] /= engine.state.iteration
        engine.state.metrics["emo_loss"] /= engine.state.iteration
        pbar.log_message(
            "Validation - total_loss: {:.4f} ent_loss: {:.4f} emo_loss: {:.4f}".format(
                engine.state.metrics["total_loss"],
                engine.state.metrics["ent_loss"],
                engine.state.metrics["emo_loss"]))
        # Save a trained model and the associated configuration
        # model_to_save = model.module if hasattr(model,
        #                                         'module') else model  # Only save the model it-self
        # output_model_file = f"../ckps/cv/cv{cv}.pth"
        # torch.save(model.state_dict(), output_model_file)
        # print(f"save {output_model_file} successfully!")

    ######################################################################

    ############################## checkpoint ###################################
    def best_f1(engine):
        f1 = engine.state.metrics["F1"]
        # loss = engine.state.metrics["loss"]
        return f1

    if not args.lite:
        ckp_dir = os.path.join(args.checkpoint_model_dir, "full", "cv", str(cv), args.hyper_cfg)
    else:
        ckp_dir = os.path.join(args.checkpoint_model_dir, "lite", "cv", str(cv), args.hyper_cfg)
    
    checkpoint_handler = ModelCheckpoint(ckp_dir,
                                         'ckp',
                                         # save_interval=args.checkpoint_interval,
                                         score_function=best_f1,
                                         score_name="F1",
                                         n_saved=5,
                                         require_empty=False, create_dir=True)

    # trainer.add_event_handler(event_name=Events.EPOCH_COMPLETED, handler=checkpoint_handler,
    #                            to_save={'model_3FC': model})
    
    val_evaluator.add_event_handler(event_name=Events.EPOCH_COMPLETED, handler=checkpoint_handler,
                                   to_save={'model_title': model})

    ######################################################################

    ############################## earlystopping ###################################
    stopping_handler = EarlyStopping(patience=2, score_function=best_f1, trainer=trainer)
    val_evaluator.add_event_handler(Events.COMPLETED, stopping_handler)

    ######################################################################

    #################################### tb logger ##################################
    # 在已经在对应基础上计算了 metric 的值 (compute_metric) 后 取值 log
    if not args.lite:
        tb_logger = TensorboardLogger(log_dir=os.path.join(args.log_dir, "full", "cv", str(cv), args.hyper_cfg))
    else:
        tb_logger = TensorboardLogger(log_dir=os.path.join(args.log_dir, "lite", "cv", str(cv), args.hyper_cfg))

    tb_logger.attach(trainer,
                     log_handler=OutputHandler(tag="training", output_transform=lambda x: {'batchloss': x[0]}),
                     event_name=Events.ITERATION_COMPLETED)

    tb_logger.attach(val_evaluator,
                     log_handler=OutputHandler(tag="validation", output_transform=lambda x: {'batchloss': x[0]}),
                     event_name=Events.ITERATION_COMPLETED)

    tb_logger.attach(trainer,
                     log_handler=OutputHandler(tag="training", metric_names=["total_loss", "ent_loss", "emo_loss"]),
                     event_name=Events.EPOCH_COMPLETED)
    # tb_logger.attach(trainer,
    #                  log_handler=OutputHandler(tag="training", output_transform=lambda x: {'loss': x[0]}),
    #                  event_name=Events.EPOCH_COMPLETED)

    '''
    tb_logger.attach(trn_evaluator,
                     log_handler=OutputHandler(tag="training",
                                               metric_names=["F1"],
                                               another_engine=trainer),
                     event_name=Events.EPOCH_COMPLETED)

    '''
    tb_logger.attach(val_evaluator,
                     log_handler=OutputHandler(tag="validation",
                                               metric_names=["total_loss", "ent_loss", "emo_loss", "F1"],
                                               another_engine=trainer),
                     event_name=Events.EPOCH_COMPLETED)

    tb_logger.attach(trainer,
                     log_handler=OptimizerParamsHandler(optimizer, "lr"),
                     event_name=Events.EPOCH_COMPLETED)
    '''

    tb_logger.attach(trainer,
                     log_handler=WeightsScalarHandler(model),
                     event_name=Events.ITERATION_COMPLETED)

    tb_logger.attach(trainer,
                     log_handler=WeightsHistHandler(model),
                     event_name=Events.EPOCH_COMPLETED)

    # tb_logger.attach(trainer,
    #                  log_handler=GradsScalarHandler(model),
    #                  event_name=Events.ITERATION_COMPLETED)

    tb_logger.attach(trainer,
                     log_handler=GradsHistHandler(model),
                     event_name=Events.EPOCH_COMPLETED)
    '''

    # lr_find()
    trainer.run(trn_dataloader, max_epochs=args.epochs)
    tb_logger.close()


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("--bert_model", default="../bert_pretrained/bert-base-chinese", type=str,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                             "bert-base-multilingual-cased, bert-base-chinese.")
    parser.add_argument('--batch_size', type=int, default=48,
                        help='input batch size for training8 (default: 64)')
    parser.add_argument('--val_batch_size', type=int, default=48,
                        help='input batch size for validation (default: 1000)')
    parser.add_argument('--epochs', type=int, default=1,
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--nw', type=int, default=4,
                        help='number of nw')
    parser.add_argument('--lr', type=float, default=3e-5,
                        help='learning rate (default: 0.01)')
    parser.add_argument("--alpha", type=float, default=3, help="alpha")
    parser.add_argument("--wd", type=float, default=0.1, help="weight decay")
    parser.add_argument('--momentum', type=float, default=0.5,
                        help='SGD momentum (default: 0.5)')
    parser.add_argument('--log_interval', type=int, default=10,
                        help='how many batches to wait before logging training status')
    parser.add_argument("--log_dir", type=str, default="../tbs",
                        help="log directory for Tensorboard log output")
    parser.add_argument("--checkpoint_model_dir", type=str, default='../ckps',
                        help="path to folder where checkpoints of trained models will be saved")
    parser.add_argument("--ckp", type=str, default='None',
                        help="ckp file")
    parser.add_argument("--hyper_cfg", type=str, default='default',
                        help="config path to folder where checkpoints of trained models will be saved")
    parser.add_argument("--checkpoint_interval", type=int, default=1,
                        help="number of batches after which a checkpoint of trained model will be created")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--dp",
                        default=0.5,
                        type=float,
                        help="")
    parser.add_argument("--gamma",
                        default=2,
                        type=float,
                        help="")
    parser.add_argument("--focal",
                        action="store_true",
                        help="")
    parser.add_argument("--lite",
                        action="store_true",
                        help="")
    parser.add_argument("--multi",
                        action="store_true",
                        help="multi alpha or not")
    parser.add_argument("--lbl_method",
                        type=str,
                        default="BIO",
                        help="BIO / BIEO")

    args = parser.parse_args()

    # 5 fold
    dataset = get_all_data()
    for cv in range(1, 6):
        train(dataset, cv)
