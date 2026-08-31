import os, importlib, sys
os.environ["DATABASE_URL"] = "sqlite:////tmp/dbg1.db"
import app.db.session as s, app.main as m
importlib.reload(s); importlib.reload(m)
s.init_db()
from fastapi.testclient import TestClient
c = TestClient(m.app)
pid = c.post("/api/projects", json={"name":"dbg","target_lang":"en"}).json()["id"]
srt = """1
00:00:01,000 --> 00:00:02,000
你好

2
00:00:03,000 --> 00:00:04,000
再见
"""
r = c.post(f"/api/projects/{pid}/upload-complete", json={"srt": srt, "scene_size": 40}).json()
print(r.get("translate", {}).get("results", [{}])[0])
