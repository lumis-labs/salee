"""Server-side shared state for Salee's generated growth content."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from config import Settings


class SharedGrowth:
    state_table = "salee_growth_state"
    artifact_table = "salee_growth_artifacts"

    def __init__(self, settings: Settings):
        self.base_url = settings.supabase_data_api_url.rstrip("/")
        self.secret_key = settings.supabase_secret_key

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.secret_key)

    def _request(self, method: str, url: str, payload: Any = None, headers: dict[str, str] | None = None) -> Any:
        merged = {"apikey": self.secret_key, "Authorization": f"Bearer {self.secret_key}", "Accept": "application/json", **(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            merged["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=merged)
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def get_runtime(self, key: str) -> str | None:
        if not self.enabled:
            return None
        query = urllib.parse.urlencode({"select": "value", "key": f"eq.{key}", "limit": "1"})
        rows = self._request("GET", f"{self.base_url}/{self.state_table}?{query}")
        return str(rows[0]["value"]) if rows else None

    def set_runtime(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        self._request("POST", f"{self.base_url}/{self.state_table}?on_conflict=key", [{"key": key, "value": value}], {"Prefer": "resolution=merge-duplicates,return=minimal"})

    def save_artifact(self, kind: str, slug: str, title: str, body: str, metadata: dict[str, Any], status: str = "published") -> None:
        if not self.enabled:
            return
        self._request("POST", f"{self.base_url}/{self.artifact_table}?on_conflict=slug", [{"kind": kind, "slug": slug, "title": title, "body": body, "metadata_json": json.dumps(metadata, sort_keys=True), "status": status}], {"Prefer": "resolution=merge-duplicates,return=minimal"})

    def artifacts(self, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        params = {"select": "kind,slug,title,body,metadata_json,status,created_at,updated_at", "status": "eq.published", "order": "updated_at.desc", "limit": str(limit)}
        if kind:
            params["kind"] = f"eq.{kind}"
        rows = self._request("GET", f"{self.base_url}/{self.artifact_table}?{urllib.parse.urlencode(params)}")
        return rows if isinstance(rows, list) else []

    def artifact(self, slug: str) -> dict[str, Any] | None:
        rows = self.artifacts(limit=100)
        return next((row for row in rows if row.get("slug") == slug), None)
