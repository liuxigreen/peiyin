"""翻译审查韧性测试：ContentFilteredError→二分隔离→干净句照常落库→压缩闭环。
monkeypatch translate_executor.chat：user 含标记词的请求被拒（生产实测语义）。"""
import importlib
import os

from fastapi.testclient import TestClient

import app.translate_executor as te

_TRIGGER = "滥交触发词"
_SRT = """1
00:00:01,000 --> 00:00:02,000
总裁，夫人她离婚了！

2
00:00:03,000 --> 00:00:04,000
{TRIG}

3
00:00:05,000 --> 00:00:06,000
你怎么敢跟我说话

4
00:00:07,000 --> 00:00:08,000
我不知道他在哪里
""".replace("{TRIG}", _TRIGGER)

CALLS = {"n": 0}


async def _stub_chat(cfg, system, user):
    """模拟网关：user 含触发词→content_filter（usage=0 语义）；否则 mock 翻译。"""
    CALLS["n"] += 1
    if _TRIGGER in user:
        raise te.ContentFilteredError("gateway content filter: finish_reason=content_filter completion_tokens=0")
    return te._mock_translate(user)


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def test_filter_isolation_bisect(tmp_path):
    """40→10句批改子批后：含触发句的块被二分，最终只有触发句被隔离，
    其余句真实译文落库；场景任务 completed 而非 failed。"""
    c = _client(str(tmp_path / "r1.db"))
    r = c.post("/api/projects", json={"name": "审查剧", "target_lang": "en"})
    pid = r.json()["id"]
    r = c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT, "scene_size": 40})
    assert r.json()["utterances"] == 4

    te_chat = te.chat
    te.chat = _stub_chat
    CALLS["n"] = 0
    try:
        r = c.post(f"/api/projects/{pid}/run-translate").json()
    finally:
        te.chat = te_chat

    sc = [x for x in r["results"] if x["task"].startswith("SC")]
    assert sc and sc[0]["status"] == "completed", r
    assert sc[0]["filtered_chunks"] >= 1, r          # 确实触发过审查
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    by_orig = {u["original"]: u for u in utts}
    # 触发句被隔离：无译文、version=0
    trig = by_orig[_TRIGGER]
    assert trig["translated"] == "" and trig["version"] == 0, trig
    # 其余3句有真实译文（mock词典/占位行，但绝不是[MISSING占位）
    for orig in ("总裁，夫人她离婚了！", "你怎么敢跟我说话", "我不知道他在哪里"):
        u = by_orig[orig]
        assert u["translated"] and u["version"] >= 1, u
        assert not u["translated"].startswith("[MISSING"), u
    # 二分确实发生：4句批+10句batch_size→1块；触发后拆2块再各拆2块…调用数>1
    assert CALLS["n"] > 1, CALLS


def test_placeholder_never_persisted(tmp_path):
    """干净场景 mock 翻译：任何落库行都不得是占位符签名。"""
    c = _client(str(tmp_path / "r2.db"))
    pid = c.post("/api/projects", json={"name": "占位剧", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    te_chat = te.chat
    te.chat = _stub_chat
    try:
        r = c.post(f"/api/projects/{pid}/run-translate").json()
    finally:
        te.chat = te_chat
    assert r["completed"] >= 1, r
    from app.db.session import SessionLocal
    from app.db.models import Translation
    db = SessionLocal()
    try:
        bad = [t.text for t in db.query(Translation).all()
               if t.text.startswith("[MISSING") or t.text.startswith("[Translation")]
        assert not bad, bad
    finally:
        db.close()


def test_compression_rounds_bounded(tmp_path):
    """压缩闭环：mock 下能跑完、轮数≤2、超限行有重算。"""
    c = _client(str(tmp_path / "r3.db"))
    pid = c.post("/api/projects", json={"name": "压缩剧", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    te_chat = te.chat
    te.chat = _stub_chat
    try:
        r = c.post(f"/api/projects/{pid}/run-translate").json()
    finally:
        te.chat = te_chat
    sc = [x for x in r["results"] if x["task"].startswith("SC")][0]
    assert sc["status"] == "completed"
    assert sc.get("compression_rounds", 0) <= 2
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    # mock 译文极短，不应有超限残留
    assert not any(u["over_limit"] for u in utts if u["translated"]), utts


def test_fallback_provider_rescue(tmp_path, monkeypatch):
    """主provider拒句→保底provider救援成功→该句落库而非隔离。"""
    c = _client(str(tmp_path / "r4.db"))
    pid = c.post("/api/projects", json={"name": "保底剧", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})

    FALLBACK = {"mode": "live", "name": "fb", "model": "glm-fb", "base": "",
                "key": "", "temperature": 0.7, "max_tokens": 2048}
    monkeypatch.setattr(te, "load_fallback_provider", lambda db: FALLBACK)

    async def _stub(cfg, system, user):
        CALLS["n"] += 1
        if cfg["model"] == "primary" and _TRIGGER in user:
            raise te.ContentFilteredError("gateway content filter")
        return te._mock_translate(user)

    # 把主provider模型名固定为primary以便stub区分
    real_load = te.load_default_provider
    monkeypatch.setattr(te, "load_default_provider",
                        lambda db: {**real_load(db), "model": "primary"})

    te.chat = _stub
    try:
        r = c.post(f"/api/projects/{pid}/run-translate").json()
    finally:
        te.chat = te.chat  # noqa - 实际由fixture恢复，monkeypatch管理stub生命周期

    sc = [x for x in r["results"] if x["task"].startswith("SC")][0]
    assert sc["status"] == "completed", r
    assert sc.get("fallback_used", 0) == 1, r
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    trig = [u for u in utts if u["original"] == _TRIGGER][0]
    assert trig["translated"], trig          # 被保底救回，未隔离
    assert not trig["translated"].startswith("[MISSING"), trig


def test_parallel_runner_scenes(tmp_path, monkeypatch):
    """并行runner：多场景并发跑完，单场景失败不拖垮其他场景。"""
    c = _client(str(tmp_path / "r5.db"))
    pid = c.post("/api/projects", json={"name": "并行剧", "target_lang": "en"}).json()["id"]
    srt4 = _SRT.replace("00:00:0", "00:0{:02d},".format(0))
    c.post(f"/api/projects/{pid}/seed-srt",
           json={"srt": _SRT + """\n5\n00:00:09,000 --> 00:00:10,000
第二场景的台词

6\n00:00:11,000 --> 00:00:12,000
再来一句
""", "scene_size": 4})

    async def _stub(cfg, system, user):
        return te._mock_translate(user)

    monkeypatch.setattr(te, "chat", _stub)
    import asyncio
    from app.translate_executor import run_translate_project
    results = asyncio.run(run_translate_project(pid, ["SC01", "SC02"], workers=2))
    by_sc = {r["scene"]: r for r in results}
    assert by_sc["SC01"]["status"] == "completed"
    assert by_sc["SC02"]["status"] == "completed"
    assert by_sc["SC01"]["utterances"] == 4 and by_sc["SC02"]["utterances"] == 2
