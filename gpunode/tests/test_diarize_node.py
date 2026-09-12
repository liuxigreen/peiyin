"""离线单测：diarize 的控制面下载和 embedding 错误边界。"""
from __future__ import annotations

import contextlib
import os
import sys
import types
import urllib.error

import numpy as np
import pytest

from gpunode import model_inventory
from gpunode.stages import diarize_node as diarize


class _Response:
    def __init__(self, chunks, status=200, content_length=None):
        self.chunks = iter(chunks)
        self.status = status
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def read(self, _size):
        return next(self.chunks, b"")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_node_token_strips_content_and_returns_empty_when_missing(tmp_path, monkeypatch):
    token_file = tmp_path / "node_token.txt"
    token_file.write_text("  persisted-token\n", encoding="utf-8")
    monkeypatch.setattr(diarize, "TOKEN_FILE", str(token_file))
    assert diarize._node_token() == "persisted-token"

    monkeypatch.setattr(diarize, "TOKEN_FILE", str(tmp_path / "missing-token.txt"))
    assert diarize._node_token() == ""


def test_download_uses_control_relative_url_and_bearer_token(tmp_path, monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return _Response([b"zh-", b"audio", b""])

    monkeypatch.setattr(diarize, "CONTROL", "https://control.example/")
    monkeypatch.setattr(diarize, "_node_token", lambda: "persisted-token")
    monkeypatch.setattr(diarize.urllib.request, "urlopen", fake_urlopen)
    target = tmp_path / "zh.mp3"

    assert diarize._download_zh_audio(str(target), "/api/nodes/voices/zhaudio/a.mp3") == str(target)
    assert target.read_bytes() == b"zh-audio"
    assert seen == {"url": "https://control.example/api/nodes/voices/zhaudio/a.mp3",
                    "auth": "Bearer persisted-token", "timeout": diarize.ZH_AUDIO_DOWNLOAD_TIMEOUT_SECONDS}


def test_download_rejects_missing_token_and_http_failure(tmp_path, monkeypatch):
    target = str(tmp_path / "zh.mp3")
    monkeypatch.setattr(diarize, "_node_token", lambda: "")
    with pytest.raises(RuntimeError, match="token missing"):
        diarize._download_zh_audio(target, "/api/nodes/voices/zhaudio/a.mp3")

    monkeypatch.setattr(diarize, "_node_token", lambda: "token")

    def fail_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "unavailable", None, None)

    monkeypatch.setattr(diarize.urllib.request, "urlopen", fail_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        diarize._download_zh_audio(target, "/api/nodes/voices/zhaudio/a.mp3")

    monkeypatch.setattr(diarize.urllib.request, "urlopen", lambda _request, timeout: _Response([b""]))
    with pytest.raises(RuntimeError, match="was empty"):
        diarize._download_zh_audio(target, "/api/nodes/voices/zhaudio/a.mp3")


def test_run_diarize_downloads_missing_payload_audio(tmp_path, monkeypatch):
    downloaded = tmp_path / "downloaded.mp3"
    downloaded.write_bytes(b"audio")
    seen = {}

    def fake_download(local_path, audio_url):
        seen.update(local_path=local_path, audio_url=audio_url)
        return str(downloaded)

    monkeypatch.setattr(diarize, "_download_zh_audio", fake_download)
    with pytest.raises(RuntimeError, match="srt_slots empty"):
        diarize.run_diarize({"payload": {"zh_audio": str(tmp_path / "absent.mp3"),
                                          "zh_audio_url": "/api/nodes/voices/zhaudio/a.mp3"}})
    assert seen == {"local_path": str(tmp_path / "absent.mp3"),
                    "audio_url": "/api/nodes/voices/zhaudio/a.mp3"}


def test_run_diarize_falls_back_to_claimed_output_payload(tmp_path, monkeypatch):
    downloaded = tmp_path / "downloaded.mp3"
    downloaded.write_bytes(b"audio")
    seen = {}

    def fake_download(local_path, audio_url):
        seen.update(local_path=local_path, audio_url=audio_url)
        return str(downloaded)

    monkeypatch.setattr(diarize, "_download_zh_audio", fake_download)
    task = {
        "id": "diarize-1",
        "task_type": "diarize",
        "payload": {},
        "output_paths": {
            "payload": {
                "zh_audio_url": "/api/nodes/tasks/source-1/artifacts/vocals/vocals.wav",
                "srt_slots": [],
            }
        },
    }

    with pytest.raises(RuntimeError, match="srt_slots empty"):
        diarize.run_diarize(task)

    assert seen == {
        "local_path": "",
        "audio_url": "/api/nodes/tasks/source-1/artifacts/vocals/vocals.wav",
    }


def test_run_diarize_keeps_nonempty_top_level_payload_precedence(tmp_path, monkeypatch):
    audio = tmp_path / "local.mp3"
    audio.write_bytes(b"audio")
    seen = []

    monkeypatch.setattr(
        diarize,
        "_download_zh_audio",
        lambda *_args: pytest.fail("local top-level audio must be used"),
    )
    monkeypatch.setattr(
        diarize,
        "_cut_slots",
        lambda path, slots: seen.append((path, slots)) or [],
    )
    with pytest.raises(RuntimeError, match="too few valid refs"):
        diarize.run_diarize({
            "payload": {
                "zh_audio": str(audio),
                "srt_slots": [{"uid": "u1", "start_ms": 0, "end_ms": 500}],
            },
            "output_paths": {
                "payload": {
                    "zh_audio_url": "/must-not-be-used",
                    "srt_slots": [],
                }
            },
        })
    assert seen == [(str(audio), [{"uid": "u1", "start_ms": 0, "end_ms": 500}])]


def _install_fake_runtime(monkeypatch, read, encode=None, init_error=None):
    """注入 soundfile/torch/ECAPA/sklearn，测试不触发真实模型、GPU 或网络。"""
    class LibsndfileError(Exception):
        pass

    soundfile = types.ModuleType("soundfile")
    soundfile.LibsndfileError = LibsndfileError
    soundfile.SoundFileError = LibsndfileError
    soundfile.SoundFileRuntimeError = LibsndfileError
    soundfile.read = read

    class Tensor:
        def __init__(self, value):
            self.value = value

        def unsqueeze(self, _axis):
            return self

    torch = types.ModuleType("torch")
    torch.from_numpy_calls = 0

    def from_numpy(value):
        torch.from_numpy_calls += 1
        return Tensor(value)

    torch.from_numpy = from_numpy
    torch.no_grad = contextlib.nullcontext

    class Embedding:
        def squeeze(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.array([1.0, 0.0], dtype=np.float32)

    class Encoder:
        def __init__(self):
            self.calls = 0

        def encode_batch(self, tensor):
            self.calls += 1
            if encode:
                return encode(tensor)
            return Embedding()

    encoder = Encoder()
    encoder.from_hparams_kwargs = None

    class EncoderClassifier:
        @staticmethod
        def from_hparams(**kwargs):
            if init_error:
                raise init_error
            encoder.from_hparams_kwargs = kwargs
            return encoder

    speechbrain = types.ModuleType("speechbrain")
    inference = types.ModuleType("speechbrain.inference")
    speaker = types.ModuleType("speechbrain.inference.speaker")
    speaker.EncoderClassifier = EncoderClassifier
    cluster = types.ModuleType("sklearn.cluster")

    class AgglomerativeClustering:
        def __init__(self, **_kwargs):
            pass

        def fit_predict(self, values):
            return np.zeros(len(values), dtype=int)

    cluster.AgglomerativeClustering = AgglomerativeClustering
    sklearn = types.ModuleType("sklearn")
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "speechbrain", speechbrain)
    monkeypatch.setitem(sys.modules, "speechbrain.inference", inference)
    monkeypatch.setitem(sys.modules, "speechbrain.inference.speaker", speaker)
    monkeypatch.setitem(sys.modules, "sklearn", sklearn)
    monkeypatch.setitem(sys.modules, "sklearn.cluster", cluster)
    return soundfile, torch, encoder


def _task_with_audio(tmp_path):
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"source")
    slots = [{"uid": f"u{i}", "start_ms": i * 1000, "end_ms": i * 1000 + 500}
             for i in range(11)]
    return {"payload": {"zh_audio": str(audio), "project_id": "p", "srt_slots": slots}}


