"""The on-screen question must name the real win rule (autopilot 2026-11-26).

Since 22 Nov the winner is whoever breaks more rings, but the question still asked
"Which country escapes first?". The render default, the loop's current value and the
lever's control must all carry the same rule-true question.
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import render  # noqa: E402


def _load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)


class QuestionNamesTheRule(unittest.TestCase):
    def test_default_question_names_rings(self):
        self.assertIn("rings", render.HOOK_TEXT.lower())
        self.assertNotIn("first", render.HOOK_TEXT.lower())

    def test_loop_value_and_lever_control_match_the_default(self):
        cfg = _load("loop_config.json")["values"].get("hook_text")
        lever = [l for l in _load("levers.json")["levers"] if l["lever"] == "hook_text"][0]
        self.assertEqual(cfg, render.HOOK_TEXT)
        self.assertEqual(lever["control"], render.HOOK_TEXT)


if __name__ == "__main__":
    unittest.main()
