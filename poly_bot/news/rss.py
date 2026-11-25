"""
RSS headline fetcher. Headlines are untrusted third-party text: they are only shown to the
LLM as data (see llm/prompts.py) and never executed or used as instructions.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from defusedxml import ElementTree as ET

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; invest-bot/0.3; +paper-trading)"
MAX_TITLE = 200
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Headline:
    source: str
    title: str
    published: datetime | None
    link: str = ""


def _clean(text: str | None) -> str:
    text = html.unescape(_TAG.sub("", text or "")).strip()
    return re.sub(r"\s+", " ", text)[:MAX_TITLE]


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(xml: bytes, source: str) -> list[Headline]:
    """Parse RSS 2.0 or Atom into headlines. Malformed feeds return []."""
    try:
        root = ET.fromstring(xml)
    except Exception:  # defusedxml raises several error types
        return []
    out: list[Headline] = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields: dict[str, Any] = {}
        for child in item:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "link" and child.get("href"):
                fields["link"] = child.get("href")
            elif ctag in ("title", "link", "pubDate", "published", "updated") and ctag not in fields:
                fields[ctag] = child.text
        title = _clean(fields.get("title"))
        if title:
            out.append(Headline(
                source=source, title=title,
                published=_parse_date(fields.get("pubDate") or fields.get("published") or fields.get("updated")),
                link=(fields.get("link") or "").strip(),
            ))
    return out


async def fetch_headlines(
    feeds: list[dict[str, str]],
    max_items: int = 30,
    max_age_hours: float = 48,
    timeout: float = 15.0,
) -> list[Headline]:
    """Fetch all feeds concurrently; dedupe, drop old items, newest first, balanced by source."""

    async def one(client: httpx.AsyncClient, feed: dict[str, str]) -> list[Headline]:
        try:
            r = await client.get(feed["url"])
            r.raise_for_status()
            return parse_feed(r.content, feed.get("name", feed["url"]))
        except (httpx.HTTPError, KeyError) as exc:
            log.warning("news.feed_failed", feed=feed.get("name"), error=str(exc))
            return []

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(*(one(client, f) for f in feeds))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    seen: set[str] = set()
    per_source: list[list[Headline]] = []
    for items in results:
        fresh = []
        for h in sorted(items, key=lambda h: h.published or cutoff, reverse=True):
            key = h.title.lower()
            if key in seen or (h.published and h.published < cutoff):
                continue
            seen.add(key)
            fresh.append(h)
        per_source.append(fresh)

    # Round-robin across sources so one prolific feed doesn't crowd out the rest
    out: list[Headline] = []
    while len(out) < max_items and any(per_source):
        for items in per_source:
            if items and len(out) < max_items:
                out.append(items.pop(0))
    log.info("news.fetched", headlines=len(out), feeds=len(feeds))
    return out
