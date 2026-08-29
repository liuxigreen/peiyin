"""给开发环境种入演示数据：.venv/bin/python scripts/seed_demo.py"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from app.db.session import SessionLocal, init_db
from app.db import models as m

init_db()
db = SessionLocal()
if db.query(m.Project).count() > 0:
    print("已有数据，跳过种子")
    sys.exit(0)

p = m.Project(name="霸道总裁的替嫁新娘", target_lang="en", status="tts_generating",
              duration_ms=5_460_000)
db.add(p)
db.commit()

for i in range(12):
    db.add(m.Segment(project_id=p.id, seg_index=i,
                     start_ms=i * 210_000, end_ms=(i + 1) * 210_000 - 3_000,
                     cut_type="silence" if i % 3 else "scene",
                     status="done" if i < 7 else ("running" if i == 7 else "pending")))

for lbl, role, cnt in [("SPK_01", "男主·霍云琛", 412), ("SPK_02", "女主·苏念念", 398),
                       ("SPK_03", "反派·顾曼", 156), ("SPK_04", "旁白", 203)]:
    db.add(m.Speaker(project_id=p.id, label=lbl, role_name=role,
                     utterance_count=cnt, is_primary=lbl == "SPK_01"))

db.add_all([
    m.VoiceAsset(name="霸总·低沉压迫感", tags=["男频", "总裁"], ref_audio_r2_key="assets/v1.wav"),
    m.VoiceAsset(name="甜妹·元气少女", tags=["女频", "女主"], ref_audio_r2_key="assets/v2.wav"),
    m.VoiceAsset(name="纪录片旁白·沉稳男声", tags=["旁白"], ref_audio_r2_key="assets/v3.wav"),
])
db.add(m.GlossaryTerm(series_name="替嫁新娘系列", source_term="苏念念",
                      target_lang="en", target_term="Su Niannian"))
db.add(m.TranslationProvider(name="DeepSeek 主力", provider_type="custom_openai_compatible",
                             api_base_url="https://api.deepseek.com/v1",
                             model_name="deepseek-chat", is_default=True))
db.commit()
print(f"种子完成: 项目'{p.name}' + 12切片 + 4角色 + 3音色")
