"""资产中心：音色库/术语库/Prompt模板"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..db.session import get_db
from ..db import models as m

router = APIRouter(prefix="/api/assets", tags=["assets"])

# ---- 音色 ----
@router.get("/voices")
def list_voices(db: Session = Depends(get_db)):
    return [{"id": v.id, "name": v.name, "tags": v.tags,
             "use_count": v.use_count} for v in db.query(m.VoiceAsset).all()]

@router.post("/voices")
def create_voice(body: dict, db: Session = Depends(get_db)):
    v = m.VoiceAsset(name=body["name"], tags=body.get("tags", []),
                     ref_audio_r2_key=body.get("ref_audio_r2_key", ""))
    db.add(v); db.commit(); return {"id": v.id}

# ---- 术语 ----
@router.get("/glossary")
def list_terms(db: Session = Depends(get_db)):
    return [{"series": g.series_name, "source": g.source_term,
             "target_lang": g.target_lang, "target": g.target_term}
            for g in db.query(m.GlossaryTerm).all()]

@router.post("/glossary")
def upsert_term(body: dict, db: Session = Depends(get_db)):
    g = db.query(m.GlossaryTerm).filter_by(
        series_name=body.get("series"), source_term=body["source_term"],
        target_lang=body.get("target_lang", "en")).first()
    if g: g.target_term = body["target_term"]
    else:
        g = m.GlossaryTerm(series_name=body.get("series"),
                           source_term=body["source_term"],
                           target_lang=body.get("target_lang", "en"),
                           target_term=body["target_term"])
        db.add(g)
    db.commit(); return {"ok": True}

# ---- Prompt模板 ----
@router.get("/prompts")
def list_prompts(db: Session = Depends(get_db)):
    return [{"id": p.id, "name": p.name, "lang": p.target_lang,
             "version": p.version, "is_default": p.is_default,
             "score": p.effect_score} for p in db.query(m.PromptTemplate).all()]

@router.post("/prompts")
def create_prompt(body: dict, db: Session = Depends(get_db)):
    p = m.PromptTemplate(name=body["name"], target_lang=body.get("target_lang", "en"),
                         content=body["content"], drama_genre=body.get("genre"))
    db.add(p); db.commit(); return {"id": p.id}
