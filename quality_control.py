"""Cheap, deterministic checks run before any outbound side effect."""

from __future__ import annotations

import re

from config import Settings


def validate_draft(text: str, settings: Settings, checkout_url: str | None = None, max_chars: int = 1500) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        errors.append("empty draft")
    if len(text) > max_chars:
        errors.append("draft exceeds channel length limit")
    if re.search(r"\b(guaranteed|risk[- ]free|100% guaranteed)\b", text, re.I):
        errors.append("unsupported certainty claim")
    if re.search(r"(?:OPENROUTER|TWILIO|AGENTMAIL)_[A-Z_]+|sk-[A-Za-z0-9_-]{12,}", text):
        errors.append("possible secret or internal credential in draft")
    configured_checkout = checkout_url or settings.checkout_url
    if configured_checkout and "checkout" in text.lower() and configured_checkout not in text:
        errors.append("draft references checkout but omits configured checkout URL")
    return errors
