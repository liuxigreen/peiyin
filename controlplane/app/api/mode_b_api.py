"""模式B API：无视频流程（字幕+中文配音 → 交付包）"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db.models import Project, Translation, Utterance
from ..db.session import get_db
from ..mode_b import audio_slots, build_package, tts_clips_mock
from ..translate_executor import run_translate_scene

router = APIRouter(prefix="/api", tags=["mode-b"])

STORAGE = os.getenv("MODE_B_STORAGE", "/tmp/peiyin-mode-b")


@router.post("/projects/{pid}/mode-b/upload-audio")
async def upload_audio_info(pid: str, body: dict, db: Session = Depends(get_db)):
    """登记中文配音音频（本地路径或R2 key）。body: {audio_path} 
    云端真桶接入后走presign直传；当前阶段workbench上传的文件路径直接登记。"""
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    if not os.path.exists(body.get("audio_path", "")):
        raise HTTPException(400, f"audio file not found: {body.get('audio_path')}")
    cfg = dict(p.config or {})
    cfg["mode_b_audio"] = body["audio_path"]
    p.config = cfg
    db.commit()
    return {"ok": True}


@router.post("/projects/{pid}/mode-b/upload-file")
async def upload_audio_file(pid: str, file: UploadFile = File(...),
                            db: Session = Depends(get_db)):
    """模式B：浏览器直接上传中文配音音频（multipart→本地STORAGE）。"""
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    os.makedirs(STORAGE, exist_ok=True)
    dest_dir = os.path.join(STORAGE, pid[:8])
    os.makedirs(dest_dir, exist_ok=True)
    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    dest = os.path.join(dest_dir, f"zh_audio{suffix}")
    with open(dest, "wb") as f:
        f.write(await file.read())
    cfg = dict(p.config or {})
    cfg["mode_b_audio"] = dest
    p.config = cfg
    db.commit()
    return {"ok": True, "path": dest, "size": os.path.getsize(dest)}


@router.post("/projects/{pid}/mode-b/run")
async def run_mode_b(pid: str, db: Session = Depends(get_db)):
    """模式B主流程：B2槽位→B3翻译→B4 TTS(降级)→B5 fit→B6交付包。
    前置：seed-srt已跑、mode_b_audio已登记、provider已配置。"""
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    audio_path = (p.config or {}).get("mode_b_audio")
    if not audio_path or not os.path.exists(audio_path):
        raise HTTPException(400, "中文配音音频未登记或不存在（先POST mode-b/upload-audio）")
    utts = (db.query(Utterance).filter_by(project_id=pid)
              .order_by(Utterance.seq_index).all())
    if not utts:
        raise HTTPException(400, "先seed-srt导入中文字幕")
    os.makedirs(STORAGE, exist_ok=True)
    work = os.path.join(STORAGE, pid[:8])
    out_dir = os.path.join(work, "package")
    os.makedirs(out_dir, exist_ok=True)

    # B2 槽位
    entries = [{"uid": u.uid, "start_ms": u.start_ms, "end_ms": u.end_ms}
               for u in utts]
    slots = audio_slots(audio_path, entries, os.path.join(work, "zh_refs"))
    # B3 翻译（五步链，live provider；含语种校验兜底）
    scenes = sorted({(u.uid or "SC01").split("-")[0] for u in utts})
    tr_results = []
    for sc in scenes:
        info = await run_translate_scene(db, p, sc)
        tr_results.append(info)
    # 收集最新译文
    latest: dict[str, Translation] = {}
    for t in (db.query(Translation).filter_by(target_lang=p.target_lang)
                 .order_by(Translation.version).all()):
        latest[t.utterance_id] = t
    translations = {u.uid: (latest[u.id].text if u.id in latest else "")
                    for u in utts}
    # B4 TTS（降级占位；GPU/Confucius4接入后替换）
    fitted = tts_clips_mock(slots, p.target_lang, os.path.join(work, "dub"))
    # B6 交付包
    qc = {"syllable_over": sum(1 for r in tr_results for k in [r] if r.get("over_limit")),
          "translations": len(translations),
          "slots_out_of_audio": sum(1 for s in slots if not s["within_audio"])}
    pkg = build_package({"id": pid, "name": p.name, "target_lang": p.target_lang},
                        entries, translations, fitted, out_dir, qc)
    return {"ok": True, "package": pkg, "clips": len(fitted),
            "scenes_translated": len(tr_results), "mode": "B"}


@router.get("/projects/{pid}/mode-b/package")
def get_package(pid: str, db: Session = Depends(get_db)):
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    pkg = os.path.join(STORAGE, pid[:8], "package")
    zips = [f for f in os.listdir(pkg) if f.endswith(".zip")] if os.path.isdir(pkg) else []
    if not zips:
        raise HTTPException(404, "交付包未生成（先run mode-b）")
    return {"ok": True, "file": os.path.join(pkg, zips[0]),
            "download_url": f"/api/projects/{pid}/mode-b/download"}


@router.get("/projects/{pid}/mode-b/download")
def download_package(pid: str, db: Session = Depends(get_db)):
    import zipfile
    from fastapi.responses import FileResponse
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    pkg = os.path.join(STORAGE, pid[:8], "package")
    zips = [f for f in os.listdir(pkg) if f.endswith(".zip")] if os.path.isdir(pkg) else []
    if not zips:
        raise HTTPException(404, "交付包未生成")
    return FileResponse(os.path.join(pkg, zips[0]), filename=zips[0],
                        media_type="application/zip")
