from argparse import Namespace

from torch.nn import functional as F
from torch.utils.data import DataLoader, random_split
from torch.optim import Adam
from transformers import AdamW
from torch.optim.lr_scheduler import StepLR, ReduceLROnPlateau


from torchnlp.metrics import get_accuracy, get_token_accuracy

from pytorch_lightning import Trainer

from electra_diet.dataset.electra_dataset import ElectraDataset
from electra_diet.model import KoElectraModel

import os, sys
import multiprocessing
# import dill

import torch
import torch.nn as nn
import pytorch_lightning as pl


class KoELECTRAClassifier(pl.LightningModule):
    def __init__(self, hparams):
        super().__init__()
        
        self.hparams = hparams
#         self.model = KoElectraModel(
#             intent_class_num=len(self.dataset.get_intent_idx()),
#             entity_class_num=len(self.dataset.get_entity_idx())
#         )
    
        self.model = KoElectraModel(
            intent_class_num=self.hparams.intent_class_num,
            entity_class_num=self.hparams.entity_class_num
        )

        self.ignore_index = -1
        self.entity_o_index = 0
        self.train_ratio = self.hparams.train_ratio
        self.batch_size = self.hparams.batch_size
        self.optimizer = self.hparams.optimizer
        self.intent_optimizer_lr = self.hparams.intent_optimizer_lr
        self.entity_optimizer_lr = self.hparams.entity_optimizer_lr
        self.intent_loss_fn = nn.CrossEntropyLoss()
        self.entity_loss_fn = nn.CrossEntropyLoss(ignore_index=self.ignore_index)

    def forward(self, input_ids, token_type_ids):
        return self.model(input_ids, token_type_ids)
    

    def prepare_data(self):
        if hasattr(self.hparams, 'tokenizer'):
            self.dataset = ElectraDataset(file_path=self.hparams.file_path, tokenizer=self.hparams.tokenizer)
        else:
            self.dataset = ElectraDataset(file_path=self.hparams.file_path, tokenizer=None)
        train_length = int(len(self.dataset) * self.train_ratio)
        
        # self.hparams.tokenize = self.get_tokenize()
        self.hparams.intent_label = self.get_intent_label()
        self.hparams.entity_label = self.get_entity_label()
        
        self.train_dataset, self.val_dataset = random_split(
            self.dataset, [train_length, len(self.dataset) - train_length],
        )
    
    def get_tokenize(self):
        return self.dataset.tokenize
    
    def get_intent_label(self):
        self.intent_dict = {}
        tmp_intent_dict = self.dataset.get_intent_idx()
        for k, v in tmp_intent_dict.items():
            self.intent_dict[str(v)] = k ##hparams key type should be string.
        return self.intent_dict 
    
    def get_entity_label(self):
        self.entity_dict = {}
        tmp_entity_dict = self.dataset.get_entity_idx()
        for k, v in tmp_entity_dict.items():
            self.entity_dict[str(v)] = k
        return self.entity_dict
            
    def train_dataloader(self):
        train_loader = DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            num_workers=multiprocessing.cpu_count(),
        )
        return train_loader

    def val_dataloader(self):
        val_loader = DataLoader(
            self.val_dataset,
            batch_size=self.batch_size,
            num_workers=multiprocessing.cpu_count(),
        )
        return val_loader

    def configure_optimizers(self):
        intent_optimizer = eval(
            f"{self.optimizer}(self.parameters(), lr={self.intent_optimizer_lr})"
        )
        entity_optimizer = eval(
            f"{self.optimizer}(self.parameters(), lr={self.entity_optimizer_lr})"
        )

        return (
            [intent_optimizer, entity_optimizer],
            # [StepLR(intent_optimizer, step_size=1),StepLR(entity_optimizer, step_size=1),],
            [
                ReduceLROnPlateau(intent_optimizer, patience=1),
                ReduceLROnPlateau(entity_optimizer, patience=1),
            ],
        )

    def training_step(self, batch, batch_idx, optimizer_idx):
        self.model.train()

        inputs, intent_idx, entity_idx = batch
        (input_ids, token_type_ids) = inputs
        
        intent_pred, entity_pred = self.forward(input_ids, token_type_ids)

        intent_acc = get_accuracy(intent_idx.cpu(), intent_pred.max(1)[1].cpu())[0]
        
        zero = torch.zeros_like(entity_idx).cpu()
        acc_entity_idx = torch.where(entity_idx.cpu()<0, zero, entity_idx.cpu())
        entity_acc = get_token_accuracy(
#             entity_idx.cpu(),
            acc_entity_idx,
            entity_pred.max(2)[1].cpu(),
            ignore_index=self.entity_o_index,
        )[0]

        tensorboard_logs = {
            "train/intent/acc": intent_acc,
            "train/entity/acc": entity_acc,
        }

        if optimizer_idx == 0:
            intent_loss = self.intent_loss_fn(intent_pred, intent_idx.squeeze(1))
            tensorboard_logs["train/intent/loss"] = intent_loss
            return {
                "loss": intent_loss,
                "log": tensorboard_logs,
            }
        if optimizer_idx == 1:
            entity_loss = self.entity_loss_fn(entity_pred.transpose(1, 2), entity_idx.long())
            tensorboard_logs["train/entity/loss"] = entity_loss
            return {
                "loss": entity_loss,
                "log": tensorboard_logs,
            }

    def validation_step(self, batch, batch_idx):
        self.model.eval()

        inputs, intent_idx, entity_idx = batch
        (input_ids, token_type_ids) = inputs
        intent_pred, entity_pred = self.forward(input_ids, token_type_ids)
    
        intent_acc = get_accuracy(intent_idx.cpu(), intent_pred.max(1)[1].cpu())[0]
        
        zero = torch.zeros_like(entity_idx).cpu()
        acc_entity_idx = torch.where(entity_idx.cpu()<0, zero, entity_idx.cpu())
        entity_acc = get_token_accuracy(
#             entity_idx.cpu(),
            acc_entity_idx,
            entity_pred.max(2)[1].cpu(),
            ignore_index=self.entity_o_index,
        )[0]

        intent_loss = self.intent_loss_fn(intent_pred, intent_idx.squeeze(1))
        entity_loss = self.entity_loss_fn(
            entity_pred.transpose(1, 2), entity_idx.long()
        )  # , ignore_index=0)

        return {
            "val_intent_acc": torch.Tensor([intent_acc]),
            "val_entity_acc": torch.Tensor([entity_acc]),
            "val_intent_loss": intent_loss,
            "val_entity_loss": entity_loss,
            "val_loss": intent_loss + entity_loss,
        }

    def validation_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        avg_intent_acc = torch.stack([x["val_intent_acc"] for x in outputs]).mean()
        avg_entity_acc = torch.stack([x["val_entity_acc"] for x in outputs]).mean()

        tensorboard_logs = {
            "val/loss": avg_loss,
            "val/intent_acc": avg_intent_acc,
            "val/entity_acc": avg_entity_acc,
        }

        return {
            "val_loss": avg_loss,
            "log": tensorboard_logs,
            "progress_bar": tensorboard_logs,
        }
    
     
