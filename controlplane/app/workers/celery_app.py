"""Celery三队列：gpu队列在gpunode容器另有实例，此处定义cpu/io与公共配置"""
from celery import Celery
import os

broker = os.getenv("REDIS_URL", "redis://localhost:6379/0")
backend = os.getenv("REDIS_URL", "redis://localhost:6379/1")

celery_app = Celery("dubbing", broker=broker, backend=backend)
celery_app.conf.update(
    task_acks_late=True,               # 允许worker崩溃后任务重回队列
    worker_prefetch_multiplier=1,      # GPU类任务禁止预取堆积
    task_routes={
        "tasks.cpu.*": {"queue": "cpu"},
        "tasks.io.*": {"queue": "io"},
        "tasks.gpu.*": {"queue": "gpu"},
    },
)
