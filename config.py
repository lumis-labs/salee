"""Configuration loader with a deliberately small dependency surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    root: Path
    openrouter_api_key: str
    openrouter_model: str
    openrouter_worker_model: str
    openrouter_planner_model: str
    openrouter_qa_model: str
    openrouter_qa_enabled: bool
    openrouter_site_url: str
    openrouter_app_name: str
    agent_name: str
    agent_full_name: str
    dashboard_password: str
    agentmail_api_key: str
    agentmail_inbox: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    stripe_restricted_key: str
    stripe_webhook_secret: str
    stripe_currency: str
    database_path: Path
    poll_interval_seconds: int
    max_emails_per_day: int
    max_sms_per_day: int
    max_llm_calls_per_cycle: int
    public_base_url: str
    webhook_secret: str
    business_name: str
    business_description: str
    offer_name: str
    offer_price_cents: int
    checkout_url: str
    owner_email: str
    followup_enabled: bool
    followup_message: str
    followup_after_hours: int
    growth_enabled: bool
    growth_interval_hours: int
    growth_max_calls_per_day: int
    prospecting_enabled: bool
    prospecting_interval_minutes: int
    prospecting_max_items: int
    prospecting_queries: str
    prospecting_source_urls: str

    @classmethod
    def from_root(cls, root: Path) -> "Settings":
        _load_dotenv(root / ".env")
        def s(name: str, default: str = "") -> str:
            return os.getenv(name, default).strip()
        def i(name: str, default: int) -> int:
            try:
                return int(s(name, str(default)))
            except ValueError:
                return default

        def url(name: str) -> str:
            value = s(name).rstrip("/")
            if value and not value.startswith(("http://", "https://")):
                value = "https://" + value
            return value

        db = Path(s("DATABASE_PATH", "data/revenue_agent.sqlite3"))
        if os.getenv("VERCEL") and not os.getenv("DATABASE_PATH"):
            db = Path("/tmp/salee-revenue-agent.sqlite3")
        if not db.is_absolute():
            db = root / db
        return cls(
            root=root,
            openrouter_api_key=s("OPENROUTER_API_KEY"),
            openrouter_model=s("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            openrouter_worker_model=s("OPENROUTER_WORKER_MODEL", s("OPENROUTER_FREE_MODEL", s("OPENROUTER_MODEL", "openai/gpt-4o-mini"))),
            openrouter_planner_model=s("OPENROUTER_PLANNER_MODEL", s("OPENROUTER_MODEL", "openai/gpt-4o-mini")),
            openrouter_qa_model=s("OPENROUTER_QA_MODEL", s("OPENROUTER_PLANNER_MODEL", s("OPENROUTER_MODEL", "openai/gpt-4o-mini"))),
            openrouter_qa_enabled=s("GROWTH_QA_ENABLED", "true").lower() in {"1", "true", "yes"},
            openrouter_site_url=s("OPENROUTER_SITE_URL", "http://localhost:8080"),
            openrouter_app_name=s("OPENROUTER_APP_NAME", "Revenue Agent"),
            agent_name=s("AGENT_NAME", "Salee"),
            agent_full_name=s("AGENT_FULL_NAME", "Salee Arman"),
            dashboard_password=s("DASHBOARD_PASSWORD"),
            agentmail_api_key=s("AGENTMAIL_API_KEY"),
            agentmail_inbox=s("AGENTMAIL_INBOX"),
            twilio_account_sid=s("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=s("TWILIO_AUTH_TOKEN"),
            twilio_phone_number=s("TWILIO_PHONE_NUMBER"),
            stripe_restricted_key=s("STRIPE_RESTRICTED_KEY", s("STRIPE_SECRET_KEY")),
            stripe_webhook_secret=s("STRIPE_WEBHOOK_SECRET"),
            stripe_currency=s("STRIPE_CURRENCY", "usd").lower(),
            database_path=db,
            poll_interval_seconds=max(10, i("POLL_INTERVAL_SECONDS", 60)),
            max_emails_per_day=max(0, i("MAX_EMAILS_PER_DAY", 25)),
            max_sms_per_day=max(0, i("MAX_SMS_PER_DAY", 10)),
            max_llm_calls_per_cycle=max(1, i("MAX_LLM_CALLS_PER_CYCLE", 8)),
            public_base_url=url("PUBLIC_BASE_URL") or url("VERCEL_URL"),
            webhook_secret=s("WEBHOOK_SECRET"),
            business_name=s("BUSINESS_NAME", "Salee"),
            business_description=s("BUSINESS_DESCRIPTION", "Salee helps small businesses find and implement practical AI workflows that increase qualified leads, follow-up, and conversion."),
            offer_name=s("OFFER_NAME", "AI Revenue Workflow Sprint"),
            offer_price_cents=max(0, i("OFFER_PRICE_CENTS", 150000)),
            checkout_url=s("CHECKOUT_URL"),
            owner_email=s("OWNER_EMAIL"),
            followup_enabled=s("FOLLOW_UP_ENABLED", "true").lower() in {"1", "true", "yes"},
            followup_message=s("FOLLOW_UP_MESSAGE", "Hi {{first_name}}, Salee can map the fastest AI workflow improvements for your business in a focused sprint. Details: {{checkout_url}} Reply STOP to opt out."),
            followup_after_hours=max(1, i("FOLLOW_UP_AFTER_HOURS", 48)),
            growth_enabled=s("GROWTH_ENABLED", "true").lower() in {"1", "true", "yes"},
            growth_interval_hours=max(1, i("GROWTH_INTERVAL_HOURS", 6)),
            growth_max_calls_per_day=max(1, i("GROWTH_MAX_CALLS_PER_DAY", 4)),
            prospecting_enabled=s("PROSPECTING_ENABLED", "true").lower() in {"1", "true", "yes"},
            prospecting_interval_minutes=max(10, i("PROSPECTING_INTERVAL_MINUTES", 30)),
            prospecting_max_items=max(1, i("PROSPECTING_MAX_ITEMS", 20)),
            prospecting_queries=s("PROSPECTING_QUERIES", "AI workflow automation|lead follow up automation|small business AI|sales process automation"),
            prospecting_source_urls=s("PROSPECTING_SOURCE_URLS"),
        )

    @property
    def missing_revenue_config(self) -> list[str]:
        required = {
            "BUSINESS_NAME": self.business_name,
            "BUSINESS_DESCRIPTION": self.business_description,
            "OFFER_NAME": self.offer_name,
            "OFFER_PRICE_CENTS": self.offer_price_cents,
            "CHECKOUT_URL": self.checkout_url,
        }
        return [key for key, value in required.items() if not value]

    @property
    def revenue_ready(self) -> bool:
        return not self.missing_revenue_config

    @property
    def missing_operational_config(self) -> list[str]:
        missing: list[str] = []
        if not self.public_base_url:
            missing.append("PUBLIC_BASE_URL")
        if not self.stripe_webhook_secret:
            missing.append("STRIPE_WEBHOOK_SECRET")
        return missing

    @property
    def stripe_mode(self) -> str:
        key = self.stripe_restricted_key
        if key.startswith(("sk_test_", "rk_test_")):
            return "test"
        if key.startswith(("sk_live_", "rk_live_")):
            return "live"
        return "unknown" if key else "missing"

    def model_for(self, role: str) -> str:
        return {
            "worker": self.openrouter_worker_model,
            "planner": self.openrouter_planner_model,
            "qa": self.openrouter_qa_model,
        }.get(role, self.openrouter_worker_model)


def load_settings(root: str | Path = ".") -> Settings:
    return Settings.from_root(Path(root).resolve())
