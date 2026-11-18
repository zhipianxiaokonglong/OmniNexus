"""Search provider request shapes and parsing, without real network calls."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from omninexus.config import CustomSearchConfig, SearchConfig
from omninexus.search.service import (
    BingProvider,
    BraveProvider,
    DuckDuckGoProvider,
    SearxngProvider,
    TavilyProvider,
)
from tests.conftest import make_transport


def config(provider: str, **kwargs) -> SearchConfig:
    return SearchConfig(enabled=True, provider=provider, top_k=3, **kwargs)


@pytest.mark.asyncio
async def test_tavily_query_only(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-secret")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Tavily result",
                        "url": "https://example.com/t",
                        "content": "snippet",
                    }
                ]
            },
        )

    provider = TavilyProvider(config("tavily"), transport=make_transport(handler))
    try:
        results = await provider.search("current query only", 3)
        assert results[0].url == "https://example.com/t"
        body = json.loads(seen[0].read())
        assert body["query"] == "current query only"
        assert body["max_results"] == 3
        assert "messages" not in body
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_brave_query_only(monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-secret")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Brave result",
                            "url": "https://example.com/b",
                            "description": "snippet",
                        }
                    ]
                }
            },
        )

    provider = BraveProvider(config("brave"), transport=make_transport(handler))
    try:
        results = await provider.search("brave query", 3)
        assert results[0].title == "Brave result"
        assert seen[0].url.params["q"] == "brave query"
        assert "session" not in str(seen[0].url)
        assert seen[0].headers["x-subscription-token"] == "brave-test-secret"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_searxng_query_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "SearXNG result",
                        "url": "https://example.com/s",
                        "content": "snippet",
                    }
                ]
            },
        )

    provider = SearxngProvider(
        config("searxng", searxng={"base_url": "http://searx.test"}),
        transport=make_transport(handler),
    )
    try:
        results = await provider.search("searx query", 3)
        assert results[0].source == "searxng"
        assert seen[0].url.params["q"] == "searx query"
        assert seen[0].url.params["format"] == "json"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_duckduckgo_html_parsing() -> None:
    html = """
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fddg">
      DDG result
    </a>
    <a class="result__snippet">A search snippet</a>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    provider = DuckDuckGoProvider(config("duckduckgo"), transport=make_transport(handler))
    try:
        results = await provider.search("ddg query", 3)
        assert len(results) == 1
        assert results[0].url == "https://example.com/ddg"
        assert results[0].title == "DDG result"
        assert "snippet" in results[0].snippet
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_bing_rss_parsing_and_redirect_unwrap() -> None:
    target = base64.urlsafe_b64encode(b"https://example.com/bing-target").decode().rstrip("=")
    feed = f"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Direct &amp; result</title>
        <link>https://example.com/direct</link>
        <description>First &lt;b&gt;snippet&lt;/b&gt;</description>
      </item>
      <item>
        <title>Redirect result</title>
        <link>https://www.bing.com/ck/a?!&amp;&amp;u=a1{target}</link>
        <description>Second snippet</description>
      </item>
    </channel></rss>
    """

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=feed)

    provider = BingProvider(config("bing"), transport=make_transport(handler))
    try:
        assert provider.configured is True
        results = await provider.search("bing query", 3)
        assert seen[0].url.params["q"] == "bing query"
        assert seen[0].url.params["format"] == "rss"
        assert [item.url for item in results] == [
            "https://example.com/direct",
            "https://example.com/bing-target",
        ]
        assert results[0].title == "Direct & result"
        assert results[0].snippet == "First snippet"
        assert results[1].rank == 2
        assert all(item.source == "bing" for item in results)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_bing_retries_transient_empty_response() -> None:
    target = base64.urlsafe_b64encode(b"https://example.com/retry").decode().rstrip("=")
    feed = f"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Retry result</title>
        <link>https://www.bing.com/ck/a?!&amp;&amp;u=a1{target}</link>
        <description>Recovered</description>
      </item>
    </channel></rss>
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="")
        return httpx.Response(200, text=feed)

    provider = BingProvider(config("bing"), transport=make_transport(handler))
    try:
        results = await provider.search("retry query", 3)
        assert calls == 2
        assert results[0].url == "https://example.com/retry"
        assert results[0].snippet == "Recovered"
    finally:
        await provider.close()


def test_custom_search_config_requires_endpoint() -> None:
    with pytest.raises(ValueError):
        SearchConfig(
            enabled=True,
            provider="custom",
            custom=CustomSearchConfig(endpoint=""),
        )
