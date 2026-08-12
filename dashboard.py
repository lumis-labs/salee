"""Small authenticated observability dashboard for Salee."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import time
from http.cookies import SimpleCookie
from typing import Any

COOKIE_NAME = "salee_dashboard_session"
SESSION_SECONDS = 24 * 60 * 60


def _signing_key(settings: Any) -> bytes:
    return (
        settings.dashboard_password
        or settings.stripe_webhook_secret
        or settings.stripe_restricted_key
        or settings.webhook_secret
    ).encode()


def session_cookie(settings: Any) -> str:
    expires = int(time.time()) + SESSION_SECONDS
    value = str(expires)
    signature = hmac.new(_signing_key(settings), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{signature}"


def is_authenticated(settings: Any, cookie_header: str) -> bool:
    if not _signing_key(settings):
        return False
    cookie = SimpleCookie()
    cookie.load(cookie_header or "")
    morsel = cookie.get(COOKIE_NAME)
    if not morsel:
        return False
    try:
        expires, signature = morsel.value.split(".", 1)
        if int(expires) < int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    expected = hmac.new(_signing_key(settings), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    return str(value)


def _rows(store: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [{key: _json_value(row[key]) for key in row.keys()} for row in store.db.execute(query, params)]


def snapshot(worker: Any) -> dict[str, Any]:
    store = worker.store
    status = worker.observed_status()
    status["checkout_link_ready"] = bool(worker.checkout_url)
    decisions = _rows(store, "SELECT id,kind,channel,contact,payload_json,status,created_at,resolved_at FROM decisions ORDER BY id DESC LIMIT 20")
    for item in decisions:
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {"raw": item.pop("payload_json", "")}

    audits = _rows(store, "SELECT id,event,payload_json,created_at FROM audit_log ORDER BY id DESC LIMIT 30")
    for item in audits:
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {"raw": item.pop("payload_json", "")}

    messages = _rows(store, "SELECT id,direction,channel,contact,subject,body,created_at FROM messages ORDER BY id DESC LIMIT 20")
    orders = _rows(store, "SELECT stripe_session_id,email,amount_total,status,fulfillment_status,intake_sent_at,created_at,updated_at FROM orders ORDER BY created_at DESC LIMIT 20")
    experiments = _rows(store, "SELECT id,name,hypothesis,variant_json,status,created_at,updated_at FROM growth_experiments ORDER BY updated_at DESC LIMIT 12")
    for item in experiments:
        try:
            item["variant"] = json.loads(item.pop("variant_json") or "{}")
        except json.JSONDecodeError:
            item["variant"] = {"raw": item.pop("variant_json", "")}
    artifacts = _rows(store, "SELECT kind,slug,title,metadata_json,status,created_at,updated_at FROM growth_artifacts ORDER BY updated_at DESC LIMIT 12")
    for item in artifacts:
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {"raw": item.pop("metadata_json", "")}
    if worker.shared_prospects.enabled:
        try:
            prospects = worker.shared_prospects.list(20)
        except Exception as exc:
            store.audit("shared_prospect_dashboard_error", {"error": str(exc)[:300]})
            prospects = []
    else:
        prospects = _rows(store, "SELECT source,external_id,url,title,summary,author,match_score,outreach_draft,status,created_at,updated_at FROM prospects ORDER BY match_score DESC, updated_at DESC LIMIT 20")

    pending = int(status.get("pending_decisions", 0))
    queued = int(status.get("queued_inbound", 0))
    if queued:
        thinking = f"Processing {queued} queued inbound event(s) through policy, quality control, and the smallest appropriate specialist."
    elif pending:
        thinking = f"Holding {pending} decision(s) for review instead of sending an uncertain or unapproved action."
    elif int(status.get("prospect_drafts", 0)):
        thinking = f"Found {status['prospect_drafts']} public opportunities; Salee is preparing contextual outreach while continuously reviewing the site and next growth action."
    elif worker.revenue_ready:
        thinking = "Monitoring inbound demand, consent, checkout events, fulfillment, and the next bounded growth cycle."
    else:
        thinking = "Revenue actions are paused until the checkout and business configuration are complete."

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "system": {
            "agent_name": worker.settings.agent_name,
            "agent_full_name": worker.settings.agent_full_name,
            "stripe_mode": worker.payment_mode,
            "revenue_ready": worker.revenue_ready,
            "missing_revenue_config": worker.missing_revenue_config,
            "missing_operational_config": worker.settings.missing_operational_config,
            "public_base_url": worker.settings.public_base_url,
            "llm_calls_this_cycle": worker.llm_calls,
            "max_llm_calls_per_cycle": worker.settings.max_llm_calls_per_cycle,
            "model_routing": {"worker": worker.settings.openrouter_worker_model, "planner": worker.settings.openrouter_planner_model, "qa": worker.settings.openrouter_qa_model},
            "growth_qa_enabled": worker.settings.openrouter_qa_enabled,
            "growth_enabled": worker.settings.growth_enabled,
            "growth_interval_hours": worker.settings.growth_interval_hours,
            "growth_max_calls_per_day": worker.settings.growth_max_calls_per_day,
            "prospecting_enabled": worker.settings.prospecting_enabled,
            "prospecting_interval_minutes": worker.settings.prospecting_interval_minutes,
            "prospecting_max_items": worker.settings.prospecting_max_items,
            "competitive_analysis_enabled": worker.settings.competitive_analysis_enabled,
            "competitive_analysis_interval_hours": worker.settings.competitive_analysis_interval_hours,
            "autonomous_review_interval_minutes": worker.settings.autonomous_review_interval_minutes,
            "autonomous_next_action": worker.store.get_runtime("autonomous_next_action") or "review site, research, and demand signals",
            "poll_interval_seconds": worker.settings.poll_interval_seconds,
        },
        "status": status,
        "thinking_now": thinking,
        "decisions": decisions,
        "audit": audits,
        "messages": messages,
        "orders": orders,
        "experiments": experiments,
        "artifacts": artifacts,
        "prospects": prospects,
    }


def login_page(error: str = "") -> str:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Salee dashboard login</title>
<style>{_styles()}</style></head><body class="login"><main class="login-card"><div class="brand"><span class="brand-mark">S</span><span>Salee Arman</span></div><h1>System dashboard</h1><p class="muted">Enter the dashboard password to inspect the agent.</p>{message}<form method="post" action="/dashboard/login"><label>Password<input name="password" type="password" autocomplete="current-password" autofocus required></label><button type="submit">Open dashboard</button></form></main></body></html>"""


