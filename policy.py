"""Policy gates for outbound communication and autonomous execution."""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import Settings
from store import Store

UNSUBSCRIBE = re.compile(r"\b(stop|unsubscribe|remove me|opt out|do not contact)\b", re.I)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def note_inbound(store: Store, address: str, channel: str, body: str) -> None:
    store.upsert_contact(address, channel)
    if UNSUBSCRIBE.search(body):
        store.set_consent(address, channel, "opted_out")
    else:
        # Replying to an inbound message is treated as an existing conversation,
        # not as a new cold campaign.
        store.set_consent(address, channel, "inbound")


def can_send(store: Store, settings: Settings, address: str, channel: str, is_reply: bool) -> Decision:
    contact = store.contact(address)
    if contact and contact["consent"] == "opted_out":
        return Decision(False, "contact opted out")
    if not is_reply and not contact:
        return Decision(False, "new outbound contact requires recorded opt-in")
    if not is_reply and contact["consent"] != "opted_in":
        return Decision(False, "outbound contact is not opted in")
    if channel == "email" and store.count_sent_today(channel) >= settings.max_emails_per_day:
        return Decision(False, "daily email limit reached")
    if channel == "sms" and store.count_sent_today(channel) >= settings.max_sms_per_day:
        return Decision(False, "daily SMS limit reached")
    return Decision(True, "allowed")

