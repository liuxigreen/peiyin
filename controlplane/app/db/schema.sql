-- V1 八张核心表 + 资产中心三张（DESIGN.md 第5节定稿）
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    source_lang VARCHAR(10) NOT NULL DEFAULT 'zh',
    target_lang VARCHAR(10) NOT NULL DEFAULT 'en',
    status VARCHAR(30) NOT NULL DEFAULT 'created',
    source_video_path VARCHAR(500),
    duration_ms INTEGER,
    total_segments INTEGER,
    total_utterances INTEGER,
    config JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS segments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seg_index INTEGER NOT NULL,
    start_ms BIGINT NOT NULL,
    end_ms BIGINT NOT NULL,
    overlap_ms INTEGER NOT NULL DEFAULT 500,
    cut_type VARCHAR(20),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    UNIQUE(project_id, seg_index)
);

CREATE TABLE IF NOT EXISTS speakers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    label VARCHAR(50) NOT NULL,
    role_name VARCHAR(100),
    is_primary BOOLEAN DEFAULT FALSE,
    ref_audio_pool JSONB DEFAULT '[]',
    utterance_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS utterances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    segment_id UUID NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    uid VARCHAR(20) NOT NULL,
    seq_index INTEGER NOT NULL,
    start_ms BIGINT NOT NULL,
    end_ms BIGINT NOT NULL,
    original_text TEXT NOT NULL,
    asr_text TEXT, asr_confidence FLOAT,
    ocr_text TEXT, ocr_confidence FLOAT,
    merged_text TEXT, merged_confidence FLOAT,
    speaker_id UUID REFERENCES speakers(id),
    emotion_label VARCHAR(30) DEFAULT 'neutral',
    speaking_rate FLOAT,
    char_count INTEGER
);

CREATE TABLE IF NOT EXISTS translations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    utterance_id UUID NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    target_lang VARCHAR(10) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    text TEXT NOT NULL,
    syllable_count INTEGER,
    syllable_ratio FLOAT,
    is_over_limit BOOLEAN DEFAULT FALSE,
    llm_model VARCHAR(100),
    prompt_version VARCHAR(50),
    is_approved BOOLEAN DEFAULT FALSE,
    UNIQUE(utterance_id, target_lang, version)
);

CREATE TABLE IF NOT EXISTS tts_clips (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    utterance_id UUID NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    target_lang VARCHAR(10) NOT NULL,
    translation_id UUID NOT NULL REFERENCES translations(id),
    version INTEGER NOT NULL DEFAULT 1,
    audio_path VARCHAR(500) NOT NULL,
    duration_ms INTEGER NOT NULL,
    tts_engine VARCHAR(50) NOT NULL,
    model_snapshot VARCHAR(100),
    prosody_rate FLOAT,
    is_time_stretched BOOLEAN DEFAULT FALSE,
    utmos_score FLOAT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS pipeline_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    segment_id UUID REFERENCES segments(id) ON DELETE CASCADE,
    task_type VARCHAR(50) NOT NULL,
    target_lang VARCHAR(10),
    gpu_required BOOLEAN DEFAULT FALSE,
    model_name VARCHAR(100),
    input_hash VARCHAR(64),
    output_hash VARCHAR(64),
    output_paths JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tasks_cache ON pipeline_tasks(input_hash)
    WHERE status = 'completed';
CREATE INDEX IF NOT EXISTS idx_tasks_ready ON pipeline_tasks(status, gpu_required)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS translation_providers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(64) NOT NULL,
    provider_type VARCHAR(32) NOT NULL,
    api_base_url TEXT,
    api_key_encrypted TEXT,
    api_key_masked VARCHAR(32),
    model_name VARCHAR(64),
    temperature FLOAT DEFAULT 0.7,
    max_tokens INT DEFAULT 4096,
    system_prompt TEXT DEFAULT '',
    prompt_template TEXT DEFAULT '',
    monthly_budget DECIMAL(10,2),
    is_default BOOLEAN DEFAULT FALSE,
    is_enabled BOOLEAN DEFAULT TRUE,
    priority INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 资产中心
CREATE TABLE IF NOT EXISTS voice_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    tags TEXT[] DEFAULT '{}',
    ref_audio_path VARCHAR(500) NOT NULL,
    duration_s FLOAT,
    tts_params JSONB DEFAULT '{}',
    quality_score INT,
    use_count INT DEFAULT 0,
    last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS glossary_terms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    series_name VARCHAR(200),
    source_term VARCHAR(200) NOT NULL,
    target_lang VARCHAR(10) NOT NULL,
    target_term VARCHAR(200) NOT NULL,
    note TEXT,
    UNIQUE(series_name, source_term, target_lang)
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    target_lang VARCHAR(10) NOT NULL,
    drama_genre VARCHAR(50),
    content TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    is_default BOOLEAN DEFAULT FALSE,
    effect_score FLOAT
);
