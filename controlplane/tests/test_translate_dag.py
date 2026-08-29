"""DAG路径翻译测试：instantiate-dag + simulate_upstream + 场景批6任务一次成链"""
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


def test_dag_path_translate_chain(tmp_path):
    """instantiate-dag后run-translate(simulate_upstream)：
    SC01场景批的6个translate任务走一次链全部完成。"""
    c = _client(str(tmp_path / "dag.db"))
    r = c.post("/api/projects", json={"name": "DAG剧", "target_lang": "en"})
    pid = r.json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT, "scene_size": 40})
    r = c.post(f"/api/projects/{pid}/instantiate-dag",
               json={"n_segments": 1, "scenes": ["SC01"]}).json()
    assert r["ok"], r
    r = c.post(f"/api/projects/{pid}/run-translate?simulate_upstream=true").json()
    assert r["ok"], r
    sc_tasks = [x for x in r["results"] if x.get("scene") == "SC01"]
    assert len(sc_tasks) == 1 and sc_tasks[0]["status"] == "completed", r
    # 全部6个family任务最终completed
    prog = c.get(f"/api/projects/{pid}/progress").json()
    assert prog["phases"]["translate"]["total"] == 6
    assert prog["phases"]["translate"]["done"] == 6
    assert prog["phases"]["translate"]["failed"] == 0
    # 译文真实落库
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    assert all(u["translated"] for u in utts)


def test_live_provider_config_used(tmp_path):
    """配置了provider后plan显示live模式（不实际调用外网）。"""
    c = _client(str(tmp_path / "prov.db"))
    pid = c.post("/api/projects", json={"name": "P", "target_lang": "en"}).json()["id"]
    r = c.post("/api/providers", json={
        "name": "deepseek-main", "provider_type": "openai",
        "api_base_url": "https://api.example.com/v1", "api_key": "sk-test-123456",
        "model_name": "deepseek-chat", "is_default": True}).json()
    assert r.get("ok", True), r
    plan = c.get(f"/api/projects/{pid}/translate-plan").json()
    assert plan["provider"]["mode"] == "live"
    assert plan["provider"]["name"] == "deepseek-main"
    assert plan["provider"]["model"] == "deepseek-chat"
