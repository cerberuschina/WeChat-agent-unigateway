"""Local dashboard — one page that shows the gateway and lets you approve agents.

Design notes
------------
* **Stdlib only.** The gateway has no third-party dependencies; a web page served
  by ``http.server`` keeps it that way. Open it in any browser on the gateway
  machine.
* **Read-mostly.** It shows state and does exactly two writes: approve / reject a
  pending bind. Editing the config stays a text-file job — a UI that silently
  rewrites your config is a liability.
* **Same gate as the admin API.** ``may_admin`` decides who may look at the page
  (default: localhost only), so exposing the port publicly does not leak the
  dashboard along with it.

The gateway hands in three callables (snapshot / approve / reject) plus two rings
(logs, traffic); everything else lives here.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

DEFAULT_HOST = "127.0.0.1"
# Ephemeral by default (the gateway passes its configured port); see virtual_ilink.
DEFAULT_PORT = 0
PAGE_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-gateway 控制台</title>
<style>
 :root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e8ee;--mut:#8b93a5;--ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--acc:#4c8dff}
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
 header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
 h1{font-size:16px;margin:0;font-weight:600}
 .dot{width:9px;height:9px;border-radius:50%;background:var(--bad);display:inline-block}
 .dot.on{background:var(--ok)}
 .grow{flex:1}
 .mut{color:var(--mut)} .mono{font-family:ui-monospace,Consolas,monospace}
 main{padding:18px 20px 60px;max-width:1180px;margin:0 auto}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
 .card h2{font-size:13px;margin:0 0 10px;color:var(--mut);font-weight:600;letter-spacing:.4px}
 .k{font-size:20px;font-weight:600}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
 th{color:var(--mut);font-weight:500}
 tr:last-child td{border-bottom:0}
 button{font:inherit;background:var(--acc);color:#fff;border:0;border-radius:7px;padding:6px 12px;cursor:pointer}
 button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
 button:hover{filter:brightness(1.1)}
 input{font:inherit;background:#0d0f13;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:6px 9px;width:160px}
 .pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;border:1px solid var(--line)}
 .pill.ok{color:var(--ok);border-color:#1e4d34} .pill.wait{color:var(--warn);border-color:#4d3d1e}
 .pill.bad{color:var(--bad);border-color:#4d1e1e}
 pre{margin:0;max-height:340px;overflow:auto;font-size:12px;line-height:1.5;color:#c8cddb}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .sec{margin-top:22px}
 .flash{position:fixed;right:18px;bottom:18px;background:#1d2330;border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:none}
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span><h1>agent-gateway 控制台</h1>
  <span class="mut" id="sub">连接中…</span><span class="grow"></span>
  <button class="ghost" onclick="refresh(true)">立即刷新</button>
</header>
<main>
  <div class="grid" id="cards"></div>

  <div class="card sec"><h2>待批准的接入（人工批准）</h2>
    <div id="pending" class="mut">没有待批准的请求。</div>
  </div>

  <div class="card sec"><h2>Agent</h2><div id="agents" class="mut">—</div></div>

  <div class="card sec"><h2>每轮消息额度（微信：用户回复前最多 10 条）</h2>
    <div id="budget" class="mut">—</div>
  </div>

  <div class="card sec"><h2>最近消息</h2><div id="traffic" class="mut">还没有消息。</div></div>

  <div class="card sec"><h2>日志</h2><pre id="logs" class="mono"></pre></div>
</main>
<div class="flash" id="flash"></div>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ago = (ts) => { const s = Math.max(0, (Date.now()/1000) - ts); return s < 60 ? Math.round(s)+"s" : s < 3600 ? Math.round(s/60)+"m" : Math.round(s/3600)+"h"; };
const clock = (ts) => new Date(ts*1000).toLocaleTimeString("zh-CN", {hour12:false});

function flash(msg, bad) {
  const el = $("flash"); el.textContent = msg;
  el.style.borderColor = bad ? "#4d1e1e" : "#1e4d34";
  el.style.display = "block"; clearTimeout(window._f);
  window._f = setTimeout(() => el.style.display = "none", 2600);
}

function card(title, value, sub) {
  return `<div class="card"><h2>${esc(title)}</h2><div class="k">${value}</div><div class="mut">${esc(sub||"")}</div></div>`;
}

function render(s) {
  const g = s.gateway || {};
  $("dot").className = "dot " + (g.running ? "on" : "");
  $("sub").textContent = `${g.account || "未绑定"} · ${g.virtual_url || "虚拟服务未开"} · 更新于 ${clock(s.now)}`;

  const v = g.virtual || {}, d = g.delivery || {};
  $("cards").innerHTML = [
    card("真微信号", esc(g.account || "未绑定"), g.base_url || ""),
    card("虚拟服务", esc(g.virtual_url || "关闭"), (v.host ? `监听 ${v.host}:${v.port}` : "")),
    card("已接入 agent", (g.agents||[]).filter(a => a.bound).length + " / " + (g.agents||[]).length,
         (g.agents||[]).map(a => a.name).join("、")),
    card("策略", v.bind_key ? "密钥 + 白名单" : "本机/局域网", `管理来源 ${(v.admin_cidrs||[]).join(", ")}`),
    card("每轮额度", (d.max_messages_per_turn ?? "-") + " 条", `正文预留 ${d.reserve_for_answer ?? "-"} 条`),
    card("后端类型", esc((g.agents||[]).map(a => a.name + ":" + a.type).join("  ") || "-"), "虚拟 / a2a / http / exec"),
  ].join("");

  const pending = v.pending || [];
  $("pending").innerHTML = pending.length ? `<table><tr><th>来源</th><th>qrcode</th><th>请求时间</th><th>起名并批准</th></tr>${
    pending.map(p => `<tr>
      <td class="mono">${esc(p.client_ip || "?")}</td>
      <td class="mono mut">${esc((p.qrcode||"").slice(0,10))}…</td>
      <td class="mut">${p.created_at ? ago(p.created_at) + " 前" : ""}</td>
      <td><div class="row"><input id="name-${esc(p.qrcode)}" placeholder="agent 名字">
        <button onclick="decide('${esc(p.qrcode)}', true)">批准</button>
        <button class="ghost" onclick="decide('${esc(p.qrcode)}', false)">拒绝</button></div></td>
    </tr>`).join("")}</table>` : '<span class="mut">没有待批准的请求。</span>';

  $("agents").innerHTML = (g.agents||[]).length ? `<table><tr><th>名字</th><th>类型</th><th>状态</th><th>虚拟号</th><th>队列</th><th>最后活动</th></tr>${
    (g.agents||[]).map(a => `<tr>
      <td>${esc(a.label || a.name)}</td><td class="mut">${esc(a.type)}</td>
      <td><span class="pill ${a.bound ? "ok" : (a.type === "virtual" ? "wait" : "ok")}">${a.bound ? "已接入" : (a.type === "virtual" ? "未接入" : "就绪")}</span></td>
      <td class="mono mut">${esc(a.account_id || "—")}</td>
      <td>${a.queue ?? 0}</td>
      <td class="mut">${a.last_seen ? ago(a.last_seen) + " 前" : "—"}</td>
    </tr>`).join("")}</table>` : "—";

  const budget = d.per_peer || {};
  const keys = Object.keys(budget);
  $("budget").innerHTML = keys.length ? `<table><tr><th>联系人</th><th>已用</th><th>剩余</th></tr>${
    keys.map(k => `<tr><td class="mono">${esc(k.slice(0, 18))}…</td><td>${budget[k].used}</td>
      <td>${budget[k].left}</td></tr>`).join("")}</table>` : '<span class="mut">这一轮还没有发出消息。</span>';

  const traffic = s.traffic || [];
  $("traffic").innerHTML = traffic.length ? `<table><tr><th>时间</th><th>方向</th><th>agent</th><th>内容</th></tr>${
    traffic.map(t => `<tr><td class="mut">${clock(t.ts)}</td><td>${t.kind === "in" ? "微信 →" : "→ 微信"}</td>
      <td>${esc(t.agent || "")}</td><td>${esc((t.text||"").slice(0, 160))}</td></tr>`).join("")}</table>`
    : '<span class="mut">还没有消息。</span>';

  $("logs").textContent = (s.logs || []).map(l => `[${clock(l.ts)}] ${l.level === "INFO" ? "" : l.level + " "}${l.text}`).join("\n");
}

async function decide(qrcode, approve) {
  const name = approve ? ($("name-" + qrcode)?.value || "").trim() : "";
  const r = await fetch(approve ? "/api/approve" : "/api/reject", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({qrcode, name}),
  });
  const d = await r.json().catch(() => ({}));
  flash(approve ? (d.ok ? "已批准：" + ((d.bind||{}).account_id || "ok") : "批准失败") :
                  (d.ok ? "已拒绝" : "拒绝失败"), !d.ok);
  refresh(true);
}

async function refresh(force) {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    render(await r.json());
  } catch (e) {
    $("dot").className = "dot"; $("sub").textContent = "连接失败：" + e.message;
  }
}
refresh(true);
setInterval(refresh, 2000);
</script></body></html>"""


