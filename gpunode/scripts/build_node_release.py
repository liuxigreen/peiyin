#!/usr/bin/env python3
"""Build a signed node payload from an explicit, reviewed file allowlist."""
from __future__ import annotations
import argparse, hashlib, hmac, json, os, re, tempfile, zipfile
from pathlib import Path, PurePosixPath

def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    lowered = [part.lower() for part in path.parts]
    if not name.startswith("gpunode/") or "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts or "." in path.parts or not path.parts or any(
        part in {"entrypoint.py", "workdir", "models"} or any(x in part for x in ("token", "secret", "key")) for part in lowered
    ):
        raise ValueError("unsafe include")
    return path

def canonical(manifest: dict) -> bytes:
    return json.dumps({k: v for k, v in manifest.items() if k != "signature"}, sort_keys=True, separators=(",", ":")).encode()

def build_release(source_root: Path, output_dir: Path, version: str, source_revision: str, hmac_key: bytes,
                  includes: list[str], package_url: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version) or not source_revision or not hmac_key or not includes or not package_url.startswith("https://"):
        raise ValueError("explicit include allowlist is required")
    root = source_root.resolve()
    checked = []
    for raw in includes:
        name = _safe_name(raw); source = (root / Path(*name.parts)).resolve()
        if not source.is_file() or source.is_symlink() or source == root or root not in source.parents: raise ValueError("unsafe include")
        checked.append((str(name), source))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".node-release-", dir=output_dir.parent)); stage_package = stage / "package.zip"
    package = output_dir / ("node-release-" + version + ".zip")
    files: dict[str, str] = {}
    seen: set[str] = set()
    with zipfile.ZipFile(stage_package, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, source in checked:
            if name in seen:
                raise ValueError("duplicate include")
            seen.add(name)
            data = source.read_bytes()
            files[name] = hashlib.sha256(data).hexdigest()
            archive.writestr(name, data)
    if not package_url.startswith("https://"):
        raise ValueError("package URL must use HTTPS")
    manifest = {"version": version, "source_revision": source_revision, "package_url": package_url,
                "package_sha256": hashlib.sha256(stage_package.read_bytes()).hexdigest(), "files": files}
    manifest["signature"] = hmac.new(hmac_key, canonical(manifest), hashlib.sha256).hexdigest()
    manifest_path = output_dir / (package.stem + ".manifest.json"); stage_manifest = stage / "manifest.json"
    stage_manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    output_dir.mkdir(parents=True, exist_ok=True); os.replace(stage_package, package); os.replace(stage_manifest, manifest_path); stage.rmdir()
    return package, manifest_path

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True); parser.add_argument("--source-revision", required=True); parser.add_argument("--package-url", required=True)
    parser.add_argument("--hmac-key-file", type=Path, required=True); parser.add_argument("--include", action="append", default=[])
    args = parser.parse_args()
    package, manifest = build_release(args.source_root, args.output_dir, args.version, args.source_revision,
                                      args.hmac_key_file.read_bytes().strip(), args.include, args.package_url)
    print(package); print(manifest)
if __name__ == "__main__": main()
