"""GPU引擎管家（节点侧）：按需启停 CosyVoice 引擎。
规则：
- 云端有 pending/running 的 tts-generate 任务 → 引擎没起就拉起（冷启动2-5分钟）
- 无TTS任务连续 15 分钟 → 杀引擎释放显存（用户日常使用不受影响）
- 幂等：引擎在跑就不管
部署为 Windows 计划任务每 60 秒运行一次（复用 keepalive 的节奏）。
"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime

BASE = os.environ.get("CONTROL_URL", "http://100.77.187.54:8500")
TOKEN_FILE = r"E:\peiyin-node\peiyin-current\gpunode\workdir\node_token.txt"
LOG = r"E:\peiyin-node\engine_manager.log"
STATE = r"E:\peiyin-node\engine_idle_since.json"
ENGINE_PORT = 50000
IDLE_MIN = 15
ENGINE_MARK = "cosyvoice_server.py"

def log(m):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%m-%d %H:%M:%S')} {m}\n")

def token():
    return open(TOKEN_FILE).read().strip()

def api(path, method="GET", body=None, timeout=30):
    req = urllib.request.Request(BASE + path, method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def engine_alive() -> bool:
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
        f"(Get-NetTCPConnection -LocalPort {ENGINE_PORT} -State Listen -EA SilentlyContinue) -ne $null"],
        capture_output=True, text=True, timeout=30)
    return "True" in (r.stdout or "")

def engine_procs():
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'cosyvoice_server.py' } | Measure-Object).Count"],
        capture_output=True, text=True, timeout=30)
    try: return int((r.stdout or "0").strip())
    except Exception: return 0

def start_engine():
    subprocess.run(["schtasks", "/run", "/tn", "peiyin-engine-start"], capture_output=True, timeout=30)

def kill_engine():
    subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'cosyvoice_server.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, timeout=60)

def main():
    try:
        n = api("/api/nodes/engine-should-run")
        should = bool(n.get("should_run"))
    except Exception as e:
        log(f"control-plane unreachable: {e}"); return
    alive = engine_alive() or engine_procs() > 0
    if should and not alive:
        log("TTS任务排队中 → 拉起引擎")
        start_engine()
    elif not should and alive:
        st = json.load(open(STATE)) if os.path.exists(STATE) else {"idle_since": None}
        if not st.get("idle_since"):
            st["idle_since"] = time.time()
            json.dump(st, open(STATE, "w"))
            log(f"队列空 → 引擎空闲计时开始({IDLE_MIN}分钟后关闭)")
        elif time.time() - st["idle_since"] > IDLE_MIN * 60:
            log(f"空闲超{IDLE_MIN}分钟 → 关闭引擎释放显存")
            kill_engine()
            st["idle_since"] = None
            json.dump(st, open(STATE, "w"))
    elif should and alive:
        if os.path.exists(STATE):   # 空闲计时中途来活 → 清计时
            os.remove(STATE)
    elif not should and not alive:
        if os.path.exists(STATE):
            os.remove(STATE)

if __name__ == "__main__":
    main()
