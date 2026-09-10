"""Pure persisted-state inspection for the Mode B production chain."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .db.models import PipelineTask, Project, Translation, TtsClip, Utterance


_PLACEHOLDER_RE = re.compile(
    r"^\s*\[(MISSING|Translation|UNTRANSLATED|Paragraph|Segment)", re.IGNORECASE
)
_FAILED_STATUSES = {"dead", "failed"}


def _valid_translation(text: str | None) -> bool:
    return bool((text or "").strip()) and not _PLACEHOLDER_RE.match(text or "")


def _has_artifact(task: PipelineTask, keys: set[str]) -> bool:
    outputs = task.output_paths if isinstance(task.output_paths, dict) else {}
    artifacts = outputs.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    return any(
        isinstance(artifact, dict)
        and artifact.get("key") in keys
        and bool(artifact.get("href") or artifact.get("path"))
        for artifact in artifacts
    )


def _qc_passed(task: PipelineTask) -> bool:
    outputs = task.output_paths if isinstance(task.output_paths, dict) else {}
    qc = outputs.get("qc")
    return isinstance(qc, dict) and qc.get("pass") is True


def _result(state: str, phase: str, next_action: str, reason: str | None,
            selected_uids: list[str], counts: dict[str, int]) -> dict:
    return {
        "state": state,
        "phase": phase,
        "next_action": next_action,
        "blocked_reason": reason,
        "selected_uids": selected_uids,
        "counts": counts,
    }


def _blocked(phase: str, action: str, reason: str, selected_uids: list[str],
             counts: dict[str, int]) -> dict:
    return _result("blocked", phase, action, reason, selected_uids, counts)


def _stage_gate(tasks: list[PipelineTask], phase: str, create_action: str,
                retry_action: str, artifact_keys: set[str], selected_uids: list[str],
                counts: dict[str, int]) -> dict | None:
    """Return a blocking response until the named global stage is durable."""
    if not tasks:
        return _blocked(phase, create_action, f"no {phase} task", selected_uids, counts)
    if any((task.status or "").lower() in _FAILED_STATUSES for task in tasks):
        return _blocked(phase, retry_action, f"{phase} task failed or dead", selected_uids, counts)
    if any((task.status or "").lower() != "completed" for task in tasks):
        return _blocked(phase, retry_action, f"{phase} task is incomplete", selected_uids, counts)
    if not any(_has_artifact(task, artifact_keys) for task in tasks):
        return _blocked(phase, retry_action, f"{phase} artifact is missing", selected_uids, counts)
    return None


def inspect_mode_b_run(db: Session, project: Project) -> dict:
    """Inspect persisted Mode B state without flushing, writing, or dispatching work.

    The response only describes the next action.  It does not create tasks, update
    project config, read artifacts from storage, or invoke a worker.
    """
    with db.no_autoflush:
        config = project.config if isinstance(project.config, dict) else {}
        run_config = config.get("mode_b_run") if isinstance(config.get("mode_b_run"), dict) else {}
        requested = run_config.get("canary_uids")
        utterances = (db.query(Utterance)
                        .filter(Utterance.project_id == project.id)
                        .order_by(Utterance.seq_index, Utterance.uid)
                        .all())
        by_uid = {utterance.uid: utterance for utterance in utterances}

        if isinstance(requested, list) and requested:
            selected_uids = []
            for uid in requested:
                if isinstance(uid, str) and uid in by_uid and uid not in selected_uids:
                    selected_uids.append(uid)
        else:
            selected_uids = [utterance.uid for utterance in utterances]
        selected = [by_uid[uid] for uid in selected_uids]
        selected_ids = {utterance.id for utterance in selected}

        translations = (db.query(Translation)
                        .filter(Translation.utterance_id.in_(selected_ids),
                                Translation.target_lang == project.target_lang)
                        .order_by(Translation.utterance_id, Translation.version.desc())
                        .all()) if selected_ids else []
        latest_valid: dict[str, Translation] = {}
        for translation in translations:
            if translation.utterance_id not in latest_valid and _valid_translation(translation.text):
                latest_valid[translation.utterance_id] = translation

        all_tts = (db.query(PipelineTask)
                   .filter(PipelineTask.project_id == project.id,
                           PipelineTask.task_type == "tts-generate")
                   .order_by(PipelineTask.task_key, PipelineTask.id)
                   .all())
        tts_tasks = []
        for task in all_tts:
            outputs = task.output_paths if isinstance(task.output_paths, dict) else {}
            payload = outputs.get("payload") if isinstance(outputs.get("payload"), dict) else {}
            if payload.get("uid") in selected_uids:
                tts_tasks.append(task)

        clips = (db.query(TtsClip)
                 .filter(TtsClip.utterance_id.in_(selected_ids),
                         TtsClip.target_lang == project.target_lang,
                         TtsClip.status == "completed")
                 .order_by(TtsClip.utterance_id, TtsClip.version.desc(), TtsClip.id)
                 .all()) if selected_ids else []
        completed_clips = {
            clip.utterance_id: clip
            for clip in clips
            if clip.audio_r2_key
            and clip.utterance_id in latest_valid
            and clip.translation_id == latest_valid[clip.utterance_id].id
        }

        completed_tts = [task for task in tts_tasks if (task.status or "").lower() == "completed"]
        qc_passed = [task for task in completed_tts if _qc_passed(task)]
        counts = {
            "selected": len(selected),
            "speaker_bound": sum(bool(utterance.speaker_id) for utterance in selected),
            "translations_valid": len(latest_valid),
            "tts_tasks": len(tts_tasks),
            "tts_completed": len(completed_tts),
            "tts_qc_passed": len(qc_passed),
            "clips_completed": len(completed_clips),
        }

        separation_tasks = (db.query(PipelineTask)
                            .filter(PipelineTask.project_id == project.id,
                                    PipelineTask.task_type == "separate-vocals")
                            .order_by(PipelineTask.task_key, PipelineTask.id)
                            .all())
        blocked = _stage_gate(separation_tasks, "separate-vocals", "create_separation",
                              "retry_separation", {"vocals"}, selected_uids, counts)
        if blocked:
            return blocked

        diarize_tasks = (db.query(PipelineTask)
                         .filter(PipelineTask.project_id == project.id,
                                 PipelineTask.task_type == "diarize")
                         .order_by(PipelineTask.task_key, PipelineTask.id)
                         .all())
        blocked = _stage_gate(diarize_tasks, "diarize", "create_diarize", "retry_diarize",
                              {"diarize", "diarize_result"}, selected_uids, counts)
        if blocked:
            return blocked

        if not selected:
            return _blocked("bind_speakers", "bind_speakers", "no selected utterances",
                            selected_uids, counts)
        if counts["speaker_bound"] != counts["selected"]:
            return _blocked("bind_speakers", "bind_speakers", "selected speakers are unbound",
                            selected_uids, counts)
        if counts["translations_valid"] != counts["selected"]:
            return _result("ready", "translate", "translate", None, selected_uids, counts)

        failed_tts = [task for task in tts_tasks
                      if (task.status or "").lower() in _FAILED_STATUSES]
        if failed_tts:
            return _blocked("tts", "retry_tts", "selected TTS task failed or dead",
                            selected_uids, counts)
        incomplete_tts = [task for task in tts_tasks
                          if (task.status or "").lower() != "completed"]
        if incomplete_tts:
            return _blocked("tts", "retry_tts", "selected TTS task is incomplete",
                            selected_uids, counts)
        if len(tts_tasks) < counts["selected"]:
            return _result("ready", "tts", "tts", None, selected_uids, counts)
        if len(qc_passed) != len(tts_tasks):
            return _blocked("tts", "retry_tts", "selected TTS QC has not passed",
                            selected_uids, counts)
        if len(completed_clips) != counts["selected"]:
            return _blocked("tts", "retry_tts", "selected TTS clip is missing",
                            selected_uids, counts)
        return _result("ready", "package", "package", None, selected_uids, counts)
