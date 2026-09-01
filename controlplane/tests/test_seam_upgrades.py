"""衔接面升级wave1测试（UPGRADE-BRIEF G1-G8）：
G2 跨场景上下文取最新版本 / G1 角色卡全维注入 / G3 句特征路由 /
G5 压缩产物过终检 / G6 节点产物回传+payload保留 / G7+G8 音色分配与payload扩展 /
单句热修 version+1 与 tts-generate 钩子类型匹配。"""
import asyncio
import importlib
import os

from fastapi.testclient import TestClient

import app.translate_executor as te

_SRT = """1
00:00:01,000 --> 00:00:03,000
总裁，夫人她离婚了！

2
00:00:04,000 --> 00:00:06,000
你怎么敢跟我说话

3
00:00:07,000 --> 00:00:09,000
我不知道他在哪里

4
00:00:10,000 --> 00:00:12,000
明天来公司见我
"""


def _client(tmp_db: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def _seed_translated(c, name: str, srt: str = _SRT, scene_size: int = 2):
    pid = c.post("/api/projects", json={"name": name, "target_lang": "en"}).json()["id"]
    c.post(f"/api/projects/{pid}/seed-srt", json={"srt": srt, "scene_size": scene_size})
    r = c.post(f"/api/projects/{pid}/run-translate").json()
    assert r["completed"] >= 1, r
    return pid


# ── G2：跨场景上下文取最新版本 ──────────────────────────────
def test_prev_scene_ctx_uses_latest_version(tmp_path):
    c = _client(str(tmp_path / "g2.db"))
    pid = _seed_translated(c, "跨场景剧")
    from app.db.models import Project, Translation, Utterance
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        p = db.get(Project, pid)
        u1 = (db.query(Utterance).filter(Utterance.project_id == pid,
                                         Utterance.uid.like("SC01-%"))
                .order_by(Utterance.seq_index).first())
        db.add(Translation(utterance_id=u1.id, target_lang="en", version=2,
                           text="PREV TAIL MARKER v2", syllable_ratio=1.0))
        db.commit()
        ctx = te._prev_scene_ctx(db, p, "SC02", "en")
        assert "PREV TAIL MARKER v2" in ctx, ctx
    finally:
        db.close()


# ── G1：角色卡全维注入（gender/age/timbre 进提示词）──────────
def test_ctx_pack_role_cards_voice_meta(tmp_path):
    c = _client(str(tmp_path / "g1.db"))
    pid = _seed_translated(c, "角色卡剧")
    from app.db.models import Project, Speaker, Utterance
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        p = db.get(Project, pid)
        db.add(Speaker(project_id=pid, label="霍云琛", role_name="Huo Yun-chen",
                       is_primary=True,
                       ref_audio_pool=[{"gender": "male", "age_band": "young",
                                        "timbre": "低沉威严"}]))
        db.commit()
        utts = db.query(Utterance).filter_by(project_id=pid).all()
        ctx = te.build_ctx_pack(db, p, utts)
        card = ctx["role_cards"][0]
        assert card["gender"] == "male" and card["age_band"] == "young", card
        assert card["timbre"] == "低沉威严" and card["is_primary"] is True, card
        s = te._card_str(card)
        assert "主角" in s and "male" in s and "音色:低沉威严" in s, s
    finally:
        db.close()


# ── G3：句特征路由 ─────────────────────────────────────────
def test_line_feats_routing():
    class _U:
        def __init__(self, t):
            self.merged_text, self.original_text = t, None

    utts = [_U("08:30"), _U("霍云琛"), _U("滚！"), _U("你竟敢这样对我")]
    feats = te._line_feats(utts, [0, 1, 2, 3], {"霍云琛"})
    assert "纯数字" in feats[1], feats
    assert "人名句" in feats[2], feats
    assert "超短句" in feats[3], feats
    assert 4 not in feats, feats
    block = te._feature_block(feats)
    assert "1→" in block and "3→" in block


# ── G5：压缩产物补过终检 ───────────────────────────────────
def test_compression_review_writes_version(tmp_path, monkeypatch):
    c = _client(str(tmp_path / "g5.db"))
    pid = _seed_translated(c, "压缩终检剧")
    from app.db.models import Project, Translation, Utterance
    from app.db.session import SessionLocal

    async def _stub(cfg, system, user):
        return "1 | Madam has filed for divorce!"

    monkeypatch.setattr(te, "chat", _stub)
    db = SessionLocal()
    try:
        p = db.get(Project, pid)
        u = (db.query(Utterance).filter_by(project_id=pid)
               .order_by(Utterance.seq_index).first())
        last = (db.query(Translation).filter_by(utterance_id=u.id, target_lang="en")
                  .order_by(Translation.version.desc()).first())
        written = {0: {"text": last.text, "ratio": last.syllable_ratio or 1.3}}
        ctx = te.build_ctx_pack(db, p, [u])
        fixed = asyncio.run(te._review_compressed(
            db, {"mode": "live", "name": "mock", "model": "mock-1"},
            te._lang_rule("en"), [u], [0], written, ctx, "en", 1.15, "mock-1"))
        assert fixed == 1, fixed
        db.commit()                     # SessionLocal autoflush=False，先落盘再查
        v2 = (db.query(Translation)
                .filter_by(utterance_id=u.id, target_lang="en")
                .order_by(Translation.version.desc()).first())
        assert v2.version == last.version + 1, (last.version, v2.version)
        assert "review" in (v2.llm_model or ""), v2.llm_model
        assert "divorce" in v2.text
    finally:
        db.close()


# ── G6：节点产物回传（complete保留payload + artifact落盘+tts_clips）──
def test_artifact_upload_and_tts_clip(tmp_path):
    os.environ["MODE_B_STORAGE"] = str(tmp_path / "storage")
    c = _client(str(tmp_path / "g6.db"))
    pid = _seed_translated(c, "回传剧")
    r = c.post(f"/api/projects/{pid}/mode-b/tts-task",
               json={"engine": "cosyvoice_api"}).json()
    assert r["ok"], r
    tid = r["task_id"]
    H = {"Authorization": "Bearer dev-node-token"}
    r = c.post(f"/api/nodes/tasks/{tid}/complete", headers=H,
               json={"outputs": [{"key": "tts", "path": "/node/out/tts_x.wav"}]})
    assert r.status_code == 200, r.text
    from app.db.models import PipelineTask, TtsClip
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        t = db.get(PipelineTask, tid)
        # complete合并outputs且payload保留（原实现列表整行赋值→qc覆盖成{"qc"}全丢）
        assert t.output_paths["payload"]["uid"], t.output_paths
        assert t.output_paths["outputs"][0]["path"] == "/node/out/tts_x.wav", \
            t.output_paths
    finally:
        db.close()

    import soundfile as sf
    import numpy as np
    wav_path = tmp_path / "seg.wav"
    sf.write(str(wav_path), (0.3 * np.sin(2 * np.pi * 440 *
             np.linspace(0, 0.5, 8000, endpoint=False))).astype("float32"), 16000)
    data = wav_path.read_bytes()
    r = c.post(f"/api/nodes/tasks/{tid}/artifact", headers=H,
               params={"filename": "seg.wav", "key": "tts"},
               content=data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact"]["bytes"] == len(data), body
    assert body["tts_clip"] and body["tts_clip"]["duration_ms"] == 500, body
    db = SessionLocal()
    try:
        t = db.get(PipelineTask, tid)
        arts = t.output_paths["artifacts"]
        assert arts[0]["path"].endswith("seg.wav"), arts
        assert os.path.exists(arts[0]["path"])
        from app.db.models import Utterance
        uid_ = t.output_paths["payload"]["uid"]
        u = db.query(Utterance).filter_by(project_id=pid, uid=uid_).first()
        clips = db.query(TtsClip).filter_by(utterance_id=u.id).all()
        assert clips, "tts_clips row missing"
        assert clips[0].status == "completed" and clips[0].duration_ms == 500
        assert clips[0].audio_r2_key.endswith("seg.wav")
    finally:
        db.close()


# ── G7+G8：音色分配 + payload扩展 ───────────────────────────
def test_tts_task_voice_assign(tmp_path):
    c = _client(str(tmp_path / "g8.db"))
    pid = _seed_translated(c, "音色分配剧")
    from app.db.models import Speaker, VoiceAsset
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        db.add(Speaker(project_id=pid, label="霍云琛", role_name="Huo",
                       is_primary=True,
                       ref_audio_pool=[{"gender": "male", "age_band": "young",
                                        "timbre": "低沉"}]))
        db.add(VoiceAsset(name="霸总", tags=["male", "young"],
                          ref_audio_r2_key="assets/bazong.wav",
                          tts_params={"engine": "cosyvoice_api", "rate": 1.05}))
        db.commit()
    finally:
        db.close()
    r = c.post(f"/api/projects/{pid}/mode-b/tts-task",
               json={"speaker": "霍云琛", "emotion": "angry"}).json()
    assert r["ok"], r
    v = r["voice"]
    assert v["engine"] == "cosyvoice_api", v
    assert v["ref_audio"] == "assets/bazong.wav", v
    assert v["rate"] == 1.05 and v["emotion"] == "angry", v
    from app.db.models import PipelineTask
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        t = db.get(PipelineTask, r["task_id"])
        pay = t.output_paths["payload"]
        assert pay["emotion"] == "angry"
        assert pay["rate"] == 1.05 and pay["engine"] == "cosyvoice_api"
        assert "cosyvoice" in t.input_hash
    finally:
        db.close()


# ── 单句热修：version+1 保留历史 + 类型匹配钩子 ─────────────
def test_save_translation_hotfix_versions(tmp_path):
    c = _client(str(tmp_path / "hf.db"))
    pid = _seed_translated(c, "热修剧", scene_size=40)
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    uid = utts[0]["uid"]
    v0 = utts[0]["version"]
    r = c.put(f"/api/projects/{pid}/utterances/{uid}/translation",
              json={"text": "Hand rewritten line one"}).json()
    assert r["ok"] and r["version"] == v0 + 1, r
    r = c.put(f"/api/projects/{pid}/utterances/{uid}/translation",
              json={"text": "Hand rewritten line two"}).json()
    assert r["version"] == v0 + 2, r
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    assert utts[0]["translated"] == "Hand rewritten line two", utts[0]
    # 历史版本仍在（原实现原地改v1，历史丢失）
    from app.db.models import Translation, Utterance
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        u = db.query(Utterance).filter_by(project_id=pid, uid=uid).first()
        vers = sorted(t.version for t in db.query(Translation)
                      .filter_by(utterance_id=u.id, target_lang="en").all())
        assert vers == list(range(1, v0 + 3)), vers
        human = (db.query(Translation)
                   .filter_by(utterance_id=u.id, llm_model="human").all())
        assert len(human) == 2 and all(t.is_approved for t in human)
    finally:
        db.close()


# ── wave2：register建档（节点名/GPU型号不再丢失）─────────────
def test_register_creates_node_row(tmp_path):
    c = _client(str(tmp_path / "w2.db"))
    r = c.post("/api/nodes/register",
               headers={"x-node-secret": "dev-node-secret"},
               json={"name": "rtx3060", "gpu_model": "RTX 3060", "vram_gb": 12,
                     "capabilities": ["tts"]})
    assert r.status_code == 200, r.text
    nodes = c.get("/api/nodes").json()
    row = [n for n in nodes if n["name"] == "rtx3060"]
    assert row and row[0]["gpu_model"] == "RTX 3060" and row[0]["online"], nodes
    # 同名重复注册：新行online，旧行置offline（不堆积）
    c.post("/api/nodes/register", headers={"x-node-secret": "dev-node-secret"},
           json={"name": "rtx3060", "gpu_model": "RTX 3060", "vram_gb": 12})
    nodes = c.get("/api/nodes").json()
    rows = [n for n in nodes if n["name"] == "rtx3060"]
    assert len(rows) == 2 and sum(1 for n in rows if n["online"]) == 1, rows


# ── wave2：tts-batch 幂等批量 ───────────────────────────────
def test_tts_batch_create_and_dedupe(tmp_path):
    c = _client(str(tmp_path / "w3.db"))
    pid = _seed_translated(c, "批量剧", scene_size=2)
    r = c.post(f"/api/projects/{pid}/mode-b/tts-batch",
               json={"scene": "SC01", "engine": "cosyvoice_api",
                     "engine_url": "http://127.0.0.1:50000/tts"}).json()
    assert r["ok"] and r["created"] == 2 and r["no_translation"] == 0, r
    # 重跑幂等：同hash全跳过
    r2 = c.post(f"/api/projects/{pid}/mode-b/tts-batch",
                json={"scene": "SC01", "engine": "cosyvoice_api",
                      "engine_url": "http://127.0.0.1:50000/tts"}).json()
    assert r2["created"] == 0 and r2["skipped"] == 2, r2
    # payload/engine 落库正确
    from app.db.models import PipelineTask
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        tsks = (db.query(PipelineTask)
                  .filter_by(project_id=pid, task_type="tts-generate").all())
        assert len(tsks) == 2 and all(
            t.output_paths["payload"]["engine"] == "cosyvoice_api" for t in tsks)
        assert all(t.task_key.startswith("TTS-B/") for t in tsks)
    finally:
        db.close()


# ── wave2：fit_clip + package-from-clips ────────────────────
def test_fit_and_package_from_clips(tmp_path):
    import numpy as np
    import soundfile as sf
    c = _client(str(tmp_path / "w4.db"))
    pid = _seed_translated(c, "交付剧", scene_size=40)
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    # 造1条已完成clip（短窗口→触发fit），其余句missing
    storage = tmp_path / "storage"
    os.environ["MODE_B_STORAGE"] = str(storage)
    wav = storage / "artifacts" / "x" / "seg.wav"
    wav.parent.mkdir(parents=True)
    sr = 16000
    sf.write(str(wav), (0.3 * np.sin(2 * np.pi * 400 *
             np.linspace(0, 4.0, sr * 4, endpoint=False))).astype("float32"), sr)
    from app.db.models import TtsClip, Translation, Utterance
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        u = (db.query(Utterance).filter_by(project_id=pid)
               .order_by(Utterance.seq_index).first())
        tr = (db.query(Translation).filter_by(utterance_id=u.id, target_lang="en")
                .order_by(Translation.version.desc()).first())
        db.add(TtsClip(utterance_id=u.id, target_lang="en", translation_id=tr.id,
                       version=tr.version, audio_r2_key=str(wav), duration_ms=4000,
                       tts_engine="cosyvoice_api", status="completed"))
        db.commit()
    finally:
        db.close()
    r = c.post(f"/api/projects/{pid}/mode-b/package-from-clips", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clips"] == 1 and body["missing"] == len(utts) - 1, body
    import zipfile
    with zipfile.ZipFile(body["zip"]) as z:
        names = z.namelist()
        assert any(n.startswith("audio/") for n in names), names
        assert "manifest.json" in names and "qc_report.json" in names, names
        assert any("subtitles.en.srt" in n for n in names), names


# ── wave2：fit_clip 单元（超窗加速 / 极端超窗标记）───────────
def test_fit_clip_stretch_and_flag(tmp_path):
    import numpy as np
    import soundfile as sf
    from app.mode_b import fit_clip
    sr = 16000
    long_wav = tmp_path / "long.wav"
    sf.write(str(long_wav), np.zeros(sr * 5, dtype="float32"), sr)   # 5s 音频
    r = fit_clip(str(long_wav), str(tmp_path / "fit"), "U1", 4000)   # 窗口4s → ratio1.25
    assert r["speed"] > 1.0 and not r["over_window"], r
    assert r["final_ms"] <= 4000 * 1.05, r
    huge = tmp_path / "huge.wav"
    sf.write(str(huge), np.zeros(sr * 10, dtype="float32"), sr)      # 10s → ratio2.5
    r2 = fit_clip(str(huge), str(tmp_path / "fit"), "U2", 4000)
    assert r2["over_window"] and r2["speed"] == 1.0, r2


# ── wave2.1：tts-requeue 补传通道 ───────────────────────────
def test_tts_requeue_missing_artifacts(tmp_path):
    c = _client(str(tmp_path / "w5.db"))
    pid = _seed_translated(c, "补传剧", scene_size=40)
    r = c.post(f"/api/projects/{pid}/mode-b/tts-task",
               json={"engine": "mock"}).json()
    tid = r["task_id"]
    H = {"Authorization": "Bearer dev-node-token"}
    assert c.post(f"/api/nodes/tasks/{tid}/complete", headers=H,
                  json={"outputs": [{"key": "tts", "path": "/node/a.wav"}]}).status_code == 200
    # 完成但无artifact → requeue打回pending
    r = c.post(f"/api/projects/{pid}/mode-b/tts-requeue", json={}).json()
    assert r["requeued"] == 1, r
    from app.db.models import PipelineTask
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        assert db.get(PipelineTask, tid).status == "pending"
    finally:
        db.close()
    # 已有artifacts的任务 → requeue跳过
    import soundfile as sf
    import numpy as np
    wav = tmp_path / "a.wav"
    sf.write(str(wav), np.zeros(1600, dtype="float32"), 16000)
    assert c.post(f"/api/nodes/tasks/{tid}/artifact", headers=H,
                  params={"filename": "a.wav"}, content=wav.read_bytes()).status_code == 200
    r = c.post(f"/api/projects/{pid}/mode-b/tts-requeue", json={}).json()
    assert r["requeued"] == 0, r


# ── wave2.2：requeue默认跳过dead（冻结队列不被误复活）─────────
def test_tts_requeue_skips_dead_by_default(tmp_path):
    c = _client(str(tmp_path / "w6.db"))
    pid = _seed_translated(c, "冻结剧", scene_size=40)
    H = {"Authorization": "Bearer dev-node-token"}
    r = c.post(f"/api/projects/{pid}/mode-b/tts-task", json={"engine": "mock"}).json()
    tid = r["task_id"]
    assert c.post(f"/api/nodes/tasks/{tid}/complete", headers=H,
                  json={"outputs": [{"key": "tts", "path": "/node/b.wav"}]}).status_code == 200
    from app.db.models import PipelineTask
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        db.get(PipelineTask, tid).status = "dead"
        db.commit()
    finally:
        db.close()
    assert c.post(f"/api/projects/{pid}/mode-b/tts-requeue", json={}).json()["requeued"] == 0
    assert c.post(f"/api/projects/{pid}/mode-b/tts-requeue",
                  json={"include_dead": True}).json()["requeued"] == 1


# ── wave2.3：rate/emotion进缓存键 + uids精选重跑 ─────────────
def test_tts_batch_rate_in_hash_and_uids(tmp_path):
    c = _client(str(tmp_path / "w7.db"))
    pid = _seed_translated(c, "语速剧", scene_size=40)
    utts = c.get(f"/api/projects/{pid}/utterances?lang=en").json()
    uids = [u["uid"] for u in utts[:2]]
    r1 = c.post(f"/api/projects/{pid}/mode-b/tts-batch",
                json={"uids": uids, "engine": "cosyvoice_api"}).json()
    assert r1["created"] == 2, r1
    r2 = c.post(f"/api/projects/{pid}/mode-b/tts-batch",
                json={"uids": uids, "engine": "cosyvoice_api", "rate": 1.25}).json()
    assert r2["created"] == 2, r2          # rate不同→新hash→重新合成
    r3 = c.post(f"/api/projects/{pid}/mode-b/tts-batch",
                json={"uids": uids, "engine": "cosyvoice_api", "rate": 1.25}).json()
    assert r3["created"] == 0 and r3["skipped"] == 2, r3
    from app.db.models import PipelineTask
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        pay = [t.output_paths["payload"] for t in
               db.query(PipelineTask).filter_by(project_id=pid, task_type="tts-generate").all()]
        rates = sorted(p["rate"] for p in pay if "rate" in p)
        assert rates == [1.25, 1.25], pay
    finally:
        db.close()


# ── wave2.4：clip挂版本跳占位符 + TTS文本剥HTML ──────────────
def test_tts_payload_strips_html(tmp_path):
    c = _client(str(tmp_path / "w8.db"))
    pid = _seed_translated(c, "剥标签剧", scene_size=40)
    from app.db.models import Translation, Utterance
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        u = (db.query(Utterance).filter_by(project_id=pid)
               .order_by(Utterance.seq_index).first())
        db.add(Translation(utterance_id=u.id, target_lang="en", version=99,
                           text="<b>Hello there</b>", syllable_ratio=1.0))
        db.commit()
    finally:
        db.close()
    r = c.post(f"/api/projects/{pid}/mode-b/tts-task", json={"engine": "mock"}).json()
    assert r["ok"], r
    from app.db.models import PipelineTask
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        t = db.get(PipelineTask, r["task_id"])
        assert t.output_paths["payload"]["text"] == "Hello there", t.output_paths["payload"]
    finally:
        db.close()


# ── wave3：联合语速上限（防听不清）+ 工作文件下载 ────────────
def test_fit_clip_total_speed_cap(tmp_path):
    import numpy as np
    import soundfile as sf
    from app.mode_b import fit_clip
    sr = 16000
    wav = tmp_path / "x.wav"
    sf.write(str(wav), np.zeros(sr * 5, dtype="float32"), sr)   # 5s，窗口2s → ratio2.5
    # tts_rate=1.0：atempo顶到1.3 → 联合1.3 ≤1.5 允许
    r = fit_clip(str(wav), str(tmp_path / "f"), "U1", 2000, tts_rate=1.0)
    assert r["speed"] == 1.3 and r["over_window"], r      # 2.5/1.3=1.92仍超→如实标记
    # tts_rate=1.25：headroom=1.2 → atempo只到1.2（联合恰1.5封顶）
    r2 = fit_clip(str(wav), str(tmp_path / "f"), "U2", 2000, tts_rate=1.25)
    assert r2["speed"] == 1.2 and r2["over_window"], r2
    # tts_rate=1.5：headroom=1.0 → 完全不再加速
    r3 = fit_clip(str(wav), str(tmp_path / "f"), "U3", 2000, tts_rate=1.5)
    assert r3["speed"] == 1.0 and r3["over_window"], r3


def test_work_file_download(tmp_path):
    os.environ["MODE_B_STORAGE"] = str(tmp_path / "storage")
    c = _client(str(tmp_path / "w9.db"))
    pid = _seed_translated(c, "下载剧", scene_size=40)
    work = tmp_path / "storage" / pid[:8]
    work.mkdir(parents=True)
    (work / "ab_samples.zip").write_bytes(b"PK\x03\x04fake")
    assert c.get(f"/api/projects/{pid}/mode-b/file/ab_samples.zip").status_code == 200
    assert c.get(f"/api/projects/{pid}/mode-b/file/../secret").status_code == 400
    assert c.get(f"/api/projects/{pid}/mode-b/file/nope.zip").status_code == 404
