"""Durable state, idempotency, and operational audit trail."""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
              address TEXT PRIMARY KEY, channel TEXT NOT NULL,
              consent TEXT NOT NULL DEFAULT 'unknown',
              opted_out_at TEXT, last_contact_at TEXT, last_followup_at TEXT,
              followup_count INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS processed_events (
              event_id TEXT PRIMARY KEY, channel TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT UNIQUE,
              channel TEXT NOT NULL, direction TEXT NOT NULL, contact TEXT NOT NULL,
              subject TEXT, body TEXT NOT NULL, thread_id TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, channel TEXT,
              contact TEXT, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL, resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_config (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbound_queue (
              external_id TEXT PRIMARY KEY, channel TEXT NOT NULL,
              payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
              stripe_session_id TEXT PRIMARY KEY, email TEXT NOT NULL,
              amount_total INTEGER NOT NULL DEFAULT 0, intake_token TEXT UNIQUE NOT NULL,
              intake_json TEXT, intake_sent_at TEXT,
              status TEXT NOT NULL DEFAULT 'paid',
              fulfillment_status TEXT NOT NULL DEFAULT 'awaiting_intake',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS growth_artifacts (
              id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
              slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'published',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS growth_experiments (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              hypothesis TEXT NOT NULL, variant_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prospects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL, external_id TEXT NOT NULL,
              url TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
              author TEXT, match_score INTEGER NOT NULL DEFAULT 0,
              outreach_draft TEXT, status TEXT NOT NULL DEFAULT 'discovered',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(source, external_id)
            );
            """
        )
        for column, definition in (
            ("last_followup_at", "TEXT"),
            ("followup_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                self.db.execute(f"ALTER TABLE contacts ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def audit(self, event: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO audit_log(event,payload_json,created_at) VALUES (?,?,?)",
            (event, json.dumps(payload, sort_keys=True), utcnow()),
        )
        self.db.commit()

    def get_runtime(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM runtime_config WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_runtime(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO runtime_config(key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utcnow()),
        )
        self.db.commit()

    def seen(self, event_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)).fetchone() is not None

    def mark_seen(self, event_id: str, channel: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO processed_events(event_id,channel,created_at) VALUES (?,?,?)",
            (event_id, channel, utcnow()),
        )
        self.db.commit()

    def upsert_contact(self, address: str, channel: str, consent: str | None = None) -> None:
        row = self.db.execute("SELECT consent FROM contacts WHERE address=?", (address,)).fetchone()
        resolved = consent or (row["consent"] if row else "unknown")
        self.db.execute(
            """INSERT INTO contacts(address,channel,consent) VALUES (?,?,?)
               ON CONFLICT(address) DO UPDATE SET channel=excluded.channel, consent=excluded.consent""",
            (address, channel, resolved),
        )
        self.db.commit()

    def set_consent(self, address: str, channel: str, consent: str) -> None:
        self.upsert_contact(address, channel, consent)
        if consent == "opted_out":
            self.db.execute("UPDATE contacts SET opted_out_at=? WHERE address=?", (utcnow(), address))
        self.db.commit()

    def contact(self, address: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM contacts WHERE address=?", (address,)).fetchone()

    def record_message(self, external_id: str, channel: str, direction: str, contact: str, body: str, subject: str = "", thread_id: str = "") -> bool:
        try:
            self.db.execute(
                """INSERT INTO messages(external_id,channel,direction,contact,subject,body,thread_id,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (external_id, channel, direction, contact, subject, body, thread_id, utcnow()),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_messages(self, contact: str, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM messages WHERE contact=? ORDER BY id DESC LIMIT ?", (contact, limit)
        ))

    def count_sent_today(self, channel: str) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel=? AND direction='outbound' AND created_at >= date('now')",
            (channel,),
        ).fetchone()[0])

    def touch_contact(self, address: str) -> None:
        self.db.execute("UPDATE contacts SET last_contact_at=? WHERE address=?", (utcnow(), address))
        self.db.commit()

    def followup_contacts(self, cutoff: str, limit: int = 25) -> list[sqlite3.Row]:
        return list(self.db.execute(
            """SELECT * FROM contacts
               WHERE consent='opted_in'
                 AND (last_followup_at IS NULL OR last_followup_at <= ?)
               ORDER BY COALESCE(last_followup_at, '1970-01-01') LIMIT ?""",
            (cutoff, limit),
        ))

    def touch_followup(self, address: str) -> None:
        self.db.execute(
            "UPDATE contacts SET last_followup_at=?, followup_count=followup_count+1, last_contact_at=? WHERE address=?",
            (utcnow(), utcnow(), address),
        )
        self.db.commit()

    def create_decision(self, kind: str, channel: str, contact: str, payload: dict[str, Any]) -> int:
        cursor = self.db.execute(
            "INSERT INTO decisions(kind,channel,contact,payload_json,created_at) VALUES (?,?,?,?,?)",
            (kind, channel, contact, json.dumps(payload), utcnow()),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def enqueue_inbound(self, event_id: str, channel: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO inbound_queue(external_id,channel,payload_json,created_at) VALUES (?,?,?,?)",
            (event_id, channel, json.dumps(payload), utcnow()),
        )
        self.db.commit()

    def pending_inbound(self, limit: int = 25) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM inbound_queue ORDER BY created_at LIMIT ?", (limit,)))

    def finish_inbound(self, event_id: str) -> None:
        self.db.execute("DELETE FROM inbound_queue WHERE external_id=?", (event_id,))
        self.db.commit()

    def retry_inbound(self, event_id: str) -> None:
        self.db.execute("UPDATE inbound_queue SET attempts=attempts+1 WHERE external_id=?", (event_id,))
        self.db.commit()

    def register_order(self, stripe_session_id: str, email: str, amount_total: int) -> sqlite3.Row:
        existing = self.db.execute("SELECT * FROM orders WHERE stripe_session_id=?", (stripe_session_id,)).fetchone()
        if existing:
            return existing
        now = utcnow()
        self.db.execute(
            "INSERT INTO orders(stripe_session_id,email,amount_total,intake_token,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (stripe_session_id, email, amount_total, secrets.token_urlsafe(24), now, now),
        )
        self.db.commit()
        return self.db.execute("SELECT * FROM orders WHERE stripe_session_id=?", (stripe_session_id,)).fetchone()

    def order_by_token(self, token: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM orders WHERE intake_token=?", (token,)).fetchone()

    def save_intake(self, token: str, intake: dict[str, str]) -> bool:
        cursor = self.db.execute(
            "UPDATE orders SET intake_json=?, fulfillment_status='pending', updated_at=? WHERE intake_token=? AND status='paid'",
            (json.dumps(intake, sort_keys=True), utcnow(), token),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def orders_needing_intake_email(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM orders WHERE status='paid' AND intake_sent_at IS NULL ORDER BY created_at LIMIT ?", (limit,)
        ))

    def mark_intake_sent(self, stripe_session_id: str) -> None:
        self.db.execute("UPDATE orders SET intake_sent_at=?, updated_at=? WHERE stripe_session_id=?", (utcnow(), utcnow(), stripe_session_id))
        self.db.commit()

    def orders_needing_fulfillment(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM orders WHERE status='paid' AND fulfillment_status='pending' ORDER BY updated_at LIMIT ?", (limit,)
        ))

    def mark_fulfilled(self, stripe_session_id: str) -> None:
        self.db.execute("UPDATE orders SET fulfillment_status='fulfilled', updated_at=? WHERE stripe_session_id=?", (utcnow(), stripe_session_id))
        self.db.commit()

    def save_artifact(self, kind: str, slug: str, title: str, body: str, metadata: dict[str, Any] | None = None) -> None:
        now = utcnow()
        self.db.execute(
            """INSERT INTO growth_artifacts(kind,slug,title,body,metadata_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET kind=excluded.kind,title=excluded.title,body=excluded.body,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (kind, slug, title, body, json.dumps(metadata or {}, sort_keys=True), now, now),
        )
        self.db.commit()

    def artifact(self, slug: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM growth_artifacts WHERE slug=? AND status='published'", (slug,)).fetchone()

    def artifacts(self, kind: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if kind:
            return list(self.db.execute("SELECT * FROM growth_artifacts WHERE kind=? AND status='published' ORDER BY updated_at DESC LIMIT ?", (kind, limit)))
        return list(self.db.execute("SELECT * FROM growth_artifacts WHERE status='published' ORDER BY updated_at DESC LIMIT ?", (limit,)))

    def save_experiment(self, name: str, hypothesis: str, variant: dict[str, Any]) -> None:
        now = utcnow()
        self.db.execute(
            "INSERT INTO growth_experiments(name,hypothesis,variant_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (name, hypothesis, json.dumps(variant, sort_keys=True), now, now),
        )
        self.db.commit()

    def save_prospect(self, source: str, external_id: str, url: str, title: str, summary: str, author: str, match_score: int, outreach_draft: str) -> None:
        now = utcnow()
        self.db.execute(
            """INSERT INTO prospects(source,external_id,url,title,summary,author,match_score,outreach_draft,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source,external_id) DO UPDATE SET title=excluded.title,summary=excluded.summary,author=excluded.author,match_score=excluded.match_score,outreach_draft=excluded.outreach_draft,updated_at=excluded.updated_at""",
            (source, external_id, url, title[:300], summary[:4000], author[:200], max(0, min(100, match_score)), outreach_draft[:4000], "draft", now, now),
        )
        self.db.commit()

    def prospects(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM prospects ORDER BY match_score DESC, updated_at DESC LIMIT ?", (limit,)))

    def status(self) -> dict[str, Any]:
        return {
            "messages": int(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            "pending_decisions": int(self.db.execute("SELECT COUNT(*) FROM decisions WHERE status='pending'").fetchone()[0]),
            "queued_inbound": int(self.db.execute("SELECT COUNT(*) FROM inbound_queue").fetchone()[0]),
            "checkout_link_ready": bool(self.get_runtime("checkout_url")),
            "paid_orders": int(self.db.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0]),
            "pending_fulfillment": int(self.db.execute("SELECT COUNT(*) FROM orders WHERE fulfillment_status='pending'").fetchone()[0]),
            "published_growth_artifacts": int(self.db.execute("SELECT COUNT(*) FROM growth_artifacts WHERE status='published'").fetchone()[0]),
            "proposed_experiments": int(self.db.execute("SELECT COUNT(*) FROM growth_experiments WHERE status='proposed'").fetchone()[0]),
            "prospects_discovered": int(self.db.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]),
            "prospect_drafts": int(self.db.execute("SELECT COUNT(*) FROM prospects WHERE status='draft'").fetchone()[0]),
            "prospecting_last_run": self.get_runtime("prospecting_last_run"),
            "last_audit": self.db.execute("SELECT created_at FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
            if self.db.execute("SELECT 1 FROM audit_log LIMIT 1").fetchone() else None,
        }


class VercelStore(Store):
    """Ephemeral SQLite plus signed order tokens for serverless invocations.

    Vercel's filesystem is not durable between invocations. Orders therefore
    carry their signed checkout identity in the intake token, allowing a cold
    invocation to reconstruct the minimum order record and complete fulfillment.
    The always-on worker continues to use regular durable SQLite.
    """

    def __init__(self, path: Path, signing_key: str):
        self.signing_key = signing_key.encode()
        super().__init__(path)

    def _token(self, session_id: str, email: str, amount_total: int) -> str:
        payload = json.dumps(
            {"session_id": session_id, "email": email, "amount_total": amount_total},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"v1.{encoded}.{signature}"

    def _decode(self, token: str) -> dict[str, Any] | None:
        try:
            version, encoded, signature = token.split(".", 2)
            expected = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
            if version != "v1" or not hmac.compare_digest(signature, expected):
                return None
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode())
            if not all(payload.get(key) for key in ("session_id", "email")):
                return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def register_order(self, stripe_session_id: str, email: str, amount_total: int) -> sqlite3.Row:
        existing = self.db.execute("SELECT * FROM orders WHERE stripe_session_id=?", (stripe_session_id,)).fetchone()
        if existing:
            return existing
        now = utcnow()
        self.db.execute(
            "INSERT INTO orders(stripe_session_id,email,amount_total,intake_token,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (stripe_session_id, email, amount_total, self._token(stripe_session_id, email, amount_total), now, now),
        )
        self.db.commit()
        return self.db.execute("SELECT * FROM orders WHERE stripe_session_id=?", (stripe_session_id,)).fetchone()

    def order_by_token(self, token: str) -> sqlite3.Row | dict[str, Any] | None:
        row = super().order_by_token(token)
        if row:
            return row
        payload = self._decode(token)
        if not payload:
            return None
        now = utcnow()
        return {
            "stripe_session_id": payload["session_id"],
            "email": payload["email"],
            "amount_total": int(payload.get("amount_total") or 0),
            "intake_token": token,
            "intake_json": None,
            "intake_sent_at": None,
            "status": "paid",
            "fulfillment_status": "awaiting_intake",
            "created_at": now,
            "updated_at": now,
        }

    def save_intake(self, token: str, intake: dict[str, str]) -> bool:
        if not super().order_by_token(token):
            payload = self._decode(token)
            if not payload:
                return False
            self.register_order(payload["session_id"], payload["email"], int(payload.get("amount_total") or 0))
        return super().save_intake(token, intake)


def build_store(settings: Any) -> Store:
    if os.getenv("VERCEL"):
        key = getattr(settings, "stripe_webhook_secret", "") or getattr(settings, "stripe_restricted_key", "")
        if key:
            return VercelStore(settings.database_path, key)
    return Store(settings.database_path)
