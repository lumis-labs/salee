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
from shared_growth import SharedGrowth
from store import Store

GROWTH_PROMPT = """You are Salee Arman's growth strategist. Improve conversion for the configured B2B AI workflow service using only the supplied evidence and public competitive research. Return strict JSON with keys: headline, subheadline, landing_variant (one of editorial, direct, consultative), faq (array of {question, answer}), blog_title, blog_slug, blog_body, seo_description, geo_summary, experiment_name, experiment_hypothesis, experiment_variant. Choose the landing variant based on the current evidence: editorial for education-led trust, direct for clear action and offer focus, consultative for high-context lead capture. Use research to identify stale or generic positioning to avoid, but do not copy competitors. Keep claims specific and honest; no guaranteed outcomes, fake testimonials, invented case studies, private traffic estimates, or unsupported statistics. The blog must be useful, original, and under 900 words. The experiment may change positioning or packaging but must not silently change price or payment behavior."""
GROWTH_QA_PROMPT = """You are Salee Arman's final quality reviewer. Review the proposed landing copy and blog for factual support, clarity, conversion value, SEO/GEO usefulness, and policy safety. Return strict JSON only: {\"approved\": true or false, \"issues\": [short strings]}. Reject unsupported guarantees, fake proof, invented statistics, manipulative urgency, or missing customer value. Approve useful, specific copy."""


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
        self.shared = SharedGrowth(settings)

    def _get_runtime(self, key: str) -> str | None:
        local = self.store.get_runtime(key)
        if local:
            return local
        if self.shared.enabled:
            return self.shared.get_runtime(key)
        return None

    def _set_runtime(self, key: str, value: str) -> None:
        self.store.set_runtime(key, value)
        if self.shared.enabled:
            self.shared.set_runtime(key, value)

    def artifacts(self, kind: str | None = None, limit: int = 20) -> list[Any]:
        local = self.store.artifacts(kind, limit)
        if local:
            return local
        return self.shared.artifacts(kind, limit) if self.shared.enabled else local

    def artifact(self, slug: str) -> Any:
        local = self.store.artifact(slug)
        if local:
            return local
        return self.shared.artifact(slug) if self.shared.enabled else None

    def due(self) -> bool:
        if not self.settings.growth_enabled:
            return False
        last = self._get_runtime("growth_last_run") or self._get_runtime("growth_last_attempt")
        if not last:
            return True
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(hours=self.settings.growth_interval_hours)
        except ValueError:
            return True

    def context(self) -> str:
        status = self.store.status()
        existing = [f"{row['kind']}: {row['title']}" for row in self.artifacts(limit=8)]
        return json.dumps({
            "agent": self.settings.agent_full_name,
            "business": self.settings.business_description,
            "offer": self.settings.offer_name,
            "price_cents": self.settings.offer_price_cents,
            "checkout_available": bool(self.settings.checkout_url or self.store.get_runtime("checkout_url")),
            "metrics": status,
            "public_competitive_research": json.loads(self._get_runtime("competitive_observations") or "[]"),
            "competitive_research_last_run": self._get_runtime("competitive_last_run"),
            "existing_artifacts": existing,
        }, sort_keys=True)

    def run(self) -> dict[str, Any]:
        if not self.due():
            return {"status": "not_due"}
        self._set_runtime("growth_last_attempt", datetime.now(timezone.utc).isoformat())
        try:
            raw = self.complete(GROWTH_PROMPT, self.context(), max_tokens=1600, role="planner")
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

        if self.settings.openrouter_qa_enabled:
            qa_input = json.dumps({key: proposal.get(key) for key in required}, ensure_ascii=False)
            try:
                qa_raw = self.complete(GROWTH_QA_PROMPT, qa_input, max_tokens=500, role="qa")
                qa = _parse_proposal(qa_raw)
            except Exception as exc:
                self.store.audit("growth_qa_error", {"error": str(exc)})
                return {"status": "qa_error", "error": str(exc)}
            if qa.get("approved") is not True:
                issues = qa.get("issues") if isinstance(qa.get("issues"), list) else ["QA reviewer did not approve the proposal"]
                self.store.audit("growth_qa_blocked", {"issues": issues[:8]})
                return {"status": "qa_blocked", "errors": issues[:8]}

        faq = proposal["faq"] if isinstance(proposal["faq"], list) else []
        self._set_runtime("landing_headline", str(proposal["headline"])[:180])
        self._set_runtime("landing_subheadline", str(proposal["subheadline"])[:400])
        variant = str(proposal.get("landing_variant") or "editorial").lower()
        if variant not in {"editorial", "direct", "consultative"}:
            variant = "editorial"
        self._set_runtime("landing_variant", variant)
        self._set_runtime("seo_description", str(proposal["seo_description"])[:300])
        self._set_runtime("geo_summary", str(proposal["geo_summary"])[:1000])
        self._set_runtime("faq_json", json.dumps(faq[:8]))
        blog_slug = _slug(str(proposal.get("blog_slug") or proposal["blog_title"]))
        metadata = {"seo_description": proposal["seo_description"]}
        self.store.save_artifact("blog", blog_slug, str(proposal["blog_title"])[:180], str(proposal["blog_body"])[:12000], metadata)
        if self.shared.enabled:
            self.shared.save_artifact("blog", blog_slug, str(proposal["blog_title"])[:180], str(proposal["blog_body"])[:12000], metadata)
        if proposal.get("experiment_name") and proposal.get("experiment_hypothesis"):
            self.store.save_experiment(str(proposal["experiment_name"])[:180], str(proposal["experiment_hypothesis"])[:500], {"variant": proposal.get("experiment_variant", "")})
        self._set_runtime("growth_last_run", datetime.now(timezone.utc).isoformat())
        self.store.audit("growth_published", {"blog_slug": blog_slug, "faq_count": len(faq), "experiment": bool(proposal.get("experiment_name"))})
        return {"status": "published", "blog_slug": blog_slug, "faq_count": len(faq)}

    def landing_page(self, checkout_url: str) -> str:
        headline = self._get_runtime("landing_headline") or "Find the AI workflow that can move revenue first."
        subheadline = self._get_runtime("landing_subheadline") or "Salee Arman maps your current lead, follow-up, and conversion process into a focused implementation plan—so you know what to automate first, what to measure, and what to leave alone."
        description = self._get_runtime("seo_description") or self.settings.business_description
        faq = json.loads(self._get_runtime("faq_json") or "[]")
        if not faq:
            faq = [
                {"question": "What do I receive?", "answer": "A practical workflow audit, three prioritized opportunities, a 14-day implementation plan, and suggested success metrics."},
                {"question": "Is this a generic AI strategy report?", "answer": "No. Salee starts from your current sales, follow-up, and operational context and keeps recommendations tied to the facts you provide."},
                {"question": "How long does it take?", "answer": "The sprint is designed to produce a focused plan for the next 14 days. It does not promise a guaranteed revenue outcome."},
            ]
        faq_html = "".join(f"<details><summary>{escape(str(item.get('question','')))}</summary><p>{escape(str(item.get('answer','')))}</p></details>" for item in faq if isinstance(item, dict))
        cta = f'<a class="cta" href="{escape(checkout_url, quote=True)}">Start the sprint — ${self.settings.offer_price_cents / 100:,.0f}</a>' if checkout_url else '<span class="pending">Checkout is being prepared.</span>'
        origin = self.settings.public_base_url or "http://localhost:8080"
        structured = json.dumps({"@context": "https://schema.org", "@type": "Service", "name": headline, "description": description, "provider": {"@type": "Organization", "name": self.settings.agent_full_name}, "offers": {"@type": "Offer", "price": self.settings.offer_price_cents / 100, "priceCurrency": self.settings.stripe_currency, "url": checkout_url}}, ensure_ascii=False)
        variant = self._get_runtime("landing_variant") or "editorial"
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(headline)}</title><link rel="canonical" href="{escape(origin, quote=True)}"><meta name="description" content="{escape(description, quote=True)}"><meta property="og:title" content="{escape(headline, quote=True)}"><meta property="og:description" content="{escape(subheadline, quote=True)}"><meta property="og:type" content="website"><meta name="viewport" content="width=device-width,initial-scale=1"><script type="application/ld+json">{escape(structured, quote=False)}</script><style>
*{{box-sizing:border-box}}:root{{--ink:#16212b;--muted:#63717c;--line:#dce3e7;--soft:#f4f7f8;--accent:#0f766e}}body{{margin:0;color:var(--ink);font:16px/1.6 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}body.variant-direct{{--accent:#b45309}}body.variant-consultative{{--accent:#315c8c}}main{{max-width:1080px;margin:auto;padding:24px}}nav{{display:flex;justify-content:space-between;align-items:center;padding:8px 0 72px}}nav strong{{letter-spacing:-.03em}}nav a{{color:var(--ink);text-decoration:none;margin-left:22px;font-size:14px}}.hero{{max-width:820px;padding-bottom:80px}}.variant-direct .hero{{max-width:900px}}.variant-consultative .hero{{max-width:700px}}.hero h1{{font-size:clamp(2.8rem,7vw,6.4rem);line-height:.98;letter-spacing:-.07em;margin:0 0 24px;max-width:800px}}.hero>p{{font-size:clamp(1.1rem,2vw,1.35rem);color:var(--muted);max-width:680px;margin:0 0 28px}}.cta{{display:inline-block;background:var(--ink);color:#fff;padding:14px 20px;border-radius:7px;text-decoration:none;font-weight:700}}.secondary{{display:inline-block;color:var(--accent);margin-left:18px;font-weight:700;text-decoration:none}}.note{{color:var(--muted);font-size:13px;margin-top:14px}}.band{{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:46px 0;display:grid;grid-template-columns:1fr 1.7fr;gap:50px}}h2{{font-size:clamp(1.7rem,3vw,2.7rem);line-height:1.05;letter-spacing:-.05em;margin:0}}h3{{margin:0 0 8px;font-size:18px}}.muted{{color:var(--muted)}}.steps{{display:grid;grid-template-columns:repeat(3,1fr);gap:26px}}.step{{border-top:3px solid var(--accent);padding-top:14px}}.step span{{color:var(--accent);font-weight:800;font-size:13px}}.offer{{padding:70px 0;display:grid;grid-template-columns:1fr 1fr;gap:60px}}ul{{padding-left:20px}}li{{margin:10px 0}}.lead-box{{background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:28px}}.lead-box h3{{font-size:23px;letter-spacing:-.04em}}label{{display:block;color:var(--muted);font-size:13px;margin:14px 0 5px}}input,textarea{{font:inherit;width:100%;border:1px solid #c9d4d9;border-radius:6px;padding:11px;background:#fff;color:var(--ink)}}textarea{{min-height:90px;resize:vertical}}.consent{{display:flex;gap:8px;align-items:flex-start;font-size:12px}}.consent input{{width:auto;margin-top:5px}}button{{border:0;border-radius:7px;background:var(--accent);color:white;padding:12px 16px;font:inherit;font-weight:700;cursor:pointer;margin-top:8px}}.faq{{padding:0 0 70px;max-width:760px}}details{{padding:16px 0;border-bottom:1px solid var(--line)}}summary{{font-weight:700;cursor:pointer}}details p{{color:var(--muted);margin-bottom:0}}footer{{border-top:1px solid var(--line);padding:24px 0 50px;color:var(--muted);font-size:13px}}@media(max-width:760px){{nav{{padding-bottom:45px}}nav a{{margin-left:10px}}.band,.offer{{grid-template-columns:1fr;gap:28px;padding:42px 0}}.steps{{grid-template-columns:1fr;gap:22px}}.secondary{{display:block;margin:15px 0 0}}}}
</style></head><body class="variant-{escape(variant, quote=True)}"><main><nav><strong>{escape(self.settings.agent_full_name)}</strong><div><a href="#how">How it works</a><a href="#intake">Get a next step</a><a href="/blog">Insights</a></div></nav><section class="hero"><h1>{escape(headline)}</h1><p>{escape(subheadline)}</p><div>{cta}<a class="secondary" href="#intake">Share your goal →</a></div><p class="note">A focused ${{self.settings.offer_price_cents / 100:,.0f}} sprint. Honest recommendations; no guaranteed outcomes.</p></section><section class="band" id="how"><div><h2>Make the next AI decision easier.</h2><p class="muted">Most teams do not need another list of tools. They need a clear order of operations.</p></div><div class="steps"><div class="step"><span>01 · MAP</span><h3>Understand the current flow</h3><p class="muted">Where leads arrive, where follow-up slows down, and where work is repeated.</p></div><div class="step"><span>02 · PRIORITIZE</span><h3>Choose the highest-leverage moves</h3><p class="muted">A short list grounded in your constraints, not generic AI hype.</p></div><div class="step"><span>03 · EXECUTE</span><h3>Leave with a 14-day plan</h3><p class="muted">Concrete next actions, ownership, and metrics to watch.</p></div></div></section><section class="offer"><div><h2>What you receive</h2><ul><li>Current-state workflow summary</li><li>Three prioritized AI revenue opportunities</li><li>A practical 14-day implementation plan</li><li>Suggested success metrics and assumptions</li></ul><p class="muted">Designed for small teams that want useful decisions before adding more software.</p></div><div class="lead-box" id="intake"><h3>Not ready to buy?</h3><p class="muted">Tell Salee what you are trying to improve and get one practical next step by email.</p><form method="post" action="/interest"><label>Email<input type="email" name="email" autocomplete="email" required></label><label>Business or website<input name="business" required></label><label>Biggest growth goal<textarea name="goal" required></label><label class="consent"><input type="checkbox" name="consent" value="yes" required><span>I agree to receive a useful reply from Salee. I can reply STOP at any time.</span></label><button type="submit">Get a practical next step</button></form></div></section><section class="faq"><h2>Common questions</h2>{faq_html}</section><footer><div>{escape(self.settings.agent_full_name)} · {escape(self.settings.business_name)}</div><div><a href="/blog">Insights</a> · <a href="/sitemap.xml">Sitemap</a> · <a href="/llms.txt">AI-readable summary</a></div></footer></main></body></html>"""
