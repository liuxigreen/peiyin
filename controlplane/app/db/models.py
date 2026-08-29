"""SQLAlchemy 2.0 可移植模型：开发=sqlite:///dev.db，生产=DATABASE_URL换Postgres。
权威生产DDL仍是 app/db/schema.sql（迁移上线时以alembic对齐）。"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (String, Integer, Float, Boolean, DateTime, Text, JSON,
                        ForeignKey, UniqueConstraint, Index, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass

def uid() -> str: return uuid.uuid4().hex

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    source_lang: Mapped[str] = mapped_column(String(10), default="zh")
    target_lang: Mapped[str] = mapped_column(String(10), default="en")
    status: Mapped[str] = mapped_column(String(30), default="created")
    source_r2_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_segments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_utterances: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

class Segment(Base):
    __tablename__ = "segments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    seg_index: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    overlap_ms: Mapped[int] = mapped_column(Integer, default=500)
    cut_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    __table_args__ = (UniqueConstraint("project_id", "seg_index"),)

class Speaker(Base):
    __tablename__ = "speakers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(50))
    role_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    ref_audio_pool: Mapped[list] = mapped_column(JSON, default=list)
    utterance_count: Mapped[int] = mapped_column(Integer, default=0)

class Utterance(Base):
    __tablename__ = "utterances"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"))
    uid: Mapped[str] = mapped_column(String(20))
    seq_index: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    original_text: Mapped[str] = mapped_column(Text)
    asr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    asr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    merged_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    merged_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    speaker_id: Mapped[str | None] = mapped_column(ForeignKey("speakers.id"), nullable=True)
    emotion_label: Mapped[str] = mapped_column(String(30), default="neutral")
    speaking_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

class Translation(Base):
    __tablename__ = "translations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    utterance_id: Mapped[str] = mapped_column(ForeignKey("utterances.id", ondelete="CASCADE"))
    target_lang: Mapped[str] = mapped_column(String(10))
    version: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text)
    syllable_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    syllable_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_over_limit: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("utterance_id", "target_lang", "version"),)

class TtsClip(Base):
    __tablename__ = "tts_clips"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    utterance_id: Mapped[str] = mapped_column(ForeignKey("utterances.id", ondelete="CASCADE"))
    target_lang: Mapped[str] = mapped_column(String(10))
    translation_id: Mapped[str] = mapped_column(ForeignKey("translations.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    audio_r2_key: Mapped[str] = mapped_column(String(500))
    duration_ms: Mapped[int] = mapped_column(Integer)
    tts_engine: Mapped[str] = mapped_column(String(50))
    model_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prosody_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_time_stretched: Mapped[bool] = mapped_column(Boolean, default=False)
    utmos_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")

class PipelineTask(Base):
    __tablename__ = "pipeline_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    segment_id: Mapped[str | None] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"), nullable=True)
    task_key: Mapped[str] = mapped_column(String(80), default="")   # 稳定键: 'T030'/'S03/T130'
    resource: Mapped[str] = mapped_column(String(8), default="cpu") # gpu/cpu/io
    task_type: Mapped[str] = mapped_column(String(50))
    target_lang: Mapped[str | None] = mapped_column(String(10), nullable=True)
    gpu_required: Mapped[bool] = mapped_column(Boolean, default=False)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    weight: Mapped[int] = mapped_column(Integer, default=50)        # 进度权重
    depends_on: Mapped[list] = mapped_column(JSON, default=list)    # task_key数组
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_paths: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    claimed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (
        Index("idx_tasks_cache", "input_hash"),
        Index("idx_tasks_ready", "status", "priority"),
        Index("uq_tasks_key", "project_id", "task_key", unique=True),
    )

class GpuNode(Base):
    __tablename__ = "gpu_nodes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    gpu_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vram_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    online: Mapped[bool] = mapped_column(Boolean, default=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

class TranslationProvider(Base):
    __tablename__ = "translation_providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(64))
    provider_type: Mapped[str] = mapped_column(String(32))
    api_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    monthly_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)

class VoiceAsset(Base):
    __tablename__ = "voice_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    ref_audio_r2_key: Mapped[str] = mapped_column(String(500))
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    tts_params: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class GlossaryTerm(Base):
    __tablename__ = "glossary_terms"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    series_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_term: Mapped[str] = mapped_column(String(200))
    target_lang: Mapped[str] = mapped_column(String(10))
    target_term: Mapped[str] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("series_name", "source_term", "target_lang"),)

class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    target_lang: Mapped[str] = mapped_column(String(10))
    drama_genre: Mapped[str | None] = mapped_column(String(50), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    effect_score: Mapped[float | None] = mapped_column(Float, nullable=True)
