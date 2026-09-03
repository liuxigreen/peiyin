"""韵律质检门（B7）：免人工听检，F0半音标准差+能量波动双指标。
平到医院朗读感的句子自动标记并触发重掷（temperature等效=加强instruct重合成）。
耳语/哭腔类台词跳过F0检查（Gemini阈值场景为正常语调句）。"""
import math

import numpy as np

FLAT_ST_STD = 1.8      # <1.8半音标准差=朗读感（微短剧需要>=2.2）
FLAT_INT_STD = 4.5     # 能量包络波动dB下限
SKIP_EMOTIONS = {"whisper", "耳语", "whisper-like", "哭腔"}


def evaluate_wav(path: str) -> dict:
    """返回{f0_st_std,int_std,voiced_n,flat}。解析失败返回flat=False+err。"""
    import parselmouth
    try:
        sound = parselmouth.Sound(path)
        pitch = sound.to_pitch(time_step=0.01)
        f0 = pitch.selected_array["frequency"]
        voiced = f0[f0 > 0]
        if len(voiced) < 10:
            return {"f0_st_std": None, "int_std": None,
                    "voiced_n": int(len(voiced)), "flat": False,
                    "skip": "low_voiced"}
        st = 12 * np.log2(voiced / np.median(voiced))
        st_std = float(np.std(st))
        intensity = sound.to_intensity()
        int_std = float(np.std(intensity.values[0]))
        flat = st_std < FLAT_ST_STD or int_std < FLAT_INT_STD
        return {"f0_st_std": round(st_std, 2), "int_std": round(int_std, 2),
                "voiced_n": int(len(voiced)), "flat": bool(flat)}
    except Exception as e:                                   # noqa: BLE001
        return {"f0_st_std": None, "int_std": None, "voiced_n": 0,
                "flat": False, "err": str(e)[:120]}


def boosted_instruct(instruct: str | None, emotion: str | None) -> str:
    """重掷指令：原instruct加强语调要求；temperature参数CosyVoice HTTP不暴露，
    用指令强化等效驱动韵律多样性。"""
    base = instruct or ""
    tail = "语调起伏要丰富，像专业配音演员一样有情绪张力和重音变化"
    if emotion:
        tail = f"用{emotion}的情绪，{tail}"
    if base:
        return f"{base}，{tail}"
    return tail


def st_std_from_f0(f0_values: list[float]) -> float:
    """纯函数便于单测：半音标准差。"""
    voiced = [v for v in f0_values if v > 0]
    if len(voiced) < 2:
        return 0.0
    med = sorted(voiced)[len(voiced) // 2]
    st = [12 * math.log2(v / med) for v in voiced]
    mean = sum(st) / len(st)
    return (sum((x - mean) ** 2 for x in st) / len(st)) ** 0.5
