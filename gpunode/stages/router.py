"""任务类型→stage分发。每个stage一个模块，接口统一：
run(task: dict) -> list[dict]  # [{key, path}] 上传产物清单"""
import os

_stages = {}

def register(task_type):
    def deco(fn):
        _stages[task_type] = fn
        return fn
    return deco

def run_task(task: dict) -> list[dict]:
    fn = _stages.get(task["task_type"])
    if not fn:
        raise KeyError(f"no stage for {task['task_type']}")
    return fn(task)

# 显式注册（各stage import时生效）
# NODE_MODE=offline(默认)→离线联调stage包；=real(G0后)→真模型stage包
_MODE = os.getenv("NODE_MODE", "offline")
if _MODE == "offline":
    from . import offline as _impl  # noqa: F401,E402
else:
    from . import demo as _impl     # noqa: F401,E402
    from . import tts_node as _tts  # noqa: F401,E402  # TTS节点：3060等GPU机器
    # G0: from . import real stages here
