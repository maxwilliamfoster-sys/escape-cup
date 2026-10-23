# vendored from growth-lab/audio_norm.py @ 460d0a43dd36de57fbf3cb904bd92ca88a91001d
# Do not edit here: change the growth-lab master and run sync_vendored.py.
"""audio_norm: two-pass LINEAR loudness normalisation (growth-lab S3, master copy).

Edit it HERE, then roll it out with sync_vendored.py. Stdlib + the ffmpeg binary.

    import audio_norm
    res = audio_norm.normalize("mix.wav", "mix_norm.wav", target=-14)   # chess/space -14, escape -16
    # res: {"input_i", "output_i", "gain_db", "limited", "normalization_type", ...}

How it works
------------
1. Measure: `loudnorm=I=<target>:TP=-1.5:LRA=11:print_format=json` into a null muxer.
2. If the static gain needed (target - measured I) is more than +6 dB, run a
   peak limiter FIRST (alimiter, auto-level off) with its ceiling set so the
   gain cannot push peaks past TP, then measure again. Without this, loudnorm's
   linear mode silently falls back to dynamic (pumping) mode on spiky audio.
3. Apply: the same loudnorm with the measured_* values, offset and linear=true
   (one constant gain, hits the target within a fraction of a LU), then
   `aresample=48000` (loudnorm resamples to 192 kHz internally).

The result says which mode loudnorm really used ("linear" or "dynamic"), so a
caller or test can catch a fallback. Silent input (I <= -70) is only resampled.

Ported from Chamanrajragu/purffle-shorts, purffle_shorts/render.py::mix_audio
and purffle_shorts/ffmpeg.py::loudnorm_report (the two-pass pattern and the
report parser), under the MIT licence:

    MIT License

    Copyright (c) 2025 Purffle

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""
import json
import os
import shutil
import subprocess
import sys

TARGET = -14.0
TRUE_PEAK = -1.5
LRA = 11
SAMPLE_RATE = 48000
LIMIT_ABOVE_DB = 6.0      # static gain above this gets a limiter first
LIMIT_MARGIN_DB = 1.0     # alimiter is sample-peak; leave room for inter-sample peaks
ALIMITER_FLOOR_DB = -24.0  # alimiter's smallest ceiling is 0.0625 (-24.08 dBFS)
LIMIT_ROUNDS = 4          # limiter re-aims (each one is a measuring pass)
SILENCE_I = -70.0

WINDOWS_FFMPEG = "C:/ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe"
_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


def find_ffmpeg():
    """$FFMPEG, then ffmpeg on PATH, then Max's Windows install."""
    env = os.environ.get("FFMPEG")
    if env:
        return env
    return shutil.which("ffmpeg") or (WINDOWS_FFMPEG if os.path.exists(WINDOWS_FFMPEG) else None)


def find_ffprobe(ffmpeg=None):
    ffmpeg = ffmpeg or find_ffmpeg() or "ffmpeg"
    d, name = os.path.split(ffmpeg)
    probe = os.path.join(d, name.replace("ffmpeg", "ffprobe")) if d else None
    return probe if probe and os.path.exists(probe) else (shutil.which("ffprobe") or "ffprobe")


def loudnorm_report(stderr):
    """The JSON block that loudnorm=print_format=json writes at the end of a run."""
    end = stderr.rfind("}")
    start = stderr.rfind("{", 0, end)
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(stderr[start:end + 1])
    except ValueError:
        return None


def _target(target, tp, lra):
    return f"I={target:g}:TP={tp:g}:LRA={lra:g}"


def static_gain(measured, target=TARGET):
    return float(target) - float(measured["input_i"])


def needs_limiter(measured, target=TARGET, above_db=LIMIT_ABOVE_DB):
    return static_gain(measured, target) > above_db


def limiter_filter(gain_db, tp=TRUE_PEAK, margin_db=LIMIT_MARGIN_DB):
    """Peak limiter whose ceiling keeps peaks under `tp` after `gain_db` of static
    gain. Runs in double precision so nothing clips before it. For gains so big
    that the ceiling would fall below alimiter's floor (near-silent input), a
    fixed pre-gain brings the signal up to the floor first."""
    ceiling_db = tp - float(gain_db) - margin_db
    head = "aformat=sample_fmts=dbl,"
    if ceiling_db >= ALIMITER_FLOOR_DB:
        return f"{head}alimiter=limit={10 ** (ceiling_db / 20):.5f}:level=0"
    pre = ALIMITER_FLOOR_DB - ceiling_db
    return f"{head}volume={pre:.2f}dB,alimiter=limit=0.0625:level=0"