def dashboard_page() -> str:
    return """<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="900"><title>Salee Arman · Dashboard</title>
<style>""" + _styles() + """</style></head><body><div class="shell"><header class="topbar"><div class="brand"><span class="brand-mark">S</span><span>Salee Arman</span></div><div class="top-actions"><span id="updated" class="muted">Loading snapshot…</span><a href="/dashboard/logout">Log out</a></div></header><main class="content"><section class="hero"><div><p class="eyebrow">Private operations view</p><h1>System overview</h1><p class="muted">Observable decisions, safeguards, customer activity, and growth work. This shows recorded reasoning signals and audit trails, not private hidden chain-of-thought.</p></div><div id="posture" class="posture">Loading…</div></section><section id="metrics" class="metrics"></section><section class="grid two"><article class="panel feature"><div class="panel-head"><div><h2>Thinking now</h2><p class="muted">Current bounded operating posture</p></div><span id="mode" class="status-dot"></span></div><p id="thinking" class="thinking">Loading…</p><div id="system-details" class="detail-grid"></div></article><article class="panel"><div class="panel-head"><div><h2>Choices considered</h2><p class="muted">Decision records and why they were held</p></div></div><div id="decisions" class="stack"></div></article></section><section class="grid two"><article class="panel"><div class="panel-head"><div><h2>Decision trace</h2><p class="muted">Latest auditable events</p></div></div><div id="audit" class="timeline"></div></article><article class="panel"><div class="panel-head"><div><h2>Recent activity</h2><p class="muted">Inbound and outbound messages</p></div></div><div id="messages" class="stack"></div></article></section><section class="grid two"><article class="panel"><div class="panel-head"><div><h2>Revenue pipeline</h2><p class="muted">Checkout, intake, and fulfillment</p></div></div><div id="orders" class="table-wrap"></div></article><article class="panel"><div class="panel-head"><div><h2>Growth loop</h2><p class="muted">Landing copy, SEO/GEO, and experiments</p></div></div><div id="growth" class="stack"></div></article></section><section class="grid"><article class="panel"><div class="panel-head"><div><h2>Public prospecting queue</h2><p class="muted">Public conversations and contextual drafts</p></div></div><div id="prospects" class="stack"></div></article></section></main></div>
<script>
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, c => c === "&" ? "&amp;" : c === "<" ? "&lt;" : c === ">" ? "&gt;" : c.charCodeAt(0) === 34 ? "&quot;" : "&#39;");
const text = (node, value) => { node.textContent = value == null ? "—" : String(value); };
const when = (value) => value ? new Date(value).toLocaleString() : "—";
const payload = (value) => typeof value === "string" ? value : JSON.stringify(value || {}, null, 2);
function metric(label, value, tone) { return `<div class="metric"><span class="muted">${esc(label)}</span><strong class="${esc(tone || "")}">${esc(value)}</strong></div>`; }
function decisionCard(item) { return `<div class="decision"><div class="row"><strong>${esc(item.kind)}</strong><span class="tag">${esc(item.status)}</span></div><p class="muted">${esc(item.channel || "system")} · ${esc(when(item.created_at))}</p><pre>${esc(payload(item.payload))}</pre></div>`; }
function auditItem(item) { return `<div class="timeline-item"><span class="timeline-line"></span><div><div class="row"><strong>${esc(item.event)}</strong><span class="muted">${esc(when(item.created_at))}</span></div><pre>${esc(payload(item.payload))}</pre></div></div>`; }
function messageItem(item) { return `<div class="activity"><div class="row"><strong>${esc(item.direction)} · ${esc(item.channel)}</strong><span class="muted">${esc(when(item.created_at))}</span></div><div class="muted">${esc(item.contact || "")}</div><p>${esc(item.body || "")}</p></div>`; }
function orderTable(items) { if (!items.length) return `<p class="empty">No paid orders recorded in this instance.</p>`; return `<table><thead><tr><th>Customer</th><th>Status</th><th>Fulfillment</th><th>Updated</th></tr></thead><tbody>${items.map(x => `<tr><td>${esc(x.email)}</td><td>${esc(x.status)}</td><td>${esc(x.fulfillment_status)}</td><td>${esc(when(x.updated_at))}</td></tr>`).join("")}</tbody></table>`; }
function growthItem(item) { const label = item.title || item.name; const detail = item.hypothesis || item.kind || item.status; return `<div class="activity"><div class="row"><strong>${esc(label)}</strong><span class="tag">${esc(item.status || "published")}</span></div><p>${esc(detail)}</p><div class="muted">${esc(when(item.updated_at || item.created_at))}</div></div>`; }
function prospectItem(item) { return `<div class="activity"><div class="row"><strong>${esc(item.title)}</strong><span class="tag">${esc(item.source)} · ${esc(item.match_score)}%</span></div><div class="muted">${esc(item.author || "public author")} · ${esc(when(item.updated_at || item.created_at))}</div><a href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.url)}</a><p>${esc(item.summary || "No summary available.")}</p><pre>${esc(item.outreach_draft || "")}</pre></div>`; }
async function load() {
  const response = await fetch('/dashboard/data', {credentials: 'same-origin', cache: 'no-store'});
  if (response.status === 401) { location.href = '/dashboard'; return; }
  const data = await response.json(); const s = data.system; const st = data.status;
  text($('updated'), `Updated ${when(data.generated_at)}`); text($('thinking'), data.thinking_now); text($('mode'), s.stripe_mode);
  $('posture').className = `posture ${s.revenue_ready ? 'good' : 'warn'}`; text($('posture'), s.revenue_ready ? 'Revenue-ready' : 'Revenue paused');
  $('metrics').innerHTML = [metric('Messages', st.messages), metric('Pending decisions', st.pending_decisions, st.pending_decisions ? 'warn-text' : ''), metric('Paid orders', st.paid_orders, 'good-text'), metric('Fulfillment queue', st.pending_fulfillment), metric('Prospect drafts', st.prospect_drafts), metric('Growth posts', st.published_growth_artifacts), metric('LLM calls / cycle', `${s.llm_calls_this_cycle} / ${s.max_llm_calls_per_cycle}`)].join('');
  $('system-details').innerHTML = [['Stripe', s.stripe_mode], ['Poll interval', `${s.poll_interval_seconds}s`], ['Prospecting', s.prospecting_enabled ? `every ${s.prospecting_interval_minutes}m · max ${s.prospecting_max_items}` : 'off'], ['Growth cadence', s.growth_enabled ? `every ${s.growth_interval_hours}h` : 'off'], ['Site review', `every ${s.autonomous_review_interval_minutes}m`], ['Next action', s.autonomous_next_action], ['Research', s.competitive_analysis_enabled ? `every ${s.competitive_analysis_interval_hours}h` : 'off'], ['Daily growth calls', s.growth_max_calls_per_day], ['Models', `${s.model_routing.worker} → ${s.model_routing.planner} → ${s.model_routing.qa}`], ['Checkout', st.checkout_link_ready ? 'ready' : 'missing'], ['Operational gaps', (s.missing_operational_config || []).join(', ') || 'none']].map(x => `<div><span class="muted">${x[0]}</span><strong>${x[1]}</strong></div>`).join('');
  $('decisions').innerHTML = data.decisions.length ? data.decisions.map(decisionCard).join('') : '<p class="empty">No decisions waiting for review.</p>';
  $('audit').innerHTML = data.audit.length ? data.audit.map(auditItem).join('') : '<p class="empty">No audit events yet.</p>';
  $('messages').innerHTML = data.messages.length ? data.messages.map(messageItem).join('') : '<p class="empty">No messages recorded.</p>';
  $('orders').innerHTML = orderTable(data.orders);
  $('growth').innerHTML = [...data.experiments, ...data.artifacts].length ? [...data.experiments, ...data.artifacts].map(growthItem).join('') : '<p class="empty">No growth artifacts yet.</p>';
  $('prospects').innerHTML = data.prospects.length ? data.prospects.map(prospectItem).join('') : '<p class="empty">No public opportunities found yet. The next prospecting cycle will retry.</p>';
}
load().catch(error => { text($('thinking'), `Dashboard data unavailable: ${error.message}`); }); setInterval(load, 15000);
</script></body></html>"""


