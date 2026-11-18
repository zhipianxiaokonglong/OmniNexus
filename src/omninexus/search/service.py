"""Configurable web search providers used for retrieval-augmented generation.

Privacy rule: only the current user query (trimmed to ``max_query_chars``) is
sent. Session history, identifiers and system prompts are never included.
"""

from __future__ import annotations

import asyncio
import base64
import html
import os
import re
import xml.etree.ElementTree as ET
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


class BingProvider(SearchProvider):
    """Bing 网页搜索。

    无需 API Key，开箱可用，因此作为默认搜索源。使用 Bing 的 RSS 结果
    （`format=rss`）而不是整页 HTML：结果结构稳定、体积小，也不依赖页面样式。
    只发送当前查询。
    """

    name = "bing"

    # Bing 是关键词搜索：整句“请给出两三条并注明来源”会把权重带偏到“最近/给出”等词上。
    # 这里只去掉请求措辞，不引入任何外部信息，发送的仍然是当前查询的内容。
    _NOISE = re.compile(
        r"最近的|近期的|近来的|最近|近期|近来|"
        r"请你|请帮我|请给出|请提供|请列出|请列举|请介绍|请解释|请说明|请总结|请推荐|请比较|请分析|"
        r"请用一句话|帮我|帮忙|给我|告诉我|我想知道|我想|我要|想要|"
        r"用一句话|简单说|简单介绍|介绍一下|介绍|解释一下|解释|说明一下|说明|"
        r"总结一下|总结|概括一下|概括|列出|列举|给出|提供|"
        r"查一下|查查|搜一下|搜索一下|查找一下|查询一下|看一下|"
        r"有哪些|是什么|为什么|怎么样|怎么|如何|是否|有没有|"
        r"两三条|几条|一些|"
        r"并且|然后|还有|以及|"
        r"并注明|并标注|并标明|并附上|并给出|"
        r"注明|标注|标明|来源|出处|链接|参考文献|"
        r"谢谢|多谢|感谢"
    )
    _PUNCT = re.compile(r"[\s\u3000，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】<>《》…—\-_/\\|]+")

    # Bing 偶尔会返回 200 + 空响应（多为瞬时限流/节点问题），换节点重试即可。
    _ENDPOINTS = (
        "https://www.bing.com/search",
        "https://cn.bing.com/search",
        "https://www.bing.com/search",
    )

    @property
    def configured(self) -> bool:
        return True

    @classmethod
    def _keyword_query(cls, text: str) -> str:
        """去掉“请给出/告诉我/来源”等请求措辞，留下适合关键词搜索的内容。"""
        cleaned = text.strip()
        if not re.search(r"[\u4e00-\u9fff]", cleaned):
            return cleaned
        reduced = cls._NOISE.sub(" ", cleaned)
        reduced = cls._PUNCT.sub(" ", reduced)
        reduced = re.sub(r"\s+", " ", reduced).strip()
        # 删过头就退回原查询，避免把用户真正想搜的内容也吞掉。
        if len(re.sub(r"[\W_]", "", reduced)) < 2:
            return cleaned
        return reduced[:80].strip()

    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        search_query = self._keyword_query(query)
        root: ET.Element | None = None
        last_status = 0
        for attempt, endpoint in enumerate(self._ENDPOINTS):
            if attempt:
                await asyncio.sleep(0.4)
            response = await self.client().get(
                endpoint,
                params={"q": search_query, "format": "rss", "setlang": "zh-hans"},
                headers={"Accept": "application/rss+xml, application/xml;q=0.9"},
            )
            last_status = response.status_code
            if response.status_code >= 400:
                continue
            if not response.text.strip():
                continue
            try:
                root = ET.fromstring(response.text)
            except ET.ParseError:
                continue
            break
        if root is None:
            raise RuntimeError(
                f"search_http_{last_status}" if last_status >= 400 else "search_empty"
            )
        results: list[SearchResult] = []
        for item in root.findall("./channel/item"):
            if len(results) >= top_k:
                break
            url = self._real_url(item.findtext("link") or "")
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_clean_text(item.findtext("title") or "", 300) or url,
                    url=url,
                    snippet=_clean_text(item.findtext("description") or "", 600),
                    rank=len(results) + 1,
                    source="bing",
                )
            )
        return results

    @staticmethod
    def _real_url(href: str) -> str | None:
        """把 Bing 的跳转链接还原成真实目标地址。"""
        href = html.unescape(href).strip()
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        host = parsed.netloc.lower()
        if (host == "bing.com" or host.endswith(".bing.com")) and parsed.path.startswith("/ck/a"):
            target = parse_qs(parsed.query).get("u", [""])[0]
            if target.startswith(("http://", "https://")):
                href = target
            else:
                if target.startswith("a1"):
                    target = target[2:]
                if not target:
                    return None
                padding = "=" * (-len(target) % 4)
                try:
                    href = base64.urlsafe_b64decode(target + padding).decode("utf-8", "replace")
                except (ValueError, TypeError):
                    return None
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
    "bing": BingProvider,
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
