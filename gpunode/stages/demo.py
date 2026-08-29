"""demo stage：协议自测用（不依赖GPU）。真实现见 subtitle/separate/tts等，G0接。"""
import time
from .router import register

@register("noop")
def noop(task):
    time.sleep(0.2)
    return [{"key": f"out/{task['id']}.json", "path": ""}]
