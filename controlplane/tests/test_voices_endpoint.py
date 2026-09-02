"""voices端点测试：索引缓存+fid格式校验+404"""
import importlib
import os

from fastapi.testclient import TestClient


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def test_voice_fid_validation(tmp_path):
    c = _client(str(tmp_path / "v.db"))
    # 非法fid（含特殊字符/过长）→400
    assert c.get("/api/nodes/voices/..%2Fetc.wav").status_code in (400, 404)
    assert c.get("/api/nodes/voices/" + "a" * 20 + ".wav").status_code == 400
    # 不存在的合法fid → 404
    assert c.get("/api/nodes/voices/abcd1234ef.wav").status_code == 404


def test_voice_index_cache_build(tmp_path, monkeypatch):
    """有音色文件时索引能命中。"""
    import hashlib
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    wav_bytes = b"RIFF" + b"\x00" * 100
    (voices_dir / "va_test.wav").write_bytes(wav_bytes)
    fid = "v" + hashlib.md5(wav_bytes).hexdigest()[:10]
    monkeypatch.setenv("NODE_VOICES_DIR", str(voices_dir))
    import app.api.nodes as nodes_mod
    importlib.reload(nodes_mod)
    idx = nodes_mod._build_voice_index()
    assert fid in idx and idx[fid].endswith("va_test.wav")
