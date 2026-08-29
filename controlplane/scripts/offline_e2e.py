"""O5: 离线全流程E2E——SRT进，成片+QC报告出。零外部依赖¥0。
链路：seed-srt → 五步翻译(mock) → 假TTS(gpunode offline) → fit(real_cpu)
     → 配音轨预拼(render) → 混流烧字幕(render) → QC三查+QC Agent钩子
用法：.venv/bin/python scripts/offline_e2e.py   （独立于pytest，可重复跑）
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile

import numpy as np
import soundfile as sf

CP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CP)
sys.path.insert(0, os.path.join(os.path.dirname(CP), "gpunode"))

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/e2e.db")
os.environ.setdefault("NODE_MODE", "offline")

import app.db.session as session_mod   # noqa: E402
import app.main as main_mod            # noqa: E402
importlib.reload(session_mod)
importlib.reload(main_mod)
session_mod.init_db()

from fastapi.testclient import TestClient  # noqa: E402

from stages.real_cpu import fit_timeline          # noqa: E402  gpunode真CPU实现
from stages.router import run_task                # noqa: E402  gpunode假stage

from app.render import (build_dub_track, mux_video, qc_report,  # noqa: E402
                        write_ass, write_srt)
from app.qc_agent import run_qc_hook              # noqa: E402

SRT = """1
00:00:01,000 --> 00:00:03,000
总裁，夫人她离婚了！

2
00:00:04,000 --> 00:00:06,500
你怎么敢跟我说话

3
00:00:07,500 --> 00:00:10,000
我不知道他在哪里

4
00:00:11,000 --> 00:00:14,000
好啊，我成全你们
"""


def make_test_video(path: str, dur_s: float = 16.0):
    from app.render import ffmpeg_bin
    import subprocess
    subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=duration={dur_s}:size=540x960:rate=12",
                    "-f", "lavfi", "-i", f"sine=frequency=180:duration={dur_s}:sample_rate=44100",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-c:a", "aac", "-shortest", path], check=True, capture_output=True)


def main() -> int:
    out_dir = tempfile.mkdtemp(prefix="dub_e2e_")
    print(f"工作目录: {out_dir}")
    c = TestClient(main_mod.app)

    # 1. 建项目+种子（模拟"上传SRT完成"）
    pid = c.post("/api/projects", json={"name": "离线E2E剧", "target_lang": "en"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/seed-srt", json={"srt": SRT, "scene_size": 40}).json()
    assert r["ok"], r
    print(f"① 种子: {r['utterances']}句")

    # 2. 五步翻译（MOCK provider）+QC钩子
    r = c.post(f"/api/projects/{pid}/run-translate").json()
    assert r["completed"] == 1, r
    print(f"② 翻译: 完成 qc={r['results'][0]['qc']}")

    # 3. 造母片 + 假TTS（gpunode offline stage）
    video = os.path.join(out_dir, "source.mp4")
    make_test_video(video)
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    tts_task = {"id": "e2e-tts-1", "task_type": "tts",
                "inputs": {"utterances": [
                    {"uid": u["uid"], "text": u["translated"],
                     "start_ms": 1000 + i * 3300,
                     "end_ms": 1000 + i * 3300 + 2300}
                    for i, u in enumerate(utts)]}}
    tts_out = run_task(tts_task)[0]["clips"]
    print(f"③ 假TTS: {len(tts_out)}句音频")

    # 4. fit-timeline（真CPU实现）
    utt_by_uid = {u["uid"]: u for u in utts}
    clips = []
    for cl in tts_out:
        u = utt_by_uid[cl["uid"]]
        clips.append({**cl, "start_ms": 1000 + list(utt_by_uid).index(cl["uid"]) * 3300,
                      "end_ms": 1000 + list(utt_by_uid).index(cl["uid"]) * 3300 + 2300})
    fitted = fit_timeline(clips, out_dir)
    print(f"④ Fit: " + ", ".join(f"{f['uid']}→{f['final_ms']}ms@{f['speed']}x" for f in fitted))

    # 5. 配音轨预拼 + 混流烧字幕（render真实现）
    total_ms = 16000
    dub = build_dub_track(
        [{"seq_index": i + 1, "start_ms": f["start_ms"], "end_ms": f["end_ms"]}
         for i, f in enumerate(fitted)],
        {i + 1: f["path"] for i, f in enumerate(fitted)},
        total_ms, os.path.join(out_dir, "dub.wav"))
    entries = [{"start_ms": f["start_ms"], "end_ms": f["start_ms"] + f["final_ms"],
                "text": utt_by_uid[fitted[i]["uid"]]["translated"]}
               for i, f in enumerate(fitted)]
    srt = write_srt(entries, os.path.join(out_dir, "subs.srt"))
    ass = write_ass(entries, os.path.join(out_dir, "subs.ass"),
                    width=540, height=960, band_center_y=760)
    final = mux_video(video, dub, os.path.join(out_dir, "final.mp4"), ass_path=ass)
    print(f"⑤ 渲染: {os.path.getsize(final)//1024}KB  字幕{srt.split('/')[-1]}/ass")

    # 6. QC三查 + QC Agent影子行
    rep = qc_report(final, expect_duration_ms=total_ms)
    print(f"⑥ QC: lufs={rep['lufs']} 静音={rep['unexpected_silences']} "
          f"漂移={rep['duration_drift_s']}s pass={rep['lufs_pass'] and rep['silence_pass'] and rep['duration_pass']}")
    from app.db.session import SessionLocal
    from app.db.models import PipelineTask
    db = SessionLocal()
    shadow = PipelineTask(project_id=pid, task_key="E2E/render", task_type="encode",
                          resource="cpu", gpu_required=False, status="completed",
                          output_paths={"final": final, "expect_ms": total_ms})
    db.add(shadow)
    db.commit()
    qc = run_qc_hook(shadow, db)
    db.close()
    print(f"⑦ QC Agent: pass={qc['pass']} action={qc['action']} "
          f"checks={[x['name'] for x in qc['checks']]}")

    qc_api = c.get(f"/api/projects/{pid}/qc").json()
    print(f"⑧ 质检Tab: {qc_api['passed']}/{qc_api['total']} pass")
    status = c.get("/api/power/status").json()
    print(f"⑨ 算力Agent: dry_run={status['dry_run']} backlog={status['gpu_backlog']}")

    ok = (rep["lufs_pass"] and rep["silence_pass"] and rep["duration_pass"]
          and qc_api["total"] >= 1)
    print(f"\n{'✅ 离线全流程E2E PASS' if ok else '❌ E2E FAIL'}  成片: {final}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