def _configure_run(monkeypatch, tmp_path, wav_count=11):
    snapshot = tmp_path / "verified-ecapa"
    snapshot.mkdir()
    (snapshot / "hyperparams.yaml").write_text("modules: {}", encoding="utf-8")
    manifest = tmp_path / "ecapa-manifest.json"
    manifest.write_text(__import__("json").dumps({
        "schema_version": 1,
        "ecapa": {
            "model_id": model_inventory.ECAPA_MODEL_ID,
            "release": "test-local-release-v1",
            "snapshot_path": str(snapshot),
            "snapshot_sha256": model_inventory.snapshot_sha256(snapshot),
        },
    }), encoding="utf-8")
    monkeypatch.setattr(
        diarize,
        "ecapa_preflight",
        lambda: model_inventory.ecapa_preflight(manifest),
    )
    monkeypatch.setattr(diarize, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(diarize, "REF_DIR", str(tmp_path / "refs"))
    monkeypatch.setattr(diarize, "_snr", lambda _path: 12.0)
    monkeypatch.setattr(
        diarize,
        "_cut_slots",
        lambda _audio, slots: [{**slot, "wav": f"mock-{i}.wav"}
                               for i, slot in enumerate(slots[:wav_count])],
    )


def test_embedding_success_reads_tensor_and_encodes_without_real_runtime(tmp_path, monkeypatch):
    _configure_run(monkeypatch, tmp_path)
    soundfile, torch, encoder = _install_fake_runtime(
        monkeypatch, lambda _path, dtype: (np.array([0.1, 0.2], dtype=np.float32), 16000)
    )

    rows = diarize.run_diarize(_task_with_audio(tmp_path))

    assert rows[0]["key"] == "diarize_result"
    result = __import__("json").load(open(rows[0]["path"], encoding="utf-8"))
    assert result["total"] == 11
    assert soundfile.read is not None
    assert torch.from_numpy_calls == 11
    assert encoder.calls == 11
    assert encoder.from_hparams_kwargs["source"] == encoder.from_hparams_kwargs["savedir"]
    assert encoder.from_hparams_kwargs["source"].endswith("verified-ecapa")
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_one_decode_error_is_skipped_and_reported(tmp_path, monkeypatch, capsys):
    _configure_run(monkeypatch, tmp_path)
    calls = {"n": 0}

    def read(path, dtype):
        calls["n"] += 1
        if path == "mock-0.wav":
            raise error_type("corrupt frame")
        return np.array([0.1, 0.2], dtype=np.float32), 16000

    soundfile, _torch, encoder = _install_fake_runtime(monkeypatch, read)
    error_type = soundfile.LibsndfileError
    rows = diarize.run_diarize(_task_with_audio(tmp_path))

    assert encoder.calls == 10
    assert "skip decode mock-0.wav: LibsndfileError: corrupt frame" in capsys.readouterr().out
    assert __import__("json").load(open(rows[0]["path"], encoding="utf-8"))["total"] == 10


def test_systemic_encoder_error_is_not_swallowed(tmp_path, monkeypatch):
    _configure_run(monkeypatch, tmp_path)
    _install_fake_runtime(
        monkeypatch,
        lambda _path, dtype: (np.array([0.1, 0.2], dtype=np.float32), 16000),
        init_error=RuntimeError("ECAPA device initialization failed"),
    )

    with pytest.raises(RuntimeError, match="ECAPA device initialization failed"):
        diarize.run_diarize(_task_with_audio(tmp_path))


def test_encode_batch_runtime_error_is_not_swallowed(tmp_path, monkeypatch):
    _configure_run(monkeypatch, tmp_path)

    def fail_encode(_tensor):
        raise RuntimeError("ECAPA encode_batch device failure")

    _install_fake_runtime(
        monkeypatch,
        lambda _path, dtype: (np.array([0.1, 0.2], dtype=np.float32), 16000),
        encode=fail_encode,
    )
    with pytest.raises(RuntimeError, match="ECAPA encode_batch device failure"):
        diarize.run_diarize(_task_with_audio(tmp_path))


def test_all_decode_errors_include_candidate_skip_and_reason_diagnostics(tmp_path, monkeypatch):
    _configure_run(monkeypatch, tmp_path, wav_count=10)
    soundfile, _torch, _encoder = _install_fake_runtime(monkeypatch, lambda _path, dtype: None)

    def always_bad(_path, dtype):
        raise soundfile.LibsndfileError("invalid audio")

    soundfile.read = always_bad
    with pytest.raises(RuntimeError, match=r"candidates=10, embeddings=0, skipped=10.*LibsndfileError: invalid audio"):
        diarize.run_diarize(_task_with_audio(tmp_path))
