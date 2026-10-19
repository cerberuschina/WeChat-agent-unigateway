"""一键起动网关这一套（网关 + 可选的 claude 客户端）。

所有机器通用：仓库位置、端口、凭证、claude 可执行文件全部**自动发现**，脚本里
没有盘符、没有用户名、没有写死的安装路径。换机器只需把仓库拷过去，然后

    python start_agent_stack.py            # 网关没跑就起，再起 claude 客户端
    python start_agent_stack.py --check    # 只体检并打印结论，不动任何进程
    python start_agent_stack.py --gateway-only
    python start_agent_stack.py --client-only
    python start_agent_stack.py --claude-cwd D:/work     # 指定 claude 的工作目录（默认 ~）

Windows 下双击 ``启动网关.cmd``（等价于 ``python start_agent_stack.py``）。

自动发现顺序：

* 仓库    —— 本文件所在目录（跟着文件走，不认盘符）
* 端口    —— ``gateway.json`` 的 ``virtual.port`` / ``dashboard.port``
* claude  —— ``PATH`` → ``~/.local/bin`` → ``~/.claude/local`` →
             ``%LOCALAPPDATA%/Claude-3p/claude-code``（多版本时取最新的）
* 凭证    —— 进程环境 → ``~/.a2a-bridge/cc.env`` → ``~/a2a-bridge/cc.env``

客户端/网关状态都只看**应用层**：网关以 dashboard ``/api/state`` 是否 200 为准
（端口 LISTENING 不代表活着，半死进程照样占着端口），客户端以自己写的 pid 文件
为准，没有 pid 文件才退到命令行扫描。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
CONFIG = REPO / "gateway.json"
IS_WIN = platform.system() == "Windows"
PY = sys.executable

FALLBACK_VIRTUAL_PORT = 18500
FALLBACK_DASHBOARD_PORT = 18600

# cc.env / 环境里认这几个键；有 token 就算"凭证齐了"
CRED_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
             "ANTHROPIC_MODEL")


# ---------------------------------------------------------------- 子进程形态
def _detached() -> dict:
    """让子进程活过本脚本：Windows 脱离控制台，POSIX 另起会话。"""
    if IS_WIN:
        return {"creationflags": 0x00000008 | 0x00000200}   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    return {"start_new_session": True}


def _no_window() -> dict:
    """探测类子进程不该弹窗口。"""
    if IS_WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


# -------------------------------------------------------------------- 配置
def load_cfg(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or CONFIG).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def data_dir(cfg: dict) -> Path:
    """状态/日志目录：以配置里的 data_dir 为准（相对路径按仓库解析）。"""
    raw = str(cfg.get("data_dir") or "").strip()
    if not raw:
        return REPO / "data"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (REPO / path)


def virtual_port(cfg: dict) -> int:
    try:
        port = int((cfg.get("virtual") or {}).get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return port if 0 < port < 65536 else FALLBACK_VIRTUAL_PORT


def dashboard_port(cfg: dict) -> int:
    try:
        port = int((cfg.get("dashboard") or {}).get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return port if 0 < port < 65536 else FALLBACK_DASHBOARD_PORT


def virtual_base(cfg: dict) -> str:
    host = str((cfg.get("virtual") or {}).get("host") or "127.0.0.1").strip()
    return f"http://{host}:{virtual_port(cfg)}"


def dashboard_url(cfg: dict) -> str:
    return f"http://127.0.0.1:{dashboard_port(cfg)}"


# -------------------------------------------------------------------- 判活
def gateway_alive(cfg: dict) -> tuple[bool, dict]:
    """只认应用层应答：dashboard /api/state 能返回 JSON 且 running 为真。"""
    # 回环地址必须绕开系统代理（FlClash/TUN 会把 127.0.0.1 探成 502/超时）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        raw = opener.open(f"{dashboard_url(cfg)}/api/state", timeout=5).read()
        state = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 任何失败都等于"没起来"
        return False, {}
    gateway = state.get("gateway") or {}
    return bool(gateway.get("running")), gateway


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WIN:
        # PROCESS_QUERY_LIMITED_INFORMATION：只问在不在，不要杀权限
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _scan_client_pid() -> int | None:
    """没有 pid 文件时（例如别人手工起的客户端）按命令行扫一遍。"""
    try:
        if IS_WIN:
            query = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and "
                     "$_.CommandLine -match 'ilink_agent_client' } | "
                     "Select-Object -First 1 -ExpandProperty ProcessId")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=20, **_no_window()).stdout
            for line in out.splitlines():
                if line.strip().isdigit():
                    return int(line.strip())
            return None
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True,
                             errors="replace", timeout=20).stdout
        for line in out.splitlines():
            if "ilink_agent_client" in line and "ps -eo" not in line:
                head = line.strip().split(None, 1)[0]
                if head.isdigit():
                    return int(head)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def client_pid(cfg: dict) -> tuple[int | None, str]:
    """客户端是否在跑：先认自己写的 pid 文件，再退到进程扫描。"""
    pid_file = data_dir(cfg) / "client-claude.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if pid and pid_alive(pid):
        return pid, pid_file.name
    scanned = _scan_client_pid()
    if scanned:
        return scanned, "进程扫描"
    return None, ""


# ---------------------------------------------------------------- 自动发现
def read_env(path: Path) -> dict:
    """读 KEY=VALUE 形式的 env 文件（cc.env）：跳过注释/空行，去引号。"""
    env: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        env[key] = value.strip().strip('"').strip("'")
    return env


def find_creds(home: Path | None = None) -> tuple[dict, Path | None]:
    """ANTHROPIC 凭证：进程环境优先，其次 cc.env（两处常见位置），都没有就返回空。"""
    env = {k: v for k, v in ((k, os.environ.get(k)) for k in CRED_KEYS) if v}
    if env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"):
        return env, None
    root = home or Path.home()
    for candidate in (root / ".a2a-bridge" / "cc.env",
                      root / "a2a-bridge" / "cc.env"):
        if not candidate.is_file():
            continue
        parsed = read_env(candidate)
        if parsed.get("ANTHROPIC_AUTH_TOKEN") or parsed.get("ANTHROPIC_API_KEY"):
            return parsed, candidate
    return {}, None


def _version_key(path: Path) -> tuple:
    """按路径里的版本号排序：…/2.0.14/claude.exe 要比 …/2.0.9/ 新。"""
    for part in reversed(path.parts[:-1]):
        numbers = [int(chunk) for chunk in part.replace("-", ".").split(".") if chunk.isdigit()]
        if numbers:
            return (tuple(numbers), str(path))
    return ((), str(path))


def pick_newest(paths) -> Path | None:
    """多版本并存时取最新的那个；没有版本号的按路径序，结果稳定。"""
    candidates = [Path(p) for p in paths if Path(p).is_file()]
    return max(candidates, key=_version_key) if candidates else None


def find_claude_exe(home: Path | None = None, localappdata: str | None = None) -> Path | None:
    """找 claude：PATH 里优先；否则扫几处常见安装位置，多版本取最新。"""
    found = shutil.which("claude")
    if found:
        return Path(found)
    root = home or Path.home()
    local = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA")
    roots = [root / ".local" / "bin", root / ".claude" / "local"]
    if local:
        roots.append(Path(local) / "Claude-3p" / "claude-code")
    candidates = []
    for directory in roots:
        if directory.is_file():
            candidates.append(directory)
            continue
        if not directory.is_dir():
            continue
        for pattern in ("claude", "claude.exe", "*/claude", "*/claude.exe",
                        "*/*/claude", "*/*/claude.exe"):
            candidates.extend(directory.glob(pattern))
    return pick_newest(candidates)


# -------------------------------------------------------------------- 启动
def start_gateway(cfg: dict) -> bool:
    """起网关：stdout/stderr 进 gateway.out.log（未捕获异常的栈只出现在那里）。"""
    out = data_dir(cfg) / "gateway.out.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as log:
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start_agent_stack 启动网关 ===\n")
        log.flush()
        subprocess.Popen([PY, "-m", "agent_gateway", "--config", CONFIG.name],
                         cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT, **_detached())
    for _ in range(30):
        time.sleep(2)
        alive, _state = gateway_alive(cfg)
        if alive:
            return True
    return False


def _session_command(exe: Path, cfg: dict) -> str:
    """claude 的长命会话命令；客户端用 posix shlex 解析，所以用 shlex.join 拼。"""
    media = (data_dir(cfg) / "media").as_posix()   # 反斜杠会被 posix 分词吃掉，统一用 /
    note = ("有人通过微信给你发消息。发来图片/文件的本地路径会写在消息里，可以直接读。"
            "生成的图片或文件想发出去，就在回答最后单独一行写 `##FILE:<绝对路径>`（图片写 `##IMAGE:`）。")
    return shlex.join([str(exe), "-p", "--input-format", "stream-json",
                       "--output-format", "stream-json", "--verbose",
                       "--permission-mode", "acceptEdits",
                       "--add-dir", media, "--append-system-prompt", note])


def start_client(cfg: dict, exe: Path, creds: dict, cwd: Path) -> tuple[int | None, str]:
    """起 claude 客户端；返回 (pid, 失败原因)。客户端要扫码时不阻塞本脚本。"""
    ddir = data_dir(cfg)
    ddir.mkdir(parents=True, exist_ok=True)
    token_file = ddir / "claude-token.txt"
    cmd = [PY, "-u", str(REPO / "clients" / "ilink_agent_client.py"),
           "--name", "claude", "--base-url", virtual_base(cfg),
           "--creds", str(ddir / "virtual-client-claude.json"),
           "--cwd", str(cwd),
           "--session-command", _session_command(exe, cfg)]
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            cmd += ["--reuse-token", token]     # 复用网关给的身份，省一次扫码
    env = dict(os.environ)
    env.update({k: str(v) for k, v in creds.items()})
    env["CLAUDE_CODE_EXE"] = str(exe)
    out = ddir / "client-claude.log"
    with open(out, "a", encoding="utf-8") as log:
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start_agent_stack 启动客户端 ===\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                                stdout=log, stderr=subprocess.STDOUT, **_detached())
    pid_file = ddir / "client-claude.pid"
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(8)                                # 客户端连不上/要扫码时会自己说清楚
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        return None, f"启动后立刻退出了，看 {out.name}"
    return proc.pid, ""


# -------------------------------------------------------------------- 入口
def _report(cfg: dict, cwd: Path, exe: Path | None, creds: dict, creds_src: Path | None,
            alive: bool, state: dict, cpid: int | None, cpid_src: str) -> None:
    print(f"仓库        : {REPO}")
    print(f"配置        : {CONFIG.name}（虚拟 iLink {virtual_base(cfg)}，控制台 {dashboard_url(cfg)}）")
    print(f"数据目录    : {data_dir(cfg)}")
    account = state.get("account") or ""
    print(f"网关        : {'已在跑 · ' + account if alive else '没在跑'}")
    print(f"claude      : {exe if exe else '✗ 没找到（PATH / ~/.local/bin / %LOCALAPPDATA%\\Claude-3p）'}")
    if creds_src:
        print(f"凭证        : 来自 {creds_src}")
    elif creds:
        print("凭证        : 来自环境变量")
    else:
        print("凭证        : ✗ 没有（设 ANTHROPIC_AUTH_TOKEN，或放 ~/.a2a-bridge/cc.env）")
    print(f"客户端      : {f'已在跑 pid={cpid}（{cpid_src}）' if cpid else '没在跑'}")
    print(f"claude 目录 : {cwd}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="一键起动网关这一套（网关 + 可选的 claude 客户端）")
    parser.add_argument("--check", action="store_true", help="只体检并打印结论，不动任何进程")
    parser.add_argument("--gateway-only", action="store_true", help="只起网关，不起客户端")
    parser.add_argument("--client-only", action="store_true", help="只起客户端（网关需已在跑）")
    parser.add_argument("--claude-cwd", default=os.environ.get("CLAUDE_CWD", ""),
                        help="claude 的工作目录，默认 ~（也可用环境变量 CLAUDE_CWD）")
    args = parser.parse_args(argv)

    if not CONFIG.is_file():
        print(f"[x] 找不到 {CONFIG}。先把 gateway.example.json 复制成 gateway.json 并改好。")
        return 2

    cfg = load_cfg()
    cwd = Path(args.claude_cwd).expanduser() if args.claude_cwd else Path.home()
    exe = find_claude_exe()
    creds, creds_src = find_creds()
    alive, state = gateway_alive(cfg)
    cpid, cpid_src = client_pid(cfg)

    if args.check:
        _report(cfg, cwd, exe, creds, creds_src, alive, state, cpid, cpid_src)
        return 0

    if not args.client_only:
        if alive:
            print(f"网关已在跑（{state.get('account') or 'account ?'}），跳过启动")
        else:
            print("网关没在跑，启动中 …")
            alive = start_gateway(cfg)
            _, state = gateway_alive(cfg)
            print("  → " + (f"已起来 {state.get('account') or ''}" if alive
                            else f"没起来，看 {(data_dir(cfg) / 'gateway.out.log')}"))
            if not alive:
                return 1

    if not args.gateway_only:
        if cpid:
            print(f"客户端已在跑（pid={cpid}，{cpid_src}），跳过启动")
        elif not exe:
            print("✗ 找不到 claude 可执行文件，跳过客户端启动")
        elif not creds:
            print("✗ 没有 ANTHROPIC 凭证，跳过客户端启动")
        else:
            print(f"启动 claude 客户端（cwd={cwd}）…")
            pid, why = start_client(cfg, exe, creds, cwd)
            print(f"  → pid {pid} 已就绪" if pid else f"  ✗ {why}")

    alive, state = gateway_alive(cfg)
    cpid, cpid_src = client_pid(cfg)
    print()
    _report(cfg, cwd, exe, creds, creds_src, alive, state, cpid, cpid_src)
    if alive:
        agents = [(a.get("name"), a.get("bound"), a.get("queue")) for a in (state.get("agents") or [])]
        print(f"网关视角    : {agents}")
    return 0 if alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
