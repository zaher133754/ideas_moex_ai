import socket

import httpx

from scout.sources import ArticleParser, PublicArticleReader, allowed_url


def test_public_source_allowlist_prevents_local_and_lookalike_urls():
    assert allowed_url("https://www.finmarket.ru/shares/news/1")
    for url in (
        "http://finmarket.ru/a",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://finmarket.ru.evil.test/a",
        "https://user:pass@finmarket.ru/a",
        "https://finmarket.ru:8080/a",
    ):
        assert not allowed_url(url)


def test_article_metadata_and_text_skip_scripts():
    parser = ArticleParser()
    parser.feed("""<html><head><meta property="article:published_time" content="2026-10-07T15:30:00+03:00">
    <script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-10-07T15:30:00+03:00"}</script>
    <script>steal_token()</script><style>.hidden {display:none}</style></head><body><article>Проверяемый текст статьи</article></body></html>""")
    assert "2026-10-07T15:30:00+03:00" in parser.dates
    assert "Проверяемый текст статьи" in parser.text
    assert "steal_token()" not in parser.text


async def test_private_dns_and_redirects_blocked(monkeypatch, now):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    def public_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await PublicArticleReader(client).read("https://finmarket.ru/news/1", now) is None
    assert len(requested) == 1
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await PublicArticleReader(client).read("https://finmarket.ru/news/1", now) is None
    assert len(requested) == 1


async def test_fetch_limit_applies_after_domain_filter(monkeypatch, now):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                text="<article>" + "Источник содержит открытый текст. " * 30 + "</article>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
    ) as client:
        result = await PublicArticleReader(client).read_many(
            ["https://untrusted.example/" + str(i) for i in range(12)] + ["https://finmarket.ru/news/13"], now
        )
    assert len(result) == 1
