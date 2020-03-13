"""
Библиотека рекомендательных систем Лаборатории по искусственному интеллекту.
"""
from typing import Union, Iterable

from pyspark.sql.types import (DoubleType, FloatType, StringType, StructField,
                               StructType, TimestampType)

LOG_SCHEMA = StructType([
    StructField("user_id", StringType()),
    StructField("item_id", StringType()),
    StructField("timestamp", TimestampType()),
    StructField("relevance", FloatType())
])

DEFAULT_CONTEXT = "no_context"

REC_SCHEMA = StructType([
    StructField("user_id", StringType()),
    StructField("item_id", StringType()),
    StructField("relevance", DoubleType())
])

IterOrList = Union[Iterable[int], int]
