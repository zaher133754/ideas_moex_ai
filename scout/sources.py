"""Public article fallback when Codex search can find, but cannot open, a page."""

import asyncio
import ipaddress
import json
import socket
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

DOMAINS = (
    "bcs-express.ru",
    "finam.ru",
    "smart-lab.ru",
    "tbank.ru",
    "moex.com",
    "moex.ru",
    "cbr.ru",
    "e-disclosure.ru",
    "rbc.ru",
    "quote.ru",
    "vedomosti.ru",
    "finmarket.ru",
    "1prime.ru",
    "interfax.ru",
    "fomag.ru",
    "finbazar.ru",
    "ovanex.org",
    "akra-ratings.ru",
    "raexpert.ru",
)


def allowed_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and any(host == domain or host.endswith("." + domain) for domain in DOMAINS)
        )
    except ValueError:
        return False


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.dates = [], []
        self.hidden_depth = 0
        self.ld_json = False
        self.ld_parts = []
        self.description = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1
            if tag == "script" and attrs.get("type") == "application/ld+json":
                self.ld_json = True
                self.ld_parts = []
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            if key in {"article:published_time", "datepublished", "date", "pubdate", "publish-date"}:
                self.dates.append(attrs.get("content", ""))
            if key in {"description", "og:description"}:
                self.description = attrs.get("content", "")
        if tag == "time" and attrs.get("datetime"):
            self.dates.append(attrs["datetime"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
            if tag == "script" and self.ld_json:
                try:
                    self.find_dates(json.loads("".join(self.ld_parts)))
                except ValueError:
                    pass
                self.ld_json = False

    def handle_data(self, data):
        if self.ld_json:
            self.ld_parts.append(data)
        if not self.hidden_depth and data.strip():
            self.text.append(data.strip())

    def find_dates(self, node):
        if isinstance(node, dict):
            if node.get("datePublished"):
                self.dates.append(str(node["datePublished"]))
            for value in node.values():
                self.find_dates(value)
        elif isinstance(node, list):
            for value in node:
                self.find_dates(value)


class PublicArticleReader:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.semaphore = asyncio.Semaphore(3)

    async def read(self, url: str, now: datetime) -> dict | None:
        original = url
        async with self.semaphore:
            try:
                for _ in range(4):
                    if not allowed_url(url):
                        return None
                    host = urlsplit(url).hostname
                    addresses = await asyncio.to_thread(
                        socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM
                    )
                    if not addresses or any(
                        not ipaddress.ip_address(item[4][0]).is_global for item in addresses
                    ):
                        return None
                    async with self.client.stream("GET", url, follow_redirects=False) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers.get("location", ""))
                            continue
                        if response.status_code != 200 or "html" not in response.headers.get(
                            "content-type", ""
                        ):
                            return None
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 2_000_000:
                                return None
                        encoding = response.encoding or "utf-8"
                        parser = ArticleParser()
                        parser.feed(bytes(content).decode(encoding, errors="replace"))
                        text = "\n".join(parser.text)
                        if len(text) < 500:
                            return None
                        return {
                            "url": original,
                            "final_url": url,
                            "retrieved_at": now.isoformat(),
                            "publication_date_candidates": list(dict.fromkeys(parser.dates))[:10],
                            "description": parser.description,
                            "text_excerpt": text[:18000],
                        }
                return None
            except (httpx.HTTPError, OSError, ValueError, LookupError):
                return None

    async def read_many(self, urls: list[str], now: datetime) -> list[dict]:
        readable = [
            url for url in urls if allowed_url(url) and not urlsplit(url).path.lower().endswith(".pdf")
        ]
        results = await asyncio.gather(*(self.read(url, now) for url in readable[:12]))
        return [item for item in results if item]
