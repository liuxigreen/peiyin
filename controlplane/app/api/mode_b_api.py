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
    # 上传翻译直连：已有非占位译文的场景跳过LLM（不烧网关配额）
    from ..translate_executor import is_placeholder as _is_ph2
    pre_latest = {}
    for t in (db.query(Translation).filter_by(target_lang=p.target_lang)
              .order_by(Translation.version).all()):
        if not _is_ph2(t.text or ""):
            pre_latest[t.utterance_id] = t
    tr_results = []
    for sc in scenes:
        sc_utts = [u for u in utts if (u.uid or "").startswith(sc + "-")]
        if sc_utts and all(u.id in pre_latest for u in sc_utts):
            tr_results.append({"scene": sc, "skipped": "already_translated"})
            continue
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
    音色解析优先级：body显式参数 > 台词绑定speaker的角色音色分配(G8) > 默认mock。
    云端预置音色文件以base64内嵌下发（节点首次落地workdir/refs缓存后复用）；
    无参考音时C0的timbre描述作instruct（CosyVoice instruct模式仍可改声线）。"""
    import hashlib as _h
    import os as _os
    import re as _re
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
    elif getattr(target_u, "speaker_id", None):
        spk = db.get(Speaker, target_u.speaker_id)       # 台词绑定产物
    voice = assign_voice(db, p, spk) if spk else {}
    clean = _re.sub(r"<[^>]+>", "", latest.text or "").strip()   # 剥HTML标签(白月光实测：<b>残留进引擎)
    payload = {"text": clean or latest.text, "lang": p.target_lang,
               "engine": body.get("engine") or voice.get("engine") or "mock",
               "engine_url": body.get("engine_url") or voice.get("engine_url"),
               "uid": target_u.uid}
    ref = body.get("ref_audio") or voice.get("ref_audio")
    if ref:
        if _os.path.exists(ref):            # 云端音色文件→按内容哈希给ID，节点经HTTP拉取+缓存
            _data = open(ref, "rb").read()
            vid = "v" + _h.md5(_data).hexdigest()[:10]
            payload["voice_id"] = vid
            payload["voice_url"] = f"/api/nodes/voices/{vid}.wav"
        else:                                # 节点侧路径原样下发
            payload["ref_audio"] = ref
    else:
        payload["ref_audio"] = None
        pool = (spk.ref_audio_pool or [{}])[0] if spk else {}
        timbre = (pool or {}).get("timbre") if isinstance(pool, dict) else ""
        if timbre:
            payload["instruct"] = f"用{timbre}的音色说"
    for k in ("emotion", "instruct", "rate"):
        v = body.get(k, voice.get(k))
        if v is not None and k not in payload:
            payload[k] = v
    # 情绪→语气指令（台词级 emotion_label 由绑定/人工标记）：
    # 无instruct且有情绪标注时，转成CosyVoice instruct_text
    if payload.get("emotion") and not payload.get("instruct"):
        zh = _EMOTION_ZH.get(payload["emotion"], payload["emotion"])
        payload["instruct"] = f"用{zh}的语气说这句话"
    return payload, spk


_EMOTION_ZH = {"angry": "愤怒", "sad": "悲伤低落", "happy": "开心喜悦",
               "excited": "兴奋激动", "fearful": "恐惧颤抖", "whisper": "压低声音耳语",
               "cold": "冷漠疏离", "desperate": "绝望哀求", "tender": "温柔",
               "哭腔": "带着哭腔", "暴怒": "暴怒咆哮", "震惊": "震惊难以置信",
               "慌张": "慌张失措", "谄媚": "谄媚讨好", "悲愤": "悲愤交加",
               "讽刺": "阴阳怪气地讽刺", "冷笑": "冷笑", "复杂": "情绪复杂",
               "坚定": "坚定有力"}


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
    from ..translate_executor import is_placeholder, is_marker
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    utts = (db.query(Utterance).filter_by(project_id=pid)
              .order_by(Utterance.seq_index).all())
    scene = body.get("scene")
    if scene:
        utts = [u for u in utts if (u.uid or "").startswith(f"{scene}-")]
    if body.get("uids"):
        want = set(body["uids"])
        utts = [u for u in utts if u.uid in want]
    limit = int(body.get("limit") or 0)
    created = skipped = no_translation = markers = 0
    import datetime as _dt
    import uuid as _uuid
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
        if is_marker(latest.text) or is_marker(u.merged_text or u.original_text):
            markers += 1
            continue
        emo = (getattr(u, "emotion_label", "") or "").strip()
        if emo and emo != "neutral":
            # 台词级情绪须在_tts_payload内转换成instruct（节点只认instruct），
            # 传入body让转换链生效；payload里仍保留emotion标签
            body = {**body, "emotion": emo}
        payload, _ = _tts_payload(db, p, u, latest, body)
        if emo and emo != "neutral" and "emotion" not in payload:
            payload["emotion"] = emo
        ih = (f"tts:{u.uid}:{latest.version}:{payload['engine']}:"
              f"{payload.get('instruct') or ''}:{payload.get('ref_audio') or ''}:"
              f"{payload.get('voice_id') or ''}:"
              f"{payload.get('rate') or ''}:{payload.get('emotion') or ''}:{emo or ''}")
        dup = (db.query(PipelineTask)
                 .filter(PipelineTask.project_id == pid,
                         PipelineTask.input_hash == ih,
                         PipelineTask.status != "dead").first())
        if dup:
            skipped += 1
            continue
        db.add(PipelineTask(
            project_id=pid, task_key=f"TTS-B/{u.uid}/{ts}{_uuid.uuid4().hex[:4]}",
            task_type="tts-generate", resource="gpu", gpu_required=True,
            weight=1, depends_on=[], input_hash=ih, status="pending",
            output_paths={"payload": payload}))
        created += 1
    db.commit()
    return {"ok": True, "created": created, "skipped": skipped,
            "no_translation": no_translation, "markers_skipped": markers,
            "scene": scene or "ALL"}


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
    clips = {}
    for c in (db.query(TtsClip).join(Utterance, TtsClip.utterance_id == Utterance.id)
              .filter(Utterance.project_id == pid, TtsClip.status == "completed")
              .order_by(TtsClip.version.desc()).all()):
        clips.setdefault(c.utterance_id, c)   # 首见=最高version，重掷新clip优先
    fit_dir = _os.path.join(work, "fit")
    import re as _re
    from ..translate_executor import is_marker
    rows = []
    n_markers = 0
    for i, u in enumerate(utts, 1):
        if is_marker(u.merged_text or u.original_text):
            n_markers += 1
            continue                      # 场景标记行不进字幕/配音/manifest
        tr = latest.get(u.id)
        clip = clips.get(u.id)
        _txt = _re.sub(r"<[^>]+>", "", tr.text or "").strip() if tr else ""
        breath = bool(rows) and any(r.get("audio_path") for r in rows) and             u.speaker_id and rows[-1].get("speaker_id") and             u.speaker_id != rows[-1].get("speaker_id")
        row = {"uid": u.uid, "seq": i, "start_ms": u.start_ms, "end_ms": u.end_ms,
               "text": _txt, "speaker_id": u.speaker_id, "breath": breath,
               "over_limit": bool(tr.is_over_limit) if tr else False,
               "audio_path": None, "final_ms": None, "speed": 1.0, "engine": ""}
        if clip and clip.audio_r2_key and _os.path.exists(clip.audio_r2_key):
            window = (u.end_ms or 0) - (u.start_ms or 0)
            tts_rate = float(clip.prosody_rate or 1.0)
            fitted = fit_clip(clip.audio_r2_key, fit_dir, u.uid, window,
                              tts_rate=tts_rate)
            row.update(audio_path=fitted["path"], final_ms=fitted["final_ms"],
                       speed=fitted["speed"], engine=clip.tts_engine,
                       tts_rate=tts_rate, over_window=fitted["over_window"])
        rows.append(row)
    if not any(r["audio_path"] for r in rows):
        raise HTTPException(400, "无已回传的TTS产物（先跑tts-batch并等节点回传）")
    zip_path = build_package_from_clips(
        {"id": pid, "name": p.name, "target_lang": p.target_lang},
        rows, out_dir, post=body.get("audio_post", True),
        me_path=body.get("me_path"))
    n_clips = sum(1 for r in rows if r["audio_path"])
    return {"ok": True, "zip": zip_path, "clips": n_clips,
            "missing": len(rows) - n_clips,
            "markers_skipped": n_markers,
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


@router.post("/projects/{pid}/bind-speakers")
async def bind_speakers_ep(pid: str, body: dict = None, db: Session = Depends(get_db)):
    """LLM文本绑定：每句台词→说话角色（utterances.speaker_id）。
    声纹聚类实装前的绑定通道；幂等（已绑定跳过，force=True覆盖）。"""
    from ..cast_agent import bind_speakers
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    body = body or {}
    r = await bind_speakers(db, p, force=bool(body.get("force")))
    return {"ok": True, **r}


@router.get("/projects/{pid}/mode-b/file/{name}")
def download_work_file(pid: str, name: str):
    """下载 MODE_B_STORAGE/{pid8}/ 下的工作文件（A/B试听包等）。
    文件名白名单防目录穿越。"""
    import os as _os
    import re as _re
    from fastapi.responses import FileResponse
    if not _re.fullmatch(r"[A-Za-z0-9_.\-]{1,120}", name):
        raise HTTPException(400, "bad filename")
    storage = _os.environ.get("MODE_B_STORAGE", "/tmp/peiyin-mode-b")
    path = _os.path.join(storage, pid[:8], name)
    if not _os.path.isfile(path):
        raise HTTPException(404, "file not found")
    return FileResponse(path, filename=name)


@router.post("/projects/{pid}/mode-b/tts-requeue")
def tts_requeue(pid: str, body: dict, db: Session = Depends(get_db)):
    """把已完成但无artifact回传的tts任务打回pending（节点更新回传代码后调用，
    补传产物）。body: {scene?: 'SC01' 只处理该场景}。已回传过artifacts的跳过。"""
    from ..db.models import PipelineTask
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    statuses = ["completed", "failed"]
    if body.get("include_dead"):
        statuses.append("dead")       # dead默认跳过：冻结队列/永久失败由tts-batch复活
    q = (db.query(PipelineTask)
           .filter(PipelineTask.project_id == pid,
                   PipelineTask.task_type == "tts-generate",
                   PipelineTask.status.in_(statuses)))
    if body.get("scene"):
        q = q.filter(PipelineTask.task_key.like(f"TTS-B/{body['scene']}-%"))
    n = 0
    for t in q.all():
        # 旧代码完成的任务 output_paths 可能是列表（outputs直存），统一按dict读
        outs = t.output_paths if isinstance(t.output_paths, dict) else {}
        if outs.get("artifacts"):
            continue
        t.status = "pending"; t.claimed_by = None; t.lease_until = None
        n += 1
    db.commit()
    return {"ok": True, "requeued": n}




@router.post("/projects/{pid}/mode-b/prosody-qc")
def prosody_qc_pass(pid: str, body: dict, db: Session = Depends(get_db)):
    """B7韵律质检门：F0半音std/能量std双指标，平坦句自动重掷
    （instruct加强等效temperature）。耳语/哭腔跳过F0门。
    metrics存tts_clips.utmos_score（复用列：现为f0_st_std）。"""
    import os as _os
    from ..db.models import PipelineTask, TtsClip
    import uuid as _uuid
    from ..prosody_qc import evaluate_wav, boosted_instruct, SKIP_EMOTIONS
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    rows = (db.query(TtsClip, Utterance)
              .join(Utterance, TtsClip.utterance_id == Utterance.id)
              .filter(Utterance.project_id == pid,
                      TtsClip.status == "completed").all())
    checked = flat = requeued = 0
    details = []
    done_tasks = {t.task_key: t for t in
                  db.query(PipelineTask)
                    .filter(PipelineTask.project_id == pid,
                            PipelineTask.task_type == "tts-generate").all()}
    payload_by_uid = {}
    for t in done_tasks.values():
        pl = (t.output_paths or {}).get("payload") or {}
        if pl.get("uid"):
            payload_by_uid[pl["uid"]] = pl
    for clip, u in rows:
        if not clip.audio_r2_key or not _os.path.exists(clip.audio_r2_key):
            continue
        m = evaluate_wav(clip.audio_r2_key)
        checked += 1
        clip.utmos_score = m.get("f0_st_std")
        emo = (getattr(u, "emotion_label", "") or "").strip()
        is_flat = bool(m.get("flat")) and emo not in SKIP_EMOTIONS
        details.append({"uid": u.uid, "f0_st_std": m.get("f0_st_std"),
                        "int_std": m.get("int_std"), "flat": is_flat,
                        "emotion": emo})
        if not is_flat:
            continue
        payload = payload_by_uid.get(u.uid)
        if not payload:
            continue
        new_pl = dict(payload)
        new_pl["instruct"] = boosted_instruct(payload.get("instruct"), emo)
        ih = ("tts:{uid}:{ver}:{eng}:{ins}:{ref}:{vid}:{rate}:{emo}:reroll"
              .format(uid=u.uid, ver=clip.version, eng=new_pl["engine"],
                      ins=new_pl.get("instruct") or "",
                      ref=new_pl.get("ref_audio") or "",
                      vid=new_pl.get("voice_id") or "",
                      rate=new_pl.get("rate") or "",
                      emo=new_pl.get("emotion") or ""))
        dup = (db.query(PipelineTask)
                 .filter(PipelineTask.input_hash == ih,
                         PipelineTask.status != "dead").first())
        if dup:
            continue
        db.add(PipelineTask(
            project_id=pid,
            task_key="TTS-B/{}/{}".format(u.uid, _uuid.uuid4().hex[:4]),
            task_type="tts-generate", resource="gpu", gpu_required=True,
            weight=1, depends_on=[], input_hash=ih, status="pending",
            output_paths={"payload": new_pl}))
        requeued += 1
    db.commit()
    return {"ok": True, "checked": checked, "flat": flat,
            "requeued": requeued, "details": details[:50]}


@router.post("/projects/{pid}/mode-b/retranslate-overlimit")
async def retranslate_overlimit(pid: str, body: dict,
                                db: Session = Depends(get_db)):
    """B7音节预算闭环：is_over_limit的最新译文自动压缩重译（version+1）。
    压缩prompt约束目标音节预算与口语化短语，LLM走默认provider链。"""
    import uuid as _uuidmod
    from ..db.models import Translation as Tr
    from ..translate_executor import (chat, load_default_provider,
                                      is_placeholder)
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    utts = {u.id: u for u in db.query(Utterance)
            .filter_by(project_id=pid).all()}
    latest = {}
    for t in (db.query(Tr).filter_by(target_lang=p.target_lang)
              .order_by(Tr.version).all()):
        latest[t.utterance_id] = t
    cfg = load_default_provider(db)
    fixed = failed = 0
    for uid, t in latest.items():
        if not t.is_over_limit or is_placeholder(t.text or ""):
            continue
        u = utts.get(uid)
        if not u:
            continue
        window_s = max(((u.end_ms or 0) - (u.start_ms or 0)) / 1000, 0.5)
        budget = max(int(window_s * 3.2), 4)   # ~3.2音节/秒英文可用预算
        system = ("You compress English dubbing lines. Keep the meaning and "
                  "the melodramatic tone. Output ONLY the compressed line. "
                  "Hard limit: at most {} syllables.".format(budget))
        user = "Original: {}\nCompress to at most {} syllables.".format(
            t.text, budget)
        try:
            out = (await chat(cfg, system, user)).strip()
        except Exception:                                    # noqa: BLE001
            failed += 1
            continue
        if not out or is_placeholder(out):
            failed += 1
            continue
        db.add(Tr(id=_uuidmod.uuid4().hex, utterance_id=uid,
                  target_lang=p.target_lang, version=t.version + 1,
                  text=out, llm_model=(cfg.get("model") or "") + "-compress",
                  is_over_limit=None))
        fixed += 1
    db.commit()
    return {"ok": True, "fixed": fixed, "failed": failed}


@router.post("/projects/{pid}/seed-translation")
def seed_translation(pid: str, body: dict, db: Session = Depends(get_db)):
    """B8：已有翻译直连配音。上传翻译字幕（双语SRT或纯译文SRT），
    按序号对齐utterances落Translation，跳过API翻译。
    块内多行自动挑拉丁字母占比最高的一行当译文；纯中文行跳过。"""
    from .translate import _parse_srt
    from ..db.models import Translation as Tr
    from ..translate_executor import is_placeholder
    import uuid as _umod
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    srt = body.get("srt") or ""
    lang = body.get("lang") or p.target_lang
    entries = _parse_srt(srt)
    if not entries:
        raise HTTPException(400, "no subtitle entries parsed from srt")

    def _pick(text: str) -> str:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        best, best_score = "", -1.0
        for ln in lines:
            letters = sum(1 for ch in ln if ch.isascii() and ch.isalpha())
            score = letters / max(len(ln), 1)
            if score > best_score:
                best, best_score = ln, score
        return best if best_score >= 0.5 else ""

    utts = (db.query(Utterance).filter_by(project_id=pid)
              .order_by(Utterance.seq_index).all())
    matched = skipped_cjk = 0
    mismatch = abs(len(entries) - len(utts))
    existing = {t.utterance_id: t for t in
                db.query(Tr).filter_by(target_lang=lang)
                  .order_by(Tr.version).all()}
    for u, e in zip(utts, entries):
        line = _pick(e["text"])
        if not line or is_placeholder(line):
            skipped_cjk += 1
            continue
        prev = existing.get(u.id)
        ver = (prev.version + 1) if prev else 1
        db.add(Tr(id=_umod.uuid4().hex, utterance_id=u.id, target_lang=lang,
                  version=ver, text=line, llm_model="uploaded",
                  is_approved=True))
        matched += 1
    db.commit()
    return {"ok": True, "utterances": len(utts), "entries": len(entries),
            "matched": matched, "skipped_not_translated": skipped_cjk,
            "index_mismatch": mismatch,
            "note": "下次run-mode-b将跳过这些句子的API翻译"}
