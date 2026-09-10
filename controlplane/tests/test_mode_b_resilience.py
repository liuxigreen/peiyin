"""Mode B package resilience regression tests."""
import importlib
import os

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient


def _client(tmp_db: str) -> TestClient:
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod

    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def test_package_endpoint_survives_breath_generation_failure(tmp_path, monkeypatch):
    """Optional breath generation failure records QC but does not prevent ZIP delivery."""
    storage = tmp_path / "storage"
    monkeypatch.setenv("MODE_B_STORAGE", str(storage))
    client = _client(str(tmp_path / "mode_b.db"))
    project_id = client.post("/api/projects", json={
        "name": "resilience", "target_lang": "en"}).json()["id"]
    srt = """1
00:00:00,000 --> 00:00:02,000
测试台词
"""
    assert client.post(f"/api/projects/{project_id}/seed-srt", json={"srt": srt}).status_code == 200

    from app.db.models import Translation, TtsClip, Utterance
    from app.db.session import SessionLocal

    wav = tmp_path / "clip.wav"
    sf.write(str(wav), np.zeros(1600, dtype="float32"), 16000)
    db = SessionLocal()
    try:
        utterance = db.query(Utterance).filter_by(project_id=project_id).one()
        translation = Translation(utterance_id=utterance.id, target_lang="en",
                                  version=1, text="Test line")
        db.add(translation)
        db.flush()
        db.add(TtsClip(utterance_id=utterance.id, target_lang="en",
                       translation_id=translation.id, version=1,
                       audio_r2_key=str(wav), duration_ms=100,
                       tts_engine="test", status="completed"))
        db.commit()
    finally:
        db.close()

    import app.audio_post as audio_post

    def _raise_breath(_path: str) -> str:
        raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr(audio_post, "make_breath", _raise_breath)
    monkeypatch.setattr(audio_post, "condition_line", lambda src, _dst, **_kwargs: src)
    monkeypatch.setattr(audio_post, "master_mix",
                        lambda _lines, _out, _total, **_kwargs: {"dur_ms": 100})

    response = client.post(f"/api/projects/{project_id}/mode-b/package-from-clips", json={})

    assert response.status_code == 200, response.text
    package = response.json()["zip"]
    assert os.path.exists(package)
