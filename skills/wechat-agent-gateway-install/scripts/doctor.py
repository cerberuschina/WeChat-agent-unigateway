#!/usr/bin/env python3
"""Preflight and smoke test for a WeChat-agent-unigateway checkout.

Run it after cloning and again after configuring:

    python3 scripts/doctor.py --repo ~/WeChat-agent-unigateway
    python3 scripts/doctor.py --repo ~/WeChat-agent-unigateway --clone

Exit code 0 = the checkout can run here; 1 = something must be fixed first.
It never talks to Tencent and never reads data/account.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/cerberuschina/WeChat-agent-unigateway.git"
PORTS = (18500, 18600)
problems: list[str] = []


def report(ok: bool, name: str, detail: str = "", hint: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        problems.append(name)
        if hint:
            print(f"      → {hint}")


def warn(name: str, detail: str = "", hint: str = "") -> None:
    print(f"WARN  {name}" + (f"  — {detail}" if detail else ""))
    if hint:
        print(f"      → {hint}")


def check_python() -> None:
    version = ".".join(str(v) for v in sys.version_info[:3])
    report(sys.version_info >= (3, 9), "Python >= 3.9", version,
           "install Python 3.10+ and rerun with it")
    if (3, 9) <= sys.version_info < (3, 10):
        warn("Python 3.9", "runtime works, but tests/test_start_stack.py needs 3.10+")


def check_git() -> bool:
    ok = shutil.which("git") is not None
    report(ok, "git on PATH", shutil.which("git") or "not found",
           "install git, or download the repository zip from GitHub instead")
    return ok


def clone(repo: Path) -> None:
    if repo.exists():
        return
    repo.parent.mkdir(parents=True, exist_ok=True)
    print(f"      cloning {REPO_URL} -> {repo}")
    result = subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(repo)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        report(False, "git clone", result.stderr.strip()[-300:],
               "check network/proxy; GitHub over HTTPS must be reachable")


def check_checkout(repo: Path) -> None:
    looks_right = (repo / "agent_gateway" / "gateway.py").is_file() and (repo / "gateway.example.json").is_file()
    report(looks_right, "checkout looks complete", str(repo),
           "point --repo at the directory that contains agent_gateway/ and login.py")


def run_demo(repo: Path) -> None:
    demo = repo / "examples" / "virtual_loop_demo.py"
    if not demo.is_file():
        report(False, "offline demo present", str(demo))
        return
    try:
        result = subprocess.run([sys.executable, str(demo)], cwd=str(repo),
                                capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        report(False, "offline smoke test", "timed out after 120s")
        return
    ok = result.returncode == 0 and "全程" in result.stdout
    detail = "virtual iLink round trip OK" if ok else (result.stderr.strip() or result.stdout.strip())[-300:]
    report(ok, "offline smoke test (no WeChat, no network)", detail,
           "run `python3 examples/virtual_loop_demo.py` yourself to see the full trace")


def check_config(repo: Path) -> None:
    config = repo / "gateway.json"
    if not config.is_file():
        warn("gateway.json", "not created yet",
             f"cd {repo} && cp gateway.example.json gateway.json, then edit \"agents\"")
        return
    code = (
        "import json,sys\n"
        "from agent_gateway.config import load_config\n"
        "c=load_config('gateway.json')\n"
        "m=getattr(c,'memory',None)\n"
        "print(json.dumps({'agents':[a.name for a in c.enabled_agents()],"
        "'virtual':c.virtual.enabled,'memory':bool(getattr(m,'enabled',False))},"
        "ensure_ascii=False))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=str(repo),
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        report(False, "gateway.json parses", result.stderr.strip().splitlines()[-1][:300],
               "fix the JSON/schema error above")
        return
    data = json.loads(result.stdout.strip().splitlines()[-1])
    agents = data.get("agents") or []
    report(bool(agents), "gateway.json has an enabled agent", ", ".join(agents) or "none",
           "add an entry under \"agents\" (see gateway.example.json)")
    if data.get("virtual"):
        report(True, "virtual iLink enabled")
    else:
        warn("virtual iLink disabled", "only a2a/http/exec agents will work")


def check_account(repo: Path) -> None:
    account = repo / "data" / "account.json"
    if not account.is_file():
        warn("WeChat not bound yet", "data/account.json missing",
             f"cd {repo} && python3 login.py   (scan the QR with the WeChat that owns the bot)")
        return
    report(True, "WeChat credentials present", str(account))
    if os.name != "nt":
        mode = account.stat().st_mode & 0o777
        if mode & 0o077:
            warn("account.json permissions", oct(mode),
                 f"chmod 600 {account}  (it is a bot token)")


def port_state(port: int) -> str:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            pass
        else:
            return "free"
    # Somebody holds it: is it this gateway?
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/state" if port == 18600
                         else f"http://127.0.0.1:{port}/health", timeout=2) as response:
            if response.status == 200:
                return "gateway"
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return "busy"


def check_ports() -> None:
    for port in PORTS:
        state = port_state(port)
        if state == "free":
            report(True, f"port {port} free")
        elif state == "gateway":
            report(True, f"port {port} held by a running gateway",
                   "stop it before starting another instance")
        else:
            report(False, f"port {port} busy", "held by another program",
                   f"find the owner (lsof -i :{port} / netstat -ano | findstr :{port}) and free it")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Doctor for WeChat-agent-unigateway")
    parser.add_argument("--repo", default=str(Path.home() / "WeChat-agent-unigateway"))
    parser.add_argument("--clone", action="store_true", help="clone from GitHub if --repo is missing")
    parser.add_argument("--skip-demo", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()

    print(f"WeChat agent gateway doctor — repo: {repo}\n")
    check_python()
    have_git = check_git()
    if not repo.exists():
        if args.clone and have_git:
            clone(repo)
        else:
            report(False, "checkout exists", "missing", "pass --clone, or clone the repo yourself")
    if repo.exists():
        check_checkout(repo)
        if not args.skip_demo:
            run_demo(repo)
        check_config(repo)
        check_account(repo)
    check_ports()

    print()
    if problems:
        print(f"{len(problems)} problem(s) to fix: " + "; ".join(problems))
        return 1
    print("Everything essential checks out.")
    print("Next: start the gateway with `python3 -m agent_gateway --config gateway.json`,")
    print("then send the bot a WeChat message (or `/help`).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
