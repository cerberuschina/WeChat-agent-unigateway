"""Backend adapters: A2A reply extraction and the local exec path."""
from __future__ import annotations

import sys
import unittest

from agent_gateway.backends import BackendError, _dotted, call_agent, call_exec, extract_a2a_reply
from agent_gateway.config import AgentConfig


class A2AExtractionTests(unittest.TestCase):
    def test_artifacts_are_the_answer(self):
        payload = {"jsonrpc": "2.0", "id": "1", "result": {
            "id": "t1", "status": {"state": "completed"},
            "artifacts": [{"artifactId": "a", "parts": [{"kind": "text", "text": "改好了"}]}]}}
        self.assertEqual(extract_a2a_reply(payload), "改好了")

    def test_multiple_artifacts_are_joined(self):
        payload = {"result": {"artifacts": [
            {"parts": [{"kind": "text", "text": "第一段"}]},
            {"parts": [{"kind": "text", "text": "第二段"}]}]}}
        self.assertEqual(extract_a2a_reply(payload), "第一段\n\n第二段")

    def test_status_message_is_the_fallback(self):
        payload = {"result": {"status": {"state": "completed",
                                        "message": {"role": "agent",
                                                    "parts": [{"kind": "text", "text": "没有 artifact"}]}}}}
        self.assertEqual(extract_a2a_reply(payload), "没有 artifact")

    def test_jsonrpc_error_becomes_a_backend_error(self):
        with self.assertRaises(BackendError) as ctx:
            extract_a2a_reply({"error": {"code": -32602, "message": "bad params"}})
        self.assertIn("-32602", str(ctx.exception))

    def test_empty_result_is_an_error_not_an_empty_reply(self):
        with self.assertRaises(BackendError):
            extract_a2a_reply({"result": {"status": {"state": "completed"}}})

    def test_failed_task_without_text_is_an_error(self):
        with self.assertRaises(BackendError) as ctx:
            extract_a2a_reply({"result": {"status": {"state": "failed"}}})
        self.assertIn("failed", str(ctx.exception))

    def test_camel_case_body_also_works(self):
        """Some peers answer with the raw task object instead of a JSON-RPC envelope."""
        payload = {"status": {"state": "completed"},
                   "artifacts": [{"parts": [{"text": "裸任务对象"}]}]}
        self.assertEqual(extract_a2a_reply(payload), "裸任务对象")


class ExecBackendTests(unittest.TestCase):
    def _agent(self, command, timeout=30.0):
        return AgentConfig(name="t", type="exec", command=command, timeout=timeout)

    def test_stdout_is_the_reply(self):
        agent = self._agent([sys.executable, "-c", "print('hello from exec')"])
        self.assertEqual(call_exec(agent, "ignored"), "hello from exec")

    def test_text_is_substituted_into_argv(self):
        agent = self._agent([sys.executable, "-c", "import sys;print(sys.argv[1])", "{text}"])
        self.assertEqual(call_exec(agent, "被替换进去了"), "被替换进去了")

    def test_text_goes_to_stdin_when_no_placeholder(self):
        agent = self._agent([sys.executable, "-c", "import sys;print(sys.stdin.read().strip())"])
        self.assertEqual(call_exec(agent, "从 stdin 进来"), "从 stdin 进来")

    def test_nonzero_exit_is_a_backend_error_with_stderr(self):
        agent = self._agent([sys.executable, "-c", "import sys;sys.stderr.write('炸了');sys.exit(3)"])
        with self.assertRaises(BackendError) as ctx:
            call_exec(agent, "x")
        self.assertIn("炸了", str(ctx.exception))

    def test_missing_binary_says_which_one(self):
        with self.assertRaises(BackendError) as ctx:
            call_exec(self._agent(["definitely-not-a-real-binary-xyz"]), "x")
        self.assertIn("definitely-not-a-real-binary-xyz", str(ctx.exception))

    def test_timeout_is_reported(self):
        agent = self._agent([sys.executable, "-c", "import time;time.sleep(5)"], timeout=0.5)
        with self.assertRaises(BackendError) as ctx:
            call_exec(agent, "x")
        self.assertIn("超时", str(ctx.exception))

    def test_call_agent_dispatches_by_type(self):
        agent = self._agent([sys.executable, "-c", "print('ok')"])
        self.assertEqual(call_agent(agent, "x"), "ok")

    def test_disabled_agent_refuses(self):
        agent = AgentConfig(name="off", type="exec", command=["echo"], enabled=False)
        with self.assertRaises(BackendError):
            call_agent(agent, "x")


class DottedPathTests(unittest.TestCase):
    def test_reads_nested_keys_and_lists(self):
        data = {"result": {"items": [{"text": "first"}]}}
        self.assertEqual(_dotted(data, "result.items.0.text"), "first")
        self.assertEqual(_dotted(data, ""), data)
        self.assertIsNone(_dotted(data, "result.nope"))


if __name__ == "__main__":
    unittest.main()
