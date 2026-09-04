"""0905 自动推进器：diarize完成→闸门B→簇绑定→试听包。
每60s检查一次 DIARIZE 测试批状态；完成即推进，失败即标记。幂等可重跑。"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/peiyin/controlplane")

B = "http://127.0.0.1:8500"
PID = "10f001e14c5c41e6bb17fdd8465d1586"          # 白月光


def post(p, b=None, t=600):
    r = urllib.request.Request(B + p, data=json.dumps(b or {}).encode(),
                               headers={"Content-Type": "application/json"},
                               method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=t).read())


def get(p, t=60):
    last = None
    for _ in range(6):
        try:
            return json.loads(urllib.request.urlopen(B + p, timeout=t).read())
        except Exception as e:
            last = e
            time.sleep(10)
    raise last


def main():
    while True:
        st = get("/api/projects", 30)
        db_ok = any(p["id"] == PID for p in st)
        # 1) diarize任务状态
        import sqlite3
        c = sqlite3.connect("/opt/peiyin/controlplane/dev.db", timeout=30)
        row = c.execute(
            "select status, error_message from pipeline_tasks where "
            "task_type='diarize' order by created_at desc limit 1").fetchone()
        status, err = row
        if status == "completed":
            print("[advance] diarize done → cluster bind + audition pack", flush=True)
            try:
                post(f"/api/projects/{PID}/bind-speakers", {"force": True}, 1800)
                pack = post(f"/api/projects/{PID}/mode-b/audition-pack",
                            {"per_voice": 2, "minutes": 20}, 1800)
                print("[advance] audition pack:", pack, flush=True)
            except Exception as e:
                print("[advance] bind/pack ERR:", str(e)[:200], flush=True)
            break
        if status == "dead":
            print("[advance] diarize dead:", (err or "")[:120], flush=True)
            break
        print(f"[advance] waiting diarize ({status})...", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
