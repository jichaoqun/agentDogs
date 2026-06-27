"""File-focused sub-agent built on top of workspace file tools."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..tools import ToolRegistry
from .registry import SubAgentResult


PATH_HINT = re.compile(
    r"`([^`]+)`|([\w\u4e00-\u9fff .\\/-]+\.(?:md|txt|py|js|jsx|ts|tsx|json|ya?ml|html|css|csv|xml|log|docx))",
    re.IGNORECASE,
)
WRITE_MARKERS = ("写入", "保存", "修改", "改写", "删除", "重命名", "创建", "新增", "覆盖", "移动")


@dataclass(slots=True)
class FileAgent:
    """Read/search-only file agent for the first task execution phase."""

    tools: ToolRegistry

    def handle_step(self, step: str, *, user_input: str = "") -> SubAgentResult:
        text = f"{step}\n{user_input}"
        if self._looks_like_write(text):
            return SubAgentResult.success(
                "该步骤涉及文件写入或破坏性操作，第一阶段只生成建议，等待人工确认后再执行。",
                data={"proposed_action": step},
                status="waiting_confirmation",
            )

        path = self._extract_path(text)
        if path:
            result = self.tools.call("read_file", {"path": path})
            tool_call = self._tool_call("read_file", {"path": path}, result.ok)
            if not result.ok:
                return SubAgentResult.failure(result.error or "文件读取失败", tool_calls=[tool_call])
            content = str((result.data or {}).get("content", ""))
            summary = self._summarize_content(path, content)
            return SubAgentResult.success(summary, data=result.data, tool_calls=[tool_call])

        query = self._search_query(step, user_input)
        if query:
            result = self.tools.call("search_files", {"query": query, "limit": 10})
            tool_call = self._tool_call("search_files", {"query": query, "limit": 10}, result.ok)
            if not result.ok:
                return SubAgentResult.failure(result.error or "文件搜索失败", tool_calls=[tool_call])
            matches = (result.data or {}).get("matches", [])
            if not matches:
                return SubAgentResult.success("未在 workspace 中找到明确匹配文件。", data=result.data, tool_calls=[tool_call])
            lines = ["在 workspace 中找到以下匹配文件："]
            lines.extend(f"- {item['path']} ({item['match']})" for item in matches[:5])
            return SubAgentResult.success("\n".join(lines), data=result.data, tool_calls=[tool_call])

        return SubAgentResult.success("该步骤没有明确文件路径或搜索关键词，FileAgent 暂不执行具体文件操作。")

    def suggest_write(self, path: str, content: str) -> SubAgentResult:
        return SubAgentResult.success(
            "已生成拟写入内容，但第一阶段不会自动保存文件。",
            data={"path": path, "content": content},
            status="waiting_confirmation",
        )

    def _looks_like_write(self, text: str) -> bool:
        return any(marker in text for marker in WRITE_MARKERS)

    def _extract_path(self, text: str) -> str:
        match = PATH_HINT.search(text)
        if not match:
            return ""
        if match.group(1):
            return match.group(1).strip()
        raw = (match.group(2) or "").strip(" ，,。；;：:")
        parts = [item.strip(" ，,。；;：:") for item in raw.split() if item.strip()]
        return parts[-1] if parts else raw

    def _search_query(self, step: str, user_input: str) -> str:
        text = f"{step} {user_input}".strip()
        if any(marker in text for marker in ("搜索", "查找", "文件", "workspace", "目录")):
            tokens = [item for item in re.split(r"\s+", text) if item]
            return tokens[-1][:80] if tokens else ""
        return ""

    def _summarize_content(self, path: str, content: str) -> str:
        text = content.strip()
        if not text:
            return f"{path} 文件为空或没有可提取文本。"
        preview = text[:700]
        suffix = "..." if len(text) > len(preview) else ""
        return f"已读取 {path}，内容预览：\n{preview}{suffix}"

    def _tool_call(self, name: str, payload: dict[str, Any], ok: bool) -> dict[str, Any]:
        return {"tool": name, "payload": payload, "ok": ok}
