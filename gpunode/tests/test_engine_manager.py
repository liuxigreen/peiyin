"""离线单测：引擎管家循环、隐藏子进程和原有启停条件。"""
from __future__ import annotations

import json

import pytest

try:
    from gpunode.stages import engine_manager as manager
except ModuleNotFoundError:
    from stages import engine_manager as manager


def test_main_without_daemon_runs_one_round(monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "run_once", lambda: calls.append("round"))

    assert manager.main([]) == 0
    assert calls == ["round"]


def test_daemon_repeats_at_interval_logs_round_error_and_stops_on_keyboardinterrupt(monkeypatch):
    monkeypatch.setenv("ENGINE_MANAGER_INTERVAL", "7")
    calls = []
    sleeps = []
    logs = []

    def fake_round():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("round failed")
        if len(calls) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(manager, "run_once", fake_round)
    monkeypatch.setattr(manager, "time", type("Clock", (), {"sleep": staticmethod(sleeps.append)})())
    monkeypatch.setattr(manager, "log", logs.append)

    assert manager.main(["--daemon"]) == 0
    assert calls == [1, 2, 3]
    assert sleeps == [7, 7]
    assert any("round failed" in message for message in logs)


@pytest.mark.parametrize("raw", ["0", "-1", "", "not-an-integer"])
def test_manager_interval_requires_positive_integer(monkeypatch, raw):
    monkeypatch.setenv("ENGINE_MANAGER_INTERVAL", raw)

    with pytest.raises(ValueError, match="positive integer"):
        manager.manager_interval()


def test_windows_commands_use_no_console_creation_flag(monkeypatch, tmp_path):
    calls = []

    class Result:
        stdout = "False"

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    flag = 0x08000000
    monkeypatch.setattr(manager.subprocess, "run", fake_run)
    monkeypatch.setattr(manager, "WINDOWS_NO_CONSOLE_FLAGS", flag)
    monkeypatch.setattr(manager, "NODE_ENGINE_LAUNCHER", str(tmp_path / "missing.ps1"))

    assert manager.engine_alive() is False
    assert manager.engine_procs() == 0
    manager.start_engine()
    manager.kill_engine()

    assert [call[0][0] for call in calls] == ["powershell", "powershell", "schtasks", "powershell"]
    assert all(call[1]["creationflags"] == flag for call in calls)


def test_start_engine_prefers_existing_launcher(monkeypatch, tmp_path):
    launcher = tmp_path / "launch_engine.ps1"
    launcher.write_text("# offline test launcher", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(manager, "NODE_ENGINE_LAUNCHER", str(launcher))
    monkeypatch.setattr(manager, "_run_no_console", fake_run)

    manager.start_engine()

    assert calls == [(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher)],
        {"capture_output": True, "timeout": 30},
    )]


def test_start_engine_falls_back_to_schtasks_when_launcher_is_missing(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(manager, "NODE_ENGINE_LAUNCHER", str(tmp_path / "missing.ps1"))
    monkeypatch.setattr(manager, "_run_no_console", fake_run)

    manager.start_engine()

    assert calls == [(
        ["schtasks", "/run", "/tn", "peiyin-engine-start"],
        {"capture_output": True, "timeout": 30},
    )]


def test_run_once_starts_engine_when_tts_should_run(monkeypatch):
    started = []
    monkeypatch.setattr(manager, "api", lambda _path: {"should_run": True})
    monkeypatch.setattr(manager, "engine_alive", lambda: False)
    monkeypatch.setattr(manager, "engine_procs", lambda: 0)
    monkeypatch.setattr(manager, "start_engine", lambda: started.append(True))
    monkeypatch.setattr(manager, "log", lambda _message: None)

    manager.run_once()

    assert started == [True]


def test_run_once_tracks_idle_time_and_releases_engine_after_timeout(tmp_path, monkeypatch):
    state = tmp_path / "engine_idle_since.json"
    now = [1000.0]
    killed = []
    monkeypatch.setattr(manager, "STATE", str(state))
    monkeypatch.setattr(manager, "api", lambda _path: {"should_run": False})
    monkeypatch.setattr(manager, "engine_alive", lambda: True)
    monkeypatch.setattr(manager, "engine_procs", lambda: 0)
    monkeypatch.setattr(manager.time, "time", lambda: now[0])
    monkeypatch.setattr(manager, "kill_engine", lambda: killed.append(True))
    monkeypatch.setattr(manager, "log", lambda _message: None)

    manager.run_once()
    assert json.loads(state.read_text(encoding="utf-8"))["idle_since"] == now[0]

    now[0] += manager.IDLE_MIN * 60 + 1
    manager.run_once()

    assert killed == [True]
    assert json.loads(state.read_text(encoding="utf-8"))["idle_since"] is None
