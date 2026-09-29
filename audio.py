"""
ASMR-style soundtrack for a match - no music, just the physics.

History: v1 played a fixed melody an octave up with a hard attack ("sharp and
annoying"); v2 added CC0 background music, which the user then didn't want.
v3 (this) makes the collisions themselves the satisfying part:

  - bounce = a soft, crisp "tap" (band-limited click, no harsh top end) on top
    of a short rounded kalimba-like "plink" from a pentatonic scale,
  - loudness AND brightness follow the real impact speed, so hard hits are
    full and glancing touches are barely-there - that variation is what makes
    it feel physical rather than a looping sample,
  - every sound is panned to where it happens on screen (binaural-ish on
    headphones),
  - ball-on-ball is a glassy marble "clack"; a ring shattering is a soft
    granular crackle over a low sine "whump"; the win is a gentle chime.

Everything is synthesised in numpy: no samples, nothing to license.
"""
import random
import wave

import numpy as np

SR = 44100
OUT_PEAK = 0.89
LIFT = 1.26                                # +2 dB; bare transients measured -17 LUFS
KNEE = 0.70
KEYS = [48, 50, 51, 53, 55, 56, 57]       # C, D, Eb, F, G, Ab, A - varies per video


def _band(noise, lo, hi):
    spec = np.fft.rfft(noise)
    f = np.fft.rfftfreq(len(noise), 1 / SR)
    spec *= 1 / np.sqrt(1 + (lo / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / hi) ** 4)
    return np.fft.irfft(spec, len(noise))


def _lowpass(x, fc):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(spec / np.sqrt(1 + (f / fc) ** 4), len(x))


def _tone(freq, dur, partials, attack):
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for ratio, amp, decay in partials:
        f = freq * ratio
        if f < SR / 2:
            out += amp * np.sin(2 * np.pi * f * t) * np.exp(-decay * t)
    return out * (1 - np.exp(-t / attack))


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


class Bank:
    """Pre-rendered sound variants (random round-robin avoids the machine-gun effect)."""

    def __init__(self, rng):
        self.rng = rng
        n = np.random.default_rng(7)
        self.taps = []
        for _ in range(8):
            d = int(0.018 * SR)
            t = np.arange(d) / SR
            click = _band(n.standard_normal(d), 900, 4200) * np.exp(-t / 0.0022)
            self.taps.append(click / np.max(np.abs(click)))
        self.grains = []
        for _ in range(12):
            d = int(0.012 * SR)
            t = np.arange(d) / SR
            g = _band(n.standard_normal(d), 1800, 6500) * np.exp(-t / 0.0016)
            self.grains.append(g / np.max(np.abs(g)))
        self._tones = {}

    def tap(self):
        return self.taps[self.rng.randrange(len(self.taps))]

    def grain(self):
        return self.grains[self.rng.randrange(len(self.grains))]

    def plink(self, midi):
        # kalimba-ish: round fundamental, faint inharmonic tine partial, quick decay
        if midi not in self._tones:
            self._tones[midi] = _tone(_hz(midi), 0.55, [(1, 1.0, 11), (2.0, 0.10, 20), (5.9, 0.035, 45)],
                                      0.0025)
        return self._tones[midi]

    def clack(self, rngf):
        d = int(0.06 * SR)
        t = np.arange(d) / SR
        f1, f2 = 2300 * rngf, 3550 * rngf
        body = (np.sin(2 * np.pi * f1 * t) + 0.6 * np.sin(2 * np.pi * f2 * t)) * np.exp(-t / 0.011)
        return body * (1 - np.exp(-t / 0.0006)) * 0.55

    def chime(self, midi, dur=2.4):
        key = ("c", midi)
        if key not in self._tones:
            self._tones[key] = _tone(_hz(midi), dur, [(1, 1.0, 1.9), (2.0, 0.12, 3.5), (3.0, 0.03, 6)],
                                     0.012)
        return self._tones[key]


def _pan(x):
    """Screen x -> (left, right) constant-power gains, kept away from hard-pan."""
    p = max(-1.0, min(1.0, (x - 540) / 540)) * 0.75
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
    """Impact speed -> 0..1, with a soft curve so mid hits still read."""
    return float(np.clip((v - lo) / (hi - lo), 0, 1)) ** 0.8


def build(match, total_seconds, win_time, rng_seed=0):
    prng = random.Random(rng_seed)
    bank = Bank(prng)
    n = int((total_seconds + 0.5) * SR)
    L, R = np.zeros(n), np.zeros(n)

    tonic = KEYS[rng_seed % len(KEYS)]
    pent = [tonic + o * 12 + s for o in range(4) for s in (0, 2, 4, 7, 9)]
    registers = [[m for m in pent if 60 <= m <= 76], [m for m in pent if 67 <= m <= 84]]
    pos = [len(registers[0]) // 2, len(registers[1]) // 2]

    for e in match.events:
        if e.kind == "bounce":
            k = _energy(e.v, 250, 1300)
            reg = registers[e.ball]
            pos[e.ball] = min(len(reg) - 1, max(0, pos[e.ball] + prng.choice([-2, -1, -1, 1, 1, 2])))
            _add(L, R, e.t, bank.plink(reg[pos[e.ball]]), 0.10 + 0.22 * k, e.x)
            _add(L, R, e.t, bank.tap(), 0.05 + 0.20 * k * k, e.x)          # harder = crisper
        elif e.kind == "clash":
            k = _energy(e.v, 150, 1400)
            _add(L, R, e.t, bank.clack(prng.uniform(0.94, 1.06)), 0.10 + 0.28 * k, e.x)
            _add(L, R, e.t, bank.tap(), 0.06 + 0.12 * k, e.x)
        elif e.kind == "break":
            # soft glass crackle: grains scattered around the ring, dense then thinning
            for _ in range(46):
                dt = prng.expovariate(1 / 0.07)
                if dt > 0.45:
                    continue
                gx = 540 + prng.uniform(-1, 1) * match.rings[e.ring].radius
                _add(L, R, e.t + dt, bank.grain(), prng.uniform(0.03, 0.09) * (1 - dt / 0.5), gx)
            whump = _tone(_hz(tonic), 0.5, [(1, 1.0, 9), (2, 0.2, 14)], 0.006)
            _add(L, R, e.t, whump, 0.16 + 0.03 * e.ring, e.x)
            _add(L, R, e.t + 0.02, bank.chime(tonic + 36 + 12 * (e.ring >= 3)), 0.07, e.x)

    # win: gentle rising chime, spread across the stereo field
    for j, iv in enumerate((0, 4, 7, 12, 16)):
        _add(L, R, win_time + 0.12 + j * 0.13, bank.chime(tonic + 36 + iv), 0.12, 300 + j * 120)

    # small soft room: a few low-passed reflections, cross-fed between channels
    out = []
    for ch, other in ((L, R), (R, L)):
        wet = np.zeros_like(ch)
        for delay, g, src in ((0.023, 0.22, ch), (0.037, 0.18, other), (0.061, 0.13, ch),
                              (0.097, 0.09, other), (0.149, 0.05, ch)):
            d = int(delay * SR)
            wet[d:] += src[:-d] * g
        out.append(_lowpass(ch + _lowpass(wet, 3000), 9000))
    stereo = np.stack(out, axis=1)[: int(total_seconds * SR)]
    stereo = stereo / (np.max(np.abs(stereo)) or 1.0) * OUT_PEAK * LIFT
    # peak-only soft knee: the ~2 dB lift only touches the loudest transients
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
