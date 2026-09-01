"""gpunode 主循环：register→claim→dispatch→heartbeat线程。
模型层(stages/*)真实加载放G0(GPU机器就位后)。此骨架完成协议闭环可自测。"""
import os, sys, threading, time, httpx, json

CONTROL = os.getenv("CONTROL_URL", "http://localhost:8500")
NODE_SECRET = os.getenv("NODE_SHARED_SECRET", "dev-node-secret")
POLL_IDLE = int(os.getenv("POLL_IDLE", "5"))

state = {"token": os.getenv("NODE_TOKEN", "dev-node-token")}

def register():
    r = httpx.post(f"{CONTROL}/api/nodes/register",
                   headers={"x-node-secret": NODE_SECRET},
                   json={"name": os.uname().nodename, "gpu_model": os.getenv("GPU_MODEL", "?"),
                         "vram_gb": int(os.getenv("GPU_VRAM", "0")), "capabilities": ["tts","asr","sep"]})
    r.raise_for_status()
    state["token"] = r.json()["node_token"]
    print("[node] registered")

def heartbeat_loop():
    while True:
        try:
            httpx.post(f"{CONTROL}/api/nodes/heartbeat",
                       headers={"Authorization": f"Bearer {state['token']}"}, timeout=10)
        except Exception as e:
            print("[hb] offline:", e)
        time.sleep(60)

def _task_heartbeat_loop(tid: str, stop: list):
    """D2修复：任务执行期间每60s续租（防长任务ProPainter被reaper误杀）"""
    import time as _t
    while not stop[0]:
        _t.sleep(60)
        if stop[0]:
            break
        try:
            httpx.post(f"{CONTROL}/api/nodes/tasks/{tid}/heartbeat",
                       headers={"Authorization": f"Bearer {state['token']}"},
                       timeout=10)
        except Exception as e:
            print("[task-hb] fail:", e)


def dispatch(task: dict):
    tid, ttype = task["id"], task["task_type"]
    # payload/pipeline_task payload 字段统一注入 task 顶层（stages 按 task["payload"] 取参）
    if "payload" not in task:
        task["payload"] = task.get("input_payload") or {}
    stop_flag = [False]
    import threading
    hb = threading.Thread(target=_task_heartbeat_loop, args=(tid, stop_flag), daemon=True)
    hb.start()
    try:
        from stages.router import run_task     # G0后实装：按type分发到stages/*
        outputs = run_task(task)
        httpx.post(f"{CONTROL}/api/nodes/tasks/{tid}/complete",
                   headers={"Authorization": f"Bearer {state['token']}"},
                   json={"outputs": outputs})
        print(f"[ok] {ttype} {tid[:8]}")
    except Exception as e:
        retryable = "OOM" not in str(e).upper()
        httpx.post(f"{CONTROL}/api/nodes/tasks/{tid}/fail",
                   headers={"Authorization": f"Bearer {state['token']}"},
                   json={"error": str(e)[:400], "retryable": retryable})
        print(f"[fail] {ttype} {tid[:8]}: {e}")
    finally:
        stop_flag[0] = True   # 停任务心跳线程

def main():
    register()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    while True:
        try:
            r = httpx.get(f"{CONTROL}/api/nodes/me/claim",
                          params={"model": os.getenv("PREFERRED_MODEL") or None},
                          headers={"Authorization": f"Bearer {state['token']}"}, timeout=30)
            task = (r.json() or {}).get("task")
            if task: dispatch(task)
            else: time.sleep(POLL_IDLE)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print("[claim err]", e); time.sleep(POLL_IDLE)

if __name__ == "__main__":
    main()
