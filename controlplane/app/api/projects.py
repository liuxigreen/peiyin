"""项目/切片/台词/翻译 CRUD"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from ..db.session import get_db
from ..db import models as m

router = APIRouter(prefix="/api/projects", tags=["projects"])

@router.post("")
def create_project(body: dict, db: Session = Depends(get_db)):
    proj = m.Project(name=body["name"],
                     target_lang=body.get("target_lang", "en"),
                     status="created",
                     config={"filename": body.get("filename", ""),
                             "mode": body.get("mode", "")})
    db.add(proj); db.commit()
    return {"id": proj.id, "name": proj.name, "status": proj.status}

@router.get("")
def list_projects(db: Session = Depends(get_db)):
    rows = db.query(m.Project).order_by(m.Project.created_at.desc()).all()
    return [{"id": r.id, "name": r.name, "status": r.status,
             "target_lang": r.target_lang,
             "total_segments": r.total_segments or 0,
             "created_at": str(r.created_at)} for r in rows]

@router.get("/{pid}")
def project_detail(pid: str, db: Session = Depends(get_db)):
    p = db.get(m.Project, pid)
    if not p: raise HTTPException(404)
    segs = db.query(m.Segment).filter_by(project_id=pid).order_by(m.Segment.seg_index).all()
    spks = db.query(m.Speaker).filter_by(project_id=pid).all()
    tasks = db.query(m.PipelineTask).filter_by(project_id=pid).count()
    return {"id": p.id, "name": p.name, "status": p.status,
            "target_lang": p.target_lang, "total_tasks": tasks,
            "segments": [{"seg_id": s.id[:8], "index": s.seg_index,
                          "range": f"{s.start_ms//1000}-{s.end_ms//1000}s",
                          "status": s.status} for s in segs],
            "speakers": [{"id": s.id, "label": s.label, "role_name": s.role_name,
                          "utts": s.utterance_count} for s in spks]}

@router.patch("/{pid}/speakers/{sid}")
def rename_speaker(pid: str, sid: str, body: dict, db: Session = Depends(get_db)):
    spk = db.get(m.Speaker, sid)
    if not spk: raise HTTPException(404)
    if "role_name" in body: spk.role_name = body["role_name"]
    db.commit()
    return {"ok": True}

@router.get("/{pid}/utterances")
def utterances(pid: str, lang: str = "en", db: Session = Depends(get_db)):
    utts = (db.query(m.Utterance).filter_by(project_id=pid)
              .order_by(m.Utterance.seq_index).limit(2000).all())
    # 每句取最新version译文（单句重翻version+1后Web显示新译文）
    from ..translate_executor import is_placeholder
    latest: dict[str, m.Translation] = {}
    for t in (db.query(m.Translation).filter_by(target_lang=lang)
                 .order_by(m.Translation.version).all()):
        if is_placeholder(t.text or ""):
            continue                     # 历史bug占位行视同不存在（隔离句显示未翻译）
        latest[t.utterance_id] = t
    trs = latest
    spk_names = {s.id: (s.role_name or s.label) for s in
                 db.query(m.Speaker).filter_by(project_id=pid).all()}
    out = []
    for u in utts:
        t = trs.get(u.id)
        out.append({"uid": u.uid, "speaker": spk_names.get(u.speaker_id, "-"),
                    "original": u.original_text,
                    "asr": u.asr_text, "ocr": u.ocr_text,
                    "translated": t.text if t else "",
                    "ratio": round(t.syllable_ratio or 0, 2) if t else 0,
                    "over_limit": t.is_over_limit if t else False,
                    "version": t.version if t else 0,
                    "conf": u.merged_confidence or 0,
                    "conflict": (u.merged_confidence or 1) < 0.7})
    return out

@router.put("/{pid}/utterances/{uid}/translation")
def save_translation(pid: str, uid: str, body: dict, db: Session = Depends(get_db)):
    """单句热修：人工译文写新版本(version+1)保留历史（llm_model=human, is_approved），
    同时把该句所属切片的 tts-generate 任务打回pending重算（增量规则）。
    原实现两处断链：①原地改version=1行，历史版本丢失；
    ②查询task_type='tts_generate'（下划线）与实际'tts-generate'永不匹配，
    重TTS钩子从未触发过。两处均修复。"""
    u = db.query(m.Utterance).filter_by(uid=uid, project_id=pid).first()
    if not u: raise HTTPException(404)
    p = db.get(m.Project, pid)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    lang = body.get("lang") or (p.target_lang if p else "en")
    from ..translate_executor import count_syllables, syllable_ratio
    last = (db.query(m.Translation)
              .filter_by(utterance_id=u.id, target_lang=lang)
              .order_by(m.Translation.version.desc()).first())
    limit = ((p.config or {}).get("syllable_limit", 1.15) if p else 1.15)
    ratio = syllable_ratio(text, u.merged_text or u.original_text or "", lang)
    t = m.Translation(utterance_id=u.id, target_lang=lang,
                      version=(last.version + 1) if last else 1, text=text,
                      syllable_count=count_syllables(text, lang),
                      syllable_ratio=ratio, is_over_limit=ratio > limit,
                      llm_model="human", is_approved=True,
                      prompt_version=last.prompt_version if last else None)
    db.add(t)
    task = (db.query(m.PipelineTask)
              .filter_by(project_id=pid, segment_id=u.segment_id,
                         task_type="tts-generate").first())
    retts = False
    if task and task.status in ("completed", "failed", "dead"):
        task.status = "pending"; task.input_hash = None; task.claimed_by = None
        retts = True
    db.commit()
    return {"ok": True, "version": t.version, "ratio": round(ratio, 3),
            "retts_triggered": retts}
