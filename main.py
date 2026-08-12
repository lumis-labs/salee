"""Run the agent worker or expose health/webhook endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from config import load_settings
from store import build_store
from worker import RevenueWorker
from providers import ProviderError, StripePayments


class Handler(BaseHTTPRequestHandler):
    worker: RevenueWorker
    settings = None

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: str) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send_html(200, self.worker.growth.landing_page(self.worker.checkout_url))
        elif parsed.path == "/blog":
            posts = self.worker.store.artifacts("blog", limit=20)
            links = "".join(f'<li><a href="/blog/{html.escape(row["slug"], quote=True)}">{html.escape(row["title"])}</a></li>' for row in posts)
            self._send_html(200, f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Salee insights</title><h1>Salee insights</h1><ul>{links or '<li>New insights are being prepared.</li>'}</ul>")
        elif parsed.path.startswith("/blog/"):
            slug = parsed.path.removeprefix("/blog/")
            row = self.worker.store.artifact(slug)
            if not row:
                self._send_html(404, "<h1>Not found</h1>")
            else:
                self._send_html(200, f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><meta name=description content='{html.escape(json.loads(row['metadata_json']).get('seo_description',''), quote=True)}'><title>{html.escape(row['title'])}</title><article style='font:16px system-ui;max-width:760px;margin:8vh auto;padding:24px;white-space:pre-wrap'><p><a href='/'>Salee Arman</a></p><h1>{html.escape(row['title'])}</h1><div>{html.escape(row['body'])}</div></article>")
        elif parsed.path == "/robots.txt":
            origin = self.settings.public_base_url or "http://localhost:8080"
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n".encode())
        elif parsed.path == "/sitemap.xml":
            origin = self.settings.public_base_url or "http://localhost:8080"
            urls = [f"{origin}/", f"{origin}/blog"] + [f"{origin}/blog/{row['slug']}" for row in self.worker.store.artifacts("blog", limit=100)]
            xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">" + "".join(f"<url><loc>{html.escape(url)}</loc></url>" for url in urls) + "</urlset>"
            self.send_response(200); self.send_header("Content-Type", "application/xml"); self.end_headers(); self.wfile.write(xml.encode())
        elif parsed.path == "/intake":
            token = parse_qs(parsed.query).get("token", [""])[0]
            order = self.worker.store.order_by_token(token)
            if not order:
                self._send_html(404, "<h1>Intake link not found</h1>")
                return
            if order["intake_json"]:
                self._send_html(200, "<h1>Intake received</h1><p>Salee is preparing your report.</p>")
                return
            self._send_html(200, """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Salee intake</title><style>body{font:16px system-ui;max-width:680px;margin:8vh auto;padding:24px}input,textarea{display:block;width:100%;margin:8px 0 18px;padding:10px;box-sizing:border-box}button{padding:12px 18px}</style><h1>Salee intake</h1><p>Answer briefly so we can prepare your AI workflow audit.</p><form method="post" action="/intake"><input type="hidden" name="token" value=""" + html.escape(token, quote=True) + """"><label>Business / website<input name="business" required></label><label>Current sales or follow-up process<textarea name="current_process"></textarea></label><label>Biggest growth goal<textarea name="goal" required></textarea></label><label>Top constraint<textarea name="constraint"></textarea></label><button>Submit intake</button></form>""")
        elif parsed.path == "/health":
            status = self.worker.store.status()
            status["checkout_link_ready"] = bool(self.worker.checkout_url)
            self._send(200, {"ok": True, "agent_name": self.settings.agent_name, "agent_full_name": self.settings.agent_full_name, "stripe_mode": self.worker.payment_mode, "revenue_ready": self.worker.revenue_ready, "missing_revenue_config": self.worker.missing_revenue_config, "missing_operational_config": self.settings.missing_operational_config, **status})
        elif parsed.path == "/metrics":
            self._send(200, self.worker.store.status())
        else:
            self._send(404, {"error": "not found"})

    def _valid_twilio(self, params: dict[str, list[str]]) -> bool:
        token = self.settings.twilio_auth_token
        if not token:
            return False
        signature = self.headers.get("X-Twilio-Signature", "")
        url = (self.settings.public_base_url or f"http://{self.headers.get('Host', 'localhost')}") + "/webhooks/twilio"
        canonical = url + "".join(k + v[0] for k in sorted(params) for v in [params[k]])
        expected = base64.b64encode(hmac.new(token.encode(), canonical.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(signature, expected)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        parsed = urlsplit(self.path)
        if parsed.path == "/webhooks/stripe":
            try:
                event = StripePayments.verify_webhook(raw, self.headers.get("Stripe-Signature", ""), self.settings.stripe_webhook_secret)
                if event.get("type") == "checkout.session.completed":
                    session = event.get("data", {}).get("object", {})
                    details = session.get("customer_details") or {}
                    email = details.get("email") or session.get("customer_email")
                    session_id = session.get("id")
                    if email and session_id:
                        self.worker.register_payment(str(session_id), str(email), int(session.get("amount_total") or 0))
                        if os.getenv("VERCEL"):
                            self.worker.process_orders()
                self._send(200, {"received": True})
            except (ProviderError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logging.getLogger("payments").warning("Stripe webhook rejected: %s", exc)
                self._send(400, {"error": "invalid webhook"})
            return
        if parsed.path == "/intake":
            params = parse_qs(raw.decode(errors="replace"), keep_blank_values=True)
            token = params.get("token", [""])[0]
            intake = {key: values[0] for key, values in params.items() if key != "token" and values}
            if self.worker.submit_intake(token, intake):
                if os.getenv("VERCEL"):
                    self.worker.process_orders()
                self._send_html(200, "<h1>Received</h1><p>Salee will email your workflow report.</p>")
            else:
                self._send_html(400, "<h1>Could not save intake</h1><p>Please check the link and required fields.</p>")
            return
        if parsed.path != "/webhooks/twilio":
            self._send(404, {"error": "not found"})
            return
        params = parse_qs(raw.decode(errors="replace"), keep_blank_values=True)
        if not self._valid_twilio(params):
            self._send(403, {"error": "invalid webhook signature"})
            return
        sender = params.get("From", [""])[0]
        body = params.get("Body", [""])[0]
        message_sid = params.get("MessageSid", [f"sms-{sender}-{body}"])[0]
        self.worker.receive_sms(message_sid, sender, body)
        self._send(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:
        logging.getLogger("http").info(fmt, *args)


def serve(worker: RevenueWorker, host: str = "0.0.0.0", port: int = 8080) -> None:
    Handler.worker = worker
    Handler.settings = worker.settings
    server = ThreadingHTTPServer((host, port), Handler)
    logging.info("HTTP server listening on %s:%s", host, port)
    server.serve_forever()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = build_store(settings)
    worker = RevenueWorker(settings, store)
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker.run_forever()
        return
    port = int(__import__("os").environ.get("PORT", "8080"))
    if __import__("os").environ.get("RUN_WORKER", "1") == "1":
        threading.Thread(target=worker.run_forever, daemon=True, name="revenue-worker").start()
    serve(worker, port=port)


if __name__ == "__main__":
    main()
