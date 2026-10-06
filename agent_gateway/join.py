"""把一个 agent 接进网关 —— 一条命令，每种 agent 一个 profile。

为什么是「一个引擎 + 一堆 profile」而不是每个 agent 一个脚本：**网关那一侧的动作
对所有 agent 都一样**（登记 agent → 开一条进来的路 → 重启 → 验证），不一样的只有
agent 自己那一侧（它的配置长什么样、要不要重启它）。所以差异被收进 profile，新增
一种 agent = 加一个 profile，不是再抄一份脚本。

四种接入方式（写进 gateway.json 的 ``agents.<name>.type``）
---------------------------------------------------------
``exec``          网关主动调它：agent 有命令行（``claude -p`` / ``codebuddy -p`` / 任意 CLI）
``a2a``           网关主动调它：agent 已经有 A2A 端点（a2a-bridge 包的 Claude / Hermes）
``http``          网关主动调它：agent 只有一个普通 JSON 接口
``virtual-token`` 网关不调它，它自己来拉。它**改不了**自己的扫码地址（WorkBuddy 的
                  微信助理把取码 URL 写死在腾讯），但它读自己配置里的 base-url →
                  给它一个**网关签发**的 token，把 base-url 指到网关。
                  token 落在 ``data/<name>-token.txt``，网关按 ``virtual.accept_tokens`` 认。
``virtual-qr``    同上，但对方**能**改 base-url（Hermes 的 ``WEIXIN_BASE_URL``、本仓库的
                  ``clients/ilink_agent_client.py``）→ 让它自己来取码，网关批准即可。

用法（命令行入口是同名根目录脚本 ``join.py``）::

    python join.py --list
    python join.py workbuddy                 # 登记 + 写它的配置 + 重启网关
    python join.py claude --mode a2a --url http://127.0.0.1:8799
    python join.py mycli --mode exec --command "agent-cli --prompt {text}" --prefix m
    python join.py myapi --mode http --url http://127.0.0.1:9000/ask \
        --body-template '{"q": "{text}"}' --reply-path reply
    python join.py --status
    python join.py workbuddy --dry-run       # 只打印要改什么，不落盘
    python join.py workbuddy --no-restart    # 改配置但不重启网关
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import ConfigError, load_config

DEFAULT_CONFIG = "gateway.json"
_TOKEN_CHARS = 32


class JoinError(RuntimeError):
    """接入失败：缺参数、配置坏了、对方没装等等。"""


# --------------------------------------------------------------------- 文件


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise JoinError(f"找不到文件：{path}")
    except json.JSONDecodeError as exc:
        raise JoinError(f"{path} 不是合法 JSON：{exc}")


def _write_json(path: Path, data: Dict[str, Any], *, backup: bool = True) -> Optional[Path]:
    """原子写 + 先备份；返回备份路径（没备份时 None）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    bak: Optional[Path] = None
    if backup and path.exists():
        bak = path.with_name(f"{path.name}.bak-join-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, bak)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return bak


def _mask(token: str) -> str:
    return f"{token[:6]}…{len(token)}ch" if len(token) > 8 else "(短)"


def mint_token(path: Path, *, keep: bool = True) -> "tuple[str, bool]":
    """生成/读取 ``data/<name>-token.txt``。返回 (token, 是否新建)。"""
    if keep and path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token, False
    token = secrets.token_urlsafe(_TOKEN_CHARS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:                                        # 有就收紧权限，没有也不报错
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token, True


# --------------------------------------------------------------------- 世界


@dataclass
class World:
    """这次操作看到的本机环境（测试里换掉它就能全套离线跑）。"""

    config_path: Path
    home: Path = field(default_factory=Path.home)
    project: Path = field(default_factory=lambda: Path.cwd())
    # WorkBuddy 的配置目录；None = 看环境变量 WORKBUDDY_HOME，再看 ~/.workbuddy
    workbuddy_home: Optional[Path] = None

    @property
    def data_dir(self) -> Path:
        return self.config_path.parent / "data"

    def token_path(self, name: str) -> Path:
        return self.data_dir / f"{name}-token.txt"


# --------------------------------------------------------------------- 计划


@dataclass
class Options:
    """命令行传进来的覆盖项。"""

    mode: str = ""
    label: str = ""
    prefix: str = ""
    url: str = ""
    command: List[str] = field(default_factory=list)
    body_template: str = ""
    reply_path: str = ""
    timeout: int = 900


@dataclass
class Plan:
    """「接一个 agent」要做的全部事情，先算出来，再决定落不落盘。"""

    name: str
    mode: str
    label: str
    prefix: str
    entry: Dict[str, Any]
    why: str
    notes: List[str] = field(default_factory=list)
    token_path: Optional[Path] = None
    auto_approve: bool = False
    client: Optional[Callable[["Plan", World, bool], List[str]]] = None
    client_preview: str = ""
    gateway_url: str = ""

    @property
    def type(self) -> str:
        return str(self.entry.get("type") or self.mode)

    def changes(self, raw: Dict[str, Any]) -> List[str]:
        """人要看的「这次要改什么」。"""
        out = [f"agents.{self.name} = {json.dumps(self.entry, ensure_ascii=False)}"]
        virt = (raw.get("virtual") or {})
        if self.token_path is not None:
            rel = _rel(self.token_path, self.token_path.parent.parent)
            out.append(f"virtual.accept_tokens.{self.name} = \"file:{rel}\"  （token 落在 {self.token_path}）")
        if self.auto_approve:
            names = list(virt.get("auto_approve") or [])
            if self.name not in names:
                out.append(f"virtual.auto_approve += \"{self.name}\"")
        if self.token_path is not None and not virt.get("enabled"):
            out.append("virtual.enabled = true（虚拟 iLink 服务得开着）")
        if self.client_preview:
            out.append(self.client_preview)
        return out


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


# --------------------------------------------------------------------- profile


@dataclass
class Profile:
    name: str
    label: str
    prefix: str
    mode: str
    summary: str
    build: Callable[[Plan, World, Options], None]
    extra_modes: "tuple[str, ...]" = ()


PROFILES: Dict[str, Profile] = {}


def _register(profile: Profile) -> Profile:
    PROFILES[profile.name] = profile
    return profile


# ---- 各类 profile 的构造函数 ------------------------------------------------


def _backend_entry(plan: Plan, opts: Options) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"type": plan.mode, "label": plan.label, "prefix": plan.prefix}
    if plan.mode in ("a2a", "http"):
        if not opts.url:
            raise JoinError(f"'{plan.name}' 用 {plan.mode} 接入必须给 --url")
        entry["url"] = opts.url
        entry["timeout"] = opts.timeout
    if plan.mode == "http":
        if not opts.body_template or not opts.reply_path:
            raise JoinError("http 接入要给 --body-template 和 --reply-path")
        entry["body_template"] = opts.body_template
        entry["reply_path"] = opts.reply_path
    if plan.mode == "exec":
        if not opts.command:
            raise JoinError(f"'{plan.name}' 用 exec 接入必须给 --command")
        entry["command"] = list(opts.command)
        entry["timeout"] = opts.timeout
    return entry


def _virtual(plan: Plan, world: World, *, token: bool, auto: bool) -> None:
    plan.entry = {"type": "virtual", "label": plan.label, "prefix": plan.prefix}
    if token:
        plan.token_path = world.token_path(plan.name)
    plan.auto_approve = auto


# ---- WorkBuddy -------------------------------------------------------------


def workbuddy_settings_path(world: World) -> Path:
    """WorkBuddy 的 claw 渠道配置（存在这里就是「它读的那份」）。

    优先级：调用方显式指定 → 环境变量 ``WORKBUDDY_HOME`` → ``~/.workbuddy``。
    """
    base = world.workbuddy_home
    if base is None:
        env = os.environ.get("WORKBUDDY_HOME")
        base = Path(env).expanduser() if env else world.home / ".workbuddy"
    return base / "settings.json"


def _client_workbuddy(plan: Plan, world: World, apply: bool) -> List[str]:
    """把虚拟凭证写进 WorkBuddy 的 claw 渠道配置（``claw.channels.weixinClawBot``）。

    实测（5.6.2）：它自己的取码 URL 写死在 ``ilinkai.weixin.qq.com``，改不了；但渠道
    配置里的 ``baseUrl`` 会被 ``resolveAccount`` 读到，之后所有收发都走它 —— 所以给它
    一个网关签发的 token + 指向网关的 baseUrl，它就以普通 ClawBot 客户端的身份连上来了。
    """
    path = workbuddy_settings_path(world)
    token = ""
    if plan.token_path is not None and plan.token_path.exists():
        token = plan.token_path.read_text(encoding="utf-8").strip()
    lines = [f"WorkBuddy：写 {path} → claw.channels.weixinClawBot"]
    if not path.parent.exists():
        return lines + [f"  ! 没找到 {path.parent}（WorkBuddy 装过、跑过一次才会有）；"
                        f"这一侧要你手动补"]
    account = f"virt-{plan.name}@im.bot"
    config = {
        "enabled": True,
        "botToken": token,
        "accountId": account,
        "channelId": account,
        "baseUrl": plan.gateway_url or "",
    }
    if not apply:
        return lines + [f"  · 会写入 baseUrl={config['baseUrl']}、botToken=***{_mask(token)[-4:]}"]
    raw = _read_json(path) if path.exists() else {}
    claw = raw.setdefault("claw", {})
    claw.setdefault("channels", {})["weixinClawBot"] = config
    bak = _write_json(path, raw)
    lines.append(f"  · 已写（备份 {bak.name if bak else '无'}）")
    lines.append("  · 重启 WorkBuddy 后生效（微信助理 不用再扫码；" 
                 "若你后来又在界面上扫码，它会把这份配置覆盖回腾讯的绑定）")
    return lines


def _build_workbuddy(plan: Plan, world: World, opts: Options) -> None:
    _virtual(plan, world, token=True, auto=False)
    plan.client = _client_workbuddy
    plan.client_preview = f"写 {workbuddy_settings_path(world)} 的 claw.channels.weixinClawBot"
    plan.notes.append("WorkBuddy 只有桌面版：真要「网关调它」这条路得走 "
                      "bridges/electron_cdp.py（见 docs/BACKENDS-WORKBUDDY.md）")


_register(Profile(
    name="workbuddy", label="WorkBuddy", prefix="w", mode="virtual-token",
    summary="桌面版改不了取码地址，但它读渠道配置里的 baseUrl → 发它一个网关 token",
    extra_modes=("exec",),
    build=_build_workbuddy,
))


# ---- Hermes（它自己就是本机在跑的那个微信渠道）------------------------------


def _client_hermes(plan: Plan, world: World, apply: bool) -> List[str]:
    """Hermes 的微信渠道：base-url 可覆盖（环境变量），但扫码两步写死腾讯。"""
    host_port = plan.gateway_url or ""
    lines = [
        "Hermes 侧（两种都行）：",
        f"  A. 让它来取码：把微信的 base-url 指到 {host_port}，重启它的网关；"
        "然后你在 /admin/binds 批准（或把它写进 virtual.auto_approve）",
        "  B. token 复用：让它继续用自己那份真 token，只把 base-url 指过来，"
        "并把这行填回 gateway.json："
        f' "reuse_real_token_for": "{plan.name}", "reuse_token_file": "…它的 token 文件…"',
        "  注意：Hermes 的扫码 URL 写死在腾讯（WEIXIN_BASE_URL 只覆盖收发），"
        "所以它没法扫我们发的码 —— 走 A/B 都不要再扫码。",
    ]
    if not apply:
        lines.insert(0, "（--dry-run：Hermes 的配置没动）")
    return lines


def _build_hermes(plan: Plan, world: World, opts: Options) -> None:
    _virtual(plan, world, token=False, auto=True)
    plan.client = _client_hermes
    plan.client_preview = "改 Hermes 微信渠道的 base-url（要重启它的网关）"
    plan.notes.append("它现在就在给你发消息：动它之前先想清楚，这条链路一断，我就联系不上你了")


_register(Profile(
    name="hermes", label="Hermes", prefix="h", mode="virtual-qr",
    summary="微信渠道能改 base-url（环境变量），但扫码写死腾讯 → 让它连我们的虚拟 iLink",
    extra_modes=("a2a",),
    build=_build_hermes,
))


# ---- Claude Code -----------------------------------------------------------


def _client_none(plan: Plan, world: World, apply: bool) -> List[str]:
    return []


def _probe_port(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return res.status < 500
    except Exception:  # noqa: BLE001 - 探活失败就是没在跑
        return False


def _build_claude(plan: Plan, world: World, opts: Options) -> None:
    if plan.mode == "exec":
        opts.command = opts.command or ["claude", "-p", "{text}"]
        plan.entry = _backend_entry(plan, opts)
        plan.notes.append("exec 后端：回复 = 命令的 stdout；"
                          "要接着聊多轮，命令模板里加 --resume {session}")
        return
    opts.url = opts.url or "http://127.0.0.1:8799"
    opts.mode = plan.mode = "a2a"
    plan.entry = _backend_entry(plan, opts)
    plan.notes.append(f"a2a 后端指向 {opts.url}（a2a-bridge 把 claude -p 包成的端点）；"
                      "端口没监听的话先起 a2a-bridge")


_register(Profile(
    name="claude", label="Claude Code", prefix="c", mode="a2a",
    summary="本机已经用 a2a-bridge 包成 A2A 端点（:8799）；也可以 --mode exec 直接跑 CLI",
    extra_modes=("exec",),
    build=_build_claude,
))


# ---- CodeBuddy / WorkBuddy 的 CLI -----------------------------------------


def _build_codebuddy(plan: Plan, world: World, opts: Options) -> None:
    opts.command = opts.command or ["codebuddy", "-p", "{text}"]
    plan.entry = _backend_entry(plan, opts)
    if not shutil.which(opts.command[0]):
        plan.notes.append(f"! 本机 PATH 上没有 `{opts.command[0]}`：装了 CodeBuddy CLI 再回来跑这条，"
                          "否则先用 workbuddy profile（它不需要 CLI）")


_register(Profile(
    name="codebuddy", label="CodeBuddy CLI", prefix="b", mode="exec",
    summary="CodeBuddy/WorkBuddy 家族的命令行版：跟 Claude Code 一样是个纯 exec 后端",
    build=_build_codebuddy,
))


# ---- 本仓库自带的 iLink 客户端（任何「能改 base-url」的 ClawBot 客户端）------


def _client_ilink(plan: Plan, world: World, apply: bool) -> List[str]:
    base = plan.gateway_url or ""
    return [
        "对方侧（能改 base-url 的 iLink 客户端）：",
        "  A. 用本仓库现成的客户端：",
        f"     python clients/ilink_agent_client.py --name {plan.name} \\",
        f"         --base-url {base} --runner \"<你的命令> {{text}}\"",
        "  B. 别的 iLink / ClawBot 客户端：把 base-url 指到上面这个地址，"
        "它自己取码 → 你批准 → 拿到虚拟身份",
        "  已加进 virtual.auto_approve：这类 agent 不用人工点批准。",
    ]


def _build_ilink(plan: Plan, world: World, opts: Options) -> None:
    _virtual(plan, world, token=False, auto=True)
    plan.client = _client_ilink
    plan.client_preview = "对方连 127.0.0.1:18500，自己取码（auto_approve 免批准）"


_register(Profile(
    name="ilink", label="iLink 客户端", prefix="i", mode="virtual-qr",
    summary="任何能改 base-url 的 ClawBot 客户端（含本仓库的 clients/ilink_agent_client.py）",
    build=_build_ilink,
))


# ---- 只有桌面版的 agent（CDP 桥）------------------------------------------


def _build_cdp(plan: Plan, world: World, opts: Options) -> None:
    profile = opts.url or f"bridges/profiles/{plan.name}.json"
    opts.command = opts.command or [sys.executable, "bridges/electron_cdp.py",
                                    "--profile", profile, "--text", "{text}"]
    plan.entry = _backend_entry(plan, opts)
    plan.notes.append(f"CDP 桥：对方要带 --remote-debugging-port 启动；"
                      f"选择器档案在 {profile}（见 docs/BACKENDS-WORKBUDDY.md）")


_register(Profile(
    name="cdp", label="桌面应用（CDP）", prefix="g", mode="exec",
    summary="只有桌面版、没有 CLI/API 的 agent：用 bridges/electron_cdp.py 往它界面里打字",
    build=_build_cdp,
))


BACKEND_MODES = ("exec", "a2a", "http")
VIRTUAL_MODES = ("virtual-token", "virtual-qr")


def build_plan(name: str, world: World, opts: Options) -> Plan:
    """算出「把 ``name`` 接进来」这件事该怎么做。"""
    key = (name or "").strip().lower()
    if not key or not all(ch.isalnum() or ch in "-_" for ch in key):
        raise JoinError(f"agent 名字只能用字母/数字/-/_，收到 {name!r}")
    profile = PROFILES.get(key)
    mode = (opts.mode or (profile.mode if profile else "")).strip()
    if mode not in BACKEND_MODES + VIRTUAL_MODES:
        raise JoinError(f"不认识的接入方式 {mode!r}；可选：{', '.join(BACKEND_MODES + VIRTUAL_MODES)}"
                        f"（现成的 profile：{', '.join(sorted(PROFILES))}）")
    label = opts.label or (profile.label if profile else key)
    prefix = (opts.prefix or (profile.prefix if profile else key[:1])).strip().lower().lstrip("/")
    plan = Plan(name=key, mode=mode, label=label, prefix=prefix, entry={},
                why=profile.summary if profile else "通用接入")
    use_profile = profile is not None and (not opts.mode or opts.mode == profile.mode
                                           or opts.mode in profile.extra_modes)
    if use_profile and profile is not None:
        profile.build(plan, world, opts)
        # profile 有可能改掉 mode（例如 claude 选中 a2a），以它为准再兜底一次
        plan.mode = plan.entry.get("type", plan.mode) if plan.entry else plan.mode
    if not plan.entry:
        if mode in VIRTUAL_MODES:
            _virtual(plan, world, token=(mode == "virtual-token"), auto=(mode == "virtual-qr"))
        else:
            plan.entry = _backend_entry(plan, opts)
        if profile is None:
            plan.notes.append("这是通用接入：agent 那一侧要你自己按它自己的方式指过来")
    plan.gateway_url = gateway_url_for(world)
    return plan


def gateway_url_for(world: World, *, raw: Optional[Dict[str, Any]] = None) -> str:
    raw = raw if raw is not None else _read_json(world.config_path)
    virt = raw.get("virtual") or {}
    host = str(virt.get("host") or "127.0.0.1")
    port = int(virt["port"]) if "port" in virt else 18500
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{shown}:{port}"


# --------------------------------------------------------------------- 落盘


def apply_plan(plan: Plan, world: World, *, restart: bool = False,
               dry_run: bool = False) -> List[str]:
    """改 gateway.json（+ 对方那一侧），需要的话重启网关。返回给人看的日志。"""
    raw = _read_json(world.config_path)
    lines: List[str] = []

    if not dry_run:
        agents = raw.setdefault("agents", {})
        agents[plan.name] = plan.entry
        if plan.token_path is not None:
            virt = raw.setdefault("virtual", {})
            virt["enabled"] = True
            accept = virt.setdefault("accept_tokens", {})
            accept[plan.name] = f"file:{_rel(plan.token_path, world.config_path.parent)}"
        if plan.auto_approve:
            virt = raw.setdefault("virtual", {})
            names = [str(n) for n in (virt.get("auto_approve") or [])]
            if plan.name not in names:
                names.append(plan.name)
            virt["auto_approve"] = names
        bak = _write_json(world.config_path, raw)
        lines.append(f"gateway.json 已更新（备份 {bak.name if bak else '无'}）")
        cfg = load_config(world.config_path)          # 写坏了立刻知道
        lines.append(f"配置自检通过：agents={', '.join(sorted(cfg.agents))}；"
                     f"virtual={'on' if cfg.virtual.enabled else 'off'} {gateway_url_for(world, raw=raw)}")
        if plan.token_path is not None:
            token, created = mint_token(plan.token_path)
            lines.append(f"token {'新建' if created else '沿用'}：{plan.token_path}"
                         f"（{_mask(token)}）")
    else:
        lines.append("[dry-run] gateway.json 一个字节都没动")

    # agent 那一侧
    if plan.client is not None:
        lines.extend(plan.client(plan, world, not dry_run))

    if restart and not dry_run:
        lines.extend(restart_gateway(world))
    return lines


# --------------------------------------------------------------------- 进程


def _port_of(raw: Dict[str, Any]) -> int:
    virt = raw.get("virtual") or {}
    return int(virt["port"]) if "port" in virt else 18500


def find_gateway_pids(port: int) -> List[int]:
    """谁在监听 ``port``（= 正在跑的网关）。找不到就空列表。"""
    pids: List[int] = []
    if os.name == "nt":
        try:
            # netstat 在中文 Windows 上输出 GBK：text=True 会让解码在读取线程里炸掉
            # （stdout 变 None），所以收 bytes 自己 replace 解码。
            result = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        out = (result.stdout or b"").decode("utf-8", "replace")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3] == "LISTENING":
                if parts[1].endswith(f":{port}"):
                    try:
                        pids.append(int(parts[4]))
                    except ValueError:
                        pass
        return pids
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if "agent_gateway" in cmdline:
                pids.append(int(entry.name))
    return pids


def _kill(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           timeout=15)
        else:
            os.kill(pid, 15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def restart_gateway(world: World, *, wait: float = 6.0) -> List[str]:
    """停掉正在跑的网关，再用当前 python 把它拉起来（脱离本进程）。"""
    raw = _read_json(world.config_path)
    port = _port_of(raw)
    lines: List[str] = []
    for pid in find_gateway_pids(port):
        if _kill(pid):
            lines.append(f"已停掉旧网关 pid={pid}")
    if lines:
        time.sleep(2)
    log = world.data_dir / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with log.open("ab") as handle:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "agent_gateway",
             "--config", str(world.config_path)],
            cwd=str(world.project), stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=creationflags)
    lines.append(f"新网关 pid={proc.pid}，日志 {log}")
    deadline = time.time() + wait
    while time.time() < deadline:
        if gateway_health(world):
            break
        time.sleep(0.4)
    up = gateway_health(world)
    lines.append(f"健康检查：{'通了' if up else '没起来 —— 看 ' + str(log)}")
    return lines


