from abc import abstractmethod
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np
import pandas as pd
import torch

from ignite.contrib.handlers import LRScheduler
from ignite.engine import Engine, Events
from ignite.handlers import (
    EarlyStopping,
    ModelCheckpoint,
    global_step_from_engine,
)
from ignite.metrics import Loss, RunningAverage
from pyspark.sql import DataFrame
from pyspark.sql import functions as sf
from torch import nn
from torch.optim.optimizer import Optimizer  # pylint: disable=E0611
from torch.optim.lr_scheduler import ReduceLROnPlateau, _LRScheduler
from torch.utils.data import DataLoader

from replay.models.base_rec import Recommender
from replay.session_handler import State
from replay.constants import IDX_REC_SCHEMA


class TorchRecommender(Recommender):
    """ Базовый класс-рекомендатель для нейросетевой модели. """

    model: Any
    device: torch.device

    # pylint: disable=too-many-arguments
    def _predict(
        self,
        log: DataFrame,
        k: int,
        users: DataFrame,
        items: DataFrame,
        user_features: Optional[DataFrame] = None,
        item_features: Optional[DataFrame] = None,
        filter_seen_items: bool = True,
    ) -> DataFrame:
        items_pd = items.toPandas()["item_idx"].values
        items_count = self.items_count
        model = self.model.cpu()
        agg_fn = self._predict_by_user

        def grouped_map(pandas_df: pd.DataFrame) -> pd.DataFrame:
            return agg_fn(pandas_df, model, items_pd, k, items_count)[
                ["user_idx", "item_idx", "relevance"]
            ]

        self.logger.debug("Предсказание модели")
        recs = (
            users.join(log, how="left", on="user_idx")
            .selectExpr("user_idx AS user_idx", "item_idx AS item_idx",)
            .groupby("user_idx")
            .applyInPandas(grouped_map, IDX_REC_SCHEMA)
        )
        return recs

    def _predict_pairs(
        self,
        pairs: DataFrame,
        log: Optional[DataFrame] = None,
        user_features: Optional[DataFrame] = None,
        item_features: Optional[DataFrame] = None,
    ) -> DataFrame:
        items_count = self.items_count
        model = self.model.cpu()
        agg_fn = self._predict_by_user_pairs
        users = pairs.select("user_idx").distinct()

        def grouped_map(pandas_df: pd.DataFrame) -> pd.DataFrame:
            return agg_fn(pandas_df, model, items_count)[
                ["user_idx", "item_idx", "relevance"]
            ]

        self.logger.debug("Оценка релевантности для пар")
        user_history = (
            users.join(log, how="inner", on="user_idx")
            .groupBy("user_idx")
            .agg(sf.collect_list("item_idx").alias("item_idx_history"))
        )
        user_pairs = pairs.groupBy("user_idx").agg(
            sf.collect_list("item_idx").alias("item_idx_to_pred")
        )
        full_df = user_pairs.join(user_history, on="user_idx", how="left")

        recs = full_df.groupby("user_idx").applyInPandas(
            grouped_map, IDX_REC_SCHEMA
        )

        return recs

    @staticmethod
    @abstractmethod
    def _predict_by_user(
        pandas_df: pd.DataFrame,
        model: nn.Module,
        items_np: np.ndarray,
        k: int,
        item_count: int,
    ) -> pd.DataFrame:
        """
        Получение рекомендаций для каждого пользователя

        :param pandas_df: DataFrame, содержащий индексы просмотренных объектов
            по каждому пользователю -- pandas-датафрейм вида
            ``[user_idx, item_idx]``
        :param model: обученная модель
        :param items_np: список допустимых для рекомендаций объектов
        :param k: количество рекомендаций
        :param item_count: общее количество объектов в рекомендателе
        :return: DataFrame c рассчитанными релевантностями --
            pandas-датафрейм вида ``[user_idx , item_idx , relevance]``
        """

    @staticmethod
    @abstractmethod
    def _predict_by_user_pairs(
        pandas_df: pd.DataFrame, model: nn.Module, item_count: int,
    ) -> pd.DataFrame:
        """
        Получение релевантности для выбранных объектов для каждого пользователя

        :param pandas_df: pandas-датафрейм, содержащий индексы просмотренных объектов
            по каждому пользователю и индексы объектов, для которых нужно получить предсказание
            ``[user_idx, item_idx_history, item_idx_to_pred]``
        :param model: обученная модель
        :param item_count: общее количество объектов в рекомендателе
        :return: DataFrame c рассчитанными релевантностями --
            pandas-датафрейм вида ``[user_idx , item_idx , relevance]``
        """

    def load_model(self, path: str) -> None:
        """
        Загрузка весов модели из файла

        :param path: путь к файлу, откуда загружать
        :return:
        """
        self.logger.debug("-- Загрузка модели из файла")
        self.model.load_state_dict(torch.load(path))

    # pylint: disable=too-many-arguments
    def _create_trainer_evaluator(
        self,
        opt: Optimizer,
        valid_data_loader: DataLoader,
        scheduler: Optional[Union[_LRScheduler, ReduceLROnPlateau]] = None,
        early_stopping_patience: Optional[int] = None,
        checkpoint_number: Optional[int] = None,
    ) -> Tuple[Engine, Engine]:
        """
        Метод, возвращающий trainer, evaluator для обучения нейронной сети.

        :param opt: Оптимайзер
        :param valid_data_loader: Загрузчик данных для валидации
        :param scheduler: Расписания для уменьшения шага обучения
        :param early_stopping_patience: количество эпох для ранней остановки
        :param early_stopping_patience: количество лучших чекпойнтов
        :return: trainer, evaluator
        """
        self.model.to(self.device)  # pylint: disable=E1101

        # pylint: disable=unused-argument
        def _run_train_step(engine, batch):
            self.model.train()
            opt.zero_grad()
            model_result = self._batch_pass(batch, self.model)
            y_pred, y_true = model_result[:2]
            if len(model_result) == 2:
                loss = self._loss(y_pred, y_true)
            else:
                loss = self._loss(y_pred, y_true, **model_result[2])
            loss.backward()
            opt.step()
            return loss.item()

        # pylint: disable=unused-argument
        def _run_val_step(engine, batch):
            self.model.eval()
            with torch.no_grad():
                return self._batch_pass(batch, self.model)

        torch_trainer = Engine(_run_train_step)
        torch_evaluator = Engine(_run_val_step)

        avg_output = RunningAverage(output_transform=lambda x: x)
        avg_output.attach(torch_trainer, "loss")
        Loss(self._loss).attach(torch_evaluator, "loss")

        # pylint: disable=unused-variable
        @torch_trainer.on(Events.EPOCH_COMPLETED)
        def log_training_loss(trainer):
            self.logger.debug(
                "Epoch[{}] current loss: {:.5f}".format(
                    trainer.state.epoch, trainer.state.metrics["loss"]
                )
            )

        # pylint: disable=unused-variable
        @torch_trainer.on(Events.EPOCH_COMPLETED)
        def log_validation_results(trainer):
            torch_evaluator.run(valid_data_loader)
            metrics = torch_evaluator.state.metrics
            self.logger.debug(
                "Epoch[{}] validation average loss: {:.5f}".format(
                    trainer.state.epoch, metrics["loss"]
                )
            )

        def score_function(engine):
            return -engine.state.metrics["loss"]

        if early_stopping_patience:
            self._add_early_stopping(
                early_stopping_patience,
                score_function,
                torch_trainer,
                torch_evaluator,
            )
        if checkpoint_number:
            self._add_checkpoint(
                checkpoint_number,
                score_function,
                torch_trainer,
                torch_evaluator,
            )
        if scheduler:
            self._add_scheduler(scheduler, torch_trainer, torch_evaluator)

        return torch_trainer, torch_evaluator

    @staticmethod
    def _add_early_stopping(
        early_stopping_patience, score_function, torch_trainer, torch_evaluator
    ):
        early_stopping = EarlyStopping(
            patience=early_stopping_patience,
            score_function=score_function,
            trainer=torch_trainer,
        )
        torch_evaluator.add_event_handler(Events.COMPLETED, early_stopping)

    def _add_checkpoint(
        self, checkpoint_number, score_function, torch_trainer, torch_evaluator
    ):
        checkpoint = ModelCheckpoint(
            State().session.conf.get("spark.local.dir"),
            create_dir=True,
            require_empty=False,
            n_saved=checkpoint_number,
            score_function=score_function,
            score_name="loss",
            filename_prefix="best",
            global_step_transform=global_step_from_engine(torch_trainer),
        )

        torch_evaluator.add_event_handler(
            Events.EPOCH_COMPLETED,
            checkpoint,
            {type(self).__name__.lower(): self.model},
        )

        # pylint: disable=unused-argument,unused-variable
        @torch_trainer.on(Events.COMPLETED)
        def load_best_model(engine):
            self.load_model(checkpoint.last_checkpoint)

    @staticmethod
    def _add_scheduler(scheduler, torch_trainer, torch_evaluator):
        if isinstance(scheduler, _LRScheduler):
            torch_trainer.add_event_handler(
                Events.EPOCH_COMPLETED, LRScheduler(scheduler)
            )
        else:

            @torch_evaluator.on(Events.EPOCH_COMPLETED)
            # pylint: disable=unused-variable
            def reduct_step(engine):
                scheduler.step(engine.state.metrics["loss"])

    @abstractmethod
    def _batch_pass(
        self, batch, model
    ) -> Tuple[torch.Tensor, torch.Tensor, Union[None, Dict[str, Any]]]:
        """
        Метод, возвращающий результат применения модели к батчу.
        Должен быть имплементирован наследниками.

        :param batch: батч с данными
        :param model: нейросетевая модель
        :return: y_pred, y_true, а также словарь дополнительных параметров,
        необходимых для расчета функции потерь
        """

    @abstractmethod
    def _loss(
        self, y_pred: torch.Tensor, y_true: torch.Tensor, *args, **kwargs
    ) -> torch.Tensor:
        """
        Метод, возвращающий значение функции потерь.
        Должен быть имплементирован наследниками.

        :param y_pred: Результат, который вернула нейросеть
        :param y_true: Ожидаемый результат
        :param *args: Прочие аргументы необходимые для расчета loss
        :return: Тензор размера 1 на 1
        """
