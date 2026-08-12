"""Small, low-frequency growth specialist and deterministic publishing layer.

The specialist proposes copy/content/experiments. Local quality control and the
store decide what is publishable. Payment prices are never silently changed by
the growth loop; offer pivots are recorded as experiments.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Callable

from config import Settings
from quality_control import validate_draft
from store import Store

GROWTH_PROMPT = """You are Salee Arman's growth strategist. Improve conversion for the configured B2B AI workflow service using only the supplied evidence. Return strict JSON with keys: headline, subheadline, faq (array of {question, answer}), blog_title, blog_slug, blog_body, seo_description, geo_summary, experiment_name, experiment_hypothesis, experiment_variant. Keep claims specific and honest; no guaranteed outcomes, fake testimonials, invented case studies, or unsupported statistics. The blog must be useful, original, and under 900 words. The experiment may change positioning or packaging but must not silently change price or payment behavior."""


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:80] or "salee-update"


def _parse_proposal(raw: str) -> dict[str, Any]:
    """Accept strict JSON plus the common fenced/extra-text model variants."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("growth response was not an object")
    return value


class GrowthEngine:
    def __init__(self, settings: Settings, store: Store, complete: Callable[..., str]):
        self.settings = settings
        self.store = store
        self.complete = complete

    def due(self) -> bool:
        if not self.settings.growth_enabled:
            return False
        last = self.store.get_runtime("growth_last_run") or self.store.get_runtime("growth_last_attempt")
        if not last:
            return True
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(hours=self.settings.growth_interval_hours)
        except ValueError:
            return True

    def context(self) -> str:
        status = self.store.status()
        existing = [f"{row['kind']}: {row['title']}" for row in self.store.artifacts(limit=8)]
        return json.dumps({
            "agent": self.settings.agent_full_name,
            "business": self.settings.business_description,
            "offer": self.settings.offer_name,
            "price_cents": self.settings.offer_price_cents,
            "checkout_available": bool(self.settings.checkout_url or self.store.get_runtime("checkout_url")),
            "metrics": status,
            "existing_artifacts": existing,
        }, sort_keys=True)

    def run(self) -> dict[str, Any]:
        if not self.due():
            return {"status": "not_due"}
        self.store.set_runtime("growth_last_attempt", datetime.now(timezone.utc).isoformat())
        try:
            raw = self.complete(GROWTH_PROMPT, self.context(), max_tokens=1600)
            proposal = _parse_proposal(raw)
        except Exception as exc:
            self.store.audit("growth_error", {"error": str(exc)})
            return {"status": "error", "error": str(exc)}

        required = ("headline", "subheadline", "faq", "blog_title", "blog_body", "seo_description", "geo_summary")
        missing = [key for key in required if not proposal.get(key)]
        if missing:
            self.store.audit("growth_quality_blocked", {"errors": [f"missing {key}" for key in missing]})
            return {"status": "blocked", "errors": missing}
        combined = "\n".join(str(proposal.get(key, "")) for key in required)
        errors = validate_draft(combined, self.settings, self.settings.checkout_url, max_chars=12000)
        if errors:
            self.store.audit("growth_quality_blocked", {"errors": errors})
            return {"status": "blocked", "errors": errors}

        faq = proposal["faq"] if isinstance(proposal["faq"], list) else []
        self.store.set_runtime("landing_headline", str(proposal["headline"])[:180])
        self.store.set_runtime("landing_subheadline", str(proposal["subheadline"])[:400])
        self.store.set_runtime("seo_description", str(proposal["seo_description"])[:300])
        self.store.set_runtime("geo_summary", str(proposal["geo_summary"])[:1000])
        self.store.set_runtime("faq_json", json.dumps(faq[:8]))
        blog_slug = _slug(str(proposal.get("blog_slug") or proposal["blog_title"]))
        self.store.save_artifact("blog", blog_slug, str(proposal["blog_title"])[:180], str(proposal["blog_body"])[:12000], {"seo_description": proposal["seo_description"]})
        if proposal.get("experiment_name") and proposal.get("experiment_hypothesis"):
            self.store.save_experiment(str(proposal["experiment_name"])[:180], str(proposal["experiment_hypothesis"])[:500], {"variant": proposal.get("experiment_variant", "")})
        self.store.set_runtime("growth_last_run", datetime.now(timezone.utc).isoformat())
        self.store.audit("growth_published", {"blog_slug": blog_slug, "faq_count": len(faq), "experiment": bool(proposal.get("experiment_name"))})
        return {"status": "published", "blog_slug": blog_slug, "faq_count": len(faq)}

    def landing_page(self, checkout_url: str) -> str:
        headline = self.store.get_runtime("landing_headline") or self.settings.offer_name
        subheadline = self.store.get_runtime("landing_subheadline") or "A focused, practical sprint that identifies the highest-leverage AI workflow improvements for your business and turns them into an execution plan."
        description = self.store.get_runtime("seo_description") or self.settings.business_description
        faq = json.loads(self.store.get_runtime("faq_json") or "[]")
        faq_html = "".join(f"<details><summary>{escape(str(item.get('question','')))}</summary><p>{escape(str(item.get('answer','')))}</p></details>" for item in faq if isinstance(item, dict))
        cta = f'<a class="cta" href="{escape(checkout_url, quote=True)}">Start the sprint — ${self.settings.offer_price_cents / 100:,.0f}</a>' if checkout_url else '<span class="pending">Checkout is being prepared.</span>'
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(headline)}</title><meta name="description" content="{escape(description, quote=True)}"><meta name="viewport" content="width=device-width,initial-scale=1"><script type="application/ld+json">{escape(json.dumps({'@context':'https://schema.org','@type':'Service','name':headline,'description':description,'provider':{'@type':'Organization','name':self.settings.agent_full_name}}, ensure_ascii=False), quote=False)}</script><style>body{{font:16px system-ui;max-width:760px;margin:10vh auto;padding:24px;color:#17202a}}h1{{font-size:clamp(2rem,5vw,4rem);margin-bottom:12px}}p{{line-height:1.6}}.cta{{display:inline-block;background:#111;color:#fff;padding:14px 20px;border-radius:8px;text-decoration:none}}.pending{{color:#666}}details{{padding:12px 0;border-bottom:1px solid #ddd}}</style></head><body><p>{escape(self.settings.agent_full_name)}</p><h1>{escape(headline)}</h1><p>{escape(subheadline)}</p>{cta}<p><small>Honest recommendations, no guaranteed outcomes.</small></p><section><h2>Common questions</h2>{faq_html}</section><p><a href="/blog">Read the latest insights</a></p></body></html>"""
