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
    has_audio = bool(audio_path and os.path.exists(audio_path))
    cfg0 = dict(p.config or {})
    cfg0["mode"] = "B"
    p.config = cfg0
    db.commit()
    utts = (db.query(Utterance).filter_by(project_id=pid)
              .order_by(Utterance.seq_index).all())
    if not utts:
        raise HTTPException(400, "先seed-srt导入中文字幕")
    os.makedirs(STORAGE, exist_ok=True)
    work = os.path.join(STORAGE, pid[:8])
    out_dir = os.path.join(work, "package")
    os.makedirs(out_dir, exist_ok=True)

    # B2 槽位（无音频时跳过——纯翻译模式）
    entries = [{"uid": u.uid, "start_ms": u.start_ms, "end_ms": u.end_ms}
               for u in utts]
    slots = audio_slots(audio_path, entries, os.path.join(work, "zh_refs")) if has_audio else []
    # B3 翻译（五步链，live provider；含语种校验兜底）
    scenes = sorted({(u.uid or "SC01").split("-")[0] for u in utts})
    tr_results = []
    for sc in scenes:
        info = await run_translate_scene(db, p, sc)
        tr_results.append(info)
    # 收集最新译文
    from ..translate_executor import is_placeholder
    latest: dict[str, Translation] = {}
    for t in (db.query(Translation).filter_by(target_lang=p.target_lang)
                 .order_by(Translation.version).all()):
        if is_placeholder(t.text or ""):
            continue                     # 隔离句不进交付包（音频位空缺→补传/人工）
        latest[t.utterance_id] = t
    translations = {u.uid: (latest[u.id].text if u.id in latest else "")
                    for u in utts}
    # B4/B6：有音频才生成TTS与交付包；无音频=纯翻译模式（音频后补再run一次出包）
    if has_audio:
        fitted = tts_clips_mock(slots, p.target_lang, os.path.join(work, "dub"))
        qc = {"syllable_over": sum(1 for r in tr_results for k in [r] if r.get("over_limit")),
              "translations": len(translations),
              "slots_out_of_audio": sum(1 for s in slots if not s["within_audio"])}
        pkg = build_package({"id": pid, "name": p.name, "target_lang": p.target_lang},
                            entries, translations, fitted, out_dir, qc)
        return {"ok": True, "package": pkg, "clips": len(fitted),
                "scenes_translated": len(tr_results), "mode": "B",
                "translations": translations}
    return {"ok": True, "mode": "B", "translations": translations,
            "clips": 0, "note": "纯翻译完成；补传配音后再次run即可生成交付包",
            "scenes_translated": len(tr_results)}


def _tts_payload(db: Session, p: Project, target_u: Utterance,
                 latest: Translation, body: dict) -> tuple[dict, Speaker | None]:
    """单句TTS payload构建（单测端点与批量端点共用）。
    显式body参数 > speaker档案音色分配(G8) > 默认mock；G7语气参数进payload。"""
    from ..db.models import Speaker
    from ..voice_assign import assign_voice
    spk = None
    if body.get("speaker_id"):
        spk = db.get(Speaker, body["speaker_id"])
    elif body.get("speaker"):
        spk = (db.query(Speaker)
                 .filter(Speaker.project_id == p.id,
                         Speaker.label.in_([body["speaker"]])
                         | Speaker.role_name.in_([body["speaker"]])).first())
    voice = assign_voice(db, p, spk) if spk else {}
    payload = {"text": latest.text, "lang": p.target_lang,
               "engine": body.get("engine") or voice.get("engine") or "mock",
               "engine_url": body.get("engine_url") or voice.get("engine_url"),
               "ref_audio": body.get("ref_audio") or voice.get("ref_audio"),
               "uid": target_u.uid}
    for k in ("emotion", "instruct", "rate"):
        v = body.get(k, voice.get(k))
        if v is not None:
            payload[k] = v
    return payload, spk


