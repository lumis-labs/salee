import tempfile
import unittest
from dataclasses import replace
import hashlib
import hmac
import json
import time
from pathlib import Path

from config import load_settings
from policy import can_send, note_inbound
from store import Store, VercelStore
from worker import RevenueWorker
from providers import StripePayments
from growth import _parse_proposal
from prospecting import ProspectingEngine
from dashboard import is_authenticated, session_cookie


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = load_settings(self.root)
        self.store = Store(self.root / "data.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_unknown_contact_cannot_receive_cold_outbound(self):
        decision = can_send(self.store, self.settings, "x@example.com", "email", is_reply=False)
        self.assertFalse(decision.allowed)

    def test_inbound_contact_can_receive_reply(self):
        note_inbound(self.store, "x@example.com", "email", "Hi")
        decision = can_send(self.store, self.settings, "x@example.com", "email", is_reply=True)
        self.assertTrue(decision.allowed)

    def test_opt_out_blocks_future_replies(self):
        note_inbound(self.store, "+15551234567", "sms", "STOP")
        decision = can_send(self.store, self.settings, "+15551234567", "sms", is_reply=True)
        self.assertFalse(decision.allowed)

    def test_duplicate_event_is_idempotent(self):
        self.assertFalse(self.store.seen("evt-1"))
        self.store.mark_seen("evt-1", "email")
        self.assertTrue(self.store.seen("evt-1"))
        self.store.mark_seen("evt-1", "email")

    def test_queued_opt_out_is_processed_without_provider_calls(self):
        worker = RevenueWorker(self.settings, self.store)
        worker.receive_sms("evt-stop", "+15550001111", "STOP")
        result = worker.process_queued()
        self.assertEqual(result, 1)
        self.assertEqual(self.store.contact("+15550001111")["consent"], "opted_out")

    def test_inbound_email_reply_uses_one_small_specialist_call(self):
        worker = RevenueWorker(replace(self.settings, checkout_url="https://buy.example/salee"), self.store)
        calls = []
        worker.llm.complete = lambda system, user, max_tokens: calls.append((system, max_tokens)) or "Here is the next step."
        worker.mail.send = lambda to, subject, text: {"message_id": "sent-1"}
        worker._respond(type("Message", (), {
            "external_id": "email-1", "channel": "email", "contact": "buyer@example.com",
            "subject": "Question", "body": "How does this work?", "thread_id": "thread-1",
        })())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages WHERE direction='outbound'").fetchone()[0], 1)

    def test_opted_in_followup_uses_configured_message_and_respects_qc(self):
        settings = replace(
            self.settings,
            business_name="Acme",
            business_description="A service",
            offer_name="Audit",
            offer_price_cents=5000,
            checkout_url="https://pay.example/audit",
            owner_email="owner@example.com",
            followup_enabled=True,
            followup_message="Hi {{first_name}}, get {{offer_name}} here: {{checkout_url}} Reply STOP to opt out.",
        )
        self.store.set_consent("buyer@example.com", "email", "opted_in")
        worker = RevenueWorker(settings, self.store)
        worker.mail.send = lambda to, subject, text: {"message_id": "followup-1"}
        self.assertEqual(worker.run_followups(), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages WHERE direction='outbound'").fetchone()[0], 1)

    def test_defaults_configure_salee_strategy_and_stripe_checkout(self):
        self.assertEqual(self.settings.agent_name, "Salee")
        self.assertEqual(self.settings.agent_full_name, "Salee Arman")
        self.assertEqual(self.settings.offer_name, "AI Revenue Workflow Sprint")
        settings = replace(self.settings, stripe_restricted_key="rk_live_placeholder")
        worker = RevenueWorker(settings, self.store)

        class FakeStripe:
            def create_product(self, name, description):
                return {"id": "prod_test"}
            def create_price(self, product_id, unit_amount, currency):
                return {"id": "price_test"}
            def create_payment_link(self, price_id):
                return {"url": "https://buy.stripe.test/salee"}

        worker.stripe = FakeStripe()
        self.assertTrue(worker.ensure_checkout())
        self.assertTrue(worker.revenue_ready)
        self.assertEqual(worker.checkout_url, "https://buy.stripe.test/salee")

    def test_legacy_test_checkout_is_not_treated_as_live(self):
        settings = replace(self.settings, stripe_restricted_key="rk_live_placeholder")
        self.store.set_runtime("checkout_url", "https://buy.stripe.com/test_legacy")
        worker = RevenueWorker(settings, self.store)
        self.assertEqual(worker.payment_mode, "test")

    def test_vercel_order_token_survives_cold_invocation(self):
        first = VercelStore(self.root / "first.sqlite3", "webhook-secret")
        order = first.register_order("cs_live_1", "buyer@example.com", 150000)
        token = order["intake_token"]
        first.close()
        second = VercelStore(self.root / "second.sqlite3", "webhook-secret")
        reconstructed = second.order_by_token(token)
        self.assertEqual(reconstructed["stripe_session_id"], "cs_live_1")
        self.assertTrue(second.save_intake(token, {"business": "Acme", "goal": "more leads"}))
        second.close()

    def test_dashboard_session_is_signed_and_expires(self):
        settings = replace(self.settings, dashboard_password="dashboard-test-password")
        cookie = session_cookie(settings)
        self.assertTrue(is_authenticated(settings, f"salee_dashboard_session={cookie}"))
        self.assertFalse(is_authenticated(settings, "salee_dashboard_session=not-valid"))

    def test_website_interest_captures_consent_and_goal(self):
        worker = RevenueWorker(replace(self.settings, checkout_url="https://buy.example/salee"), self.store)
        self.assertTrue(worker.capture_interest("buyer@example.com", "Acme", "more qualified leads"))
        self.assertEqual(self.store.contact("buyer@example.com")["consent"], "opted_in")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound'").fetchone()[0], 1)

    def test_model_roles_route_to_cheap_worker_and_strong_planner(self):
        settings = replace(
            self.settings,
            openrouter_worker_model="openrouter/free",
            openrouter_planner_model="openai/gpt-5.4",
            openrouter_qa_model="openai/gpt-5.4",
        )
        self.assertEqual(settings.model_for("worker"), "openrouter/free")
        self.assertEqual(settings.model_for("planner"), "openai/gpt-5.4")
        self.assertEqual(settings.model_for("qa"), "openai/gpt-5.4")

    def test_paid_order_gets_intake_and_automated_report(self):
        worker = RevenueWorker(self.settings, self.store)
        sent = []
        worker.mail.send = lambda to, subject, text: sent.append((to, subject, text)) or {"message_id": f"m-{len(sent)}"}
        worker.llm.complete = lambda system, user, max_tokens: "Workflow audit report"
        worker.register_payment("cs_test_1", "buyer@example.com", 150000)
        self.assertEqual(worker.process_orders(), 1)
        order = self.store.db.execute("SELECT * FROM orders WHERE stripe_session_id='cs_test_1'").fetchone()
        self.assertTrue(worker.submit_intake(order["intake_token"], {"business": "Acme", "goal": "more qualified leads"}))
        self.assertEqual(worker.process_orders(), 1)
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.store.db.execute("SELECT fulfillment_status FROM orders WHERE stripe_session_id='cs_test_1'").fetchone()[0], "fulfilled")

    def test_stripe_webhook_signature_verifies_raw_payload(self):
        payload = json.dumps({"id": "evt_1"}).encode()
        secret = "whsec_test"
        timestamp = int(time.time())
        digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        event = StripePayments.verify_webhook(payload, f"t={timestamp},v1={digest}", secret)
        self.assertEqual(event["id"], "evt_1")

    def test_growth_parser_accepts_fenced_json(self):
        self.assertEqual(_parse_proposal("```json\n{\"headline\":\"x\"}\n```"), {"headline": "x"})

    def test_prospecting_saves_scored_public_draft(self):
        settings = replace(self.settings, prospecting_queries="AI workflow", public_base_url="https://salee.example")
        engine = ProspectingEngine(settings, self.store)
        engine._discover = lambda query: [{
            "source": "test-source", "external_id": "1", "url": "https://example.com/post",
            "title": "AI workflow automation", "summary": "A public discussion about an AI workflow.", "author": "public-user",
        }]
        result = engine.run()
        self.assertEqual(result["saved"], 1)
        prospect = self.store.prospects(1)[0]
        self.assertEqual(prospect["status"], "draft")
        self.assertIn("https://salee.example", prospect["outreach_draft"])

    def test_autonomous_review_checks_landing_surface(self):
        worker = RevenueWorker(replace(self.settings, checkout_url="https://buy.example/salee"), self.store)
        result = worker.autonomous_review()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["issues"], [])


if __name__ == "__main__":
    unittest.main()
