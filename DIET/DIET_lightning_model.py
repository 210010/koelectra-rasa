from argparse import Namespace

from torch.nn import functional as F
from torch.utils.data import DataLoader, random_split
from torch.optim import Adam
from torch.optim.lr_scheduler import StepLR

from pytorch_lightning import Trainer

from .dataset.intent_entity_dataset import RasaIntentEntityDataset
from .model.models import EmbeddingTransformer

import os, sys
import torch
import torch.nn as nn
import pytorch_lightning as pl


class DualIntentEntityTransformer(pl.LightningModule):
    def __init__(
        self, data_file_path, train_ratio=0.8, batch_size=32, optimizer="Adam", lr=1e-3
    ):
        super().__init__()

        self.dataset = RasaIntentEntityDataset(data_file_path)

        self.model = EmbeddingTransformer(
            vocab_size=self.dataset.get_vocab_size(),
            seq_len=self.dataset.get_seq_len(),
            intent_class_num=len(self.dataset.get_intent_idx()),
            entity_class_num=len(self.dataset.get_entity_idx()),
        )

        self.train_ratio = train_ratio
        self.batch_size = batch_size
        self.optimizer = optimizer
        self.lr = lr
        self.loss_fn = nn.CrossEntropyLoss()

    def forward(self, x):
        return self.model(x)

    def prepare_data(self):
        train_length = int(len(self.dataset) * self.train_ratio)

        self.train_dataset, self.val_dataset = random_split(
            self.dataset, [train_length, len(self.dataset) - train_length],
        )

    def train_dataloader(self):
        train_loader = DataLoader(self.train_dataset, batch_size=self.batch_size)
        return train_loader

    def val_dataloader(self):
        val_loader = DataLoader(self.val_dataset, batch_size=self.batch_size)
        return val_loader

    def configure_optimizers(self):
        intent_optimizer = eval(
            f"{self.optimizer}(self.model.parameters(), lr={self.lr})"
        )
        entity_optimizer = eval(
            f"{self.optimizer}(self.model.parameters(), lr={self.lr})"
        )
        return (
            [intent_optimizer, entity_optimizer],
            [
                StepLR(intent_optimizer, step_size=1),
                StepLR(entity_optimizer, step_size=1),
            ],
        )

    def training_step(self, batch, batch_idx, optimizer_idx):
        tokens, intent_idx, entity_idx = batch

        intent_pred, entity_pred = self.forward(tokens)

        if optimizer_idx == 0:
            intent_loss = self.loss_fn(intent_pred, intent_idx.squeeze(1))
            return {"loss": intent_loss, "intent_loss": intent_loss}
        if optimizer_idx == 1:
            entity_loss = self.loss_fn(entity_pred.transpose(1, 2), entity_idx.long())
            return {"loss": entity_loss, "entity_loss": entity_loss}

    def validation_step(self, batch, batch_idx):
        tokens, intent_idx, entity_idx = batch

        intent_pred, entity_pred = self.forward(tokens)

        intent_loss = self.loss_fn(intent_pred, intent_idx.squeeze(1))
        entity_loss = self.loss_fn(
            entity_pred.transpose(1, 2), entity_idx.long()
        )  # , ignore_index=0)

        return {
            "val_intent_loss": intent_loss,
            "val_entity_loss": entity_loss,
            "val_loss": intent_loss + entity_loss,
        }

    def validation_epoch_end(self, outputs):
        #OPTIONAL
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        tensorboard_logs = {'val_loss': avg_loss}
        return {'avg_val_loss': avg_loss, 'log': tensorboard_logs}

    def valdition_epoch_end(self, outputs):
        avg_intent_loss = torch.stack([x["val_intent_loss"] for x in outputs]).mean()
        avg_entity_loss = torch.stack([x["val_entity_loss"] for x in outputs]).mean()

        tensorboard_logs = {
            "val_intent_loss": avg_intent_loss,
            "val_entity_loss": avg_entity_loss,
        }

        return {
            "val_intent_loss": avg_intent_loss,
            "val_entity_loss": avg_entity_loss,
            "log": tensorboard_logs,
        }
