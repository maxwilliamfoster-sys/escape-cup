"""
Soundtrack for a match: a CC0 background track + soft sound effects in its key.

v1 played a fixed public-domain melody an octave up with a hard 4 ms attack and
bright overtones; it was "sharp and annoying". v2 (this):
  - background music from assets/music (OpenGameArt, CC0 only - see tracks.json),
  - every bounce is a soft mallet note (slow attack, almost no upper partials,
    low-passed) chosen from the TRACK'S pentatonic scale, so the plinks sit in
    tune with whatever is playing. Each ball has its own register and wanders
    by small steps, so the two teams sound different but never clash,
  - ring breaks are a warm chime chord plus a low-passed "whoosh", not white noise,
  - the win fanfare is an arpeggio in the same key.
"""
import json
import os
import random
import subprocess
import wave

import numpy as np

SR = 44100
ROOT = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(ROOT, "assets", "music")
MUSIC_GAIN = 0.50          # music bed relative to the effects bus (after both are normalised)
OUT_RMS = 0.13             # ~ -14 LUFS, where TikTok normalises anyway


def tracks():
    with open(os.path.join(MUSIC_DIR, "tracks.json"), encoding="utf-8") as f:
        return json.load(f)


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def _tone(freq, dur, partials, attack):
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for ratio, amp, decay in partials:
        f = freq * ratio
        if f < SR / 2:
            out += amp * np.sin(2 * np.pi * f * t) * np.exp(-decay * t)
    return out * (1 - np.exp(-t / attack))          # smooth exponential attack, no click


# Soft mallet: strong fundamental, a whisper of octave, a faint marimba-like 4th partial.
MALLET = [(1, 1.0, 4.2), (2, 0.10, 8.0), (3.98, 0.035, 16.0)]
CHIME = [(1, 1.0, 2.0), (2.0, 0.22, 3.4), (3.0, 0.07, 5.0)]


def _lowpass(x, fc):
    """Gentle 2nd-order-like low-pass on a whole bus via FFT (no scipy needed)."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= 1 / np.sqrt(1 + (f / fc) ** 4)
    return np.fft.irfft(spec, len(x))


def _add(buf, start, clip, gain):
    i = int(start * SR)
    if i >= len(buf) or i < 0:
        return
    n = min(len(clip), len(buf) - i)
    buf[i:i + n] += clip[:n] * gain


def _pentatonic(tonic_pc, mode, lo, hi):
    major_tonic = (tonic_pc + 3) % 12 if mode == "minor" else tonic_pc
    pcs = {(major_tonic + s) % 12 for s in (0, 2, 4, 7, 9)}
    return [n for n in range(lo, hi + 1) if n % 12 in pcs], major_tonic


def _whoosh(rng, dur=0.5):
    n = int(dur * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    env = (1 - np.exp(-t / 0.03)) * np.exp(-t / 0.12)
    return noise * env


def _load_music(track, seconds):
    raw = subprocess.run([_ffmpeg(), "-loglevel", "error", "-i", os.path.join(MUSIC_DIR, track["file"]),
                          "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    # skip a quiet intro: start at the first second that is at least 60% of median loudness
    sec = SR
    rms = np.array([np.sqrt(np.mean(x[i:i + sec] ** 2)) for i in range(0, len(x) - sec, sec)])
    start = 0
    if len(rms):
        med = np.median(rms)
        loud = np.nonzero(rms >= 0.6 * med)[0]
        start = int(loud[0]) * sec if len(loud) else 0
    x = x[start:]
    need = int(seconds * SR)
    if len(x) < need:                    # loop with a 1 s crossfade
        xf = SR
        out = x.copy()
        while len(out) < need:
            ramp = np.linspace(0, 1, xf)
            out[-xf:] = out[-xf:] * (1 - ramp) + x[:xf] * ramp
            out = np.concatenate([out, x[xf:]])
        x = out
    x = x[:need]
    fade_in, fade_out = int(0.12 * SR), int(1.4 * SR)
    x[:fade_in] *= np.linspace(0, 1, fade_in)
    x[-fade_out:] *= np.linspace(1, 0, fade_out)
    return x


def _ffmpeg():
    from render import ffmpeg_bin
    return ffmpeg_bin()


def _norm_rms(x, target):
    rms = np.sqrt(np.mean(x ** 2)) or 1.0
    return x * (target / rms)


def build(match, track, total_seconds, win_time, rng_seed=0):
    rng = np.random.default_rng(rng_seed)
    prng = random.Random(rng_seed)
    fx = np.zeros(int((total_seconds + 0.5) * SR))

    tonic, mode = track["tonic"], track["mode"]
    low_notes, major_tonic = _pentatonic(tonic, mode, 55, 74)     # ball A: warm register
    high_notes, _ = _pentatonic(tonic, mode, 64, 86)              # ball B: brighter register
    registers = [low_notes, high_notes]
    pos = [len(low_notes) // 2, len(high_notes) // 2]
    cache = {}

    def note(midi, dur=1.4, partials=MALLET, attack=0.010):
        key = (midi, dur, id(partials), attack)
        if key not in cache:
            cache[key] = _tone(_hz(midi), dur, partials, attack)
        return cache[key]

    for e in match.events:
        if e.kind == "bounce":
            reg = registers[e.ball]
            # small melodic steps read as a tune; big leaps read as random
            pos[e.ball] = min(len(reg) - 1, max(0, pos[e.ball] + prng.choice([-2, -1, -1, 1, 1, 2])))
            _add(fx, e.t, note(reg[pos[e.ball]]), 0.26 * prng.uniform(0.75, 1.0))
        elif e.kind == "clash":
            _add(fx, e.t, note(major_tonic + 48, 0.25, [(1, 1.0, 22)], 0.004), 0.10)   # soft wooden tock
        elif e.kind == "break":
            root = major_tonic + 72 - 12 * (e.ring < 3)
            for k, iv in enumerate((0, 4, 7, 12)):
                _add(fx, e.t + k * 0.045, note(root + iv, 2.0, CHIME, 0.006), 0.10)
            _add(fx, e.t, _whoosh(rng), 0.10 + 0.02 * e.ring)

    root = major_tonic + 60
    for k, iv in enumerate((0, 4, 7, 12, 16)):
        _add(fx, win_time + 0.10 + k * 0.10, note(root + iv, 1.6, MALLET, 0.008), 0.24)
    for iv in (0, 4, 7, 12):
        _add(fx, win_time + 0.65, note(root + iv, 2.8, CHIME, 0.01), 0.10)

    fx = _lowpass(fx, 3800)
    # short room reflections so notes bloom instead of sounding dry
    wet = np.zeros_like(fx)
    for delay, g in ((0.031, 0.28), (0.053, 0.21), (0.083, 0.15), (0.127, 0.10), (0.181, 0.06)):
        d = int(delay * SR)
        wet[d:] += fx[:-d] * g
    fx = _norm_rms(fx + _lowpass(wet, 2500), 0.12)

    music = _load_music(track, len(fx) / SR)
    music = _norm_rms(music, 0.12) * MUSIC_GAIN
    out = _norm_rms(fx + music[: len(fx)], OUT_RMS)
    # soft knee on peaks only: everything under the knee is untouched, so the
    # mix stays open instead of being squashed flat
    knee = 0.80
    mag = np.abs(out)
    over = mag > knee
    out[over] = np.sign(out[over]) * (knee + (1 - knee) * np.tanh((mag[over] - knee) / (1 - knee)))
    return out[: int(total_seconds * SR)]


def write_wav(samples, path):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())
