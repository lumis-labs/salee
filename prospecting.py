"""Bounded public-source prospect discovery and outreach drafting."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.robotparser
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

from config import Settings
from store import Store


def _clean(value: str, limit: int = 4000) -> str:
    value = re.sub(r"<[^>]+>", " ", unescape(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _score(text: str, queries: list[str]) -> int:
    lowered = text.lower()
    terms = {term for query in queries for term in re.findall(r"[a-z0-9]{4,}", query.lower())}
    hits = sum(1 for term in terms if term in lowered)
    return min(100, hits * 12)


def _draft(settings: Settings, title: str, summary: str, url: str) -> str:
    return (
        f"I saw your question about {title[:140]}. The useful first step is to map the current "
        "lead or follow-up workflow before choosing another tool: where the request arrives, "
        "what happens next, and where a handoff gets delayed.\n\n"
        f"I work on practical AI workflow plans for small businesses. If it is useful, here is a "
        f"short overview of the approach: {settings.public_base_url or 'https://salee-nine.vercel.app'}\n\n"
        "The context you shared is the important part; I would start there rather than assume a "
        "generic automation is the answer."
    )


class ProspectingEngine:
    """Discover public conversations; never harvest private contact data."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def due(self) -> bool:
        if not self.settings.prospecting_enabled:
            return False
        last = self.store.get_runtime("prospecting_last_run")
        if not last:
            return True
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(minutes=self.settings.prospecting_interval_minutes)
        except ValueError:
            return True

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "Salee/1.0 public-prospecting"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    def _fetch_public_source(self, url: str) -> list[dict[str, str]]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return []
        robots = urllib.robotparser.RobotFileParser()
        robots.set_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        try:
            robots.read()
            if not robots.can_fetch("Salee/1.0", url):
                self.store.audit("prospecting_source_blocked", {"source": url, "reason": "robots.txt"})
                return []
        except Exception:
            return []
        source = parsed.netloc
        request = urllib.request.Request(url, headers={"User-Agent": "Salee/1.0 public-prospecting"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(500_000)
                content_type = response.headers.get("Content-Type", "")
        except Exception as exc:
            self.store.audit("prospecting_source_error", {"source": url, "error": str(exc)[:300]})
            return []
        if "json" in content_type or raw.lstrip().startswith((b"{", b"[")):
            try:
                data = json.loads(raw)
                items = data.get("items", data) if isinstance(data, dict) else data
                return [{"source": source, "external_id": str(item.get("id") or item.get("url") or item.get("link") or index), "url": str(item.get("url") or item.get("link") or url), "title": _clean(str(item.get("title") or item.get("name") or "Public item"), 300), "summary": _clean(str(item.get("summary") or item.get("description") or item.get("text") or "")), "author": _clean(str(item.get("author") or ""), 120)} for index, item in enumerate(items[:50]) if isinstance(item, dict)]
            except (TypeError, json.JSONDecodeError):
                return []
        try:
            root = ET.fromstring(raw)
            rows = []
            for index, item in enumerate(root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")):
                title = item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "Public item"
                link_node = item.find("link") or item.find("{http://www.w3.org/2005/Atom}link")
                link = (link_node.text if link_node is not None else "") or (link_node.attrib.get("href", "") if link_node is not None else "")
                summary = item.findtext("description") or item.findtext("summary") or ""
                rows.append({"source": source, "external_id": link or str(index), "url": link or url, "title": _clean(title, 300), "summary": _clean(summary), "author": ""})
            return rows
        except ET.ParseError:
            title = re.search(r"<title[^>]*>(.*?)</title>", raw.decode(errors="replace"), re.I | re.S)
            description = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', raw.decode(errors="replace"), re.I | re.S)
            return [{"source": source, "external_id": url, "url": url, "title": _clean(title.group(1) if title else url, 300), "summary": _clean(description.group(1) if description else ""), "author": ""}]

    def _discover(self, query: str) -> list[dict[str, str]]:
        encoded = urllib.parse.quote(query)
        rows: list[dict[str, str]] = []
        try:
            data = self._get_json(f"https://hn.algolia.com/api/v1/search_by_date?query={encoded}&tags=story&hitsPerPage=8")
            for item in data.get("hits", []):
                rows.append({"source": "hackernews", "external_id": str(item.get("objectID", "")), "url": str(item.get("url") or f"https://news.ycombinator.com/item?id={item.get('objectID')}"), "title": _clean(str(item.get("title") or ""), 300), "summary": _clean(str(item.get("story_text") or "")), "author": _clean(str(item.get("author") or ""), 120)})
        except Exception as exc:
            self.store.audit("prospecting_source_error", {"source": "hackernews", "error": str(exc)[:300]})
        try:
            data = self._get_json(f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=activity&q={encoded}&site=stackoverflow&pagesize=8&filter=default")
            for item in data.get("items", []):
                rows.append({"source": "stackexchange", "external_id": str(item.get("question_id", "")), "url": str(item.get("link", "")), "title": _clean(str(item.get("title") or ""), 300), "summary": _clean(str(item.get("body_markdown") or item.get("tags") or "")), "author": _clean(str((item.get("owner") or {}).get("display_name") or ""), 120)})
        except Exception as exc:
            self.store.audit("prospecting_source_error", {"source": "stackexchange", "error": str(exc)[:300]})
        return rows

    def run(self) -> dict[str, Any]:
        if not self.due():
            return {"status": "not_due"}
        queries = [item.strip() for item in self.settings.prospecting_queries.split("|") if item.strip()]
        rows: list[dict[str, str]] = []
        for query in queries[:8]:
            rows.extend(self._discover(query))
        for url in [item.strip() for item in self.settings.prospecting_source_urls.split(",") if item.strip()][:8]:
            rows.extend(self._fetch_public_source(url))
        saved = 0
        for item in rows:
            score = _score(f"{item['title']} {item['summary']}", queries)
            if score < 12 or not item.get("url"):
                continue
            self.store.save_prospect(item["source"], item["external_id"], item["url"], item["title"], item["summary"], item.get("author", ""), score, _draft(self.settings, item["title"], item["summary"], item["url"]))
            saved += 1
            if saved >= self.settings.prospecting_max_items:
                break
        now = datetime.now(timezone.utc).isoformat()
        self.store.set_runtime("prospecting_last_run", now)
        self.store.audit("prospecting_complete", {"queries": queries, "discovered": len(rows), "saved": saved})
        return {"status": "completed", "discovered": len(rows), "saved": saved}
