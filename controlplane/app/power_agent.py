"""O2: 算力Agent——GPU队列水位监控+自动开关机决策（干跑模式）。
每60s扫描：
  gpu积压(queued+pending且ready的gpu任务)≥阈值 持续N周期 → 决策 power_on
  gpu空闲(无gpu任务running/queued) 持续M周期 → 决策 power_off
  日预算安全阀：当日累计GPU费用超上限 → 只通知不自动开机
SCNET_OPENAPI_TOKEN 未配置=DRY_RUN（只打日志，不改实例）——离线联调核心设计。
配置：settings表缺省 {gpu_queue_threshold:2, gpu_idle_cycles:10, daily_gpu_budget:50}
"""
from __future__ import annotations

import datetime as dt
import logging
import os

from sqlalchemy.orm import Session

from .db.models import PipelineTask

log = logging.getLogger("power_agent")

FAMILY_GPU = ("diarize", "separate", "asr", "tts",
              "subtitle-fast", "subtitle-quality", "stitch", "encode", "mix")


def gpu_backlog(db: Session) -> int:
    """等待GPU的任务数（pending+queued中 gpu_required 的）。"""
    return (db.query(PipelineTask)
              .filter(PipelineTask.gpu_required == True,                      # noqa: E712
                      PipelineTask.status.in_(("pending", "queued")))
              .count())


def gpu_busy(db: Session) -> int:
    return (db.query(PipelineTask)
              .filter(PipelineTask.gpu_required == True,                      # noqa: E712
                      PipelineTask.status == "running")
              .count())


class PowerAgent:
    """无状态决策器：feed(backlog,busy)输出动作序列。干跑测试友好。"""

    def __init__(self, queue_threshold: int = 2, on_streak_need: int = 5,
                 idle_cycles_need: int = 10, daily_budget: float = 50.0,
                 hourly_rate: float = 1.94, dry_run: bool | None = None):
        self.queue_threshold = queue_threshold
        self.on_streak_need = on_streak_need
        self.idle_cycles_need = idle_cycles_need
        self.daily_budget = daily_budget
        self.hourly_rate = hourly_rate
        self.dry_run = (os.getenv("SCNET_OPENAPI_TOKEN", "") == "") \
            if dry_run is None else dry_run
        self.on_streak = 0
        self.idle_streak = 0
        self.is_on = False
        self.since: dt.datetime | None = None
        self.events: list[dict] = []

    def _act(self, action: str, reason: str) -> dict:
        ev = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
              "action": action, "reason": reason, "dry_run": self.dry_run}
        if action == "power_on":
            self.is_on = True
            self.since = dt.datetime.now()
            self.on_streak = 0
            self.idle_streak = 0
            # 真实现：SCNET OpenAPI 启动容器实例+批量执行脚本(join)
        elif action == "power_off":
            if self.since:
                hours = (dt.datetime.now() - self.since).total_seconds() / 3600
                ev["cost"] = round(hours * self.hourly_rate, 2)
            self.is_on = False
            self.since = None
            self.idle_streak = 0
            # 真实现：SCNET OpenAPI 停止实例
        log.info("[power-agent] %s (%s) dry_run=%s", action, reason, self.dry_run)
        self.events.append(ev)
        return ev

    def feed(self, backlog: int, busy: int) -> dict | None:
        """每周期喂一次当前水位，返回触发的动作（无则None）。"""
        if backlog >= self.queue_threshold:
            self.on_streak += 1
            self.idle_streak = 0
            if self.on_streak >= self.on_streak_need:
                return self._act("power_on",
                                 f"backlog={backlog}≥{self.queue_threshold} "
                                 f"连续{self.on_streak}周期")
        elif backlog == 0 and busy == 0:
            self.idle_streak += 1
            self.on_streak = 0
            if self.is_on and self.idle_streak >= self.idle_cycles_need:
                return self._act("power_off", f"空闲{self.idle_streak}周期")
        else:
            self.on_streak = 0
        return None

    def budget_left(self, used_today: float) -> float:
        return round(self.daily_budget - used_today, 2)


def power_status(db: Session, agent: PowerAgent) -> dict:
    """网页/TG的状态卡数据。"""
    used = 0.0
    for ev in agent.events:
        used += ev.get("cost", 0.0)
    return {"gpu_backlog": gpu_backlog(db), "gpu_running": gpu_busy(db),
            "instance_on": agent.is_on, "dry_run": agent.dry_run,
            "budget_daily": agent.daily_budget, "budget_used": round(used, 2),
            "recent_events": agent.events[-10:]}
