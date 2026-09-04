"""句级音频后处理 + 母带混音（B7，Gemini顾问方案落地）。
纯 ffmpeg CPU 实现：手机扬声器导向的句级 conditioning、边界 padding、
呼吸声插入、room-tone 床、M&E sidechain ducking、-13 LUFS 母带。
红线：不碰 DB，不做网络，输入输出全是 wav 路径。"""
import os
import subprocess

FF = "ffmpeg"


def _run(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-400:]}")


def ffprobe_ms(path: str) -> int:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return int(float(r.stdout.strip()) * 1000)


def make_breath(dst: str, ms: int = 150) -> str:
    """合成微呼吸声：粉噪低通4kHz，比台词低22dB。"""
    _run([FF, "-y", "-f", "lavfi",
          "-i", f"anoisesrc=c=pink:r=48000:a=0.08:d={ms/1000:.3f}",
          "-af", "lowpass=f=4000,volume=-22dB", "-ar", "48000", dst])
    return dst


def condition_line(src: str, dst: str, pre_ms: int = 40, post_ms: int = 140,
                   breath: str | None = None) -> str:
    """句级conditioning：手机扬声器EQ+两级压缩+限幅，前后padding。
    breath 提供时把呼吸声拼在句首（说话人切换/高情绪句）。"""
    pre = max(pre_ms, 1)
    chain = [f"adelay={pre}:all=1",
             "highpass=f=95:p=2",
             "equalizer=f=380:t=q:w=1.2:g=-2.5",
             "equalizer=f=2800:t=q:w=1.0:g=+3.2",
             "equalizer=f=7200:t=q:w=2.0:g=-3.5",
             "compand=attacks=0.03:decays=0.3:"
             "points=-80/-80|-40/-32|-20/-16|-10/-10|0/-8:soft-knee=6",
             "alimiter=limit=0.84:attack=5:release=50:asc=0",
             f"apad=pad_dur={post_ms/1000:.3f}"]
    if breath:
        # P0修复(0905审计)：呼吸在台词前（呼吸→台词），此前concat顺序反了
        args = [FF, "-y", "-i", breath, "-i", src, "-filter_complex",
                f"[0:a]volume=-22dB[b];"
                f"[1:a]{','.join(chain)}[v];"
                f"[b][v]concat=n=2:v=0:a=1",
                "-ar", "48000", dst]
    else:
        args = [FF, "-y", "-i", src, "-af", ",".join(chain), "-ar", "48000", dst]
    _run(args)
    return dst


def make_room_tone(dst: str, seconds: float) -> str:
    """防真空底噪床：粉噪带通塑形，-48dB。"""
    _run([FF, "-y", "-f", "lavfi",
          "-i", f"anoisesrc=c=pink:r=48000:a=0.003:d={seconds:.2f}",
          "-af", "bandpass=f=1200:width_type=h:w=2000,volume=-48dB",
          "-ar", "48000", dst])
    return dst


def master_mix(lines: list[dict], out_path: str, total_ms: int,
               me_path: str | None = None, work_dir: str = "/tmp") -> dict:
    """时间轴母带混音。lines=[{path,start_ms}]（已conditioning）。
    room-tone床 + 逐句adelay + amix → (可选M&E sidechain ducking) → loudnorm
    I=-13 TP=-1 LRA=6（竖屏手机端响度）。返回{path,dur_ms}。"""
    bed_len = total_ms / 1000 + 5.0
    bed = make_room_tone(os.path.join(work_dir, "room_tone.wav"), bed_len)
    script = os.path.join(work_dir, "mix_script.txt")
    parts = []
    mix_ins = ["-i", bed] + sum([["-i", l["path"]] for l in lines], [])
    labels = []
    for i, l in enumerate(lines):
        ms = max(int(l["start_ms"]), 0)
        parts.append(f"[{i+1}:a]adelay={ms}:all=1[a{i}]")
        labels.append(f"[a{i}]")
    parts.append(f"[0:a]{''.join(labels)}amix=inputs={len(lines)+1}:normalize=0[dlg]")
    if me_path:
        # P0修复(0905审计)：M&E是最后一个输入，索引必须动态算（此前写死[2:a]，
        # 多句项目里拿的是第二句台词做sidechain——M&E根本没进混音）
        me_idx = 1 + len(lines)
        parts.append("[dlg]asplit=2[d1][d2]")
        parts.append(f"[{me_idx}:a][d1]sidechaincompress=threshold=0.05:ratio=4.5:"
                     "attack=25:release=260:makeup=1.0:link=average[duck]")
        parts.append("[duck][d2]amix=inputs=2:duration=first:normalize=0[pre]")
    else:
        parts.append("[dlg]anull[pre]")
    parts.append("[pre]loudnorm=I=-13.0:TP=-1.0:LRA=6.0[out]")
    with open(script, "w") as f:
        f.write(";\n".join(parts))
    args = [FF, "-y"] + mix_ins
    if me_path:
        args += ["-i", me_path]
    args += ["-filter_complex_script", script, "-map", "[out]",
             "-ar", "48000", out_path]
    _run(args)
    # 两遍法：单遍loudnorm对短内容估偏低，先测量再线性应用锁定-13 LUFS
    r = subprocess.run([FF, "-i", out_path, "-af",
                        "loudnorm=I=-13.0:TP=-1.0:LRA=6.0:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    meas = {}
    import json as _json
    if r.stderr:
        tail = r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1]
        try:
            meas = _json.loads(tail)
        except Exception:                                    # noqa: BLE001
            meas = {}
    if {"input_i", "input_tp", "input_lra", "input_thresh"} <= meas.keys():
        args2 = [FF, "-y", "-i", out_path, "-af",
                 ("loudnorm=I=-13.0:TP=-1.0:LRA=6.0"
                  ":measured_I={input_i}:measured_TP={input_tp}"
                  ":measured_LRA={input_lra}:measured_thresh={input_thresh}"
                  ":linear=true:print_format=summary").format(**meas),
                 "-ar", "48000", out_path + ".n.wav"]
        _run(args2)
        os.replace(out_path + ".n.wav", out_path)
    return {"path": out_path, "dur_ms": ffprobe_ms(out_path)}
