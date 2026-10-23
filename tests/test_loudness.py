"""
Two-pass loudness normalisation (growth-lab S3 audio_norm, MASTER_PLAN #13 / plan-escape #8)
and the tighter Escape gate window (upload_gate BOT_PROFILES["escape"] = -17..-15 LUFS).

    py -m unittest tests.test_loudness -v
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import audio_norm  # noqa: E402
import main  # noqa: E402
import quality  # noqa: E402
import render  # noqa: E402
import upload_gate  # noqa: E402


class TestNormalizeAudio(unittest.TestCase):
    def test_target_is_minus_16(self):
        self.assertEqual(render.LOUDNESS_TARGET, -16.0)

    def test_calls_audio_norm_with_the_escape_target_and_returns_the_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "audio.wav")
            with mock.patch.object(audio_norm, "normalize",
                                   return_value={"output_i": -16.0, "normalization_type": "linear"}) as n:
                out = render.normalize_audio(wav)
            self.assertEqual(n.call_args.args[0], wav)
            self.assertEqual(n.call_args.args[1], out)
            self.assertEqual(n.call_args.kwargs["target"], -16.0)
            self.assertNotEqual(out, wav)

    def test_falls_back_to_the_raw_mix_when_normalising_fails(self):
        # quality.LUFS_RANGE and the gate window stay the backstop for a raw mix
        with mock.patch.object(audio_norm, "normalize", side_effect=RuntimeError("ffmpeg died")):
            self.assertEqual(render.normalize_audio("x/audio.wav"), "x/audio.wav")

    @unittest.skipUnless(audio_norm.find_ffmpeg(), "ffmpeg not installed")
    def test_real_quiet_tone_lands_inside_the_gate_window(self):
        ff = audio_norm.find_ffmpeg()
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "audio.wav")
            subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "sine=frequency=660:duration=6:sample_rate=44100", "-af", "volume=-30dB",
                            wav], check=True)
            out = render.normalize_audio(wav)
            self.assertNotEqual(out, wav)
            got = audio_norm.measure(out, target=-16.0)["input_i"]
            lo, hi = upload_gate.profile_for(main.gate_profile(60))["lufs"]
            self.assertTrue(lo <= got <= hi, f"{got} LUFS outside {lo}..{hi}")


class TestGateWindow(unittest.TestCase):
    def test_gate_uses_the_escape_bot_profile(self):
        prof = main.gate_profile(42)
        self.assertEqual(prof["bot"], "escape")
        self.assertNotIn("lufs", prof)               # a bot-supplied lufs would override the window
        self.assertEqual(upload_gate.profile_for(prof)["lufs"], (-17.0, -15.0))
        self.assertEqual(prof["seconds"], 42)

    def test_gate_passes_that_profile_to_check(self):
        with mock.patch.object(upload_gate, "check", return_value={"ok": True}) as chk:
            main.gate("v.mp4", "cap", {"recent_captions": []}, 30)
        self.assertEqual(upload_gate.profile_for(chk.call_args.kwargs["profile"])["lufs"], (-17.0, -15.0))

    def test_quality_backstop_stays_wide(self):
        lo, hi = quality.LUFS_RANGE
        self.assertTrue(lo <= -17.0 and hi >= -15.0)


if __name__ == "__main__":
    unittest.main()
