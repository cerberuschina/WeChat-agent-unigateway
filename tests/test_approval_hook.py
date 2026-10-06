"""钩子本体：它必须能把一次工具调用变成「等你放行」，也必须能安全地弃权。

真正的端到端就是这里测的：起一个网关（内存里、临时端口），把钩子当子进程喂一份
PreToolUse 事件，另一头替用户回答，然后看钩子吐出的裁定。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "claude_approval_hook.py"

sys.path.insert(0, str(ROOT))

from agent_gateway.approvals import ALLOW, DENY, ApprovalBroker  # noqa: E402
from agent_gateway.virtual_ilink import VirtualILinkServer  # noqa: E402


def run_hook(event: dict, env: dict) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.update(env)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                          capture_output=True, text=True, encoding="utf-8", env=merged,
                          timeout=60)


def decision_of(proc: subprocess.CompletedProcess) -> dict:
    text = (proc.stdout or "").strip()
    if not text:
        return {}
    return json.loads(text)["hookSpecificOutput"]


class HookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.broker = ApprovalBroker(ttl=15)
        self.asked: list = []
        self.server = VirtualILinkServer(
            data_dir=Path(self._tmp.name), port=0,
            accept_tokens={"tok-claude": "claude"}, approvals=self.broker,
            on_approval=lambda bind, approval: self.asked.append(approval))
        self.server.start()
        self.env = {"AGW_APPROVAL_URL": self.server.base_url().rstrip("/"),
                    "AGW_TOKEN": "tok-claude", "AGW_PEER": "wx-user",
                    "AGW_APPROVAL_TTL": "15"}
        self.event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "npm test"}}

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def answer(self, decision: str, delay: float = 0.3) -> threading.Timer:
        def resolve():
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.asked:
                    self.broker.resolve(self.asked[0].id, decision, by="wx-user")
                    return
                time.sleep(0.05)

        timer = threading.Timer(delay, resolve)
        timer.start()
        return timer

    def test_a_yes_on_the_phone_becomes_an_allow(self):
        timer = self.answer(ALLOW)
        proc = run_hook(self.event, self.env)
        timer.join(timeout=12)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = decision_of(proc)
        self.assertEqual(info["permissionDecision"], "allow")
        self.assertIn("微信", info["permissionDecisionReason"])
        self.assertIn("npm test", self.asked[0].title)

    def test_a_no_on_the_phone_becomes_a_deny(self):
        timer = self.answer(DENY)
        proc = run_hook(self.event, self.env)
        timer.join(timeout=12)
        self.assertEqual(decision_of(proc)["permissionDecision"], "deny")

    def test_no_answer_means_deny(self):
        event = dict(self.event)
        env = dict(self.env, AGW_APPROVAL_TTL="1")
        proc = run_hook(event, env)
        self.assertEqual(decision_of(proc)["permissionDecision"], "deny")
        self.assertIn("超时", decision_of(proc)["permissionDecisionReason"])

    def test_tools_we_do_not_ask_about_are_left_alone(self):
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Read",
                         "tool_input": {"file_path": "a.py"}}, self.env)
        self.assertEqual(proc.stdout.strip(), "", "读取不该惊动人")
        self.assertEqual(self.asked, [])

    def test_a_session_without_our_env_is_not_broken(self):
        proc = run_hook(self.event, {"AGW_APPROVAL_URL": "", "AGW_TOKEN": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "", "弃权：交给普通的权限流程")
        self.assertEqual(self.asked, [])

    def test_an_unreachable_gateway_denies_rather_than_allows(self):
        env = dict(self.env, AGW_APPROVAL_URL="http://127.0.0.1:9")   # nothing listens
        proc = run_hook(self.event, env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(decision_of(proc)["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
