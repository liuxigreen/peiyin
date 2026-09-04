"""闸门B（0905）：配角音色映射 + 音色分布红线。
配角/群杂按 性别×年龄段 映射到6预设（DubFox思路：配角几个固定音色是行业常态）；
红线检查：音色塌缩（单音色占比>50% 或 实际音色数<角色数/4）→ 流水线转blocked。"""
from __future__ import annotations

# 主配角映射表：key=(gender, age_band) → 按序尝试的音色资产名
# （同名多种子音色时按序取，凑不满6类就复用）
CAST_MAP: dict[tuple[str, str], list[str]] = {
    ("male", "young"): ["va_male_young", "va_male_lively"],
    ("male", "middle"): ["va_male_mature", "va_male_young"],
    ("male", "elder"): ["va_male_mature"],
    ("male", "teen"): ["va_male_lively", "va_male_young"],
    ("female", "young"): ["va_female_young", "va_female_sharp"],
    ("female", "middle"): ["va_female_warm", "va_female_young"],
    ("female", "elder"): ["va_female_warm"],
    ("female", "teen"): ["va_female_young", "va_female_warm"],
}
ELDER_FALLBACK = {"male": "va_male_mature", "female": "va_female_warm"}
# 群杂轮换池（有台词的龙套：服务员/保镖/路人）——分散到不抢主配角的音色
CROWD_POOL = ["va_male_lively", "va_male_mature", "va_female_sharp", "va_female_warm"]

GATE_MAX_VOICE_SHARE = 0.50   # 单音色占比>50% = 塌缩
GATE_MIN_VOICES_RATIO = 0.25  # 实际音色数 < 角色数/4 = 塌缩


def pick_asset_for(candidates: list[tuple[str, list[str]]],
                   gender: str, age: str, is_crowd: bool = False,
                   crowd_index: int = 0) -> str | None:
    """candidates=[(asset_name, tags)]。按映射表选音色名；群杂走轮换池。"""
    names = {n for n, _ in candidates}
    if is_crowd:
        pool = [n for n in CROWD_POOL if n in names]
        return pool[crowd_index % len(pool)] if pool else None
    g = gender if gender in ("male", "female") else ""
    a = age if age in ("young", "middle", "elder", "teen") else "young"
    for want in CAST_MAP.get((g, a), []):
        if want in names:
            return want
    if g in ELDER_FALLBACK:
        for n, tags in candidates:
            if n == ELDER_FALLBACK[g]:
                return n
    for n, tags in candidates:                      # 最后兜底：性别任配
        if g and g in (tags or []):
            return n
    return candidates[0][0] if candidates else None


def voice_distribution_gate(roles: list[dict], assignments: list[dict]) -> dict:
    """roles=[{label,is_primary}], assignments=[{label,asset}]（每角色最终音色）。
    返回 {pass, red_flags, n_roles, n_voices, top_share}。"""
    n_roles = max(len(roles), 1)
    used = {}
    for a in assignments:
        used[a["asset"]] = used.get(a["asset"], 0) + 1
    total = sum(used.values()) or 1
    top_asset, top_n = max(used.items(), key=lambda x: x[1])
    top_share = top_n / total
    n_voices = len(used)
    red = []
    if top_share > GATE_MAX_VOICE_SHARE:
        red.append(f"voice collapse: {top_asset} covers {top_share:.0%} of roles")
    if n_voices < max(n_roles * GATE_MIN_VOICES_RATIO, 2):
        red.append(f"too few voices: {n_voices} for {n_roles} roles")
    return {"pass": not red, "red_flags": red, "n_roles": n_roles,
            "n_voices": n_voices, "top_share": round(top_share, 2),
            "distribution": used}
