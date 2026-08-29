"""N1引导链测试：upload-complete一步到位（种子+DAG+自动开翻）"""
import importlib
import os

from fastapi.testclient import TestClient

_SRT = """1
00:00:01,000 --> 00:00:03,000
总裁，夫人她离婚了！

2
00:00:04,000 --> 00:00:06,000
你怎么敢跟我说话
"""


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def test_upload_complete_full_chain(tmp_path):
    """上传SRT完成→自动出译文，一调用全链通。"""
    c = _client(str(tmp_path / "n1.db"))
    pid = c.post("/api/projects", json={"name": "自动剧", "target_lang": "en"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/upload-complete", json={
        "srt": _SRT, "r2_key": "uploads/x/source.mp4", "scene_size": 40}).json()
    assert r["ok"], r
    assert r["mode"] == "full"
    assert r["utterances"] == 2
    assert r["dag_created"] > 0
    assert r["translate"]["completed"] >= 1
    # 对照表有译文
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    assert all(u["translated"] for u in utts)
    # 项目状态processing
    detail = c.get(f"/api/projects/{pid}").json()
    assert detail["status"] == "processing"


def test_upload_complete_video_only(tmp_path):
    """只传母片（无SRT）：登记key返回probe-seeded，不开翻。"""
    c = _client(str(tmp_path / "n2.db"))
    pid = c.post("/api/projects", json={"name": "母片剧", "target_lang": "en"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/upload-complete",
               json={"r2_key": "uploads/x/source.mp4"}).json()
    assert r["ok"] and r["mode"] == "probe-seeded"
    detail = c.get(f"/api/projects/{pid}").json()
    assert detail["status"] != "processing"
