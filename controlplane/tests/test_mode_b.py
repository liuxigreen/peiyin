"""模式B端到端测试：字幕+中文配音 → 交付包（mock TTS + live翻译链mock provider）"""
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


def test_mode_b_e2e(tmp_path):
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
    # 跑模式B
    r = c.post(f"/api/projects/{pid}/mode-b/run").json()
    assert r["ok"], r
    assert r["clips"] == 2
    assert r["mode"] == "B"
    # 交付包存在且结构完整
    pkg = c.get(f"/api/projects/{pid}/mode-b/package").json()
    assert pkg["ok"]
    import zipfile
    z = zipfile.ZipFile(pkg["file"])
    names = z.namelist()
    assert any(n.endswith(".srt") for n in names), names
    assert any(n.endswith(".ass") for n in names)
    assert "manifest.json" in names and "qc_report.json" in names
    audio_files = [n for n in names if n.startswith("audio/")]
    assert len(audio_files) == 2
    # manifest含译文
    import json as J
    man = J.loads(z.read("manifest.json"))
    assert all(c["text"] for c in man["clips"])


def test_mode_b_requires_audio(tmp_path):
    c = _client(str(tmp_path / "mb2.db"))
    pid = c.post("/api/projects", json={"name": "B", "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": _SRT})
    r = c.post(f"/api/projects/{pid}/mode-b/run")
    assert r.status_code == 400   # 没登记音频→400
