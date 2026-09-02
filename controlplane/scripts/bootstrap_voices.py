"""空盘引导：预置音色再生成 + 入库（系统盘更换后白月光重跑的前置步骤）。

用法（ECS 上，需 service 同环境）：
  cd /opt/peiyin/controlplane && eval export $(systemctl show peiyin -p Environment --value)
  .venv/bin/python scripts/bootstrap_voices.py

依赖：pip install edge-tts（imageio_ffmpeg 已在 requirements）。
产物：MODE_B_STORAGE/voices/{name}.wav + voice_assets 六条记录。
音频走 voice_id+HTTP 下发（0902红线：绝不入库）。幂等可重跑。
"""
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.getenv("MODE_B_STORAGE", "/tmp/peiyin-mode-b"), "voices")
TEXT = "这是我们预置的配音声线参考样本，声音会克隆出同一种音色来说外语。"
VOICES = {
    "va_male_young":   ("zh-CN-YunjianNeural", ["male", "young"],
                        "热血 激情 阳光 年轻"),
    "va_male_mature":  ("zh-CN-YunyangNeural", ["male", "young"],
                        "低沉 威严 冷静 成熟"),
    "va_male_lively":  ("zh-CN-YunxiNeural", ["male", "teen"],
                        "阳光 少年 明亮 轻快"),
    "va_female_young": ("zh-CN-XiaoyiNeural", ["female", "young"],
                        "甜美 活泼 年轻"),
    "va_female_warm":  ("zh-CN-XiaoxiaoNeural", ["female", "middle"],
                        "温暖 知性"),
    "va_female_sharp": ("zh-CN-XiaoniNeural", ["female", "young"],
                        "尖锐 冷艳 刻薄 恨意"),
}


def _ffmpeg():
    import shutil
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


async def gen(name: str, vid: str) -> str:
    import edge_tts
    mp3, wav = f"{OUT}/{name}.mp3", f"{OUT}/{name}.wav"
    if not os.path.exists(wav):
        await edge_tts.Communicate(TEXT, vid).save(mp3)
        subprocess.run([_ffmpeg(), "-y", "-i", mp3, "-ar", "22050", "-ac", "1", wav],
                       capture_output=True, check=True)
    return wav


def register(wavs: dict) -> None:
    from app.db.session import SessionLocal
    from app.db.models import VoiceAsset
    db = SessionLocal()
    for name, path in wavs.items():
        tags, desc = VOICES[name][1], VOICES[name][2]
        row = db.query(VoiceAsset).filter_by(name=name).first()
        if row:
            row.tags, row.ref_audio_r2_key = tags, path
            row.tts_params = {**(row.tts_params or {}), "desc": desc}
        else:
            db.add(VoiceAsset(name=name, tags=tags, ref_audio_r2_key=path,
                              tts_params={"desc": desc}))
        print("ok", name, "->", path)
    db.commit(); db.close()


async def main():
    os.makedirs(OUT, exist_ok=True)
    wavs = {}
    for name, (vid, _tags, _desc) in VOICES.items():
        try:
            wavs[name] = await gen(name, vid)
            print("gen", name, os.path.getsize(wavs[name]) // 1024, "KB")
        except Exception as e:                               # noqa: BLE001
            print("FAIL", name, vid, str(e)[:80])
    register(wavs)


if __name__ == "__main__":
    asyncio.run(main())
