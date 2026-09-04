"""gpunode 主循环：register→claim→dispatch→heartbeat线程。
模型层(stages/*)真实加载放G0(GPU机器就位后)。此骨架完成协议闭环可自测。"""
import os, sys, threading, time, httpx, json

CONTROL = os.getenv("CONTROL_URL", "http://localhost:8500")
# 持久连接：claim/complete/heartbeat/artifact 全部复用同一条 keep-alive 连接。
# 节点走代理+CF隧道，每请求新建连接的握手开销实测把吞吐拖到5句/分；
# 复用后请求开销<1s。读/写超时放大（artifact上传、complete）。
HTTP = httpx.Client(timeout=httpx.Timeout(connect=15, read=300, write=300, pool=300))
NODE_SECRET = os.getenv("NODE_SHARED_SECRET", "dev-node-secret")
POLL_IDLE = int(os.getenv("POLL_IDLE", "5"))

state = {"token": os.getenv("NODE_TOKEN", "dev-node-token")}
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "workdir", "node_token.txt")

def register():
    """token持久化：重启复用已存token（心跳验证），失效才重新register——
    原实现每次重启换新token，控制面节点行无限堆积。"""
    if os.path.exists(TOKEN_FILE):
        tok = open(TOKEN_FILE).read().strip()
        if tok:
            try:
                HTTP.post(f"{CONTROL}/api/nodes/heartbeat",
                           headers={"Authorization": f"Bearer {tok}"},
                           timeout=10).raise_for_status()
                state["token"] = tok
                print("[node] token reused")
                return
            except Exception:
                pass          # token失效/网络抖动→走重新register
    r = HTTP.post(f"{CONTROL}/api/nodes/register",
                   headers={"x-node-secret": NODE_SHARED_SECRET},
                   json={"name": os.uname().nodename, "gpu_model": os.getenv("GPU_MODEL", "?"),
                         "vram_gb": int(os.getenv("GPU_VRAM", "0")), "capabilities": ["tts","asr","sep"]})
    r.raise_for_status()
    state["token"] = r.json()["node_token"]
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(state["token"])
    print("[node] registered")

def heartbeat_loop():
    while True:
        try:
            HTTP.post(f"{CONTROL}/api/nodes/heartbeat",
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
            HTTP.post(f"{CONTROL}/api/nodes/tasks/{tid}/heartbeat",
                       headers={"Authorization": f"Bearer {state['token']}"},
                       timeout=10)
        except Exception as e:
            print("[task-hb] fail:", e)


ART_MAX_MB = int(os.getenv("NODE_ARTIFACT_MAX_MB", "80"))


def upload_artifacts(tid: str, outputs):
    """G6产物回传：outputs里节点本地文件POST回控制面（raw body，免multipart）。
    失败仅告警——complete已成功，控制面仍可按 output_paths 里节点侧path补拉。"""
    import os as _os
    for o in (outputs if isinstance(outputs, list) else []):
        p = (o or {}).get("path")
        if not p or not _os.path.isfile(p):
            continue
        try:
            sz = _os.path.getsize(p)
            if sz > ART_MAX_MB << 20:
                print(f"[art] skip >{ART_MAX_MB}MB: {p}")
                continue
            with open(p, "rb") as f:
                data = f.read()
            r = HTTP.post(
                f"{CONTROL}/api/nodes/tasks/{tid}/artifact",
                params={"filename": _os.path.basename(p), "key": o.get("key", "")},
                headers={"Authorization": f"Bearer {state['token']}",
                         "Content-Type": "application/octet-stream"},
                content=data, timeout=300)
            r.raise_for_status()
            print(f"[art] uploaded {_os.path.basename(p)} ({sz >> 10}KB)")
        except Exception as e:
            print("[art] upload fail:", e)


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
        HTTP.post(f"{CONTROL}/api/nodes/tasks/{tid}/complete",
                   headers={"Authorization": f"Bearer {state['token']}"},
                   json={"outputs": outputs})
        print(f"[ok] {ttype} {tid[:8]}")
        upload_artifacts(tid, outputs)         # G6：产物文件回传控制面
    except Exception as e:
        retryable = "OOM" not in str(e).upper()
        HTTP.post(f"{CONTROL}/api/nodes/tasks/{tid}/fail",
                   headers={"Authorization": f"Bearer {state['token']}"},
                   json={"error": str(e)[:400], "retryable": retryable})
        print(f"[fail] {ttype} {tid[:8]}: {e}")
    finally:
        stop_flag[0] = True   # 停任务心跳线程

def main():
    register()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    workers = max(1, int(os.getenv("NODE_WORKERS", "2")))
    sem = threading.BoundedSemaphore(workers)

    def _worker(t_):
        try:
            dispatch(t_)
        finally:
            sem.release()

    print(f"[node] worker pool = {workers}")
    while True:
        try:
            # 非阻塞拿令牌：池满则歇，避免任务堆 积+引擎过载
            if not sem.acquire(blocking=False):
                time.sleep(POLL_IDLE)
                continue
            r = HTTP.get(f"{CONTROL}/api/nodes/me/claim",
                          params={"model": os.getenv("PREFERRED_MODEL") or None},
                          headers={"Authorization": f"Bearer {state['token']}"}, timeout=30)
            if r.status_code == 401:
                print("[claim] token rejected → re-register")
                os.path.exists(TOKEN_FILE) and os.remove(TOKEN_FILE)
                register()
                sem.release()
                continue
            task = (r.json() or {}).get("task")
            if task:
                print(f"[dispatch] {task.get('task_key')} in pool")
                threading.Thread(target=_worker, args=(task,), daemon=True).start()
            else:
                sem.release()
                time.sleep(POLL_IDLE)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print("[claim err]", e)
            try:
                sem.release()
            except ValueError:
                pass
            time.sleep(POLL_IDLE)

if __name__ == "__main__":
    main()
