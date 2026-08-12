"""Public competitive-positioning research for Salee's growth loop."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

from config import Settings
from shared_growth import SharedGrowth
from store import Store


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self._tag = ""
        self._capture = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        self._tag = tag
        if tag == "meta" and attrs_dict.get("name", "").lower() == "description":
            self.description = (attrs_dict.get("content") or "")[:500]
        if tag == "title" or tag in {"h1", "h2"}:
            self._capture = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag == self._tag:
            value = re.sub(r"\s+", " ", " ".join(self._buffer)).strip()
            if tag == "title":
                self.title = value[:300]
            elif value:
                self.headings.append(value[:240])
            self._capture = False


class CompetitiveResearch:
    def __init__(self, settings: Settings, store: Store, shared: SharedGrowth | None = None):
        self.settings = settings
        self.store = store
        self.shared = shared or SharedGrowth(settings)

    def _get_runtime(self, key: str) -> str | None:
        value = self.store.get_runtime(key)
        if value:
            return value
        return self.shared.get_runtime(key) if self.shared.enabled else None

    def _set_runtime(self, key: str, value: str) -> None:
        self.store.set_runtime(key, value)
        if self.shared.enabled:
            self.shared.set_runtime(key, value)

    def due(self) -> bool:
        if not self.settings.competitive_analysis_enabled:
            return False
        last = self._get_runtime("competitive_last_run")
        if not last:
            return True
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(hours=self.settings.competitive_analysis_interval_hours)
        except ValueError:
            return True

    def _fetch(self, url: str) -> dict[str, Any] | None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        robots = urllib.robotparser.RobotFileParser(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        try:
            robots.read()
            if not robots.can_fetch("Salee/1.0", url):
                return {"url": url, "status": "blocked_by_robots"}
        except Exception:
            return {"url": url, "status": "robots_unavailable"}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Salee/1.0 public-research"})
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(400_000).decode(errors="replace")
            parser = _PageParser()
            parser.feed(raw)
            return {"url": url, "status": "ok", "title": parser.title, "description": parser.description, "headings": parser.headings[:12]}
        except Exception as exc:
            return {"url": url, "status": "error", "error": str(exc)[:200]}

    def run(self) -> dict[str, Any]:
        if not self.due():
            return {"status": "not_due"}
        urls = [item.strip() for item in self.settings.competitive_analysis_urls.split(",") if item.strip()][:8]
        observations = [item for item in (self._fetch(url) for url in urls) if item]
        self._set_runtime("competitive_observations", json.dumps(observations, ensure_ascii=False))
        now = datetime.now(timezone.utc).isoformat()
        self._set_runtime("competitive_last_run", now)
        body = json.dumps({"observed_at": now, "sources": observations}, ensure_ascii=False, indent=2)
        metadata = {"source_count": len(observations), "public_only": True}
        self.store.save_artifact("competitive_analysis", f"competitive-analysis-{now[:10]}", "Public competitive positioning scan", body, metadata)
        if self.shared.enabled:
            self.shared.save_artifact("competitive_analysis", f"competitive-analysis-{now[:10]}", "Public competitive positioning scan", body, metadata)
        self.store.audit("competitive_analysis_complete", {"sources": len(urls), "observations": len(observations)})
        return {"status": "completed", "sources": len(urls), "observations": len(observations)}
