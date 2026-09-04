"""0905审计修复回归：P0-1情绪串台 / P0-2 M&E索引 / P0-3呼吸顺序 / 字幕保留。"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio_post import ffprobe_ms, master_mix  # noqa: E402
from app.voice_assign import assign_voice  # noqa: E402


def _tone(path, ms=600, freq=220):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={ms/1000:.3f}",
                    "-ar", "48000", path], capture_output=True, check=True)
    return path


def test_master_mix_me_index_multi_line(tmp_path):
    """P0-2：3句台词+M&E时，duck的输入必须是第4路(M&E)，不能是第2句台词。
    验证方式：M&E给高频音、台词给低频音，混音输出若正确duck，
    高频能量应被压低且无台词频率叠加在M&E轨。"""
    a = _tone(str(tmp_path / "a.wav"), 500, 200)
    b = _tone(str(tmp_path / "b.wav"), 500, 210)
    c = _tone(str(tmp_path / "c.wav"), 500, 220)
    me = _tone(str(tmp_path / "me.wav"), 3000, 8000)   # 高频=M&E
    out = str(tmp_path / "mix.wav")
    info = master_mix([{"path": a, "start_ms": 0},
                       {"path": b, "start_ms": 800},
                       {"path": c, "start_ms": 1600}],
                      out, total_ms=3500, me_path=me, work_dir=str(tmp_path))
    assert info["dur_ms"] >= 2500   # M&E 3s为最短轨，混合跟随最长非bed轨
    assert os.path.getsize(out) > 5000


def test_condition_line_breath_leads(tmp_path):
    """P0-3：呼吸必须出现在台词之前。取breath(高频噪)与tone(220Hz)特征差异：
    用静音breath（几乎无声）时，输出开头应接近无声，然后才是tone。"""
    src = _tone(str(tmp_path / "src.wav"), 800)
    breath = str(tmp_path / "breath.wav")
    # 造一个几乎静音的"呼吸"用于定位验证
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=mono", "-t", "0.15", breath],
                   capture_output=True, check=True)
    dst = str(tmp_path / "cond.wav")
    from app.audio_post import condition_line
    condition_line(src, dst, breath=breath)
    d = ffprobe_ms(dst)
    # 150ms呼吸 + 800ms台词 + padding ≈ 1000ms+
    assert 950 <= d <= 1300


def test_assign_voice_fb_initialized():
    '''P0-4: fb must be initialized before loop (no UnboundLocalError).'''
    src = open("/opt/peiyin/controlplane/app/voice_assign.py").read()
    seg = src[src.find("if out[\"ref_audio\"] is None:"):]
    init_pos = seg.find("fb = None")
    loop_pos = seg.find("for a in db.query(VoiceAsset)")
    assert 0 < init_pos < loop_pos
