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
    u = db.query(m.Utterance).filter_by(uid=uid, project_id=pid).first()
    if not u: raise HTTPException(404)
    lang = body.get("lang", "en")
    t = (db.query(m.Translation)
           .filter_by(utterance_id=u.id, target_lang=lang, version=1).first())
    if not t:
        t = m.Translation(utterance_id=u.id, target_lang=lang, version=1); db.add(t)
    t.text = body["text"]
    # 单句重算钩子：input_hash失效→下游TTS任务重排（DESIGN.md §7增量规则）
    task = (db.query(m.PipelineTask)
              .filter_by(segment_id=u.segment_id, task_type="tts_generate").first())
    if task and task.status == "completed":
        task.status = "pending"; task.input_hash = None; task.claimed_by = None
    db.commit()
    return {"ok": True, "retts_triggered": bool(task)}
