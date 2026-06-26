"""Search-oriented tools used by simple and specialist agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import ipaddress
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from ..utils.llm_config import SearchConfig
from .base import ToolRegistry, ToolResult, ToolSpec


DDG_HTML_URL = "https://duckduckgo.com/html/"
MAX_SEARCH_HTML_CHARS = 1_000_000
MAX_PAGE_TEXT_CHARS = 240_000
MAX_EXCERPT_CHARS = 1_200
CACHE_TTL_SECONDS = 600
PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home", ".corp")


def register_search_tools(
    registry: ToolRegistry,
    search_config: SearchConfig | None = None,
) -> ToolRegistry:
    tools = SearchTools(registry, search_config or SearchConfig())
    registry.register(
        ToolSpec(
            name="workspace_search",
            description="搜索 workspace 中的文件名和文本内容，返回统一搜索结果。",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "include_content": {"type": "boolean"},
                },
            },
            risk_level="low",
            capabilities=["search.workspace", "workspace.read"],
        ),
        tools.workspace_search,
    )
    registry.register(
        ToolSpec(
            name="keyword_search",
            description="在给定文本或结果列表中做关键词匹配和简单排序。",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "text": {"type": "string"},
                    "items": {"type": "array"},
                    "limit": {"type": "integer"},
                },
            },
            risk_level="low",
            capabilities=["search.keyword"],
        ),
        tools.keyword_search,
    )
    registry.register(
        ToolSpec(
            name="web_search",
            description="联网搜索并可抓取网页正文。默认使用无 API key 的 DuckDuckGo HTML best-effort provider。",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                    "fetch_pages": {"type": "integer"},
                },
            },
            risk_level="low",
            capabilities=["search.web", "web.read"],
        ),
        tools.web_search,
    )
    return registry


@dataclass(slots=True)
class SearchTools:
    registry: ToolRegistry
    search_config: SearchConfig = field(default_factory=SearchConfig)
    _cache: dict[str, tuple[float, Any]] = field(default_factory=dict, init=False, repr=False)

    def workspace_search(self, payload: dict[str, Any] | None = None) -> ToolResult:
        data = payload or {}
        query = str(data.get("query", "")).strip()
        if not query:
            return ToolResult.failure("搜索关键词不能为空")
        limit = self._bounded_int(data.get("limit"), default=10, minimum=1, maximum=50)
        result = self.registry.call(
            "search_files",
            {
                "query": query,
                "limit": limit,
                "include_content": bool(data.get("include_content", True)),
            },
        )
        if not result.ok:
            return result
        matches = (result.data or {}).get("matches", [])
        normalized = [
            {
                "title": item.get("name") or item.get("path", ""),
                "source": "workspace",
                "path": item.get("path", ""),
                "url": "",
                "summary": item.get("snippet") or f"文件名命中：{item.get('path', '')}",
                "content_excerpt": item.get("snippet", ""),
                "match_reason": item.get("match", ""),
                "fetched": True,
                "error": "",
                "score": 1,
            }
            for item in matches
        ]
        return ToolResult.success(
            f"workspace 搜索找到 {len(normalized)} 个结果。",
            data={"query": query, "results": normalized},
            artifacts=[item["path"] for item in normalized if item["path"]],
        )

    def keyword_search(self, payload: dict[str, Any] | None = None) -> ToolResult:
        data = payload or {}
        query = str(data.get("query", "")).strip()
        if not query:
            return ToolResult.failure("关键词不能为空")
        limit = self._bounded_int(data.get("limit"), default=10, minimum=1, maximum=50)
        items = data.get("items")
        text = str(data.get("text", ""))
        if isinstance(items, list) and items:
            results = self._rank_items(query, items, limit)
        elif text.strip():
            results = self._search_text(query, text, limit)
        else:
            return ToolResult.failure("关键词搜索需要 text 或 items")
        return ToolResult.success(
            f"关键词搜索找到 {len(results)} 个结果。",
            data={"query": query, "results": results},
        )

    def web_search(self, payload: dict[str, Any] | None = None) -> ToolResult:
        data = payload or {}
        query = str(data.get("query", "")).strip()
        if not query:
            return ToolResult.failure("联网搜索关键词不能为空")
        if not self._web_enabled():
            return ToolResult.failure(
                "联网搜索未启用。请在 config/llm.yaml 的 search.enabled 或 AGENT_WEB_SEARCH_ENABLED 中启用。",
                data={"query": query, "results": []},
            )
        if self.search_config.provider.lower() != "duckduckgo":
            return ToolResult.failure(
                f"暂不支持搜索 provider：{self.search_config.provider}",
                data={"query": query, "results": []},
            )

        max_results = self._bounded_int(
            data.get("max_results"),
            default=self.search_config.max_results,
            minimum=1,
            maximum=20,
        )
        fetch_pages = self._bounded_int(
            data.get("fetch_pages"),
            default=self.search_config.fetch_pages,
            minimum=0,
            maximum=max_results,
        )
        try:
            results = self._duckduckgo_search(query, max_results)
        except Exception as exc:
            return ToolResult.failure(
                f"联网搜索失败：{exc}",
                data={"query": query, "results": []},
            )

        for index, item in enumerate(results):
            if index >= fetch_pages:
                item["fetched"] = False
                item["content_excerpt"] = ""
                item["error"] = ""
                continue
            fetch = self._fetch_page(item["url"], query)
            item.update(fetch)
            if fetch.get("title") and not item.get("title"):
                item["title"] = fetch["title"]
            if fetch.get("content_excerpt") and not item.get("summary"):
                item["summary"] = fetch["content_excerpt"][:220]

        return ToolResult.success(
            f"联网搜索找到 {len(results)} 个结果。",
            data={"query": query, "results": results},
            artifacts=[item["url"] for item in results if item.get("url")],
        )

    def _web_enabled(self) -> bool:
        env = os.getenv("AGENT_WEB_SEARCH_ENABLED", "").strip().lower()
        if env:
            return env in {"1", "true", "yes", "on"}
        return self.search_config.enabled

    def _duckduckgo_search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        cache_key = f"ddg:{query}:{max_results}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return [dict(item) for item in cached]
        response = self._http_get(DDG_HTML_URL, params={"q": query})
        if response.status_code >= 400:
            raise RuntimeError(f"DuckDuckGo HTTP {response.status_code}")
        parser = DuckDuckGoParser()
        parser.feed(response.text[:MAX_SEARCH_HTML_CHARS])
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in parser.results:
            url = self._normalize_ddg_url(item.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            title = self._clean_text(item.get("title", "")) or url
            summary = self._clean_text(item.get("summary", ""))
            results.append(
                {
                    "title": title,
                    "source": self._source_from_url(url),
                    "path": "",
                    "url": url,
                    "summary": summary,
                    "content_excerpt": "",
                    "match_reason": "duckduckgo",
                    "fetched": False,
                    "error": "",
                    "score": max_results - len(results),
                }
            )
            if len(results) >= max_results:
                break
        self._cache_set(cache_key, [dict(item) for item in results])
        return results

    def _fetch_page(self, url: str, query: str) -> dict[str, Any]:
        if not self._is_safe_http_url(url):
            return {
                "fetched": False,
                "content_excerpt": "",
                "error": "URL 被安全策略拒绝",
            }
        cache_key = f"fetch:{url}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return dict(cached)
        try:
            response = self._http_get(url)
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            if content_type and not any(kind in content_type for kind in ("text/html", "text/plain", "application/xhtml+xml")):
                raise RuntimeError(f"不支持的内容类型：{content_type.split(';')[0]}")
            title, text = extract_readable_text(response.text[:MAX_PAGE_TEXT_CHARS])
            excerpt = self._snippet(text, query, MAX_EXCERPT_CHARS)
            result = {
                "title": title,
                "fetched": bool(excerpt),
                "content_excerpt": excerpt,
                "error": "" if excerpt else "未提取到有效正文",
            }
        except Exception as exc:
            result = {
                "fetched": False,
                "content_excerpt": "",
                "error": f"抓取失败：{exc}",
            }
        self._cache_set(cache_key, result)
        return dict(result)

    def _http_get(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return httpx.get(
            url,
            params=params,
            timeout=self.search_config.timeout,
            follow_redirects=True,
            headers={"User-Agent": self.search_config.user_agent},
        )

    def _rank_items(self, query: str, items: list[Any], limit: int) -> list[dict[str, Any]]:
        lowered = query.lower()
        ranked: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                haystack = " ".join(
                    str(item.get(key, ""))
                    for key in ("title", "summary", "snippet", "content", "content_excerpt", "path", "url", "source")
                )
                title = str(item.get("title") or item.get("path") or item.get("url") or "结果")
                source = str(item.get("source") or "")
                path = str(item.get("path") or "")
                url = str(item.get("url") or "")
            else:
                haystack = str(item)
                title = haystack[:40] or "结果"
                source = ""
                path = ""
                url = ""
            score = haystack.lower().count(lowered)
            if score:
                ranked.append(
                    {
                        "title": title,
                        "source": source,
                        "path": path,
                        "url": url,
                        "summary": self._snippet(haystack, query),
                        "content_excerpt": self._snippet(haystack, query),
                        "match_reason": "keyword",
                        "fetched": bool(haystack),
                        "error": "",
                        "score": score,
                    }
                )
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[:limit]

    def _search_text(self, query: str, text: str, limit: int) -> list[dict[str, Any]]:
        lowered = query.lower()
        results: list[dict[str, Any]] = []
        for index, line in enumerate(text.splitlines(), start=1):
            if lowered in line.lower():
                summary = line.strip()
                results.append(
                    {
                        "title": f"文本第 {index} 行",
                        "source": "text",
                        "path": "",
                        "url": "",
                        "summary": summary,
                        "content_excerpt": summary,
                        "match_reason": "keyword",
                        "fetched": True,
                        "error": "",
                        "score": line.lower().count(lowered),
                    }
                )
            if len(results) >= limit:
                break
        return results

    def _snippet(self, text: str, query: str, length: int = 180) -> str:
        compact = self._clean_text(text)
        if not compact:
            return ""
        index = compact.lower().find(query.lower())
        if index < 0:
            return compact[:length]
        before = max(0, (length - len(query)) // 2)
        start = max(0, index - before)
        end = min(len(compact), start + length)
        return compact[start:end]

    def _normalize_ddg_url(self, value: str) -> str:
        href = unescape(value or "").strip()
        if not href:
            return ""
        absolute = urljoin("https://duckduckgo.com", href)
        parsed = urlparse(absolute)
        params = parse_qs(parsed.query)
        if "uddg" in params and params["uddg"]:
            return unquote(params["uddg"][0])
        return absolute

    def _is_safe_http_url(self, value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.strip("[]").lower()
        if host in {"localhost", "0.0.0.0"} or host.endswith(PRIVATE_HOST_SUFFIXES) or "." not in host:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    def _source_from_url(self, url: str) -> str:
        return urlparse(url).hostname or "web"

    def _bounded_int(self, value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value if value is not None else default)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    def _clean_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", unescape(value or "")).strip()

    def _cache_get(self, key: str) -> Any | None:
        item = self._cache.get(key)
        if item is None:
            return None
        timestamp, value = item
        if time.monotonic() - timestamp > CACHE_TTL_SECONDS:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)


class DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current_link: dict[str, str] | None = None
        self._current_text: list[str] = []
        self._in_snippet = False
        self._snippet_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = values.get("class", "") or ""
        if tag == "a" and "result__a" in classes:
            self._current_link = {"url": values.get("href", "") or "", "title": "", "summary": ""}
            self._current_text = []
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._in_snippet = True
            self._snippet_text = []

    def handle_data(self, data: str) -> None:
        if self._current_link is not None:
            self._current_text.append(data)
        if self._in_snippet:
            self._snippet_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_link is not None:
            self._current_link["title"] = " ".join(self._current_text).strip()
            self.results.append(self._current_link)
            self._current_link = None
            self._current_text = []
        elif tag in {"a", "div"} and self._in_snippet:
            summary = " ".join(self._snippet_text).strip()
            if summary and self.results:
                self.results[-1]["summary"] = summary
            self._in_snippet = False
            self._snippet_text = []


class ReadableTextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "form", "nav", "header", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        elif not self._skip_depth:
            self.parts.append(text)


def extract_readable_text(html: str) -> tuple[str, str]:
    parser = ReadableTextParser()
    parser.feed(html)
    title = re.sub(r"\s+", " ", unescape(parser.title)).strip()
    text = re.sub(r"\s+", " ", unescape(" ".join(parser.parts))).strip()
    return title, text