class LogRing(logging.Handler):
    """Keep the last N log records so the page can show what just happened."""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.records: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 - a broken log record must not break logging
            return
        with self._lock:
            self.records.append({"ts": record.created, "level": record.levelname, "text": text})

    def tail(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.records)[-limit:]


class TrafficRing:
    """Recent message flow, newest last (the page reverses it for reading)."""

    def __init__(self, capacity: int = 200):
        self.items: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, kind: str, peer: str, agent: str = "", text: str = "") -> None:
        with self._lock:
            self.items.append({"ts": time.time(), "kind": kind, "peer": peer,
                               "agent": agent, "text": text})

    def tail(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.items)[-limit:][::-1]


class _DashboardServer(ThreadingHTTPServer):
    """Same reasoning as the virtual server: never share a port silently."""

    allow_reuse_address = False


class Dashboard:
    """Serves the console page and the two write actions (approve / reject)."""

    def __init__(self, *, snapshot: Callable[[], Dict[str, Any]],
                 approve: Callable[[str, str], Any], reject: Callable[[str], Any],
                 may_admin: Callable[[str], bool],
                 logs: Optional[LogRing] = None, traffic: Optional[TrafficRing] = None,
                 host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 on_log: Optional[Callable[[str], None]] = None):
        self.snapshot = snapshot
        self.approve = approve
        self.reject = reject
        self.may_admin = may_admin
        self.logs = logs or LogRing()
        self.traffic = traffic or TrafficRing()
        self.host = host
        self.port = port
        self._log = on_log or (lambda _m: None)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        return {"now": time.time(), "gateway": self.snapshot(),
                "traffic": self.traffic.tail(), "logs": self.logs.tail()}

    def start(self) -> tuple[str, int]:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _page(self) -> None:
                body = PAGE_TEMPLATE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _allowed(self) -> bool:
                ip = (self.client_address or ("?",))[0]
                if dashboard.may_admin(ip):
                    return True
                dashboard._log(f"dashboard: refused {ip}")
                self._json({"ok": False, "error": "forbidden",
                            "hint": "控制台默认只允许本机访问（admin_cidrs）"}, status=403)
                return False

            def do_GET(self) -> None:  # noqa: N802
                if not self._allowed():
                    return
                path = self.path.partition("?")[0]
                if path in ("/", "/index.html"):
                    self._page()
                elif path == "/api/state":
                    self._json(dashboard.state())
                else:
                    self._json({"ok": False, "error": "not found"}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                if not self._allowed():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._json({"ok": False, "error": "bad json"}, status=400)
                    return
                path = self.path.partition("?")[0]
                if path == "/api/approve":
                    bind = dashboard.approve(str(payload.get("qrcode") or ""),
                                             str(payload.get("name") or ""))
                    self._json({"ok": bool(bind),
                                "bind": bind.as_dict() if hasattr(bind, "as_dict") else None})
                elif path == "/api/reject":
                    self._json({"ok": bool(dashboard.reject(str(payload.get("qrcode") or "")))})
                else:
                    self._json({"ok": False, "error": "not found"}, status=404)

        self._httpd = _DashboardServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="dashboard", daemon=True)
        self._thread.start()
        self._log(f"dashboard listening on {self.base_url()}")
        return self.host, self.port

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"http://{host}:{self.port}/"
