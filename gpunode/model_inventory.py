"""离线模型快照清单与本地完整性预检。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST_PATH = Path(__file__).with_name("models") / "manifest.json"
ECAPA_MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"


@dataclass(frozen=True)
class ModelPreflight:
    """不泄露机器路径或密钥的模型可用性结果。"""

    ready: bool
    model_id: str
    release: str | None
    snapshot_path: str | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_sha256(snapshot_path: str | Path) -> str:
    """返回目录快照的稳定内容摘要；符号链接不属于受支持的发布包。"""
    root = Path(snapshot_path)
    if not root.is_dir():
        raise ValueError("snapshot directory is absent")

    digest = hashlib.sha256(b"gpunode-model-snapshot-v1\0")
    files = 0
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            raise ValueError("snapshot contains a symbolic link")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("snapshot contains a non-regular file")
        relative_path = candidate.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative_path)
        digest.update(b"\0")
        with candidate.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
        files += 1
    if not files:
        raise ValueError("snapshot contains no files")
    return digest.hexdigest()


def _load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("model manifest is absent") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("model manifest is invalid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("model manifest schema_version must be 1")
    model = document.get("ecapa")
    if not isinstance(model, dict):
        raise ValueError("model manifest ECAPA entry is absent")
    if model.get("model_id") != ECAPA_MODEL_ID:
        raise ValueError("model manifest ECAPA model_id is invalid")
    if not isinstance(model.get("release"), str) or not model["release"].strip():
        raise ValueError("model manifest ECAPA release is absent")
    return model


def ecapa_preflight(manifest_path: str | Path = DEFAULT_MANIFEST_PATH) -> ModelPreflight:
    """只读取本地文件，校验 ECAPA 快照能否供 diarize 离线加载。"""
    try:
        model = _load_manifest(manifest_path)
    except ValueError as exc:
        return ModelPreflight(False, ECAPA_MODEL_ID, None, None, str(exc))

    release = model["release"]
    snapshot_path = model.get("snapshot_path")
    expected_sha256 = model.get("snapshot_sha256")
    if not isinstance(snapshot_path, str) or not snapshot_path.strip() or snapshot_path.startswith("__"):
        return ModelPreflight(False, ECAPA_MODEL_ID, release, None,
                              "ECAPA snapshot_path is not configured")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        return ModelPreflight(False, ECAPA_MODEL_ID, release, snapshot_path,
                              "ECAPA snapshot_sha256 is not configured")
    if any(character not in "0123456789abcdef" for character in expected_sha256.lower()):
        return ModelPreflight(False, ECAPA_MODEL_ID, release, snapshot_path,
                              "ECAPA snapshot_sha256 is invalid")

    try:
        actual_sha256 = snapshot_sha256(snapshot_path)
    except (OSError, ValueError) as exc:
        return ModelPreflight(False, ECAPA_MODEL_ID, release, snapshot_path,
                              f"ECAPA snapshot unavailable: {exc}")
    if actual_sha256 != expected_sha256.lower():
        return ModelPreflight(False, ECAPA_MODEL_ID, release, snapshot_path,
                              "ECAPA snapshot SHA256 mismatch")
    return ModelPreflight(True, ECAPA_MODEL_ID, release, snapshot_path)


if __name__ == "__main__":
    print(json.dumps(ecapa_preflight().to_dict(), ensure_ascii=False, sort_keys=True))
