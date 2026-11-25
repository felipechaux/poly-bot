"""RSS/Atom parsing."""

from __future__ import annotations

from poly_bot.news.rss import parse_feed

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Ecopetrol &amp; el &lt;b&gt;petr\xc3\xb3leo&lt;/b&gt;</title><link>https://x/1</link>
<pubDate>Tue, 24 Nov 2026 13:00:00 GMT</pubDate></item>
<item><title>  </title></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Fed holds rates</title><link href="https://y/2"/><updated>2026-11-24T10:00:00Z</updated></entry>
</feed>"""


def test_parse_rss_cleans_html_and_skips_empty():
    items = parse_feed(RSS, "LR")
    assert len(items) == 1
    assert items[0].title == "Ecopetrol & el petróleo"
    assert items[0].published is not None and items[0].published.hour == 13


def test_parse_atom():
    items = parse_feed(ATOM, "MW")
    assert items[0].title == "Fed holds rates" and items[0].link == "https://y/2"


def test_malformed_and_entity_bomb_return_empty():
    assert parse_feed(b"<rss><channel><item>", "x") == []
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><rss>&b;</rss>'
    assert parse_feed(bomb, "x") == []
