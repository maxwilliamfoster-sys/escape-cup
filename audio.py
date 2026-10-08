"""
Soundtrack for a match - no music, just soft collision sounds.

History (all user feedback / data):
  v1 fixed melody, octave up, hard attack            -> "sharp and annoying"
  v2 + CC0 background music                          -> not wanted
  v3 crisp click taps + glassy clacks (1-4 kHz)      -> "still too sharp, hurting my ears"
  v4 felt tones 130-440 Hz, low-passed at 1.8 kHz    -> soft, BUT 68% of the energy sat
     below 300 Hz, which phone speakers barely play: on most phones the videos were
     near-silent, and this genre lives on its sound (2026-10-01, 0 likes, viewers
     gone at 0:01).
  v5 (this): the same softness rules - no clicks, swelled attacks, nothing designed
     above ~1.4 kHz, mix steeply low-passed at 2.5 kHz - but the tones sit in
     500-1400 Hz, the range phone speakers reproduce well:
       - bounce: a round "bloop" (sine with a quick downward pitch glide), loudness
         from the real impact speed, panned to where it happens,
       - ring break: the next note UP a pentatonic ladder as rings fall - the rising
         pitch is the genre's signature build-up,
       - ball contact: a soft wooden knock (~600 Hz),
       - win: a gentle rising chime.
Everything is synthesised in numpy: no samples, nothing to license.
"""
import random
import wave

import numpy as np

SR = 44100
OUT_PEAK = 0.89
LIFT = 1.5                                  # +3.5 dB: sparse transients measured -19 LUFS; the knee catches peaks
KNEE = 0.72
KEYS = [60, 62, 63, 65, 67]                # C, D, Eb, F, G (MIDI, octave 4) - varies per video


def _lowpass(x, fc):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(spec / np.sqrt(1 + (f / fc) ** 4), len(x))


def _highpass(x, fc):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(spec / np.sqrt(1 + (fc / np.maximum(f, 1)) ** 4), len(x))


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def _tone(freq, dur, partials, attack, glide=0.0, glide_t=0.04):
    """Additive tone. glide: start this fraction above freq and fall to it (a 'bloop')."""
    t = np.arange(int(dur * SR)) / SR
    bend = 1 + glide * np.exp(-t / glide_t)
    phase = 2 * np.pi * np.cumsum(freq * bend) / SR
    out = np.zeros_like(t)
    for ratio, amp, decay in partials:
        if freq * ratio * (1 + glide) < SR / 2:
            out += amp * np.sin(phase * ratio) * np.exp(-decay * t)
    return out * (1 - np.exp(-t / attack))


BLOOP = [(1, 1.0, 16), (2, 0.06, 26)]
MARIMBA = [(1, 1.0, 6.5), (4.0, 0.05, 30)]
CHIME = [(1, 1.0, 2.6), (2.0, 0.08, 4.5)]


class Bank:
    def __init__(self):
        self._c = {}

    def bloop(self, midi):
        key = ("b", midi)
        if key not in self._c:
            self._c[key] = _tone(_hz(midi), 0.35, BLOOP, 0.004, glide=0.22, glide_t=0.03)
        return self._c[key]

    def note(self, midi):
        key = ("n", midi)
        if key not in self._c:
            self._c[key] = _tone(_hz(midi), 0.9, MARIMBA, 0.005)
        return self._c[key]

    def knock(self):
        if "k" not in self._c:
            self._c["k"] = _tone(600, 0.12, [(1, 1.0, 40), (1.5, 0.3, 55)], 0.003)
        return self._c["k"]

    def chime(self, midi):
        key = ("c", midi)
        if key not in self._c:
            self._c[key] = _tone(_hz(midi), 2.0, CHIME, 0.02)
        return self._c[key]


def _pan(x):
    """Screen x -> (left, right) constant-power gains, kept away from hard-pan."""
    p = max(-1.0, min(1.0, (x - 540) / 540)) * 0.7
    a = (p + 1) * np.pi / 4
    return np.cos(a), np.sin(a)


def _add(L, R, start, clip, gain, x=540.0):
    i = int(start * SR)
    if i < 0 or i >= len(L):
        return
    n = min(len(clip), len(L) - i)
    gl, gr = _pan(x)
    L[i:i + n] += clip[:n] * gain * gl
    R[i:i + n] += clip[:n] * gain * gr


def _energy(v, lo, hi):
    return float(np.clip((v - lo) / (hi - lo), 0, 1)) ** 0.8


def build(match, total_seconds, win_time, rng_seed=0):
    prng = random.Random(rng_seed)
    bank = Bank()
    n = int((total_seconds + 0.5) * SR)
    L, R = np.zeros(n), np.zeros(n)

    tonic = KEYS[rng_seed % len(KEYS)]
    pent = [tonic + o * 12 + s for o in range(3) for s in (0, 2, 4, 7, 9)]
    bounce_notes = [m for m in pent if 72 <= m <= 84]           # ~520-1050 Hz
    ladder = [m for m in pent if 72 <= m <= 89]                 # ~520-1400 Hz, rises with rings
    n_rings = max(e.ring for e in match.events if e.kind == "break") + 1 if any(
        e.kind == "break" for e in match.events) else 1

    many = getattr(match, "n_balls", 2) > 2
    last_bounce = -1.0
    for e in match.events:
        if e.kind == "bounce":
            # 16 balls bounce constantly: thin to one sound per 60 ms or it turns to noise
            if many and e.t - last_bounce < 0.06:
                continue
            last_bounce = e.t
            k = _energy(e.v, 250, 1300) * (0.7 if many else 1.0)
            midi = bounce_notes[prng.randrange(len(bounce_notes))]
            _add(L, R, e.t, bank.bloop(midi), 0.05 + 0.13 * k, e.x)
        elif e.kind == "clash":
            if many:
                continue                                  # constant contact in a crowd: skip
            k = _energy(e.v, 150, 1400)
            _add(L, R, e.t, bank.knock(), 0.06 + 0.12 * k, e.x)
        elif e.kind == "break":
            step = round(e.ring * (len(ladder) - 1) / max(1, n_rings - 1))
            _add(L, R, e.t, bank.note(ladder[step]), 0.30, e.x)
            _add(L, R, e.t + 0.01, bank.note(ladder[step] - 12), 0.10, e.x)   # warm octave below

    for j, iv in enumerate((0, 4, 7, 12)):
        _add(L, R, win_time + 0.10 + j * 0.12, bank.chime(tonic + 12 + iv), 0.16, 340 + j * 130)

    out = []
    for ch, other in ((L, R), (R, L)):
        wet = np.zeros_like(ch)
        for delay, g, src in ((0.023, 0.20, ch), (0.037, 0.16, other), (0.061, 0.11, ch),
                              (0.097, 0.07, other)):
            d = int(delay * SR)
            wet[d:] += src[:-d] * g
        mix = ch + _lowpass(wet, 2200)
        mix = _lowpass(_lowpass(mix, 2500), 2500)    # steep: nothing sharp survives
        out.append(_highpass(mix, 150))               # no inaudible rumble eating headroom
    stereo = np.stack(out, axis=1)[: int(total_seconds * SR)]
    stereo = stereo / (np.max(np.abs(stereo)) or 1.0) * OUT_PEAK * LIFT
    mag = np.abs(stereo)
    over = mag > KNEE
    stereo[over] = np.sign(stereo[over]) * (KNEE + (OUT_PEAK - KNEE) * np.tanh((mag[over] - KNEE) / (OUT_PEAK - KNEE)))
    return stereo


def write_wav(samples, path):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    if pcm.ndim == 1:
        pcm = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
