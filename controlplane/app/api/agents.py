"""O1/O2: 质检Tab + 算力Agent状态 API"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import Project
from ..db.session import get_db
from ..qc_agent import qc_summary
from ..power_agent import PowerAgent, power_status

router = APIRouter(prefix="/api", tags=["agents"])

# 进程级单例（干跑模式；真API模式由SCNET_OPENAPI_TOKEN启用后重建）
_power = PowerAgent()


@router.get("/projects/{pid}/qc")
def project_qc(pid: str, db: Session = Depends(get_db)):
    if not db.get(Project, pid):
        from fastapi import HTTPException
        raise HTTPException(404)
    return qc_summary(db, pid)


@router.get("/power/status")
def power_status_api(db: Session = Depends(get_db)):
    return power_status(db, _power)


@router.post("/power/tick")
def power_tick(db: Session = Depends(get_db)):
    """手动喂一拍（网页按钮/外部cron可调；常驻线程化在P1收尾）。"""
    ev = _power.feed(power_status(db, _power)["gpu_backlog"],
                     power_status(db, _power)["gpu_running"])
    return {"event": ev, **power_status(db, _power)}