def apply_filter(measured, target=TARGET, tp=TRUE_PEAK, lra=LRA, sample_rate=SAMPLE_RATE):
    m = measured
    # af_loudnorm treats measured_LRA == 0 as "not given" and then silently uses
    # dynamic mode. A steady tone or drone really measures 0.00 LU, so pass a
    # hair above it (seen with ffmpeg 8.1.1 on a sine, 2026-10-23).
    m_lra = m["input_lra"] if float(m["input_lra"]) > 0 else 0.01
    return (f"loudnorm={_target(target, tp, lra)}:measured_I={m['input_i']}:"
            f"measured_TP={m['input_tp']}:measured_LRA={m_lra}:"
            f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
            f"linear=true:print_format=json,aresample={sample_rate}")


def _run(ffmpeg, args):
    p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({p.returncode}): {p.stderr[-600:]}")
    return p.stderr


def measure(path, target=TARGET, tp=TRUE_PEAK, lra=LRA, pre_filter=None, ffmpeg=None):
    """loudnorm's first-pass numbers as floats (input_i, input_tp, input_lra,
    input_thresh, target_offset). Raises RuntimeError if they are missing."""
    ffmpeg = ffmpeg or find_ffmpeg() or "ffmpeg"
    af = f"loudnorm={_target(target, tp, lra)}:print_format=json"
    if pre_filter:
        af = f"{pre_filter},{af}"
    err = _run(ffmpeg, ["-i", str(path), "-vn", "-af", af, "-f", "null", "-"])
    rep = loudnorm_report(err) or {}
    try:
        return {k: float(rep[k]) for k in _KEYS}
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"no loudnorm report for {path}: {err[-300:]}")


def _codec_args(dst, keep_video):
    ext = os.path.splitext(str(dst))[1].lower()
    if ext == ".wav":
        a = ["-c:a", "pcm_s16le"]
    elif ext == ".mp3":
        a = ["-c:a", "libmp3lame", "-b:a", "192k"]
    elif ext == ".flac":
        a = ["-c:a", "flac"]
    else:
        a = ["-c:a", "aac", "-b:a", "192k"]
    maps = ["-map", "0:a:0"]
    if keep_video:
        maps += ["-map", "0:v?", "-c:v", "copy"]
        if ext in (".mp4", ".m4a", ".mov"):
            a += ["-movflags", "+faststart"]
    else:
        maps += ["-vn"]
    return maps + a + ["-ar", str(SAMPLE_RATE)]


def normalize(src, dst, target=TARGET, tp=TRUE_PEAK, lra=LRA, keep_video=False, ffmpeg=None,
              log=None):
    """Normalise `src` to `target` LUFS (two-pass linear) into `dst` at 48 kHz.
    keep_video=True copies the video stream untouched (for a finished MP4).
    Returns a dict describing what was done."""
    ffmpeg = ffmpeg or find_ffmpeg() or "ffmpeg"
    log = log or (lambda *_: None)
    first = measure(src, target, tp, lra, ffmpeg=ffmpeg)
    res = {"target": target, "input_i": first["input_i"], "input_tp": first["input_tp"],
           "gain_db": round(static_gain(first, target), 2), "limited": False}
    if first["input_i"] <= SILENCE_I:
        _run(ffmpeg, ["-i", str(src), "-af", f"aresample={SAMPLE_RATE}",
                      *_codec_args(dst, keep_video), str(dst)])
        res.update(normalization_type="skipped_silence", output_i=None)
        return res

    pre, measured = None, first
    if needs_limiter(first, target):
        # Limiting loud spikes lowers the integrated loudness, which raises the
        # gain still needed, so re-aim the ceiling until peaks + gain fit under TP.
        gain = static_gain(first, target)
        for _ in range(LIMIT_ROUNDS):
            pre = limiter_filter(gain, tp)
            measured = measure(src, target, tp, lra, pre_filter=pre, ffmpeg=ffmpeg)
            new_gain = static_gain(measured, target)
            if measured["input_tp"] + new_gain <= tp:
                break
            gain = max(new_gain, gain + 0.5)
        res["limited"] = True
        res["limiter"] = pre
        log(f"[audio_norm] +{res['gain_db']:.1f} dB needed: limiter first ({pre})")

    af = apply_filter(measured, target, tp, lra)
    if pre:
        af = f"{pre},{af}"
    err = _run(ffmpeg, ["-i", str(src), "-af", af, *_codec_args(dst, keep_video), str(dst)])
    rep = loudnorm_report(err) or {}
    res["normalization_type"] = rep.get("normalization_type", "unknown")
    try:
        res["output_i"] = float(rep.get("output_i"))
        res["output_tp"] = float(rep.get("output_tp"))
    except (TypeError, ValueError):
        res["output_i"] = None
    log(f"[audio_norm] {src}: {first['input_i']:.1f} -> {res.get('output_i')} LUFS "
        f"({res['normalization_type']})")
    return res


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Two-pass linear loudnorm to 48 kHz")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--target", type=float, default=TARGET)
    ap.add_argument("--keep-video", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(normalize(a.src, a.dst, a.target, keep_video=a.keep_video, log=print)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
