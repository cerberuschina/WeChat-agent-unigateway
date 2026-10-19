"""开机一键起动网关这一套（网关 + claude 客户端）。

用法：双击 启动网关.cmd，或 python start_agent_stack.py

它会：
  1) 如果网关没在跑，就在 K:\\hermesWork 起网关（日志 data/gateway.log）
  2) 读 ~/.a2a-bridge/cc.env 的凭证、自动挑最新的 claude.exe
  3) 起 claude 客户端（长命会话模式，--cwd K:\\ClaudeWork，--reuse-token）
  4) 打印两边状态

注意：客户端需要 ANTHROPIC 凭证；凭证过期就重跑 cc_env.py 刷新。
"""
import json, os, shlex, subprocess, sys, time, urllib.request
from pathlib import Path

REPO = Path(r"K:\hermesWork")
SCRATCH = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "cache" / "scratch"
PY = sys.executable
BRIDGE = Path(r"C:\Users\Cerbe\a2a-bridge")


def ps(cmd):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""


def gateway_alive():
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        d = json.loads(op.open("http://127.0.0.1:18600/api/state", timeout=5).read().decode())
        return d.get("gateway", {}).get("running"), d.get("gateway", {})
    except Exception:
        return False, {}


def client_alive():
    n = ps('(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "ilink_agent_client" '
           '-and $_.ProcessId -ne $PID } | '
           'Measure-Object).Count').strip()
    try:
        return int(n) > 0
    except Exception:
        return False


def newest_claude_exe():
    root = Path(os.environ["LOCALAPPDATA"]) / "Claude-3p" / "claude-code"
    cands = sorted(root.glob("*/*/claude.exe"), key=lambda p: [int(x) for x in p.parts[-3].split(".") if x.isdigit()])
    return cands[-1] if cands else None


def read_env(path):
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    print("== 1) 网关 ==")
    alive, st = gateway_alive()
    if alive:
        print("   已在跑：", st.get("account"))
    else:
        print("   没在跑，启动中 …")
        log = open(REPO / "data" / "gateway.log", "a", encoding="utf-8")
        subprocess.Popen([PY, "-m", "agent_gateway", "--config", "gateway.json"], cwd=str(REPO),
                         stdout=log, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008)
        for _ in range(30):
            time.sleep(2)
            alive, st = gateway_alive()
            if alive:
                break
        print("   状态：", "已起来 " + str(st.get("account")) if alive else "没起来，去看 data/gateway.log")

    print("== 2) claude 客户端 ==")
    if client_alive():
        print("   已在跑，跳过")
        return
    exe = newest_claude_exe()
    creds = read_env(Path.home() / ".a2a-bridge" / "cc.env")
    if not exe or not creds.get("ANTHROPIC_AUTH_TOKEN"):
        print("   ✗ 缺东西：exe=", exe, "| 凭证键=", sorted(creds))
        print("     先跑：python C:/Users/Cerbe/a2a-bridge/cc_env.py 刷新凭证")
        return
    token_file = REPO / "data" / "claude-token.txt"
    token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
    note = ("有人通过微信给你发消息。发来图片/文件的本地路径会写在消息里，可以直接读。"
            "生成的图片或文件想发出去，就在回答最后单独一行写 `##FILE:<绝对路径>`（图片写 `##IMAGE:`）。")
    session_command = shlex.join([str(exe), "-p", "--input-format", "stream-json",
                                  "--output-format", "stream-json", "--verbose",
                                  "--permission-mode", "acceptEdits",
                                  "--add-dir", str(REPO / "data" / "media"),
                                  "--append-system-prompt", note])
    env = dict(os.environ)
    env.update(creds)
    env["CLAUDE_CODE_EXE"] = str(exe)
    log = open(REPO / "data" / "client-claude.log", "a", encoding="utf-8")
    log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start_agent_stack 启动客户端 ===\n")
    log.flush()
    cmd = [PY, "-u", str(REPO / "clients" / "ilink_agent_client.py"),
           "--name", "claude", "--base-url", "http://127.0.0.1:18500",
           "--creds", str(REPO / "data" / "virtual-client-claude.json"),
           "--cwd", r"K:\ClaudeWork", "--session-command", session_command]
    if token:
        cmd += ["--reuse-token", token]
    p = subprocess.Popen(cmd, cwd=r"K:\ClaudeWork", env=env, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008)
    time.sleep(12)
    print("   客户端 PID", p.pid, "| 还活着:", p.poll() is None)
    alive, st = gateway_alive()
    if alive:
        print("   网关视角：", [(a["name"], a["bound"], a.get("queue")) for a in st.get("agents", [])])


if __name__ == "__main__":
    main()
