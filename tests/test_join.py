"""join.py：把一个 agent 接进网关的「一条命令」。

全是离线测试：临时目录当 WORLD，不碰真网关、不联网（探活那两处被 mock 掉）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_gateway.config import ConfigError, VirtualConfig, load_config, read_token_source
from agent_gateway import join
from agent_gateway.join import JoinError, Options, World, apply_plan, build_plan


def base_config() -> dict:
    return {
        "account": {"account_id": "x@im.bot", "token": "real-token",
                    "base_url": "https://ilinkai.weixin.qq.com"},
        "data_dir": "data",
        "default_agent": "claude",
        "virtual": {"enabled": True, "host": "127.0.0.1", "port": 18500,
                    "auto_approve": ["claude"]},
        "agents": {"claude": {"type": "a2a", "label": "Claude Code", "prefix": "c",
                              "url": "http://127.0.0.1:8799"}},
    }


class TempWorld:
    """一个临时「本机」：gateway.json + HOME 都在 tmp 里。"""

    def __init__(self, config: dict | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        wb_home = root / ".workbuddy"
        self.world = World(config_path=root / "gateway.json", home=root, project=root,
                           workbuddy_home=wb_home)
        self.world.config_path.write_text(
            json.dumps(config if config is not None else base_config(), ensure_ascii=False),
            encoding="utf-8")
        wb_home.mkdir(parents=True, exist_ok=True)                     # 假装 WorkBuddy 装过

    def cleanup(self) -> None:
        self._tmp.cleanup()


class TokenSourceTest(unittest.TestCase):
    def test_literal(self):
        self.assertEqual(read_token_source("tok-123"), "tok-123")

    def test_env(self):
        os.environ["JOIN_TEST_TOKEN"] = "from-env"
        try:
            self.assertEqual(read_token_source("env:JOIN_TEST_TOKEN"), "from-env")
        finally:
            os.environ.pop("JOIN_TEST_TOKEN", None)

    def test_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.txt"
            path.write_text("  from-file\n", encoding="utf-8")
            self.assertEqual(read_token_source(f"file:{path}"), "from-file")

    def test_missing_and_empty(self):
        self.assertEqual(read_token_source(""), "")
        self.assertEqual(read_token_source("file:/definitely/not/here"), "")
        self.assertEqual(read_token_source("env:JOIN_TEST_NOT_SET"), "")


class VirtualConfigTokensTest(unittest.TestCase):
    def test_accept_tokens_merge_with_legacy_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "hermes.txt"
            legacy.write_text("hermes-token", encoding="utf-8")
            minted = Path(tmp) / "wb.txt"
            minted.write_text("wb-token", encoding="utf-8")
            cfg = VirtualConfig(reuse_real_token_for="hermes", reuse_token_file=str(legacy),
                                accept_tokens={"workbuddy": "file:" + str(minted)})
            tokens = cfg.reuse_tokens("own-token")
            self.assertEqual(tokens["hermes-token"], "hermes")
            self.assertEqual(tokens["wb-token"], "workbuddy")
            # 两边都没给来源时，legacy 槽退回「网关自己那个 token」
            bare = VirtualConfig(reuse_real_token_for="hermes")
            self.assertEqual(bare.reuse_tokens("own-token"), {"own-token": "hermes"})
            self.assertEqual(VirtualConfig().reuse_tokens("own-token"), {})

    def test_file_source_is_resolved_against_the_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "wb-token.txt").write_text("wb-token", encoding="utf-8")
            raw = base_config()
            raw["agents"]["workbuddy"] = {"type": "virtual", "label": "W", "prefix": "w"}
            raw["virtual"]["accept_tokens"] = {"workbuddy": "file:data/wb-token.txt"}
            path = root / "gateway.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.virtual.reuse_tokens("other"), {"wb-token": "workbuddy"})

    def test_load_config_reads_the_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = base_config()
            raw["agents"]["workbuddy"] = {"type": "virtual", "label": "WorkBuddy", "prefix": "w"}
            raw["virtual"]["accept_tokens"] = {"WorkBuddy": "env:SOMETHING"}
            path = Path(tmp) / "gateway.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.virtual.accept_tokens, {"workbuddy": "env:SOMETHING"})

    def test_unknown_agent_in_map_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = base_config()
            raw["virtual"]["accept_tokens"] = {"ghost": "file:x"}
            path = Path(tmp) / "gateway.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class BuildPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorld()
        self.world = self.tmp.world

    def tearDown(self):
        self.tmp.cleanup()

    def test_workbuddy_is_a_token_join(self):
        plan = build_plan("workbuddy", self.world, Options())
        self.assertEqual(plan.type, "virtual")
        self.assertEqual(plan.prefix, "w")
        self.assertEqual(plan.token_path, self.world.data_dir / "workbuddy-token.txt")
        self.assertFalse(plan.auto_approve)
        self.assertIn("virtual.accept_tokens.workbuddy", "\n".join(plan.changes(base_config())))

    def test_claude_defaults_to_the_a2a_bridge(self):
        plan = build_plan("claude", self.world, Options())
        self.assertEqual(plan.entry["type"], "a2a")
        self.assertEqual(plan.entry["url"], "http://127.0.0.1:8799")

    def test_claude_can_run_as_a_cli(self):
        plan = build_plan("claude", self.world, Options(mode="exec"))
        self.assertEqual(plan.entry["command"], ["claude", "-p", "{text}"])

    def test_generic_exec_needs_a_command(self):
        with self.assertRaises(JoinError):
            build_plan("mycli", self.world, Options(mode="exec"))
        plan = build_plan("mycli", self.world,
                          Options(mode="exec", command=["my-cli", "--ask", "{text}"]))
        self.assertEqual(plan.prefix, "m")
        self.assertEqual(plan.entry["command"], ["my-cli", "--ask", "{text}"])

    def test_generic_http_needs_template_and_path(self):
        with self.assertRaises(JoinError):
            build_plan("myapi", self.world, Options(mode="http", url="http://127.0.0.1:9000"))
        plan = build_plan("myapi", self.world, Options(
            mode="http", url="http://127.0.0.1:9000", body_template='{"q": "{text}"}',
            reply_path="reply"))
        self.assertEqual(plan.entry["reply_path"], "reply")

    def test_generic_virtual_qr_goes_into_auto_approve(self):
        plan = build_plan("nono", self.world, Options(mode="virtual-qr"))
        self.assertTrue(plan.auto_approve)
        self.assertIsNone(plan.token_path)

    def test_rejects_nonsense(self):
        with self.assertRaises(JoinError):
            build_plan("who", self.world, Options(mode="telepathy"))
        with self.assertRaises(JoinError):
            build_plan("bad name", self.world, Options(mode="exec", command=["x"]))


class ApplyPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorld()
        self.world = self.tmp.world
        self.before = json.loads(self.world.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_touches_nothing(self):
        plan = build_plan("workbuddy", self.world, Options())
        lines = apply_plan(plan, self.world, restart=False, dry_run=True)
        self.assertIn("[dry-run] gateway.json 一个字节都没动", "\n".join(lines))
        self.assertEqual(json.loads(self.world.config_path.read_text(encoding="utf-8")),
                         self.before)
        self.assertFalse(self.world.token_path("workbuddy").exists())

    def test_apply_registers_agent_token_and_settings(self):
        plan = build_plan("workbuddy", self.world, Options())
        apply_plan(plan, self.world, restart=False)

        raw = json.loads(self.world.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["agents"]["workbuddy"]["type"], "virtual")
        self.assertEqual(raw["virtual"]["accept_tokens"],
                         {"workbuddy": "file:data/workbuddy-token.txt"})
        cfg = load_config(self.world.config_path)                    # 写出来的东西能被读回
        self.assertEqual(cfg.virtual.reuse_tokens("own"), {
            self.world.token_path("workbuddy").read_text(encoding="utf-8").strip(): "workbuddy"})

        token = self.world.token_path("workbuddy").read_text(encoding="utf-8").strip()
        self.assertGreaterEqual(len(token), 24)
        settings = json.loads(
            (self.world.home / ".workbuddy" / "settings.json").read_text(encoding="utf-8"))
        channel = settings["claw"]["channels"]["weixinClawBot"]
        self.assertEqual(channel["baseUrl"], "http://127.0.0.1:18500")
        self.assertEqual(channel["botToken"], token)
        self.assertTrue(channel["enabled"])

    def test_apply_keeps_the_existing_workbuddy_file(self):
        settings_path = self.world.home / ".workbuddy" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"claw": {"channels": {"wechatmp": {"enabled": True}},
                                                      "users": {}}}), encoding="utf-8")
        plan = build_plan("workbuddy", self.world, Options())
        apply_plan(plan, self.world, restart=False)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertIn("wechatmp", settings["claw"]["channels"])      # 别的渠道没被动
        backups = list(settings_path.parent.glob("settings.json.bak-join-*"))
        self.assertEqual(len(backups), 1)

    def test_apply_is_idempotent(self):
        for _ in range(2):
            apply_plan(build_plan("ilink", self.world, Options()), self.world, restart=False)
        raw = json.loads(self.world.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["virtual"]["auto_approve"].count("ilink"), 1)
        self.assertEqual(raw["agents"]["ilink"]["type"], "virtual")

    def test_mint_token_reuses_the_existing_file(self):
        first, created = join.mint_token(self.world.token_path("x"))
        second, created_again = join.mint_token(self.world.token_path("x"))
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorld()
        self.world = self.tmp.world

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_says_offline_without_a_gateway(self):
        with mock.patch.object(join, "gateway_health", return_value=None):
            text = "\n".join(join.status_lines(self.world))
        self.assertIn("没在跑", text)
        self.assertIn("claude", text)

    def test_status_shows_binds_when_up(self):
        plan = build_plan("workbuddy", self.world, Options())
        apply_plan(plan, self.world, restart=False)
        with mock.patch.object(join, "gateway_health",
                               return_value={"ok": True, "binds": ["workbuddy"]}), \
             mock.patch.object(join, "gateway_binds",
                               return_value=[{"name": "workbuddy", "account_id": "virt-wb@im.bot",
                                              "queue": 0}]):
            text = "\n".join(join.status_lines(self.world))
        self.assertIn("在跑", text)
        self.assertIn("virt-wb@im.bot", text)

    def test_no_gateway_listening_means_no_pids(self):
        self.assertEqual(join.find_gateway_pids(1), [])


class CliTest(unittest.TestCase):
    def test_list_and_status_run_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway.json"
            path.write_text(json.dumps(base_config()), encoding="utf-8")
            with mock.patch.object(join, "gateway_health", return_value=None):
                self.assertEqual(join.main(["--config", str(path), "--list"]), 0)
                self.assertEqual(join.main(["--config", str(path), "--status"]), 0)

    def test_dry_run_through_the_cli(self):
        tmp = TempWorld()
        try:
            code = join.main(["--config", str(tmp.world.config_path), "workbuddy",
                              "--dry-run", "--no-restart"])
            self.assertEqual(code, 0)
            self.assertNotIn("workbuddy",
                             json.loads(tmp.world.config_path.read_text(encoding="utf-8"))["agents"])
        finally:
            tmp.cleanup()

    def test_unknown_option_is_reported_not_raised(self):
        tmp = TempWorld()
        try:
            self.assertEqual(join.main(["--config", str(tmp.world.config_path), "x",
                                        "--mode", "telepathy"]), 2)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
