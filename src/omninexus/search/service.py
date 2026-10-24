"""Configurable web search providers used for retrieval-augmented generation.

Privacy rule: only the current user query (trimmed to ``max_query_chars``) is
sent. Session history, identifiers and system prompts are never included.
"""

from __future__ import annotations

import html
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from omninexus.config import SearchConfig
from omninexus.core.models import Citation, SearchResult
from omninexus.logging import get_logger, register_secret

log = get_logger("search")

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def clean_query(text: str, max_chars: int = 256) -> str:
    query = _SPACE.sub(" ", text or "").strip()
    return query[:max_chars]


def _clean_text(value: str, limit: int = 500) -> str:
    value = html.unescape(_TAG.sub(" ", value or ""))
    return _SPACE.sub(" ", value).strip()[:limit]


def _valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@dataclass(slots=True)
class SearchOutcome:
    results: list[SearchResult] = field(default_factory=list)
    available: bool = False
    reason: str = ""
    provider: str = "none"

    @property
    def citations(self) -> list[Citation]:
        return [
            Citation(
                url=item.url,
                title=item.title,
                snippet=item.snippet,
                rank=item.rank,
                source=item.source,
            )
            for item in self.results
        ]


class SearchProvider(ABC):
    name = "none"

    def __init__(self, config: SearchConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.config.timeout_s, connect=min(5.0, self.config.timeout_s)
                ),
                transport=self._transport,
                follow_redirects=True,
                headers={"User-Agent": "OmniNexus/0.1 (+local search)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @abstractmethod
    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        raise NotImplementedError


class TavilyProvider(SearchProvider):
    name = "tavily"

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self.config.tavily.api_key_env))

    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        key = os.environ.get(self.config.tavily.api_key_env)
        if not key:
            raise RuntimeError("search_key_missing")
        register_secret(key)
        response = await self.client().post(
            f"{self.config.tavily.base_url.rstrip('/')}/search",
            json={
                "api_key": key,
                "query": query,
                "max_results": top_k,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"search_http_{response.status_code}")
        data = response.json()
        results = []
        for index, item in enumerate((data.get("results") or [])[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not _valid_url(url):
                continue
            results.append(
                SearchResult(
                    title=_clean_text(str(item.get("title") or url), 300),
                    url=url,
                    snippet=_clean_text(str(item.get("content") or ""), 600),
                    rank=index,
                    source="tavily",
                )
            )
        return results


class BraveProvider(SearchProvider):
    name = "brave"

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self.config.brave.api_key_env))

    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        key = os.environ.get(self.config.brave.api_key_env)
        if not key:
            raise RuntimeError("search_key_missing")
        register_secret(key)
        response = await self.client().get(
            f"{self.config.brave.base_url.rstrip('/')}/res/v1/web/search",
            params={"q": query, "count": top_k, "safesearch": "moderate"},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"search_http_{response.status_code}")
        data = response.json()
        results = []
        for index, item in enumerate(
            ((data.get("web") or {}).get("results") or [])[:top_k], start=1
        ):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not _valid_url(url):
                continue
            results.append(
                SearchResult(
                    title=_clean_text(str(item.get("title") or url), 300),
                    url=url,
                    snippet=_clean_text(str(item.get("description") or ""), 600),
                    rank=index,
                    source="brave",
                )
            )
        return results


class SearxngProvider(SearchProvider):
    name = "searxng"

    @property
    def configured(self) -> bool:
        return bool(self.config.searxng.base_url)

    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        response = await self.client().get(
            f"{self.config.searxng.base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "safesearch": 1},
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"search_http_{response.status_code}")
        data = response.json()
        results = []
        for index, item in enumerate((data.get("results") or [])[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not _valid_url(url):
                continue
            results.append(
                SearchResult(
                    title=_clean_text(str(item.get("title") or url), 300),
                    url=url,
                    snippet=_clean_text(str(item.get("content") or ""), 600),
                    rank=index,
                    source="searxng",
                )
            )
        return results


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"

    @property
    def configured(self) -> bool:
        return True

    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        response = await self.client().get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"Accept": "text/html"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"search_http_{response.status_code}")
        page = response.text
        link_pattern = re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.I | re.S,
        )
        snippet_pattern = re.compile(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>', re.I | re.S
        )
        snippets = snippet_pattern.findall(page)
        results: list[SearchResult] = []
        for match in link_pattern.finditer(page):
            if len(results) >= top_k:
                break
            href, title = match.group(1), match.group(2)
            url = self._real_url(href)
            if not url:
                continue
            index = len(results)
            snippet = snippets[index] if index < len(snippets) else ""
            results.append(
                SearchResult(
                    title=_clean_text(title, 300) or url,
                    url=url,
                    snippet=_clean_text(snippet, 600),
                    rank=index + 1,
                    source="duckduckgo",
                )
            )
        return results

    @staticmethod
    def _real_url(href: str) -> str | None:
        href = html.unescape(href)
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            href = unquote(target)
        return href if _valid_url(href) else None


class CustomSearchProvider(SearchProvider):
    name = "custom"

    @property
    def configured(self) -> bool:
        return bool(self.config.custom.endpoint)

    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        key = (
            os.environ.get(self.config.custom.api_key_env)
            if self.config.custom.api_key_env
            else None
        )
        if key:
            register_secret(key)
            headers[self.config.custom.auth_header] = (
                f"{self.config.custom.auth_scheme} {key}".strip()
            )
        response = await self.client().post(
            self.config.custom.endpoint,
            json={"query": query, "top_k": top_k},
            headers=headers,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"search_http_{response.status_code}")
        data = response.json()
        raw = data.get("results") if isinstance(data, dict) else data
        results = []
        for index, item in enumerate((raw or [])[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "")
            if not _valid_url(url):
                continue
            results.append(
                SearchResult(
                    title=_clean_text(str(item.get("title") or url), 300),
                    url=url,
                    snippet=_clean_text(str(item.get("snippet") or item.get("content") or ""), 600),
                    rank=index,
                    source="custom",
                )
            )
        return results


_PROVIDERS: dict[str, type[SearchProvider]] = {
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "searxng": SearxngProvider,
    "duckduckgo": DuckDuckGoProvider,
    "custom": CustomSearchProvider,
}


class SearchService:
    def __init__(
        self, config: SearchConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self._provider: SearchProvider | None = None
        cls = _PROVIDERS.get(config.provider)
        if config.enabled and cls is not None:
            self._provider = cls(config, transport=transport)

    @property
    def available(self) -> bool:
        return bool(self.config.enabled and self._provider and self._provider.configured)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "provider": self.config.provider,
            "available": self.available,
            "top_k": self.config.top_k,
            "privacy": "only the current query is sent; no history or session data",
        }

    async def search(self, query: str, *, top_k: int | None = None) -> SearchOutcome:
        provider_name = self.config.provider
        if not self.config.enabled:
            return SearchOutcome(available=False, reason="disabled", provider=provider_name)
        if self._provider is None:
            return SearchOutcome(available=False, reason="not_configured", provider=provider_name)
        if not self._provider.configured:
            return SearchOutcome(
                available=False, reason="missing_credentials", provider=provider_name
            )
        cleaned = clean_query(query, self.config.max_query_chars)
        if not cleaned:
            return SearchOutcome(available=False, reason="empty_query", provider=provider_name)
        limit = max(1, min(top_k or self.config.top_k, 10))
        try:
            results = await self._provider.search(cleaned, limit)
        except Exception as exc:
            log.warning(
                "search_failed provider=%s reason=%s",
                provider_name,
                type(exc).__name__,
                extra={"provider": provider_name},
            )
            return SearchOutcome(available=False, reason="search_failed", provider=provider_name)
        seen: set[str] = set()
        unique: list[SearchResult] = []
        for result in results:
            if result.url in seen or not _valid_url(result.url):
                continue
            seen.add(result.url)
            result.rank = len(unique) + 1
            unique.append(result)
        return SearchOutcome(
            results=unique[:limit],
            available=True,
            provider=provider_name,
        )

    async def close(self) -> None:
        if self._provider is not None:
            await self._provider.close()
