"""Small server-side Supabase bridge for sharing prospect records across runtimes."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from config import Settings


class SharedProspects:
    table = "salee_prospects"

    def __init__(self, settings: Settings):
        self.base_url = settings.supabase_data_api_url.rstrip("/")
        self.secret_key = settings.supabase_secret_key

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.secret_key)

    def _request(self, method: str, url: str, payload: Any = None, headers: dict[str, str] | None = None) -> Any:
        merged = {
            "apikey": self.secret_key,
            "Authorization": f"Bearer {self.secret_key}",
            "Accept": "application/json",
            **(headers or {}),
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            merged["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=merged)
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def upsert(self, rows: list[dict[str, Any]]) -> bool:
        if not self.enabled or not rows:
            return False
        self._request(
            "POST",
            f"{self.base_url}/{self.table}?on_conflict=source,external_id",
            rows,
            {"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        return True

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        query = urllib.parse.urlencode({
            "select": "source,external_id,url,title,summary,author,match_score,outreach_draft,status,created_at,updated_at",
            "order": "match_score.desc,updated_at.desc",
            "limit": str(limit),
        })
        result = self._request("GET", f"{self.base_url}/{self.table}?{query}")
        return result if isinstance(result, list) else []

    def status(self) -> dict[str, Any]:
        rows = self.list(1000)
        if not rows:
            return {"prospects_discovered": 0, "prospect_drafts": 0, "prospecting_last_run": None}
        timestamps = [str(row.get("updated_at")) for row in rows if row.get("updated_at")]
        return {
            "prospects_discovered": len(rows),
            "prospect_drafts": sum(1 for row in rows if row.get("status") == "draft"),
            "prospecting_last_run": max(timestamps) if timestamps else None,
        }
