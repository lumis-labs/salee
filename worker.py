"""The always-on coordinator and its small, cost-bounded specialist roles."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings
from growth import GrowthEngine
from prospecting import ProspectingEngine
from policy import can_send, note_inbound
from providers import AgentMail, InboundMessage, OpenRouter, ProviderError, StripePayments, Twilio
from quality_control import validate_draft
from store import Store

LOG = logging.getLogger("revenue_agent")

REPLY_PROMPT = """You are Salee Arman, the customer-conversation specialist for Salee. Write a concise, honest reply to an inbound customer message. You may explain the configured offer only from the supplied business context. Never claim a payment, refund, delivery, result, or policy that is not evidenced. Sign as Salee Arman only when natural. If the request is risky or ambiguous, ask one clarifying question."""
OFFER_PROMPT = """You are Salee Arman, the offer specialist. Given the business description and an inbound sales question, propose one clear next step that could convert interest into the configured paid offer. Keep it truthful, specific, and under 120 words. Include the configured checkout URL exactly once."""
FULFILLMENT_PROMPT = """You are Salee Arman's delivery specialist. Create a practical AI revenue workflow audit for a paying customer. Use only the customer's supplied facts. Return: (1) current-state summary, (2) three highest-leverage workflow opportunities, (3) a 14-day implementation plan, (4) suggested success metrics, and (5) assumptions/questions. Do not guarantee revenue, invent integrations, or give regulated advice. Keep it useful and under 1,000 words."""


def classify_message(body: str) -> str:
    lowered = body.lower()
    if any(term in lowered for term in ("unsubscribe", "stop", "remove me", "do not contact")):
        return "unsubscribe"
    if any(term in lowered for term in ("viagra", "casino", "crypto giveaway", "prize winner")):
        return "spam"
    if any(term in lowered for term in ("price", "pricing", "buy", "purchase", "cost", "checkout", "interested", "demo")):
        return "sales"
    return "support"


class RevenueWorker:
    def __init__(self, settings: Settings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings.database_path)
        self.llm = OpenRouter(settings)
        self.mail = AgentMail(settings)
        self.twilio = Twilio(settings)
        self.stripe = StripePayments(settings)
        self.llm_calls = 0
        self.growth = GrowthEngine(settings, self.store, self._complete)
        self.prospecting = ProspectingEngine(settings, self.store)

    @property
    def checkout_url(self) -> str:
        return self.settings.checkout_url or self.store.get_runtime("checkout_url") or ""

    @property
    def payment_mode(self) -> str:
        configured = self.store.get_runtime("checkout_mode")
        if configured:
            return configured
        checkout = self.checkout_url
        if "buy.stripe.com/test_" in checkout or "test_" in checkout:
            return "test"
        return "manual" if self.settings.checkout_url else self.settings.stripe_mode

    @property
    def missing_revenue_config(self) -> list[str]:
        missing = [key for key in self.settings.missing_revenue_config if key != "CHECKOUT_URL"]
        if not self.checkout_url:
            missing.append("CHECKOUT_URL_OR_STRIPE_RESTRICTED_KEY")
        elif self.payment_mode == "test":
            missing.append("LIVE_CHECKOUT_REQUIRED")
        return missing

    @property
    def revenue_ready(self) -> bool:
        return not self.missing_revenue_config

    def ensure_checkout(self) -> bool:
        if self.checkout_url and not (self.payment_mode == "test" and self.settings.stripe_mode == "live"):
            return True
        if not self.settings.stripe_restricted_key:
            return False
        try:
            product_id = self.store.get_runtime("stripe_product_id") if self.settings.stripe_mode != "live" else self.store.get_runtime("stripe_product_id_live")
            if not product_id:
                product = self.stripe.create_product(self.settings.offer_name, self.settings.business_description)
                product_id = str(product["id"])
                self.store.set_runtime("stripe_product_id_live" if self.settings.stripe_mode == "live" else "stripe_product_id", product_id)
            price_id = self.store.get_runtime("stripe_price_id") if self.settings.stripe_mode != "live" else self.store.get_runtime("stripe_price_id_live")
            if not price_id:
                price = self.stripe.create_price(product_id, self.settings.offer_price_cents, self.settings.stripe_currency)
                price_id = str(price["id"])
                self.store.set_runtime("stripe_price_id_live" if self.settings.stripe_mode == "live" else "stripe_price_id", price_id)
            link = self.stripe.create_payment_link(price_id)
            url = str(link["url"])
            self.store.set_runtime("checkout_url", url)
            self.store.set_runtime("checkout_mode", self.settings.stripe_mode)
            self.store.audit("checkout_created", {"provider": "stripe", "mode": self.settings.stripe_mode, "product_id": product_id, "price_id": price_id})
            return True
        except (ProviderError, KeyError, TypeError) as exc:
            self.store.audit("checkout_error", {"provider": "stripe", "error": str(exc)})
            LOG.warning("Checkout provisioning failed: %s", exc)
            return False

    def _complete(self, system: str, user: str, max_tokens: int, role: str = "worker") -> str:
        if self.llm_calls >= self.settings.max_llm_calls_per_cycle:
            raise ProviderError("per-cycle LLM budget reached")
        self.llm_calls += 1
        self.store.audit("llm_call", {"role": role, "model": self.settings.model_for(role), "max_tokens": max_tokens})
        if role == "worker":
            return self.llm.complete(system, user, max_tokens=max_tokens)
        return self.llm.complete(system, user, max_tokens=max_tokens, role=role)

    def _business_context(self) -> str:
        return "\n".join([
            f"Business: {self.settings.business_name or '[not configured]'}",
            f"Description: {self.settings.business_description or '[not configured]'}",
            f"Offer: {self.settings.offer_name or '[not configured]'}",
            f"Price cents: {self.settings.offer_price_cents}",
            f"Checkout URL: {self.checkout_url or '[not configured]'}",
        ])

    def _render_followup(self, address: str) -> str:
        first_name = address.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
        return self.settings.followup_message.replace("{{first_name}}", first_name).replace("{{business_name}}", self.settings.business_name).replace("{{offer_name}}", self.settings.offer_name).replace("{{checkout_url}}", self.checkout_url)

    def register_payment(self, session_id: str, email: str, amount_total: int) -> None:
        order = self.store.register_order(session_id, email, amount_total)
        self.store.upsert_contact(email, "email", "inbound")
        self.store.audit("payment_recorded", {"session_id": session_id, "amount_total": amount_total, "order_id": order["stripe_session_id"]})

    def submit_intake(self, token: str, intake: dict[str, str]) -> bool:
        cleaned = {key: value.strip()[:2000] for key, value in intake.items() if value and value.strip()}
        if not cleaned.get("business") or not cleaned.get("goal"):
            return False
        saved = self.store.save_intake(token, cleaned)
        if saved:
            self.store.audit("intake_received", {"fields": sorted(cleaned)})
        return saved

    def capture_interest(self, email: str, business: str, goal: str) -> bool:
        """Capture an explicit website inquiry and send one useful reply."""
        email = email.strip().lower()[:320]
        business = business.strip()[:500]
        goal = goal.strip()[:1200]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not business or not goal:
            return False
        event_id = "interest-" + hashlib.sha256(f"{email}|{business}|{goal}".encode()).hexdigest()[:32]
        self.store.upsert_contact(email, "email", "opted_in")
        if not self.store.record_message(event_id, "email", "inbound", email, f"Business: {business}\nGoal: {goal}"):
            return True
        self.store.audit("interest_captured", {"contact": email, "business": business, "goal": goal})
        if not self.settings.agentmail_api_key:
            return True
        body = (
            f"Thanks for sharing what you are working on at {business}.\n\n"
            f"Salee will use your goal — {goal} — to frame the fastest practical AI workflow opportunities.\n\n"
            f"If you want the complete implementation plan, you can start here: {self.checkout_url}\n\n"
            "You can reply to this email with more context or STOP to opt out."
        )
        decision = can_send(self.store, self.settings, email, "email", is_reply=True)
        if not decision.allowed:
            self.store.audit("interest_response_blocked", {"contact": email, "reason": decision.reason})
            return True
        try:
            result = self.mail.send(email, f"Your AI workflow goal for {self.settings.business_name}", body)
            external_id = result.get("message_id") or result.get("sid") or f"interest-reply-{time.time_ns()}"
            self.store.record_message(str(external_id), "email", "outbound", email, body)
            self.store.audit("interest_response_sent", {"contact": email, "provider_id": external_id})
        except ProviderError as exc:
            self.store.audit("interest_response_error", {"contact": email, "error": str(exc)})
            LOG.warning("Interest response failed: %s", exc)
        return True

    def _intake_url(self, token: str) -> str:
        origin = self.settings.public_base_url or "http://localhost:8080"
        return f"{origin}/intake?token={token}"

    def process_orders(self) -> int:
        processed = 0
        for order in self.store.orders_needing_intake_email():
            intake_url = self._intake_url(order["intake_token"])
            body = (
                f"Thanks for purchasing {self.settings.offer_name} from {self.settings.agent_full_name}.\n\n"
                f"Please complete this short intake so Salee can prepare your workflow audit:\n{intake_url}\n\n"
                "We will use only the information you provide."
            )
            try:
                result = self.mail.send(order["email"], f"Your {self.settings.offer_name} intake", body)
                external_id = result.get("message_id") or result.get("sid") or f"order-{time.time_ns()}"
                self.store.record_message(str(external_id), "email", "outbound", order["email"], body)
                self.store.mark_intake_sent(order["stripe_session_id"])
                self.store.audit("intake_requested", {"session_id": order["stripe_session_id"], "provider_id": external_id})
                processed += 1
            except ProviderError as exc:
                self.store.audit("intake_request_error", {"session_id": order["stripe_session_id"], "error": str(exc)})
                LOG.warning("Intake request failed: %s", exc)

        for order in self.store.orders_needing_fulfillment():
            try:
                intake = json.loads(order["intake_json"] or "{}")
                report = self._complete(FULFILLMENT_PROMPT, f"{self._business_context()}\nCustomer intake:\n{json.dumps(intake, sort_keys=True)}", max_tokens=1200)
                quality_errors = validate_draft(report, self.settings, max_chars=8000)
                if quality_errors:
                    self.store.create_decision("fulfillment_quality_review", "email", order["email"], {"errors": quality_errors})
                    continue
                result = self.mail.send(order["email"], f"Your {self.settings.offer_name} report", report)
                external_id = result.get("message_id") or result.get("sid") or f"report-{time.time_ns()}"
                self.store.record_message(str(external_id), "email", "outbound", order["email"], report)
                self.store.mark_fulfilled(order["stripe_session_id"])
                self.store.audit("order_fulfilled", {"session_id": order["stripe_session_id"], "provider_id": external_id})
                processed += 1
            except ProviderError as exc:
                self.store.audit("fulfillment_error", {"session_id": order["stripe_session_id"], "error": str(exc)})
                LOG.warning("Fulfillment failed: %s", exc)
        return processed

    def _respond(self, message: InboundMessage) -> bool:
        note_inbound(self.store, message.contact, message.channel, message.body)
        if self.store.seen(message.external_id):
            return True
        self.store.record_message(message.external_id, message.channel, "inbound", message.contact, message.body, message.subject, message.thread_id)

        if classify_message(message.body) == "unsubscribe":
            self.store.mark_seen(message.external_id, message.channel)
            self.store.audit("outbound_blocked", {"contact": message.contact, "reason": "opt-out"})
            return True

        classification = classify_message(message.body)
        if classification == "spam":
            self.store.mark_seen(message.external_id, message.channel)
            self.store.audit("inbound_ignored", {"contact": message.contact, "reason": "spam"})
            return True
        if not self.revenue_ready:
            self.store.mark_seen(message.external_id, message.channel)
            self.store.create_decision("revenue_not_ready", message.channel, message.contact, {"classification": classification})
            self.store.audit("outbound_blocked", {"contact": message.contact, "reason": "checkout not ready"})
            return True

        history = "\n".join(f"{row['direction']}: {row['body']}" for row in reversed(self.store.recent_messages(message.contact, 6)))
        try:
            prompt = OFFER_PROMPT if classification == "sales" else REPLY_PROMPT
            response = self._complete(prompt, f"{self._business_context()}\nConversation:\n{history}\nLatest message:\n{message.body}", max_tokens=220)
        except ProviderError as exc:
            self.store.audit("llm_error", {"contact": message.contact, "error": str(exc)})
            LOG.warning("LLM unavailable: %s", exc)
            return False

        decision = can_send(self.store, self.settings, message.contact, message.channel, is_reply=True)
        if not decision.allowed:
            self.store.mark_seen(message.external_id, message.channel)
            self.store.audit("outbound_blocked", {"contact": message.contact, "channel": message.channel, "reason": decision.reason})
            return True
        quality_errors = validate_draft(response, self.settings, self.checkout_url)
        if quality_errors:
            self.store.mark_seen(message.external_id, message.channel)
            self.store.create_decision("quality_review", message.channel, message.contact, {"draft": response, "errors": quality_errors})
            self.store.audit("quality_blocked", {"contact": message.contact, "errors": quality_errors})
            return True
        try:
            if message.channel == "email":
                result = self.mail.send(message.contact, f"Re: {message.subject}" if message.subject else "Re: your message", response)
            else:
                result = self.twilio.send_sms(message.contact, response[:1500])
            external_id = result.get("message_id") or result.get("sid") or f"sent-{time.time_ns()}"
            self.store.record_message(str(external_id), message.channel, "outbound", message.contact, response, message.subject, message.thread_id)
            self.store.mark_seen(message.external_id, message.channel)
            self.store.touch_contact(message.contact)
            self.store.audit("outbound_sent", {"contact": message.contact, "channel": message.channel, "provider_id": external_id})
        except ProviderError as exc:
            self.store.audit("send_error", {"contact": message.contact, "channel": message.channel, "error": str(exc)})
            LOG.warning("Send failed: %s", exc)
            return False
        return True

    def poll_email(self) -> int:
        count = 0
        for raw in self.mail.list_messages():
            sender = str(raw.get("from", ""))
            if not sender or sender == self.settings.agentmail_inbox:
                continue
            to = raw.get("to", [])
            if isinstance(to, list) and self.settings.agentmail_inbox not in to:
                continue
            body = str(raw.get("preview", ""))
            if not body:
                body = str(raw.get("text", ""))
            self._respond(InboundMessage(
                external_id=str(raw.get("message_id") or raw.get("id") or raw.get("timestamp")),
                channel="email", contact=sender, subject=str(raw.get("subject", "")),
                body=body, thread_id=str(raw.get("thread_id", "")),
            ))
            count += 1
        return count

    def receive_sms(self, external_id: str, sender: str, body: str) -> None:
        self.store.enqueue_inbound(external_id, "sms", {
            "contact": sender, "body": body, "subject": "", "thread_id": "",
        })

    def run_followups(self) -> int:
        if not (self.settings.followup_enabled and self.settings.followup_message and self.revenue_ready):
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=self.settings.followup_after_hours)).isoformat()
        sent = 0
        for contact in self.store.followup_contacts(cutoff):
            address, channel = contact["address"], contact["channel"]
            decision = can_send(self.store, self.settings, address, channel, is_reply=False)
            if not decision.allowed:
                self.store.audit("followup_blocked", {"contact": address, "channel": channel, "reason": decision.reason})
                continue
            body = self._render_followup(address)
            quality_errors = validate_draft(body, self.settings, self.checkout_url)
            if quality_errors:
                self.store.create_decision("followup_quality_review", channel, address, {"draft": body, "errors": quality_errors})
                continue
            try:
                if channel == "email":
                    result = self.mail.send(address, f"Following up: {self.settings.offer_name}", body)
                elif channel == "sms":
                    result = self.twilio.send_sms(address, body[:1500])
                else:
                    self.store.audit("followup_blocked", {"contact": address, "reason": "unsupported channel"})
                    continue
                external_id = result.get("message_id") or result.get("sid") or f"followup-{time.time_ns()}"
                self.store.record_message(str(external_id), channel, "outbound", address, body, "", "")
                self.store.touch_followup(address)
                self.store.audit("followup_sent", {"contact": address, "channel": channel, "provider_id": external_id})
                sent += 1
            except ProviderError as exc:
                self.store.audit("followup_error", {"contact": address, "channel": channel, "error": str(exc)})
                LOG.warning("Follow-up failed: %s", exc)
        return sent

    def process_queued(self) -> int:
        processed = 0
        for row in self.store.pending_inbound():
            payload = json.loads(row["payload_json"])
            message = InboundMessage(row["external_id"], row["channel"], payload["contact"], payload.get("subject", ""), payload["body"], payload.get("thread_id", ""))
            if self._respond(message):
                self.store.finish_inbound(row["external_id"])
                processed += 1
            else:
                self.store.retry_inbound(row["external_id"])
        return processed

    def cycle(self) -> dict[str, Any]:
        self.llm_calls = 0
        processed = 0
        self.ensure_checkout()
        try:
            processed = self.poll_email()
        except ProviderError as exc:
            self.store.audit("poll_error", {"channel": "email", "error": str(exc)})
            LOG.warning("Email poll failed: %s", exc)
        processed += self.process_queued()
        processed += self.process_orders()
        processed += self.run_followups()
        growth_result = self.growth.run()
        prospecting_result = self.prospecting.run()
        self.store.audit("cycle_complete", {"processed": processed, "status": self.store.status()})
        return {"processed": processed, "growth": growth_result, "prospecting": prospecting_result, **self.store.status()}

    def run_forever(self) -> None:
        LOG.info("Revenue worker started; interval=%ss", self.settings.poll_interval_seconds)
        while True:
            self.cycle()
            time.sleep(self.settings.poll_interval_seconds)