@router.post("/projects/{pid}/mode-b/tts-task")
def create_tts_task(pid: str, body: dict, db: Session = Depends(get_db)):
    """创建单句TTS测试任务（GPU节点claim执行）。
    body: {uid?: 指定句(默认第一句), engine?: cosyvoice_api|fish_api|mock,
           engine_url?: 节点本机引擎地址, ref_audio?: 节点侧参考音频路径,
           speaker_id|speaker?: 角色（触发G8音色分配）,
           emotion?: 情绪标签, instruct?: 语气指令, rate?: 语速因子}
    流程：取该句最新英文译文 → (可选)assign_voice分配音色 → 建 pending 任务
    (gpu_required) → 节点轮询领取 → 完成后节点artifact回传wav（G6），
    output_paths 记录控制面侧产物路径 + tts_clips 落库。"""
    import datetime as _dt
    from ..db.models import PipelineTask
    from ..translate_executor import is_placeholder
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    utts = (db.query(Utterance).filter_by(project_id=pid)
              .order_by(Utterance.seq_index).all())
    uid_filter = body.get("uid")
    target_u = next((u for u in utts if u.uid == uid_filter), None) if uid_filter else (
        utts[0] if utts else None)
    if not target_u:
        raise HTTPException(400, "no utterance")
    latest = (db.query(Translation)
                .filter_by(utterance_id=target_u.id, target_lang=p.target_lang)
                .order_by(Translation.version.desc()).first())
    if not latest or is_placeholder(latest.text or ""):
        raise HTTPException(400, "target utterance has no valid translation")
    payload, _ = _tts_payload(db, p, target_u, latest, body)
    task_key = f"TTS-TEST/{target_u.uid}/{int(_dt.datetime.now().timestamp())}"
    t = PipelineTask(
        project_id=pid, task_key=task_key, task_type="tts-generate",
        resource="gpu", gpu_required=True, weight=5, depends_on=[],
        input_hash=(f"tts-test:{target_u.uid}:{latest.version}:"
                    f"{payload['engine']}:{payload.get('instruct') or ''}"),
        status="pending",
        output_paths={"payload": payload})
    db.add(t)
    db.commit()
    return {"ok": True, "task_id": t.id, "task_key": task_key,
            "text": latest.text[:80], "voice": {k: v for k, v in payload.items()
                                                if k in ("engine", "engine_url",
                                                         "ref_audio", "emotion",
                                                         "instruct", "rate")},
            "note": "节点启动 gpunode/entrypoint.py 后自动领取执行；"
                    "产物经 /api/nodes/tasks/{id}/artifact 回传"}


@router.post("/projects/{pid}/mode-b/tts-batch")
def create_tts_batch(pid: str, body: dict, db: Session = Depends(get_db)):
    """批量创建单句TTS任务（白月光整套配音测试主通道）。
    body: {scene?: 'SC01'（缺省全项目）, limit?: 最多创建N句,
           engine/engine_url/ref_audio/emotion/instruct/rate?: 同tts-task}
    幂等：input_hash = uid+译文版本+engine+instruct，已有同hash任务
    （pending/running/completed）跳过——重跑/补跑安全。"""
    from ..db.models import PipelineTask
    from ..translate_executor import is_placeholder
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    utts = (db.query(Utterance).filter_by(project_id=pid)
              .order_by(Utterance.seq_index).all())
    scene = body.get("scene")
    if scene:
        utts = [u for u in utts if (u.uid or "").startswith(f"{scene}-")]
    limit = int(body.get("limit") or 0)
    created = skipped = no_translation = 0
    import datetime as _dt
    ts = int(_dt.datetime.now().timestamp())
    for n, u in enumerate(utts):
        if limit and created >= limit:
            break
        latest = (db.query(Translation)
                    .filter_by(utterance_id=u.id, target_lang=p.target_lang)
                    .order_by(Translation.version.desc()).first())
        if not latest or is_placeholder(latest.text or ""):
            no_translation += 1
            continue
        payload, _ = _tts_payload(db, p, u, latest, body)
        ih = (f"tts:{u.uid}:{latest.version}:{payload['engine']}:"
              f"{payload.get('instruct') or ''}:{payload.get('ref_audio') or ''}")
        dup = (db.query(PipelineTask)
                 .filter(PipelineTask.project_id == pid,
                         PipelineTask.input_hash == ih,
                         PipelineTask.status != "dead").first())
        if dup:
            skipped += 1
            continue
        db.add(PipelineTask(
            project_id=pid, task_key=f"TTS-B/{u.uid}/{ts + n}",
            task_type="tts-generate", resource="gpu", gpu_required=True,
            weight=1, depends_on=[], input_hash=ih, status="pending",
            output_paths={"payload": payload}))
        created += 1
    db.commit()
    return {"ok": True, "created": created, "skipped": skipped,
            "no_translation": no_translation, "scene": scene or "ALL"}


