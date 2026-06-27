"""Simple low-risk task agent backed by the internal tool registry."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..tools import ToolRegistry, ToolResult
from .registry import SubAgentResult, SubAgentSpec
from .search_agent import SearchAgent


PATH_HINT = re.compile(
    r"`([^`]+)`|([\w\u4e00-\u9fff .\\/-]+\.(?:md|txt|py|js|jsx|ts|tsx|json|ya?ml|html|css|csv|xml|log|docx|pdf))",
    re.IGNORECASE,
)
PATH_PREFIXES = (
    "请帮我查看",
    "请帮我读取",
    "帮我查看",
    "帮我读取",
    "帮我打开",
    "请查看",
    "请读取",
    "请打开",
    "查看一下",
    "读取一下",
    "打开一下",
    "看一下",
    "查看",
    "读取",
    "打开",
    "预览",
    "帮我",
    "请",
)
PATH_SUFFIXES = (
    "中的内容是什么",
    "里面的内容是什么",
    "的内容是什么",
    "中的内容",
    "里面的内容",
    "的内容",
    "内容是什么",
)
LIST_MARKERS = (
    "有哪些文件",
    "文件列表",
    "列出文件",
    "列一下文件",
    "查看目录",
    "目录列表",
    "文件树",
    "当前工作目录",
    "当前目录",
    "当前项目",
)
READ_MARKERS = ("读取", "查看", "打开", "预览", "内容", "看一下")
INFO_MARKERS = ("信息", "属性", "大小", "类型", "元信息")
SEARCH_MARKERS = ("搜索", "查找", "检索", "找一下", "查询", "查一下", "搜一下", "包含")
WEB_MARKERS = ("联网", "网上", "网络", "互联网", "web", "Web")
HIGH_RISK_MARKERS = ("写入", "保存", "修改", "改写", "删除", "重命名", "创建", "新增", "覆盖", "移动", "上传", "下载")


@dataclass(slots=True)
class SimpleTaskAgent:
    """Execute explicit one-step low-risk tool tasks.

    The agent may see every registered tool, but it only auto-runs tools whose
    ToolSpec declares low risk. Higher risk tools are deferred to MainAgent's
    confirmation-oriented flow.
    """

    CAPABILITY = SubAgentSpec(
        name="simple_task",
        description="兼容层：执行明确、低风险、一步可完成的本地工具任务。",
        handles=["列出 workspace 文件", "读取明确路径文件", "查看文件信息", "兼容旧的一步搜索任务"],
        does_not_handle=["复杂任务编排", "高风险写操作", "需要深度自治的搜索任务"],
        capabilities=["tool.route", "file.list", "file.read", "file.search", "file.info", "compat.simple_task"],
        tools=["list_workspace_tree", "read_file", "workspace_search", "web_search", "file_info"],
        input_contract={"type": "plain_text", "requires_explicit_target": True},
        output_contract={"type": "SubAgentResult", "summary": "直接工具结果或兼容性提示"},
        risk_level="low",
        examples=["当前项目中有哪些文件", "读取 readme.md", "搜索 workspace 中的 protein"],
    )

    tools: ToolRegistry
    search_agent: SearchAgent | None = None

    @classmethod
    def capability_spec(cls) -> SubAgentSpec:
        return cls.CAPABILITY

    def handle(self, user_input: str) -> SubAgentResult:
        text = user_input.strip()
        if self._looks_high_risk(text):
            return SubAgentResult.success(
                "这个请求涉及写入、删除、重命名或上传下载等高风险操作，需要进入计划确认流程，不能直接自动执行。",
                status="waiting_confirmation",
            )

        path = self._extract_path(text)
        if self._looks_like_list(text):
            return self._list_tree(path if self._looks_like_directory_path(path) else "")
        if path and self._looks_like_info(text):
            return self._file_info(path)
        if path and self._looks_like_read(text):
            return self._read_file(path)
        if self._looks_like_search(text):
            if self.search_agent is not None:
                return self.search_agent.handle(text)
            query = self._extract_search_query(text, path)
            if query:
                return self._search_files(query)

        return SubAgentResult.failure("SimpleTaskAgent 暂时无法识别这个简单工具任务。")

    def _list_tree(self, path: str = "") -> SubAgentResult:
        payload = {"path": path} if path else {}
        result = self._call_low_risk("list_workspace_tree", payload)
        tool_call = self._tool_call("list_workspace_tree", payload, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "读取文件树失败", tool_calls=[tool_call])
        content = self._format_tree(result.data)
        return SubAgentResult.success(content, data=result.data, tool_calls=[tool_call])

    def _read_file(self, path: str) -> SubAgentResult:
        payload = {"path": path}
        result = self._call_low_risk("read_file", payload)
        tool_call = self._tool_call("read_file", payload, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "读取文件失败", tool_calls=[tool_call])
        content = str((result.data or {}).get("content", "")).strip()
        preview = content[:1200]
        suffix = "\n\n内容较长，已截断展示。" if len(content) > len(preview) else ""
        if not preview:
            preview = "文件为空或没有可展示文本。"
        return SubAgentResult.success(f"已读取 {path}：\n\n{preview}{suffix}", data=result.data, tool_calls=[tool_call])

    def _file_info(self, path: str) -> SubAgentResult:
        payload = {"path": path}
        result = self._call_low_risk("file_info", payload)
        tool_call = self._tool_call("file_info", payload, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "读取文件信息失败", tool_calls=[tool_call])
        data = result.data or {}
        lines = [
            f"路径：{data.get('path', path) or 'workspace'}",
            f"名称：{data.get('name', '')}",
            f"类型：{data.get('type', '')}",
            f"大小：{data.get('size', 0)} bytes",
            f"MIME：{data.get('mime_type', '')}",
        ]
        return SubAgentResult.success("\n".join(lines), data=data, tool_calls=[tool_call])

    def _search_files(self, query: str) -> SubAgentResult:
        payload = {"query": query, "limit": 10}
        tool_name = "workspace_search" if self._has_tool("workspace_search") else "search_files"
        result = self._call_low_risk(tool_name, payload)
        tool_call = self._tool_call(tool_name, payload, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "搜索文件失败", tool_calls=[tool_call])
        data = result.data or {}
        matches = data.get("results") or data.get("matches", [])
        if not matches:
            return SubAgentResult.success(f"未在 workspace 中找到与“{query}”匹配的文件。", data=result.data, tool_calls=[tool_call])
        lines = [f"在 workspace 中找到 {len(matches)} 个匹配项："]
        for item in matches:
            detail = item.get("summary") or item.get("snippet") or item.get("match_reason") or item.get("match", "")
            lines.append(f"- {item.get('path', '')} ({detail})")
        return SubAgentResult.success("\n".join(lines), data=result.data, tool_calls=[tool_call])

    def _call_low_risk(self, name: str, payload: dict[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if tool.spec.risk_level != "low":
            return ToolResult.failure(f"工具 {name} 风险等级为 {tool.spec.risk_level}，需要人工确认。")
        return self.tools.call(name, payload)

    def _has_tool(self, name: str) -> bool:
        try:
            self.tools.get(name)
            return True
        except KeyError:
            return False

    def _looks_like_list(self, text: str) -> bool:
        return any(marker in text for marker in LIST_MARKERS)

    def _looks_like_read(self, text: str) -> bool:
        return any(marker in text for marker in READ_MARKERS)

    def _looks_like_info(self, text: str) -> bool:
        return any(marker in text for marker in INFO_MARKERS)

    def _looks_like_search(self, text: str) -> bool:
        return any(marker in text for marker in SEARCH_MARKERS) or any(marker in text for marker in WEB_MARKERS)

    def _looks_high_risk(self, text: str) -> bool:
        return any(marker in text for marker in HIGH_RISK_MARKERS)

    def _extract_path(self, text: str) -> str:
        match = PATH_HINT.search(text)
        if not match:
            return ""
        if match.group(1):
            return match.group(1).strip()
        raw = (match.group(2) or "").strip(" ，,。；;：:")
        parts = [item.strip(" ，,。；;：:") for item in raw.split() if item.strip()]
        candidate = parts[-1] if parts else raw
        return self._clean_path_candidate(candidate)

    def _clean_path_candidate(self, path: str) -> str:
        candidate = path.strip(" `，。；;：:?？!！")
        changed = True
        while changed:
            changed = False
            for prefix in PATH_PREFIXES:
                if candidate.startswith(prefix):
                    candidate = candidate[len(prefix):].strip(" `，。；;：:?？!！")
                    changed = True
            for suffix in PATH_SUFFIXES:
                if candidate.endswith(suffix):
                    candidate = candidate[:-len(suffix)].strip(" `，。；;：:?？!！")
                    changed = True
        return candidate

    def _looks_like_directory_path(self, path: str) -> bool:
        return bool(path and not re.search(r"\.[A-Za-z0-9]+$", path))

    def _extract_search_query(self, text: str, path: str) -> str:
        cleaned = text
        for marker in SEARCH_MARKERS:
            cleaned = cleaned.replace(marker, " ")
        for marker in ("文件", "内容", "workspace", "中", "的", "一下", "包含"):
            cleaned = cleaned.replace(marker, " ")
        if path:
            cleaned = cleaned.replace(path, " ")
        tokens = [item.strip(" ，,。；;：:") for item in re.split(r"\s+", cleaned) if item.strip(" ，,。；;：:")]
        return (tokens[-1] if tokens else path).strip()[:80]

    def _format_tree(self, node: dict[str, Any] | None) -> str:
        if not node:
            return "workspace 文件树为空或无法读取。"
        lines = ["workspace 文件列表："]
        count = 0
        truncated = False

        def walk(item: dict[str, Any], depth: int) -> None:
            nonlocal count, truncated
            if depth > 3:
                return
            children = item.get("children") or []
            for child in children:
                if count >= 100:
                    truncated = True
                    return
                count += 1
                prefix = "  " * depth + "- "
                suffix = "/" if child.get("type") == "directory" else ""
                lines.append(f"{prefix}{child.get('name', '')}{suffix}")
                if child.get("type") == "directory":
                    walk(child, depth + 1)

        walk(node, 0)
        if count == 0:
            lines.append("- workspace 为空")
        if truncated:
            lines.append("...结果较多，已截断展示前 100 项。")
        return "\n".join(lines)

    def _tool_call(self, name: str, payload: dict[str, Any], ok: bool) -> dict[str, Any]:
        return {"tool": name, "payload": payload, "ok": ok}
