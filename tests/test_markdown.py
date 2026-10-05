"""Markdown → WeChat rendering and bubble splitting (pure functions, exact output)."""
from __future__ import annotations

import unittest

from agent_gateway import markdown as md


class RenderTests(unittest.TestCase):
    def test_inline_markers_are_stripped(self):
        self.assertEqual(md.render("这是 **加粗** 和 *斜体* 和 `代码`"), "这是 加粗 和 斜体 和 代码")

    def test_headings_and_bullets_read_naturally(self):
        out = md.render("# 标题\n\n- 第一点\n- 第二点")
        self.assertEqual(out, "【标题】\n\n· 第一点\n· 第二点")

    def test_code_fence_keeps_every_character(self):
        src = "看这个：\n\n```python\nx = a ** b  # 不要动我\nprint('a*b')\n```\n"
        out = md.render(src)
        self.assertIn("x = a ** b  # 不要动我", out)
        self.assertIn("print('a*b')", out)
        self.assertIn("```python", out)

    def test_table_loses_the_dashes_row_and_reads_as_rows(self):
        src = "| 名称 | 值 |\n|---|---|\n| a | 1 |\n| b | 2 |"
        out = md.render(src)
        self.assertEqual(out, "名称 ｜ 值\na ｜ 1\nb ｜ 2")

    def test_links_keep_the_url(self):
        self.assertEqual(md.render("看 [文档](https://x.dev) 吧"), "看 文档 (https://x.dev) 吧")

    def test_rule_and_blank_runs_are_tidied(self):
        self.assertEqual(md.render("a\n\n\n\nb\n---\nc"), "a\n\nb\n—————\nc")

    def test_empty_input(self):
        self.assertEqual(md.render(""), "")


class SplitTests(unittest.TestCase):
    def test_short_content_is_one_bubble(self):
        self.assertEqual(md.split("短"), ["短"])

    def test_split_prefers_blank_lines_and_respects_the_limit(self):
        blocks = ["第一段" * 20, "第二段" * 20, "第三段" * 20]
        text = "\n\n".join(blocks)
        bubbles = md.split(text, max_chars=120)
        self.assertGreater(len(bubbles), 1)
        for bubble in bubbles:
            self.assertLessEqual(len(bubble), 120)
        self.assertEqual("".join(bubbles).replace("\n", ""), text.replace("\n", ""))

    def test_a_code_fence_is_never_cut_in_half_mid_fence(self):
        code = "\n".join(f"line {i} " + "x" * 40 for i in range(20))
        text = f"前言\n\n```python\n{code}\n```\n\n后记"
        bubbles = md.split(text, max_chars=200)
        for bubble in bubbles:
            if "```" in bubble:
                self.assertEqual(bubble.count("```") % 2, 0, f"围栏没闭合：{bubble[:60]!r}")

    def test_oversized_fence_is_split_by_lines_and_reopened(self):
        code = "\n".join(f"line {i} " + "y" * 50 for i in range(10))
        text = f"```\n{code}\n```"
        bubbles = md.split(text, max_chars=150)
        self.assertGreater(len(bubbles), 1)
        for bubble in bubbles:
            self.assertTrue(bubble.startswith("```"), bubble[:40])
            self.assertTrue(bubble.rstrip().endswith("```"), bubble[-40:])
            self.assertLessEqual(len(bubble), 150)

    def test_prepare_renders_then_splits(self):
        bubbles = md.prepare("**粗**\n\n" + "很长" * 1000, max_chars=100)
        self.assertGreater(len(bubbles), 1)
        self.assertNotIn("**", bubbles[0])

    def test_empty(self):
        self.assertEqual(md.split(""), [])
        self.assertEqual(md.prepare(""), [])


if __name__ == "__main__":
    unittest.main()
