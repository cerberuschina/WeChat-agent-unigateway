"""Offline tests for the optional Electron/CDP bridge (no app, no network)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bridges import electron_cdp as bridge


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, payload: dict) -> Path:
        path = self.tmp / "profile.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_defaults_are_filled_in(self):
        path = self.write({"target_match": "wb", "composer_selector": "textarea",
                           "message_selector": ".msg"})
        profile = bridge.load_profile(path)
        self.assertEqual(profile["settle_seconds"], 1.5)
        self.assertEqual(profile["timeout"], 600)
        self.assertEqual(profile["send_selector"], "")
        self.assertEqual(profile["busy_selector"], "")

    def test_missing_required_field_is_a_readable_error(self):
        path = self.write({"target_match": "wb"})
        with self.assertRaises(bridge.BridgeError) as ctx:
            bridge.load_profile(path)
        self.assertIn("composer_selector", str(ctx.exception))

    def test_shipped_workbuddy_profile_is_valid(self):
        profile = bridge.load_profile(Path("bridges/profiles/workbuddy.json"))
        self.assertEqual(profile["port"], 9222)
        self.assertIn("composer_selector", profile)


class TargetTests(unittest.TestCase):
    def test_picks_by_url_or_title_case_insensitively(self):
        targets = [
            {"title": "Other", "url": "https://example.com", "webSocketDebuggerUrl": "ws://1"},
            {"title": "WorkBuddy", "url": "app://workbuddy/index.html", "webSocketDebuggerUrl": "ws://2"},
        ]
        self.assertEqual(bridge.pick_target(targets, "workbuddy")["webSocketDebuggerUrl"], "ws://2")
        self.assertEqual(bridge.pick_target(targets, "OTHER")["webSocketDebuggerUrl"], "ws://1")

    def test_no_match_lists_what_is_available(self):
        targets = [{"title": "A", "url": "u", "webSocketDebuggerUrl": "ws://1"}]
        with self.assertRaises(bridge.BridgeError) as ctx:
            bridge.pick_target(targets, "nope")
        self.assertIn("A", str(ctx.exception))


class SnippetTests(unittest.TestCase):
    def test_fill_embeds_the_text_as_a_json_literal(self):
        js = bridge.js_composer_fill("textarea", "", '带"引号"和\n换行')
        self.assertIn(json.dumps('带"引号"和\n换行'), js)
        self.assertNotIn("__TEXT__", js)
        self.assertIn("querySelector(\"textarea\")", js)

    def test_count_and_last_message_snippets_are_wellformed(self):
        self.assertIn("querySelectorAll", bridge.js_count_messages(".m"))
        self.assertIn("innerText", bridge.js_last_message(".m"))
        self.assertEqual(bridge.js_is_busy(""), "false")
        self.assertIn("querySelector", bridge.js_is_busy(".busy"))

    def test_missing_debug_port_says_how_to_start_the_app(self):
        with self.assertRaises(bridge.BridgeError) as ctx:
            bridge.list_targets(port=9, timeout=0.2)
        self.assertIn("remote-debugging-port", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