# --------------------------------------------------------------------- 状态


def _get_json(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def gateway_health(world: World, *, raw: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return _get_json(gateway_url_for(world, raw=raw) + "/health")


def gateway_binds(world: World, *, raw: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    payload = _get_json(gateway_url_for(world, raw=raw) + "/admin/binds")
    return list((payload or {}).get("binds") or [])


def status_lines(world: World) -> List[str]:
    raw = _read_json(world.config_path)
    base = gateway_url_for(world, raw=raw)
    health = gateway_health(world, raw=raw)
    binds = {b.get("name"): b for b in gateway_binds(world, raw=raw)} if health else {}
    out = [f"网关：{base} —— {'在跑' if health else '没在跑（看 data/gateway.log）'}"]
    if health:
        out.append(f"  已接入：{', '.join(health.get('binds') or []) or '（还没有）'}")
    accept = (raw.get("virtual") or {}).get("accept_tokens") or {}
    for name, entry in (raw.get("agents") or {}).items():
        kind = entry.get("type") or "?"
        bits = [kind]
        if kind == "virtual":
            bits.append("token 接入" if name in accept else
                        ("二维码接入" if name in ((raw.get("virtual") or {}).get("auto_approve") or [])
                         else "没接入（等取码/批准）"))
        elif entry.get("url"):
            bits.append(str(entry["url"]))
        if name in binds:
            bits.append(f"虚拟号 {binds[name].get('account_id')}"
                        f"（队列 {binds[name].get('queue')}）")
        out.append(f"  · {name:<10} {' / '.join(bits)}")
    return out


# --------------------------------------------------------------------- CLI


def _split_command(text: str) -> List[str]:
    if not text.strip():
        return []
    return shlex.split(text, posix=(os.name != "nt"))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="join.py",
        description="把一个 agent 接进本机网关（登记 + 开一条进来的路 + 重启 + 验证）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="现成的 profile：\n  " + "\n  ".join(
            f"{p.name:<10} {p.summary}" for p in sorted(PROFILES.values(), key=lambda p: p.name))
        + "\n\n其它 agent 也能接：给 --mode exec|a2a|http|virtual-qr|virtual-token。")
    parser.add_argument("agent", nargs="?", help="agent 名字（workbuddy / claude / … 或自定义）")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="网关配置文件（默认 gateway.json）")
    parser.add_argument("--mode", default="", help="接入方式：exec|a2a|http|virtual-qr|virtual-token")
    parser.add_argument("--label", default="", help="微信里显示的名字")
    parser.add_argument("--prefix", default="", help="路由前缀，微信里 /<前缀> 指名发给它")
    parser.add_argument("--url", default="", help="a2a/http 的地址；cdp 时是选择器档案路径")
    parser.add_argument("--command", default="", help="exec 的命令模板，用 {text} 占位")
    parser.add_argument("--body-template", default="", help='http 的请求体模板（含 {text}）')
    parser.add_argument("--reply-path", default="", help="http 回复在 JSON 里的路径，如 choices.0.text")
    parser.add_argument("--timeout", type=int, default=900, help="单条消息的超时秒数（默认 900）")
    parser.add_argument("--list", action="store_true", help="列出可用的 profile")
    parser.add_argument("--status", action="store_true", help="看现在接进来了谁")
    parser.add_argument("--dry-run", action="store_true", help="只打印要改什么")
    parser.add_argument("--no-restart", action="store_true", help="改完不重启网关")
    args = parser.parse_args(argv)

    world = World(config_path=Path(args.config).expanduser().resolve(),
                  project=Path(args.config).expanduser().resolve().parent)

    if args.list:
        for profile in sorted(PROFILES.values(), key=lambda p: p.name):
            modes = ", ".join((profile.mode,) + profile.extra_modes)
            print(f"{profile.name:<10} /{profile.prefix}  {profile.label:<14} [{modes}]")
            print(f"           {profile.summary}")
        print("\n通用接入（任何 agent）：--mode exec|a2a|http|virtual-qr|virtual-token")
        return 0

    try:
        if args.status or not args.agent:
            for line in status_lines(world):
                print(line)
            if not args.agent and not args.status:
                print("\n要用哪个 agent？`python join.py --list` 看现成的，"
                      "或 `python join.py <名字> --mode exec --command \"...\"`")
            return 0

        opts = Options(mode=args.mode, label=args.label, prefix=args.prefix, url=args.url,
                       command=_split_command(args.command), body_template=args.body_template,
                       reply_path=args.reply_path, timeout=args.timeout)
        plan = build_plan(args.agent, world, opts)
        print(f"接入计划：{plan.name}（{plan.label}，/{plan.prefix}，{plan.type}）")
        print(f"  为什么这么接：{plan.why}")
        for change in plan.changes(_read_json(world.config_path)):
            print(f"  · {change}")
        print()
        for line in apply_plan(plan, world, restart=not args.no_restart,
                               dry_run=args.dry_run):
            print(line)
        for note in plan.notes:
            print(f"注意：{note}")
        if not args.dry_run:
            print()
            for line in status_lines(world):
                print(line)
        return 0
    except (JoinError, ConfigError) as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