def _styles() -> str:
    return """
:root{color-scheme:dark;--bg:#09131f;--surface:#101e2d;--surface2:#14263a;--line:#233a50;--text:#eff6fb;--muted:#8fa6b8;--mint:#71e3bd;--amber:#f3bb68;--red:#ff8f86;--radius:16px}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{min-height:100vh}.topbar{height:72px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 4vw;background:#0b1826}.brand,.top-actions,.row{display:flex;align-items:center;gap:10px}.brand{font-weight:700;letter-spacing:.01em}.brand-mark{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:var(--mint);color:#092019;font-weight:900}.top-actions{font-size:12px}.top-actions a{color:var(--muted);text-decoration:none}.content{max-width:1440px;margin:0 auto;padding:44px 4vw 72px}.hero{display:flex;justify-content:space-between;gap:28px;align-items:end;margin-bottom:28px}.eyebrow{margin:0 0 7px;color:var(--mint);font-size:11px;letter-spacing:.12em;text-transform:uppercase}.hero h1{font-size:clamp(32px,4vw,52px);line-height:1.02;letter-spacing:-.05em;margin:0 0 12px}.muted{color:var(--muted)}.posture{border:1px solid var(--line);border-radius:999px;padding:10px 16px;color:var(--amber);white-space:nowrap}.posture.good{color:var(--mint);border-color:#2b6e5d}.metrics{display:grid;grid-template-columns:repeat(6,1fr);border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);margin-bottom:18px}.metric{padding:17px 18px;border-right:1px solid var(--line)}.metric:last-child{border-right:0}.metric span,.metric strong{display:block}.metric span{font-size:12px}.metric strong{font-size:23px;letter-spacing:-.03em;margin-top:5px}.good-text{color:var(--mint)}.warn-text{color:var(--amber)}.grid{display:grid;gap:18px;margin-bottom:18px}.grid.two{grid-template-columns:1fr 1fr}.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:22px;min-width:0}.panel.feature{background:linear-gradient(145deg,#102536,#101e2d)}.panel-head{display:flex;justify-content:space-between;gap:14px;margin-bottom:18px}.panel h2{margin:0;font-size:17px;letter-spacing:-.02em}.panel-head p{margin:3px 0 0;font-size:12px}.status-dot{font-size:11px;text-transform:uppercase;color:var(--mint)}.thinking{font-size:19px;line-height:1.35;max-width:620px;margin:10px 0 26px}.detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.detail-grid div{border-top:1px solid var(--line);padding-top:10px}.detail-grid span,.detail-grid strong{display:block;font-size:12px}.detail-grid strong{margin-top:3px}.stack,.timeline{display:grid;gap:11px;max-height:420px;overflow:auto;padding-right:3px}.decision,.activity{border-top:1px solid var(--line);padding-top:12px}.decision:first-child,.activity:first-child{border-top:0;padding-top:0}.decision p,.activity p{margin:5px 0 0}.decision pre,.timeline-item pre{white-space:pre-wrap;word-break:break-word;color:#b6c9d6;background:#0b1724;border:1px solid #1c3145;border-radius:10px;padding:10px;margin:9px 0 0;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}.tag{color:var(--mint);font-size:11px;text-transform:uppercase;letter-spacing:.06em}.timeline-item{display:grid;grid-template-columns:13px 1fr;gap:10px}.timeline-line{width:7px;height:7px;border-radius:50%;background:var(--mint);margin-top:7px;box-shadow:0 0 0 4px #1a3a3b}.timeline-item .row{justify-content:space-between;align-items:baseline}.timeline-item .row strong{font-size:13px}.activity strong{font-size:13px}.activity p{color:#c1d0db;white-space:pre-wrap;word-break:break-word;max-height:110px;overflow:auto}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:500}.empty{color:var(--muted);margin:0}.login{display:grid;place-items:center;min-height:100vh;padding:20px}.login-card{width:min(420px,100%);background:var(--surface);border:1px solid var(--line);border-radius:20px;padding:32px;box-shadow:0 20px 70px #0004}.login-card h1{font-size:30px;letter-spacing:-.04em;margin:34px 0 8px}.login-card p{margin:0 0 22px}.login-card label{display:block;color:var(--muted);font-size:12px}.login-card input{display:block;width:100%;margin-top:7px;border:1px solid var(--line);border-radius:10px;background:#0b1724;color:var(--text);padding:12px 13px;font:inherit}.login-card button{width:100%;margin-top:18px;border:0;border-radius:10px;background:var(--mint);color:#092019;padding:12px;font-weight:800;cursor:pointer}.error{color:var(--red)!important;font-size:13px}.login-card .brand{font-size:15px}@media(max-width:900px){.metrics{grid-template-columns:repeat(3,1fr)}.metric:nth-child(3){border-right:0}.metric:nth-child(n+4){border-top:1px solid var(--line)}.grid.two{grid-template-columns:1fr}.hero{align-items:start;flex-direction:column}.posture{align-self:start}}@media(max-width:520px){.topbar{padding:0 18px}.top-actions #updated{display:none}.content{padding:30px 18px 50px}.metrics{grid-template-columns:repeat(2,1fr)}.metric{border-right:0!important}.metric:nth-child(n+3){border-top:1px solid var(--line)}.panel{padding:17px}.detail-grid{grid-template-columns:repeat(2,1fr)}.hero h1{font-size:39px}}
"""
