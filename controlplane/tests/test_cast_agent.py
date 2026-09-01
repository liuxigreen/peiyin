"""角色提取Agent测试：mock LLM返回JSON角色→speakers/glossary落库+幂等重跑。"""
import importlib
import json
import os

from fastapi.testclient import TestClient

import app.cast_agent as ca
import app.translate_executor as te

_CAST_JSON = json.dumps({"characters": [
    {"label": "阿弋", "role_name": "A-Yi", "is_primary": True,
     "gender": "female", "age_band": "young", "timbre": "清亮倔强",
     "en_variants": ["Ayi", "Ah Yi"]},
    {"label": "盛君廷", "role_name": "Sheng Junting", "is_primary": True,
     "gender": "male", "age_band": "young", "timbre": "低沉贵气",
     "en_variants": []},
    {"label": "路人甲", "role_name": "Passerby", "is_primary": False,
     "gender": "unknown", "age_band": "unknown", "timbre": "",
     "en_variants": []},
]}, ensure_ascii=False)


async def _stub_chat(cfg, system, user):
    return _CAST_JSON


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


_SRT = """1
00:00:01,000 --> 00:00:02,000
阿弋你不许走

2
00:00:03,000 --> 00:00:04,000
盛君廷你放开她

3
00:00:05,000 --> 00:00:06,000
让开
"""


def test_extract_cast_end_to_end(tmp_path):
    c = _client(str(tmp_path / "cast.db"))
    pid = c.post("/api/projects", json={"name": "角色剧", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})

    chat_bak = te.chat
    te.chat = _stub_chat
    try:
        r = c.post(f"/api/projects/{pid}/extract-cast").json()
    finally:
        te.chat = chat_bak
    assert r["ok"], r
    assert r["characters"] == 3, r

    # speakers 落库 + 角色卡元数据
    from app.db.session import SessionLocal
    from app.db.models import Speaker, GlossaryTerm
    db = SessionLocal()
    try:
        spks = {s.label: s for s in db.query(Speaker).all()}
        assert set(spks) == {"阿弋", "盛君廷", "路人甲"}
        ayi = spks["阿弋"]
        assert ayi.is_primary and ayi.role_name == "A-Yi"
        meta = ayi.ref_audio_pool[0]
        assert meta["gender"] == "female" and meta["age_band"] == "young"
        # glossary 人名对照落库（含变体note）
        gl = {g.source_term: g for g in db.query(GlossaryTerm).all()}
        assert gl["阿弋"].target_term == "A-Yi"
        assert "Ayi" in (gl["阿弋"].note or "")
        assert "盛君廷" in gl
    finally:
        db.close()

    # 幂等重跑：不重复建行
    te.chat = _stub_chat
    try:
        r2 = c.post(f"/api/projects/{pid}/extract-cast").json()
    finally:
        te.chat = chat_bak
    assert r2["ok"] and r2["speakers_created"] == 0, r2
    db = SessionLocal()
    try:
        assert db.query(Speaker).count() == 3
        assert db.query(GlossaryTerm).count() == 3      # 路人甲英文不同(Passerby)也入表
    finally:
        db.close()
