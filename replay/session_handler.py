"""
Этот модуль позволяет безболезненно создавать и получать спарк сессии.
"""

import logging
import os
from math import floor
from typing import Any, Dict, Optional

import psutil
import torch
from pyspark.sql import SparkSession


def get_spark_session(
    spark_memory: Optional[int] = None,
    shuffle_partitions: Optional[int] = None,
) -> SparkSession:
    """
    инициализирует и возращает SparkSession с "годными" параметрами по
    умолчанию (для пользователей, которые не хотят сами настраивать Spark)

    :param spark_memory: количество гигабайт оперативной памяти, которую нужно выделить под Spark;
        если не задано, выделяется половина всей доступной памяти
    :param shuffle_partitions: количество партиций для Spark; если не задано, равно числу доступных цпу
    """
    if spark_memory is None:
        spark_memory = floor(psutil.virtual_memory().total / 1024 ** 3 / 2)
    if shuffle_partitions is None:
        shuffle_partitions = os.cpu_count()
    driver_memory = f"{spark_memory}g"
    user_home = os.environ["HOME"]
    spark = (
        SparkSession.builder.config("spark.driver.memory", driver_memory)
        .config("spark.sql.shuffle.partitions", str(shuffle_partitions))
        .config("spark.local.dir", os.path.join(user_home, "tmp"))
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.host", "localhost")
        .config("spark.sql.execution.arrow.pyspark.enabled", "true")
        .master("local[*]")
        .enableHiveSupport()
        .getOrCreate()
    )
    return spark


def logger_with_settings() -> logging.Logger:
    """ Настройка логгеров и изменение их уровня """
    spark_logger = logging.getLogger("py4j")
    spark_logger.setLevel(logging.WARN)
    ignite_engine_logger = logging.getLogger("ignite.engine.engine.Engine")
    ignite_engine_logger.setLevel(logging.WARN)
    logger = logging.getLogger("replay")
    formatter = logging.Formatter(
        "%(asctime)s, %(name)s, %(levelname)s: %(message)s",
        datefmt="%d-%b-%y %H:%M:%S",
    )
    hdlr = logging.StreamHandler()
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)
    logger.setLevel(logging.DEBUG)
    return logger


# pylint: disable=too-few-public-methods
class Borg:
    """
    Обеспечивает доступ к расшаренному состоянию
    """

    _shared_state: Dict[str, Any] = {}

    def __init__(self):
        self.__dict__ = self._shared_state


# pylint: disable=too-few-public-methods
class State(Borg):
    """
    В этот класс можно положить свою спарк сессию, чтобы она была доступна модулям библиотеки.
    Каждый модуль, которому нужна спарк сессия, будет искать её здесь и создаст дефолтную сессию,
    если ни одной не было создано до сих пор.

    Здесь же хранится ``default device`` для ``pytorch`` (CPU или CUDA, если доступна).
    """

    def __init__(
        self,
        session: Optional[SparkSession] = None,
        device: Optional[torch.device] = None,
    ):
        Borg.__init__(self)
        if not hasattr(self, "logger_set"):
            self.logger = logger_with_settings()
            self.logger_set = True

        if session is None:
            if not hasattr(self, "session"):
                self.session = get_spark_session()
        else:
            self.session = session

        if device is None:
            if not hasattr(self, "device"):
                if torch.cuda.is_available():
                    self.device = torch.device(
                        f"cuda:{torch.cuda.current_device()}"
                    )
                else:
                    self.device = torch.device("cpu")
        else:
            self.device = device
