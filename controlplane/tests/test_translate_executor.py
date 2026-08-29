"""翻译executor测试：mock provider全链（seed-srt → run-translate → translations落库）"""
import importlib
import os

from fastapi.testclient import TestClient

_SRT = """1
00:00:01,000 --> 00:00:03,000
总裁，夫人她离婚了！

2
00:00:04,000 --> 00:00:06,000
你怎么敢跟我说话

3
00:00:07,000 --> 00:00:09,000
我不知道他在哪里
"""


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def _mkproj(c, name):
    r = c.post("/api/projects", json={"name": name, "target_lang": "en"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_seed_and_plan(tmp_path):
    c = _client(str(tmp_path / "t0.db"))
    pid = _mkproj(c, "种子测试")
    r = c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT, "scene_size": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["utterances"] == 3
    assert body["scenes"] == ["SC01", "SC02"]
    plan = c.get(f"/api/projects/{pid}/translate-plan").json()
    assert plan["provider"]["mode"] == "mock"
    assert plan["total_utterances"] == 3
    assert plan["scenes"] == {"SC01": 2, "SC02": 1}


def test_light_path_e2e(tmp_path):
    """轻量路径：无DAG直接跑，译文落库+音节比+对照表可读。"""
    c = _client(str(tmp_path / "t1.db"))
    pid = _mkproj(c, "测试剧")
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    r = c.post(f"/api/projects/{pid}/run-translate").json()
    assert r["completed"] == r["ran"] == 1, r
    r = c.get(f"/api/projects/{pid}/progress").json()
    # 轻量路径仅1条QC影子行（无DAG任务）
    assert r["counts"]["completed"] == 1, r["counts"]
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    assert len(utts) == 3
    t = utts[0]
    assert t["translated"], t
    assert t["version"] == 1
    assert t["ratio"] > 0
    assert "总裁" in t["original"]


def test_version_increment_on_rerun(tmp_path):
    """重复run-translate：同句译文version+1，对照表显示最新版。"""
    c = _client(str(tmp_path / "t3.db"))
    pid = _mkproj(c, "重跑剧")
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    assert c.post(f"/api/projects/{pid}/run-translate").json()["completed"] == 1
    assert c.post(f"/api/projects/{pid}/run-translate").json()["completed"] == 1
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    versions = sorted(u["version"] for u in utts)
    assert versions == [2, 2, 2]
