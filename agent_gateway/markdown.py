"""Markdown → WeChat rendering, and splitting into sendable bubbles.

WeChat shows a text item verbatim: it renders no Markdown at all. So `**bold**`
arrives as literal asterisks, a table arrives as pipes, and a long answer is
rejected or mangled by the client's own limits. This module is the layer that
turns an agent's Markdown into something that *reads* correctly in WeChat:

* inline markers are stripped (``**x**`` → ``x``, backticks → plain), but only
  outside fenced code — code keeps every character it was written with;
* headings become ``【标题】``, bullets become ``·``, tables lose their dashes row
  and become readable rows, links become ``标题 (url)``;
* long answers are split into ≤ ``max_chars`` bubbles **at block boundaries**,
  never inside a code fence (a fence too long on its own is split by lines and
  re-opened with a fresh ``` on each part, so each bubble still reads as code).

Both functions are pure and dependency-free, which is why the tests can pin the
exact output.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# iLink chunks around 2048 chars; 1800 leaves room for the fence markers we add.
DEFAULT_MAX_CHARS = 1800

_FENCE_RE = re.compile(r"^(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _walk(content: str):
    """Yield ``(line, is_fence, in_code_block_before)`` — fence state is per line."""
    in_code = False
    for raw in content.splitlines():
        line = raw.rstrip()
        is_fence = bool(_FENCE_RE.match(line.strip()))
        yield line, is_fence, in_code
        if is_fence:
            in_code = not in_code


def _inline(text: str) -> str:
    """Strip the markers WeChat would otherwise show literally."""
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    return text


def render(content: str) -> str:
    """Markdown → the text WeChat should show."""
    if not content:
        return ""
    out: List[str] = []
    blank_run = 0
    table_rows: List[str] = []

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        for row in table_rows:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            out.append(" ｜ ".join(c for c in cells if c))
        table_rows = []

    for line, is_fence, in_code in _walk(content):
        if is_fence or in_code:
            flush_table()
            out.append(line)
            blank_run = 0
            continue

        stripped = line.strip()

        # tables: collect rows, drop the |---|---| rule, join cells with ｜
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            if _TABLE_RULE_RE.match(stripped):
                continue
            table_rows.append(line)
            continue
        flush_table()

        if not stripped:
            blank_run += 1
            if blank_run <= 1:
                out.append("")
            continue
        blank_run = 0

        heading = _HEADING_RE.match(stripped)
        if heading:
            out.append(f"【{_inline(heading.group(2).strip())}】")
            continue
        if _RULE_RE.match(stripped):
            out.append("—————")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            indent = " " * (len(bullet.group(1)) // 2 * 2)
            out.append(f"{indent}· {_inline(bullet.group(2))}")
            continue
        out.append(_inline(line))

    flush_table()
    return "\n".join(out).strip()


def _split_block(block: str, max_chars: int) -> List[str]:
    """Split one oversized block; inside a fence, re-open it on every bubble."""
    lines = block.splitlines()
    inside = bool(_FENCE_RE.match(lines[0].strip())) if lines else False
    fence = lines[0].strip()[:3] if inside else ""
    body = lines[1:] if inside else lines
    if inside and body and _FENCE_RE.match(body[-1].strip()):
        body = body[:-1]

    parts: List[str] = []
    current: List[str] = []
    limit = max_chars - (len(fence) + 1 if inside else 0)
    for line in body:
        candidate = sum(len(x) + 1 for x in current) + len(line) + 1
        if current and candidate > limit:
            parts.append("\n".join(current))
            current = []
        if len(line) + 1 > limit:              # a single monster line: hard-cut it
            if current:
                parts.append("\n".join(current))
                current = []
            parts.extend(line[i:i + limit] for i in range(0, len(line), limit))
            continue
        current.append(line)
    if current:
        parts.append("\n".join(current))
    if not inside:
        return parts
    return [f"{fence}\n{p}\n{fence}" for p in parts]


def split(content: str, max_chars: int = DEFAULT_MAX_CHARS) -> List[str]:
    """Split rendered text into bubbles, preferring blank-line boundaries."""
    if not content:
        return []
    if len(content) <= max_chars:
        return [content]

    blocks: List[str] = []
    current: List[str] = []
    for line, is_fence, in_code in _walk(content):
        if is_fence and not in_code:           # opening fence starts its own block
            if current:
                blocks.append("\n".join(current))
                current = []
        current.append(line)
        if is_fence and in_code:               # closing fence ends it
            blocks.append("\n".join(current))
            current = []
            continue
        if not in_code and not line.strip() and current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))

    bubbles: List[str] = []
    current_lines: List[str] = []
    used = 0
    for block in blocks:
        block = block.strip("\n")
        if not block:
            continue
        cost = len(block) + (2 if current_lines else 0)
        if current_lines and used + cost > max_chars:
            bubbles.append("\n\n".join(current_lines))
            current_lines, used = [], 0
        if cost > max_chars:
            if current_lines:
                bubbles.append("\n\n".join(current_lines))
                current_lines, used = [], 0
            bubbles.extend(_split_block(block, max_chars))
            continue
        current_lines.append(block)
        used += cost
    if current_lines:
        bubbles.append("\n\n".join(current_lines))
    return [b for b in bubbles if b.strip()]


def prepare(content: str, max_chars: int = DEFAULT_MAX_CHARS) -> List[str]:
    """The one call the gateway needs: render, then split."""
    return split(render(content), max_chars)
