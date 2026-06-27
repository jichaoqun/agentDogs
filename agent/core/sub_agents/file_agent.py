"""File-focused sub-agent built on top of workspace file tools."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..state import TaskBrief
from ..tools import ToolRegistry
from .registry import SubAgentResult, SubAgentSpec


PATH_HINT = re.compile(
    r"`([^`]+)`|([\w\u4e00-\u9fff .\\/-]+\.(?:md|txt|py|js|jsx|ts|tsx|json|ya?ml|html|css|csv|xml|log|docx))",
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
WRITE_MARKERS = ("写入", "保存", "修改", "改写", "删除", "重命名", "创建", "新增", "覆盖", "移动")


@dataclass(slots=True)
class FileAgent:
    """Read/search-only file agent for the first task execution phase."""

    CAPABILITY = SubAgentSpec(
        name="file_agent",
        description="处理 workspace 文件读取、文件搜索、文件信息查看和只读摘要。",
        handles=["读取明确路径文件", "搜索 workspace 文件", "查看文件信息", "只读文件摘要"],
        does_not_handle=["联网搜索", "天气/新闻等实时信息", "写入/删除/移动文件的自动执行"],
        capabilities=["file.read", "file.search", "file.analysis", "file.info"],
        tools=["list_workspace_tree", "read_file", "search_files", "workspace_search", "file_info"],
        input_contract={"type": "TaskBrief or step text", "fields": ["user_goal", "context.path", "expected_output"]},
        output_contract={"type": "SubAgentResult", "fields": ["summary", "findings", "evidence", "tool_calls"]},
        risk_level="low",
        examples=["读取 readme.md", "查找 workspace 里的 protein", "查看 app.py 的文件信息"],
    )

    tools: ToolRegistry

    @classmethod
    def capability_spec(cls) -> SubAgentSpec:
        return cls.CAPABILITY

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

    def handle_brief(self, brief: TaskBrief) -> SubAgentResult:
        context = brief.context or {}
        path = str(context.get("path") or "").strip()
        if path:
            return self._read_path(path)
        query = str(context.get("query") or brief.normalized_input or brief.user_goal).strip()
        if query:
            return self._search_query_text(query)
        return SubAgentResult.failure("FileAgent 没有收到明确文件路径或搜索关键词。", confidence=0.2)

    def suggest_write(self, path: str, content: str) -> SubAgentResult:
        return SubAgentResult.success(
            "已生成拟写入内容，但第一阶段不会自动保存文件。",
            data={"path": path, "content": content},
            status="waiting_confirmation",
        )

    def _read_path(self, path: str) -> SubAgentResult:
        result = self.tools.call("read_file", {"path": path})
        tool_call = self._tool_call("read_file", {"path": path}, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "文件读取失败", tool_calls=[tool_call], confidence=0.2)
        content = str((result.data or {}).get("content", ""))
        summary = self._summarize_content(path, content)
        return SubAgentResult.success(
            summary,
            data=result.data,
            summary=summary.splitlines()[0] if summary else "",
            findings=[{"path": path, "summary": summary[:240]}],
            evidence=[{"path": path, "source": "workspace"}],
            confidence=0.84,
            tool_calls=[tool_call],
        )

    def _search_query_text(self, query: str) -> SubAgentResult:
        result = self.tools.call("search_files", {"query": query, "limit": 10})
        tool_call = self._tool_call("search_files", {"query": query, "limit": 10}, result.ok)
        if not result.ok:
            return SubAgentResult.failure(result.error or "文件搜索失败", tool_calls=[tool_call], confidence=0.2)
        matches = (result.data or {}).get("matches", [])
        if not matches:
            content = "未在 workspace 中找到明确匹配文件。"
            return SubAgentResult.success(content, data=result.data, summary=content, confidence=0.45, tool_calls=[tool_call])
        lines = ["在 workspace 中找到以下匹配文件："]
        lines.extend(f"- {item['path']} ({item['match']})" for item in matches[:5])
        content = "\n".join(lines)
        return SubAgentResult.success(
            content,
            data=result.data,
            summary=lines[0],
            findings=[{"path": item.get("path", ""), "match": item.get("match", "")} for item in matches[:5]],
            evidence=[{"path": item.get("path", ""), "source": "workspace"} for item in matches[:5]],
            confidence=0.72,
            tool_calls=[tool_call],
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
