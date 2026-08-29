"""O1/O2测试：QC Agent质检钩子 + 算力Agent开关机决策（干跑）"""
import importlib
import os

import numpy as np
import soundfile as sf

from fastapi.testclient import TestClient


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


_SRT = """1
00:00:01,000 --> 00:00:03,000
总裁，夫人她离婚了！

2
00:00:04,000 --> 00:00:06,000
你怎么敢跟我说话
"""


def _run_translate(c, pid):
    return c.post(f"/api/projects/{pid}/run-translate").json()


def test_qc_pass_written(tmp_path):
    """翻译质量过关→QC结果写进任务行，质检Tab可查。"""
    c = _client(str(tmp_path / "qc1.db"))
    pid = c.post("/api/projects", json={"name": "QC好剧", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    r = _run_translate(c, pid)
    assert r["completed"] == 1 and r["results"][0]["qc"] is True, r
    qc = c.get(f"/api/projects/{pid}/qc").json()
    assert qc["total"] >= 1 and qc["passed"] == qc["total"], qc
    item = qc["items"][0]
    names = {x["name"] for x in item["checks"]}
    assert "音节比≤1.15" in names and "空译=0" in names


def test_qc_catch_failure(tmp_path):
    """空译文场景→QC钩子捕获（音节/空译检查失败→review）。"""
    c = _client(str(tmp_path / "qc2.db"))
    pid = c.post("/api/projects", json={"name": "QC坏剧", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    # 直接污染DB：把译文清空模拟坏翻译
    from app.db.session import SessionLocal
    from app.db.models import Translation
    db = SessionLocal()
    c.post(f"/api/projects/{pid}/run-translate")
    for t in db.query(Translation).all():
        t.text = ""
    db.commit()
    db.close()
    r = _run_translate(c, pid)   # version+1后再跑，新译文正常→pass
    assert r["completed"] == 1
    # 直接单测钩子对空译的判定
    from app.db.session import SessionLocal as S2
    from app.db.models import PipelineTask
    from app.qc_agent import qc_translate
    db2 = S2()
    task = db2.query(PipelineTask).filter_by(project_id=pid).first()
    # 人为清空所有译文再调用钩子
    for t in db2.query(Translation).all():
        t.text = ""
    db2.commit()
    result = qc_translate(task, db2)
    assert result["pass"] is False and result["action"] == "review"
    assert any(not x["ok"] for x in result["checks"])
    db2.close()


def test_power_agent_decisions():
    """算力Agent：积压连续→开机；清空连续→关机；费用记账。"""
    from app.power_agent import PowerAgent
    a = PowerAgent(queue_threshold=2, on_streak_need=3, idle_cycles_need=2,
                   dry_run=True)
    # 积压2连续3拍 → 开机
    assert a.feed(2, 0) is None
    assert a.feed(2, 0) is None
    ev = a.feed(2, 0)
    assert ev and ev["action"] == "power_on" and ev["dry_run"]
    # 开机后积压清零 → 空闲2拍 → 关机+记账
    assert a.feed(0, 0) is None
    ev2 = a.feed(0, 0)
    assert ev2 and ev2["action"] == "power_off" and "cost" in ev2
    # 忙碌中间态不打断计数逻辑
    a2 = PowerAgent(queue_threshold=1, on_streak_need=1, dry_run=True)
    ev3 = a2.feed(3, 1)
    assert ev3 and ev3["action"] == "power_on"


def test_power_status_api(tmp_path):
    c = _client(str(tmp_path / "p1.db"))
    st = c.get("/api/power/status").json()
    assert st["dry_run"] is True and st["instance_on"] is False
    tick = c.post("/api/power/tick").json()
    assert "gpu_backlog" in tick