@router.post("/projects/{pid}/mode-b/package-from-clips")
def package_from_clips(pid: str, body: dict, db: Session = Depends(get_db)):
    """B6真TTS交付包：从 tts_clips（节点artifact回传产物）构建。
    字幕=原slot时间码；音频 fit（atempo≤1.3，仍超标记over_window）；
    无产物句进missing。返回zip+下载路径（复用mode-b/download）。"""
    import os as _os
    from ..db.models import PipelineTask, TtsClip
    from ..mode_b import build_package_from_clips, fit_clip
    from ..translate_executor import is_placeholder
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    storage = _os.environ.get("MODE_B_STORAGE", "/tmp/peiyin-mode-b")
    work = _os.path.join(storage, pid[:8])
    out_dir = _os.path.join(work, "package")
    _os.makedirs(out_dir, exist_ok=True)
    utts = (db.query(Utterance).filter_by(project_id=pid)
              .order_by(Utterance.seq_index).all())
    latest: dict[str, Translation] = {}
    for t in (db.query(Translation).filter_by(target_lang=p.target_lang)
                 .order_by(Translation.version).all()):
        if is_placeholder(t.text or ""):
            continue
        latest[t.utterance_id] = t
    clips = {c.utterance_id: c for c in
             (db.query(TtsClip).join(Utterance, TtsClip.utterance_id == Utterance.id)
                .filter(Utterance.project_id == pid, TtsClip.status == "completed")
                .order_by(TtsClip.version.desc()).all())}
    fit_dir = _os.path.join(work, "fit")
    rows = []
    for i, u in enumerate(utts, 1):
        tr = latest.get(u.id)
        clip = clips.get(u.id)
        row = {"uid": u.uid, "seq": i, "start_ms": u.start_ms, "end_ms": u.end_ms,
               "text": tr.text if tr else "",
               "over_limit": bool(tr.is_over_limit) if tr else False,
               "audio_path": None, "final_ms": None, "speed": 1.0, "engine": ""}
        if clip and clip.audio_r2_key and _os.path.exists(clip.audio_r2_key):
            window = (u.end_ms or 0) - (u.start_ms or 0)
            fitted = fit_clip(clip.audio_r2_key, fit_dir, u.uid, window)
            row.update(audio_path=fitted["path"], final_ms=fitted["final_ms"],
                       speed=fitted["speed"], engine=clip.tts_engine,
                       over_window=fitted["over_window"])
        rows.append(row)
    if not any(r["audio_path"] for r in rows):
        raise HTTPException(400, "无已回传的TTS产物（先跑tts-batch并等节点回传）")
    zip_path = build_package_from_clips(
        {"id": pid, "name": p.name, "target_lang": p.target_lang},
        rows, out_dir)
    n_clips = sum(1 for r in rows if r["audio_path"])
    return {"ok": True, "zip": zip_path, "clips": n_clips,
            "missing": len(rows) - n_clips,
            "over_window": sum(1 for r in rows if r.get("over_window")),
            "download_url": f"/api/projects/{pid}/mode-b/download"}


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
