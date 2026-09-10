"""模式 B 主流程测试：混音输入只能进入真实 GPU 克隆链。"""
import importlib
import os

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

_SRT = """1
00:00:01,000 --> 00:00:03,500
你敢动她一下试试？

2
00:00:04,000 --> 00:00:07,000
我已经不是三年前的那个废物了
"""


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def _make_zh_audio(path: str, dur_s: float = 8.0, sr: int = 16000):
    t = np.linspace(0, dur_s, int(dur_s * sr), endpoint=False)
    sf.write(path, (0.4 * np.sin(2 * np.pi * 280 * t)).astype(np.float32), sr)


def test_mode_b_audio_starts_separation_without_mock_delivery(tmp_path):
    c = _client(str(tmp_path / "mb.db"))
    pid = c.post("/api/projects", json={
        "name": "模式B剧", "target_lang": "en"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT}).json()
    assert r["utterances"] == 2
    # 中文配音音频
    audio = str(tmp_path / "zh_dub.wav")
    _make_zh_audio(audio, 8.0)
    r = c.post(f"/api/projects/{pid}/mode-b/upload-audio",
               json={"audio_path": audio}).json()
    assert r["ok"]
    # 有中文配音时，主流程只创建真实的分离任务；不能回退生成 mock TTS 或交付包。
    r = c.post(f"/api/projects/{pid}/mode-b/run").json()
    assert r["ok"], r
    assert r["mode"] == "B"
    assert r["phase"] == "separate"
    assert r["task_id"]
    assert "真实克隆链路" in r["note"]
    pkg = c.get(f"/api/projects/{pid}/mode-b/package")
    assert pkg.status_code == 404


def test_mode_b_pure_translate_without_audio(tmp_path):
    """无音频=纯翻译模式：200完成，clips=0，附补传提示。"""
    c = _client(str(tmp_path / "mb2.db"))
    pid = c.post("/api/projects", json={"name": "B", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    r = c.post(f"/api/projects/{pid}/mode-b/run")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["clips"] == 0
    assert "补传配音" in body["note"]


def test_diarize_audio_url_matches_payload_and_download(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("MODE_B_STORAGE", str(storage))
    c = _client(str(tmp_path / "diarize.db"))
    pid = c.post("/api/projects", json={"name": "声纹剧", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    audio_bytes = b"diarize-source-audio"
    audio = tmp_path / "zh_audio.mp3"
    audio.write_bytes(audio_bytes)
    uploaded = c.post(f"/api/projects/{pid}/mode-b/upload-audio",
                      json={"audio_path": str(audio)})
    assert uploaded.status_code == 200

    created = c.post(f"/api/projects/{pid}/mode-b/diarize", json={})
    assert created.status_code == 200
    response = created.json()
    assert "/api/nodes/voices/zhaudio/" in response["audio_url"]

    from app.db.models import PipelineTask
    import app.db.session as session_mod
    db = session_mod.SessionLocal()
    try:
        task = db.get(PipelineTask, response["task_id"])
        payload_url = task.output_paths["payload"]["zh_audio_url"]
    finally:
        db.close()
    assert payload_url == response["audio_url"]

    downloaded = c.get(response["audio_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == audio_bytes
