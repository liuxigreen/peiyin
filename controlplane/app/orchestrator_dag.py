"""O1: DAG任务定义 + 幂等写入工具
全流程24种任务的静态定义（task_key/资源类型/依赖模板/权重）。
编排器据此为一个project实例化具体任务行。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# ── 任务规格表：key前缀 → (名称, 资源类型, 权重, 是否切片级/句组级) ──
GLOBAL_TASKS: dict[str, tuple[str, str, int]] = {
    "T010": ("probe",          "io",  1),
    "T020": ("audio-extract",  "gpu", 5),
    "T030": ("vad-scene",      "cpu", 2),
    "T040": ("diarize",        "gpu", 5),
    "T050": ("speakers-build", "cpu", 2),
    "T060": ("plan-chunks",    "cpu", 2),
}
SEGMENT_TASKS: dict[str, tuple[str, str, int]] = {
    "T110": ("subtitle-fast",  "gpu", 5),
    "T120": ("separate",       "gpu", 5),
    "T130": ("asr",            "gpu", 5),
    "T140": ("ocr",            "cpu", 2),
    "T150": ("align",          "cpu", 2),
    # TTS依赖SC/T220（音节校验），见build_project_dag动态展开
    "T310": ("tts",            "gpu", 5),
    "T320": ("fit-timeline",   "cpu", 2),
    "T330": ("mix",            "cpu", 2),
    "T340": ("encode",         "cpu", 2),
}
SCENE_TASKS: dict[str, tuple[str, str, int]] = {
    # 翻译五步链：上下文包→直译→意译(配音约束)→跨批终检→落库
    "T205": ("ctx-pack",         "cpu", 20),
    "T211": ("translate-r1",     "io", 60),
    "T212": ("translate-r2",     "io", 80),
    "T213": ("translate-review", "io", 50),
    "T214": ("merge-dubtrack",   "cpu", 20),
    "T220": ("syllable-check",   "cpu", 40),
}
FINAL_TASKS: dict[str, tuple[str, str, int]] = {
    "T410": ("stitch",    "cpu", 2),
    "T420": ("subtitles", "cpu", 2),
    "T430": ("qc",        "cpu", 2),
    "T440": ("finalize",  "io",  1),
}

# 静态依赖模板。{seg}/{scene}为占位符。
DEPS_TEMPLATE: dict[str, list[str]] = {
    # 全片链
    "T020": ["T010"], "T030": ["T020"], "T040": ["T030"],
    "T050": ["T040"], "T060": ["T050"],
    # 切片链（依赖T060全局完成；seg内部有序）
    "T110": ["T060"], "T120": ["T110"], "T130": ["T120"],
    "T140": ["T120"],
    "T150": ["T130", "T140"],
    # 翻译场景批：依赖全部align完成 → 由submit时动态解析; 这里存占位
    "T220": ["T210"],
    # 合成链回到seg粒度，T310依赖全部translate done(提交时展开)
    "T320": ["T310"], "T330": ["T320"], "T340": ["T330"],
    # 收尾链
    "T410": ["*T340"],           # *前缀=通配所有切片的该类任务
    "T420": ["T410"], "T430": ["T420"], "T440": ["T430"],
}


def dag_key(template: str, seg: str | None = None, scene: str | None = None) -> str:
    return template.replace("{seg}", seg or "").replace("{scene}", scene or "")


@dataclass
class TaskRow:
    task_key: str
    task_type: str
    resource: str          # gpu/cpu/io
    weight: int
    depends_on: list[str]
    model_name: str | None = None


def build_project_dag(n_segments: int, scenes: list[str]) -> list[TaskRow]:
    """实例化一个项目的完整DAG。scenes=场景批id列表(翻译分组)。"""
    rows: list[TaskRow] = []

    def add(key, ttype, res, weight, deps):
        rows.append(TaskRow(key, ttype, res, weight, deps))

    # 全片链
    for k in GLOBAL_TASKS:
        name, res, w = GLOBAL_TASKS[k]
        deps = [dag_key(d) for d in DEPS_TEMPLATE.get(k, [])]
        add(k, name, res, w, [d for d in deps if "*" not in d])

    # 切片链
    all_t340: list[str] = []
    for i in range(n_segments):
        seg = f"S{i+1:02d}"
        seg_dependent = ("T120", "T130", "T140", "T150", "T320", "T330", "T340")
        for k in SEGMENT_TASKS:
            name, res, w = SEGMENT_TASKS[k]
            key = f"{seg}/{k}"
            if k == "T110":
                deps = ["T060"]
            elif k == "T310":
                # TTS依赖全部场景批的音节校验完成
                deps = [f"{sc}/T220" for sc in scenes]
            else:
                deps = [f"{seg}/{d}" for d in DEPS_TEMPLATE.get(k, [])]
            add(key, name, res, w, deps)
        all_t340.append(f"{seg}/T340")

    # 场景翻译批（五步链）：T205依赖全部切片align完成；链内有序；
    # T213跨批校对→全部场景的r2完成后才跑；T214/T220回场景粒度
    all_align = [f"S{i+1:02d}/T150" for i in range(n_segments)]
    for sc in scenes:
        name, res, w = SCENE_TASKS["T205"]
        add(f"{sc}/T205", name, res, w, list(all_align))
        for k in ("T211", "T212"):
            name, res, w = SCENE_TASKS[k]
            prev = {"T211": "T205", "T212": "T211"}[k]
            add(f"{sc}/{k}", name, res, w, [f"{sc}/{prev}"])
    # T213 全局唯一：读所有批的r2输出做一致性终检
    name, res, w = SCENE_TASKS["T213"]
    add("T213", name, res, w, [f"{sc}/T212" for sc in scenes])
    # T214/T220 按场景落库+音节校验
    for sc in scenes:
        name, res, w = SCENE_TASKS["T214"]
        add(f"{sc}/T214", name, res, w, ["T213"])
        name, res, w = SCENE_TASKS["T220"]
        add(f"{sc}/T220", name, res, w, [f"{sc}/T214"])

    # 收尾链
    name, res, w = FINAL_TASKS["T410"]
    add("T410", name, res, w, list(all_t340))
    for k in ("T420", "T430", "T440"):
        name, res, w = FINAL_TASKS[k]
        add(k, name, res, w, [dag_key(d) for d in DEPS_TEMPLATE.get(k, [])])

    return rows
def compute_input_hash(task_type: str, payload: dict) -> str:
    blob = json.dumps([task_type, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:64]
