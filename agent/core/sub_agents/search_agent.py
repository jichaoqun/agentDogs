"""Search-focused sub-agent backed by registered search tools."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..tools import ToolRegistry
from .registry import SubAgentResult


WEB_MARKERS = ("联网", "网上", "网络", "互联网", "web", "Web")
KEYWORD_MARKERS = ("关键词", "匹配")
SEARCH_MARKERS = ("搜索", "查找", "检索", "找一下", "查询", "查一下", "搜一下", "搜一搜", "包含")
WORKSPACE_MARKERS = ("workspace", "项目", "文件", "内容", "当前项目", "当前工作目录")
FILLER_MARKERS = ("请", "帮我", "一下", "一个", "这个", "一下儿", "关于")
SCOPE_MARKERS = ("中的", "中", "里", "里面", "里边", "内")


@dataclass(slots=True)
class SearchAgent:
    """Choose read-only search tools and format their results."""

    tools: ToolRegistry

    def handle(self, user_input: str) -> SubAgentResult:
        text = user_input.strip()
        if self._looks_like_web(text):
            query = self._extract_query(text, WEB_MARKERS + SEARCH_MARKERS + FILLER_MARKERS)
            return self._web_search(query)
        if self._looks_like_keyword(text):
            query = self._extract_query(text, KEYWORD_MARKERS + SEARCH_MARKERS + SCOPE_MARKERS + FILLER_MARKERS)
            return self._keyword_search(query)
        query = self._extract_query(text, SEARCH_MARKERS + WORKSPACE_MARKERS + SCOPE_MARKERS + FILLER_MARKERS)
        return self._workspace_search(query)

    def handle_step(self, step: str, *, user_input: str = "") -> SubAgentResult:
        return self.handle(f"{step}\n{user_input}".strip())

    def _workspace_search(self, query: str) -> SubAgentResult:
        if not query:
            return SubAgentResult.failure("搜索关键词不能为空")
        payload = {"query": query, "limit": 10}
        result = self.tools.call("workspace_search", payload)
        tool_call = self._tool_call("workspace_search", payload, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "workspace 搜索失败", tool_calls=[tool_call])
        return SubAgentResult.success(
            self._format_results("workspace 搜索", query, (result.data or {}).get("results", [])),
            data=result.data,
            tool_calls=[tool_call],
        )

    def _keyword_search(self, query: str) -> SubAgentResult:
        if not query:
            return SubAgentResult.failure("关键词不能为空")
        payload = {"query": query, "text": query, "limit": 10}
        result = self.tools.call("keyword_search", payload)
        tool_call = self._tool_call("keyword_search", payload, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "关键词搜索失败", tool_calls=[tool_call])
        return SubAgentResult.success(
            self._format_results("关键词搜索", query, (result.data or {}).get("results", [])),
            data=result.data,
            tool_calls=[tool_call],
        )

    def _web_search(self, query: str) -> SubAgentResult:
        if not query:
            return SubAgentResult.failure("联网搜索关键词不能为空")
        payload = {"query": query, "max_results": 5, "fetch_pages": 3}
        result = self.tools.call("web_search", payload)
        tool_call = self._tool_call("web_search", payload, result.ok)
        if not result.ok:
            return SubAgentResult.success(
                result.error or "联网搜索暂不可用。",
                data=result.data,
                tool_calls=[tool_call],
            )
        return SubAgentResult.success(
            self._format_results("联网搜索", query, (result.data or {}).get("results", [])),
            data=result.data,
            tool_calls=[tool_call],
        )

    def _format_results(self, title: str, query: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return f"{title}没有找到与“{query}”相关的结果。"
        lines = [f"{title}找到 {len(results)} 个结果："]
        for index, item in enumerate(results, start=1):
            source = item.get("source") or "unknown"
            location = item.get("path") or item.get("url") or ""
            summary = str(item.get("summary") or "").strip()
            excerpt = str(item.get("content_excerpt") or "").strip()
            reason = item.get("match_reason") or "keyword"
            fetched = item.get("fetched")
            error = str(item.get("error") or "").strip()
            lines.append(f"{index}. {item.get('title') or location or '结果'}")
            lines.append(f"   来源：{source}")
            if location:
                lines.append(f"   位置：{location}")
            if summary:
                lines.append(f"   摘要：{summary[:240]}")
            if excerpt and excerpt != summary:
                lines.append(f"   正文摘录：{excerpt[:360]}")
            if fetched is not None:
                lines.append(f"   抓取：{'成功' if fetched else '未抓取'}")
            if error:
                lines.append(f"   说明：{error}")
            lines.append(f"   命中：{reason}")
        return "\n".join(lines)

    def _looks_like_web(self, text: str) -> bool:
        return any(marker in text for marker in WEB_MARKERS)

    def _looks_like_keyword(self, text: str) -> bool:
        return any(marker in text for marker in KEYWORD_MARKERS)

    def _extract_query(self, text: str, markers: tuple[str, ...]) -> str:
        cleaned = text
        for marker in sorted(markers, key=len, reverse=True):
            cleaned = cleaned.replace(marker, " ")
        cleaned = re.sub(r"[，。；：！？,.!?;:]+", " ", cleaned)
        tokens = [item.strip() for item in re.split(r"\s+", cleaned) if item.strip()]
        return " ".join(tokens)[:160]

    def _tool_call(self, name: str, payload: dict[str, Any], ok: bool) -> dict[str, Any]:
        return {"tool": name, "payload": payload, "ok": ok}
