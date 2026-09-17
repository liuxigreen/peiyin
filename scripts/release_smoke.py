#!/usr/bin/env python3
"""Validate the current Git candidate without consuming local deployment data."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_HASHES = {
    "release-artifacts/node-release-channel-bootstrap-v1.zip": "ea12af690d6b824fbb1fc8305d45f400c0ad452be9545ab2bb9eb554242cf7b3",
    "release-artifacts/node-release-channel-bootstrap-v2.zip": "41cac276a0a9cfa7aaf1126d66743a18d1f042d1d20ea8c3bf1151c510ecfbf6",
    "release-artifacts/published/node-release-1.0.0.zip": "a4b7b6de71e5419a91b2f00eddcc86748e1565e1841df0e176167d0be315a1e1",
}
REQUIRED = {
    ".gitignore",
    "README.md",
    "controlplane/requirements.txt",
    "controlplane/requirements-dev.txt",
    "frontend/package.json",
    "frontend/bun.lock",
    "gpunode/requirements.txt",
    "release-manifests/upgrade-seam-wave1.json",
    *ARTIFACT_HASHES,
}
FORBIDDEN_PREFIXES = ("assets/zh_audio.mp3", "controlplane/web/dist.old/")
WORKTREE_CANDIDATE_PATHS = {
    ".gitignore",
    "README.md",
    ".github/workflows/ci.yml",
    "controlplane/requirements-dev.txt",
    "scripts/release_smoke.py",
    "release-manifests/upgrade-seam-wave1.json",
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as package:
        return {
            member.name: package.extractfile(member).read()
            for member in package.getmembers() if member.isfile()
        }


def make_tar(contents: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as package:
        for name in sorted(contents):
            item = tarfile.TarInfo(name)
            item.size = len(contents[name])
            item.mode = 0o100644
            item.mtime = 0
            package.addfile(item, io.BytesIO(contents[name]))
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-package", type=Path,
        help="optional destination for the validated git-archive tar package",
    )
    parser.add_argument(
        "--include-worktree", action="store_true",
        help="validate only the declared release-readiness working-tree changes before commit",
    )
    args = parser.parse_args()

    commit = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", commit],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    contents = archive_members(archive)
    worktree_paths: list[str] = []
    if args.include_worktree:
        changed = set(filter(None, git("diff", "--name-only").splitlines()))
        untracked = set(filter(None, git("ls-files", "--others", "--exclude-standard").splitlines()))
        worktree_paths = sorted(changed | untracked)
        unexpected = sorted(set(worktree_paths) - WORKTREE_CANDIDATE_PATHS)
        if unexpected:
            raise RuntimeError(f"worktree contains paths outside release readiness scope: {unexpected}")
        for path in worktree_paths:
            contents[path] = (ROOT / path).read_bytes()
        archive = make_tar(contents)

    missing = sorted(REQUIRED - contents.keys())
    forbidden = sorted(name for name in contents if name.startswith(FORBIDDEN_PREFIXES))
    if missing or forbidden:
        raise RuntimeError(f"archive invalid: missing={missing} forbidden={forbidden}")
    if args.output_package:
        args.output_package.parent.mkdir(parents=True, exist_ok=True)
        args.output_package.write_bytes(archive)

    for path, expected in ARTIFACT_HASHES.items():
        actual = sha256(contents[path])
        if actual != expected:
            raise RuntimeError(f"artifact checksum mismatch: {path}: {actual}")

    for version in ("v1", "v2"):
        checksum_path = f"release-artifacts/node-release-channel-bootstrap-{version}.sha256"
        expected = ARTIFACT_HASHES[f"release-artifacts/node-release-channel-bootstrap-{version}.zip"]
        expected_line = f"{expected}  node-release-channel-bootstrap-{version}.zip"
        if contents[checksum_path].decode().strip() != expected_line:
            raise RuntimeError(f"checksum sidecar mismatch: {checksum_path}")

    manifest_path = "release-artifacts/published/node-release-1.0.0.manifest.json"
    manifest = json.loads(contents[manifest_path])
    published_path = "release-artifacts/published/node-release-1.0.0.zip"
    if manifest.get("package_sha256") != sha256(contents[published_path]):
        raise RuntimeError("published manifest package checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(contents[published_path])) as payload:
        bad_member = payload.testzip()
        if bad_member:
            raise RuntimeError(f"published payload is corrupt: {bad_member}")
        actual_names = set(payload.namelist())
        expected_names = set(manifest["files"])
        if actual_names != expected_names:
            raise RuntimeError("published manifest member set mismatch")
        for name, expected in manifest["files"].items():
            if sha256(payload.read(name)) != expected:
                raise RuntimeError(f"published manifest member checksum mismatch: {name}")

    compiled = 0
    for name, data in contents.items():
        if name.endswith(".py"):
            compile(data, f"{commit}:{name}", "exec", dont_inherit=True)
            compiled += 1

    result = {
        "candidate_commit": commit,
        "branch": branch,
        "archive_sha256": sha256(archive),
        "archive_file_count": len(contents),
        "compiled_python_sources": compiled,
        "artifact_hashes": ARTIFACT_HASHES,
        "protected_data_absent_from_archive": list(FORBIDDEN_PREFIXES),
        "manifest_member_hashes_verified": len(manifest["files"]),
        "output_package": str(args.output_package) if args.output_package else None,
        "worktree_overlay": worktree_paths,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError,
            zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as error:
        print(f"release smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1)
