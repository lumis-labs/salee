"""Minimal HTTP adapters for OpenRouter, AgentMail, and Twilio."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from config import Settings


class ProviderError(RuntimeError):
    pass


def _request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
             body: bytes | None = None, timeout: int = 30) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except urllib.error.URLError as exc:
        raise ProviderError(str(exc)) from exc


def _json_request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
                  payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    merged = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        merged["Content-Type"] = "application/json"
    status, response_headers, raw = _request(url, method=method, headers=merged, body=body, timeout=timeout)
    if status >= 400:
        raise ProviderError(f"HTTP {status}: {raw[:500].decode(errors='replace')}")
    return json.loads(raw) if raw else {}


class OpenRouter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(self, system: str, user: str, max_tokens: int = 500, role: str = "worker") -> str:
        if not self.settings.openrouter_api_key:
            raise ProviderError("OPENROUTER_API_KEY is not configured")
        payload = {
            "model": self.settings.model_for(role),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "HTTP-Referer": self.settings.openrouter_site_url,
            "X-OpenRouter-Title": self.settings.openrouter_app_name,
        }
        for attempt in range(3):
            status, response_headers, raw = _request(
                "https://openrouter.ai/api/v1/chat/completions",
                method="POST", headers={**headers, "Content-Type": "application/json"},
                body=json.dumps(payload).encode(), timeout=60,
            )
            if status in (429, 503) and attempt < 2:
                try:
                    delay = min(30, max(1, int(response_headers.get("Retry-After", "2"))))
                except ValueError:
                    delay = 2
                time.sleep(delay)
                continue
            if status >= 400:
                raise ProviderError(f"OpenRouter HTTP {status}: {raw[:500].decode(errors='replace')}")
            data = json.loads(raw)
            return str(data["choices"][0]["message"]["content"]).strip()
        raise ProviderError("OpenRouter request failed after retries")


class AgentMail:
    base_url = "https://api.agentmail.to/v0"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.headers = {"Authorization": f"Bearer {settings.agentmail_api_key}"}

    def list_messages(self, limit: int = 25) -> list[dict[str, Any]]:
        if not self.settings.agentmail_api_key or not self.settings.agentmail_inbox:
            return []
        url = f"{self.base_url}/inboxes/{urllib.parse.quote(self.settings.agentmail_inbox, safe='')}/messages?limit={limit}&ascending=false"
        data = _json_request(url, headers=self.headers)
        return list(data.get("messages", []))

    def send(self, to: str, subject: str, text: str) -> dict[str, Any]:
        if not self.settings.agentmail_api_key or not self.settings.agentmail_inbox:
            raise ProviderError("AgentMail is not configured")
        url = f"{self.base_url}/inboxes/{urllib.parse.quote(self.settings.agentmail_inbox, safe='')}/messages/send"
        return _json_request(url, method="POST", headers=self.headers, payload={"to": to, "subject": subject, "text": text})


class Twilio:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_sms(self, to: str, body: str) -> dict[str, Any]:
        if not (self.settings.twilio_account_sid and self.settings.twilio_auth_token and self.settings.twilio_phone_number):
            raise ProviderError("Twilio is not configured")
        token = base64.b64encode(f"{self.settings.twilio_account_sid}:{self.settings.twilio_auth_token}".encode()).decode()
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.settings.twilio_account_sid}/Messages.json"
        encoded = urllib.parse.urlencode({"To": to, "From": self.settings.twilio_phone_number, "Body": body}).encode()
        status, _, raw = _request(url, method="POST", headers={"Authorization": f"Basic {token}", "Content-Type": "application/x-www-form-urlencoded"}, body=encoded)
        if status >= 400:
            raise ProviderError(f"Twilio HTTP {status}: {raw[:500].decode(errors='replace')}")
        return json.loads(raw)


class StripePayments:
    """Stripe-hosted Payment Link provisioning; card data never touches Salee."""

    api_base = "https://api.stripe.com"
    api_version = "2026-06-24.dahlia"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _post_form(self, path: str, fields: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if not self.settings.stripe_restricted_key:
            raise ProviderError("STRIPE_RESTRICTED_KEY is not configured")
        body = urllib.parse.urlencode(fields).encode()
        auth = base64.b64encode(f"{self.settings.stripe_restricted_key}:".encode()).decode()
        status, _, raw = _request(
            f"{self.api_base}{path}", method="POST",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Stripe-Version": self.api_version,
                "Idempotency-Key": idempotency_key,
            }, body=body,
        )
        if status >= 400:
            raise ProviderError(f"Stripe HTTP {status}: {raw[:500].decode(errors='replace')}")
        return json.loads(raw)

    def create_product(self, name: str, description: str) -> dict[str, Any]:
        return self._post_form("/v1/products", {"name": name, "description": description[:500]}, "salee-product-v1")

    def create_price(self, product_id: str, unit_amount: int, currency: str) -> dict[str, Any]:
        return self._post_form("/v1/prices", {
            "product": product_id, "unit_amount": unit_amount, "currency": currency,
        }, "salee-price-v1")

    def create_payment_link(self, price_id: str) -> dict[str, Any]:
        return self._post_form("/v1/payment_links", {
            "line_items[0][price]": price_id, "line_items[0][quantity]": 1,
        }, "salee-payment-link-v1")

    @staticmethod
    def verify_webhook(payload: bytes, signature: str, secret: str, tolerance_seconds: int = 300) -> dict[str, Any]:
        """Verify Stripe's signed raw request body before parsing it."""
        if not secret or not signature:
            raise ProviderError("Stripe webhook verification is not configured")
        parts: dict[str, list[str]] = {}
        for item in signature.split(","):
            key, _, value = item.partition("=")
            parts.setdefault(key, []).append(value)
        timestamp = int(parts.get("t", ["0"])[0])
        signed = f"{timestamp}.{payload.decode()}".encode()
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
            raise ProviderError("invalid Stripe webhook signature")
        if abs(int(time.time()) - timestamp) > tolerance_seconds:
            raise ProviderError("stale Stripe webhook signature")
        return json.loads(payload)


@dataclass(frozen=True)
class InboundMessage:
    external_id: str
    channel: str
    contact: str
    subject: str
    body: str
    thread_id: str = ""
