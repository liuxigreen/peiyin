"""Persisted-state coverage for the pure Mode B reconciler."""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, PipelineTask, Project, Segment, Translation, TtsClip, Utterance
from app.mode_b_reconciler import inspect_mode_b_run


def _db(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciler.db'}")
    Base.metadata.create_all(engine)
    return Session(engine)


def _project(db: Session, canary=None):
    config = {"mode_b_run": {"canary_uids": canary}} if canary is not None else {}
    project = Project(id="project", name="Mode B", target_lang="en", config=config)
    db.add(project)
    db.add(Segment(id="segment", project_id=project.id, seg_index=1, start_ms=0, end_ms=4000))
    utterances = [
        Utterance(id="utt-1", project_id=project.id, segment_id="segment", uid="U1",
                  seq_index=1, start_ms=0, end_ms=1000, original_text="一"),
        Utterance(id="utt-2", project_id=project.id, segment_id="segment", uid="U2",
                  seq_index=2, start_ms=1000, end_ms=2000, original_text="二"),
        Utterance(id="utt-3", project_id=project.id, segment_id="segment", uid="U3",
                  seq_index=3, start_ms=2000, end_ms=3000, original_text="三"),
    ]
    db.add_all(utterances)
    db.commit()
    return project, utterances


def _task(db: Session, project: Project, task_type: str, key: str, *, status="completed",
          outputs=None):
    task = PipelineTask(project_id=project.id, task_type=task_type, task_key=key,
                        status=status, output_paths=outputs or {})
    db.add(task)
    db.flush()
    return task


def _ready_global_gates(db: Session, project: Project):
    _task(db, project, "separate-vocals", "separate", outputs={"artifacts": [
        {"key": "vocals", "href": "/artifact/vocals"}]})
    _task(db, project, "diarize", "diarize", outputs={"artifacts": [
        {"key": "diarize_result", "href": "/artifact/diarize"}]})
    db.commit()


def test_canary_order_is_idempotent_and_does_not_read_unselected_work(tmp_path):
    db = _db(tmp_path)
    try:
        project, _ = _project(db, canary=["U3", "U1", "missing", "U3"])
        before = {model.__tablename__: db.query(model).count()
                  for model in (Project, Utterance, PipelineTask, Translation, TtsClip)}

        first = inspect_mode_b_run(db, project)
        second = inspect_mode_b_run(db, project)

        assert first == second
        assert first["selected_uids"] == ["U3", "U1"]
        assert first["counts"]["selected"] == 2
        assert first["phase"] == "separate-vocals"
        assert first["state"] == "blocked" and first["blocked_reason"]
        after = {model.__tablename__: db.query(model).count()
                 for model in (Project, Utterance, PipelineTask, Translation, TtsClip)}
        assert after == before and not db.new and not db.dirty
    finally:
        db.close()


def test_artifact_gates_then_binding_and_translation_phases(tmp_path):
    db = _db(tmp_path)
    try:
        project, utterances = _project(db, canary=["U2", "U1"])
        separation = _task(db, project, "separate-vocals", "separate")
        db.commit()
        assert inspect_mode_b_run(db, project)["phase"] == "separate-vocals"

        separation.output_paths = {"artifacts": [{"key": "vocals", "href": "/vocals"}]}
        _task(db, project, "diarize", "diarize")
        db.commit()
        assert inspect_mode_b_run(db, project)["phase"] == "diarize"

        diarize = db.query(PipelineTask).filter_by(task_key="diarize").one()
        diarize.output_paths = {"artifacts": [{"key": "diarize_result", "href": "/clusters"}]}
        db.commit()
        binding = inspect_mode_b_run(db, project)
        assert binding["next_action"] == "bind_speakers" and binding["state"] == "blocked"

        for utterance in utterances:
            utterance.speaker_id = "speaker-1"
        db.commit()
        assert inspect_mode_b_run(db, project)["next_action"] == "translate"

        db.add_all([
            Translation(utterance_id=utterances[0].id, target_lang="en", version=1, text="One"),
            Translation(utterance_id=utterances[1].id, target_lang="en", version=1, text="[MISSING]"),
        ])
        db.commit()
        assert inspect_mode_b_run(db, project)["next_action"] == "translate"

        db.add(Translation(utterance_id=utterances[1].id, target_lang="en", version=2, text="Two"))
        db.commit()
        assert inspect_mode_b_run(db, project)["next_action"] == "tts"
    finally:
        db.close()


def test_tts_qc_and_clip_gate_package_and_dead_task_blocks(tmp_path):
    db = _db(tmp_path)
    try:
        project, utterances = _project(db, canary=["U1", "U2"])
        _ready_global_gates(db, project)
        for utterance in utterances:
            utterance.speaker_id = "speaker-1"
        translations = [
            Translation(utterance_id=utterances[0].id, target_lang="en", version=1, text="One"),
            Translation(utterance_id=utterances[1].id, target_lang="en", version=1, text="Two"),
        ]
        db.add_all(translations)
        db.flush()
        tasks = [
            _task(db, project, "tts-generate", "tts-1", outputs={"payload": {"uid": "U1"},
                                                                      "qc": {"pass": True}}),
            _task(db, project, "tts-generate", "tts-2", outputs={"payload": {"uid": "U2"},
                                                                      "qc": {"pass": False}}),
        ]
        db.add_all([
            TtsClip(utterance_id=utterances[0].id, target_lang="en", translation_id=translations[0].id,
                    version=1, audio_r2_key="clips/u1.wav", duration_ms=100, tts_engine="test",
                    status="completed"),
            TtsClip(utterance_id=utterances[1].id, target_lang="en", translation_id=translations[1].id,
                    version=1, audio_r2_key="clips/u2.wav", duration_ms=100, tts_engine="test",
                    status="completed"),
        ])
        db.commit()

        qc_blocked = inspect_mode_b_run(db, project)
        assert qc_blocked["state"] == "blocked" and qc_blocked["phase"] == "tts"
        assert qc_blocked["blocked_reason"]

        tasks[1].output_paths = {"payload": {"uid": "U2"}, "qc": {"pass": True}}
        db.commit()
        package = inspect_mode_b_run(db, project)
        assert package["state"] == "ready" and package["next_action"] == "package"
        assert package["counts"]["tts_qc_passed"] == 2

        tasks[0].status = "dead"
        db.commit()
        dead = inspect_mode_b_run(db, project)
        assert dead["state"] == "blocked" and dead["blocked_reason"]
    finally:
        db.close()
