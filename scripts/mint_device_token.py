#!/usr/bin/env python3
"""给一台设备签发一个虚拟身份 token（agent@device），并可写进网关配置。

    python scripts/mint_device_token.py hermes mac-air --config config/gateway.json
    python scripts/mint_device_token.py hermes mac-air --remove --config config/gateway.json

做的事：
  1. 生成一个随机 token，写到 ``<data-dir>/<agent>-<device>-token.txt``（0600）；
  2. ``--config`` 给了就把 ``virtual.accept_tokens["<agent>@<device>"]`` 指向它
     （没给就只打印一段可以直接粘贴的 JSON）；
  3. 改完要重启网关才生效。

token 文件的位置由 ``--data-dir`` 决定，默认取配置里的 ``data_dir``（相对配置
文件解析，和网关自己的规矩一致）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

INSTANCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="签发 agent@device 的虚拟 token")
    parser.add_argument("agent", help="agent 名字（gateway.json 里 agents 的一个键）")
    parser.add_argument("device", help="设备名，例如 mac-air / win59 / vps（同一 agent 可多台）")
    parser.add_argument("--config", default="", help="写进这个 gateway.json；不给就只打印")
    parser.add_argument("--data-dir", default="", help="token 文件放哪（默认配置里的 data_dir）")
    parser.add_argument("--token-prefix", default="",
                        help="写进配置的路径前缀（在容器里跑网关时用 /app/data/，脚本在宿主机上跑）")
    parser.add_argument("--remove", action="store_true", help="撤销：删掉这条 accept_tokens 并删 token 文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = parser.parse_args(argv)

    agent = args.agent.strip().lower()
    device = args.device.strip()
    if not INSTANCE_RE.match(device):
        print("设备名只能是字母、数字、'.', '_', '-'（≤32）", file=sys.stderr)
        return 2
    key = f"{agent}@{device}"

    raw = None
    cfg_path = Path(args.config).expanduser() if args.config else None
    if cfg_path:
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"找不到配置：{cfg_path}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"配置不是合法 JSON：{exc}", file=sys.stderr)
            return 2
        agents = [str(n).lower() for n in (raw.get("agents") or {})]
        if agent not in agents:
            print(f"配置里没有 agent '{agent}'；先用 join.py 或手工加一条 agents.{agent}", file=sys.stderr)
            return 2
        data_dir = Path(args.data_dir) if args.data_dir else Path(str((raw.get("data_dir")) or "data"))
        if not data_dir.is_absolute():
            data_dir = cfg_path.parent / data_dir
    else:
        data_dir = Path(args.data_dir or "data").expanduser()

    token_path = data_dir / f"{agent}-{device}-token.txt"

    if args.remove:
        if cfg_path and raw is not None:
            token_path.unlink(missing_ok=True)
            accept = ((raw.get("virtual") or {}).get("accept_tokens") or {})
            removed = accept.pop(key, None)
            if not removed:
                print(f"{key} 本来就不在配置里", file=sys.stderr)
                return 1
            if not args.dry_run:
                cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
            print(f"已撤销 {key}；重启网关后这台设备的 token 会得到 ret=-14。")
            return 0
        print(f"撤销需要 --config（并会删 {token_path}）", file=sys.stderr)
        return 2

    token = secrets.token_urlsafe(32)
    prefix = args.token_prefix or str(token_path.parent)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    source = f"file:{prefix}{token_path.name}"
    if cfg_path and raw is not None:
        accept = raw.setdefault("virtual", {}).setdefault("accept_tokens", {})
        accept[key] = source
        if not args.dry_run:
            data_dir.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token + "\n", encoding="utf-8")
            os.chmod(token_path, 0o600)
            cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        print(f"{key}: 已写入 {cfg_path}，token 文件 {token_path}（重启网关生效）")
    else:
        print(json.dumps({"virtual": {"accept_tokens": {key: source}}},
                         ensure_ascii=False, indent=2))
        print(f"\ntoken（放进 {token_path}，权限 600）：\n{token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
