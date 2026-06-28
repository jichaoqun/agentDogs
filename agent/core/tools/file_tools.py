"""Workspace-scoped file tools used by agents and API layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import mimetypes
from pathlib import Path
import shutil
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

from .base import ToolRegistry, ToolResult, ToolSpec


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
TRASH_DIR_NAME = ".trash"
MAX_TEXT_BYTES = 2 * 1024 * 1024
EDITABLE_SUFFIXES = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".py",
    ".csv",
    ".xml",
    ".log",
}


class WorkspacePathError(ValueError):
    """Raised when a requested path is outside the configured workspace."""


class WorkspaceFileError(RuntimeError):
    """Raised when a workspace file operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class WorkspaceFileTools:
    root: Path = DEFAULT_WORKSPACE_ROOT

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        self.root.mkdir(parents=True, exist_ok=True)

    def list_tree(self, payload: dict[str, Any] | None = None) -> ToolResult:
        try:
            target = self.resolve((payload or {}).get("path", ""))
            if not target.exists() or not target.is_dir():
                return ToolResult.failure("目录不存在")
            tree = self.file_node(target)
            return ToolResult.success("已读取 workspace 文件树。", data=tree)
        except Exception as exc:
            return ToolResult.failure(str(exc))

    def read_file(self, payload: dict[str, Any] | None = None) -> ToolResult:
        try:
            target = self.resolve(str((payload or {}).get("path", "")), allow_root=False)
            self.reject_trash_path(target)
            if not target.is_file():
                return ToolResult.failure("文件不存在")
            suffix = target.suffix.lower()
            if self.is_editable(target):
                content = self.read_text_file(target)
                editable = True
            elif suffix == ".docx":
                content = self.extract_docx_text(target)
                editable = False
            else:
                return ToolResult.failure("该文件类型不支持文本读取")
            return ToolResult.success(
                f"已读取文件：{self.relative_path(target)}",
                data={
                    "path": self.relative_path(target),
                    "name": target.name,
                    "content": content,
                    "editable": editable,
                    "mime_type": self.mime_type(target),
                },
                artifacts=[self.relative_path(target)],
            )
        except Exception as exc:
            return ToolResult.failure(str(exc))

    def write_file(self, payload: dict[str, Any] | None = None) -> ToolResult:
        try:
            data = payload or {}
            target = self.resolve(str(data.get("path", "")), allow_root=False)
            self.reject_trash_path(target)
            if not target.exists():
                return ToolResult.failure("文件不存在")
            if not self.is_editable(target):
                return ToolResult.failure("该文件类型不支持写入")
            content = str(data.get("content", ""))
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_TEXT_BYTES:
                return ToolResult.failure("文件超过 2MB，暂不支持保存")
            target.write_bytes(encoded)
            return ToolResult.success(
                f"已写入文件：{self.relative_path(target)}",
                data={"path": self.relative_path(target), "bytes": len(encoded)},
                artifacts=[self.relative_path(target)],
            )
        except Exception as exc:
            return ToolResult.failure(str(exc))

    def create_directory(self, payload: dict[str, Any] | None = None) -> ToolResult:
        try:
            target = self.resolve(str((payload or {}).get("path", "")), allow_root=False)
            self.reject_trash_path(target)
            target.mkdir(parents=True, exist_ok=True)
            return ToolResult.success(
                f"已创建目录：{self.relative_path(target)}",
                data={"path": self.relative_path(target)},
                artifacts=[self.relative_path(target)],
            )
        except Exception as exc:
            return ToolResult.failure(str(exc))

    def publish_artifact(self, payload: dict[str, Any] | None = None) -> ToolResult:
        try:
            data = payload or {}
            source = self._resolve_artifact_source(str(data.get("source", "")))
            target = self.resolve(str(data.get("target", "")), allow_root=False)
            self.reject_trash_path(target)
            if target.exists() and target.is_dir():
                target = target / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            return ToolResult.success(
                f"已发布 artifact：{self.relative_path(target)}",
                data={"path": self.relative_path(target), "source": str(source)},
                artifacts=[self.relative_path(target)],
            )
        except Exception as exc:
            return ToolResult.failure(str(exc))

    def search_files(self, payload: dict[str, Any] | None = None) -> ToolResult:
        try:
            data = payload or {}
            query = str(data.get("query", "")).strip()
            limit = max(1, min(int(data.get("limit", 20)), 100))
            include_content = bool(data.get("include_content", True))
            if not query:
                return ToolResult.failure("搜索关键词不能为空")
            matches: list[dict[str, Any]] = []
            lowered = query.lower()
            for item in self.root.rglob("*"):
                if self.is_trash_path(item) or not item.is_file():
                    continue
                relative = self.relative_path(item)
                name_hit = lowered in item.name.lower() or lowered in relative.lower()
                content_hit = False
                snippet = ""
                if include_content and self.is_editable(item) and item.stat().st_size <= MAX_TEXT_BYTES:
                    text = self.read_text_file(item)
                    index = text.lower().find(lowered)
                    if index >= 0:
                        content_hit = True
                        start = max(0, index - 80)
                        end = min(len(text), index + len(query) + 120)
                        snippet = text[start:end]
                if name_hit or content_hit:
                    matches.append(
                        {
                            "path": relative,
                            "name": item.name,
                            "match": "content" if content_hit else "name",
                            "snippet": snippet,
                        }
                    )
                if len(matches) >= limit:
                    break
            return ToolResult.success(f"找到 {len(matches)} 个匹配项。", data={"matches": matches})
        except Exception as exc:
            return ToolResult.failure(str(exc))

    def file_info(self, payload: dict[str, Any] | None = None) -> ToolResult:
        try:
            target = self.resolve(str((payload or {}).get("path", "")), allow_root=False)
            self.reject_trash_path(target)
            if not target.exists():
                return ToolResult.failure("文件或目录不存在")
            return ToolResult.success(
                f"已读取文件信息：{self.relative_path(target)}",
                data=self.file_node(target, include_children=False),
                artifacts=[self.relative_path(target)],
            )
        except Exception as exc:
            return ToolResult.failure(str(exc))

    def resolve(self, path: str = "", *, allow_root: bool = True) -> Path:
        raw = (path or "").replace("\\", "/").strip("/")
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise WorkspacePathError("非法路径")
        if not raw and not allow_root:
            raise WorkspacePathError("路径不能为空")
        resolved = (self.root / candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspacePathError("路径超出 workspace")
        return resolved

    def _resolve_artifact_source(self, source: str) -> Path:
        raw = (source or "").replace("\\", "/").strip()
        if raw.startswith("/api/v1/artifacts/"):
            run_id, filename = raw.removeprefix("/api/v1/artifacts/").split("/", 1)
            raw = f"runtime/artifacts/{run_id}/{filename}"
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise WorkspacePathError("非法 artifact 路径")
        resolved = (PROJECT_ROOT / candidate).resolve()
        artifacts_root = (PROJECT_ROOT / "runtime" / "artifacts").resolve()
        if resolved != artifacts_root and artifacts_root not in resolved.parents:
            raise WorkspacePathError("artifact 路径超出 runtime/artifacts")
        if not resolved.is_file():
            raise WorkspaceFileError("artifact 不存在")
        return resolved

    def relative_path(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved == self.root:
            return ""
        return resolved.relative_to(self.root).as_posix()

    def reject_trash_path(self, path: Path) -> None:
        if self.is_trash_path(path):
            raise WorkspacePathError("不能直接操作回收区")

    def is_trash_path(self, path: Path) -> bool:
        relative = self.relative_path(path)
        first = relative.split("/", 1)[0] if relative else ""
        return first == TRASH_DIR_NAME

    def mime_type(self, path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def is_editable(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in EDITABLE_SUFFIXES

    def read_text_file(self, path: Path) -> str:
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise WorkspaceFileError("文件超过 2MB，暂不支持直接读取")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace")

    def extract_docx_text(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                xml_bytes = archive.read("word/document.xml")
        except (KeyError, zipfile.BadZipFile) as exc:
            raise WorkspaceFileError("无法预览该 DOCX 文件") from exc
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise WorkspaceFileError("无法预览该 DOCX 文件") from exc
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs: list[str] = []
        for paragraph in root.findall(".//w:p", namespace):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
            if text.strip():
                paragraphs.append(text)
        return "\n".join(paragraphs)

    def file_node(self, path: Path, *, include_children: bool = True) -> dict[str, Any]:
        stat = path.stat()
        is_dir = path.is_dir()
        children: list[dict[str, Any]] = []
        if include_children and is_dir:
            items = [item for item in path.iterdir() if not self.is_trash_path(item)]
            for item in sorted(items, key=lambda item: (not item.is_dir(), item.name.lower())):
                children.append(self.file_node(item))
        return {
            "path": self.relative_path(path),
            "name": path.name if path != self.root else "workspace",
            "type": "directory" if is_dir else "file",
            "size": 0 if is_dir else stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "mime_type": "inode/directory" if is_dir else self.mime_type(path),
            "editable": self.is_editable(path),
            "children": children,
        }


def create_file_tool_registry(root: Path = DEFAULT_WORKSPACE_ROOT) -> ToolRegistry:
    tools = WorkspaceFileTools(root)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="list_workspace_tree",
            description="列出 workspace 中的文件树，默认排除 .trash。",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            risk_level="low",
            capabilities=["file.list", "workspace.read"],
        ),
        tools.list_tree,
    )
    registry.register(
        ToolSpec(
            name="read_file",
            description="读取 workspace 内文本类文件或 DOCX 纯文本内容。",
            input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
            risk_level="low",
            capabilities=["file.read", "workspace.read"],
        ),
        tools.read_file,
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description="写入 workspace 内文本类文件。高风险，自动执行前必须人工确认。",
            input_schema={
                "type": "object",
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
            risk_level="high",
            capabilities=["file.write", "workspace.write"],
        ),
        tools.write_file,
    )
    registry.register(
        ToolSpec(
            name="create_directory",
            description="在 workspace 内创建目录。高风险，执行前必须人工确认。",
            input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
            risk_level="high",
            capabilities=["file.mkdir", "workspace.write"],
        ),
        tools.create_directory,
    )
    registry.register(
        ToolSpec(
            name="publish_artifact",
            description="把 runtime/artifacts 中的产物复制到 workspace。高风险，执行前必须人工确认。",
            input_schema={
                "type": "object",
                "required": ["source", "target"],
                "properties": {"source": {"type": "string"}, "target": {"type": "string"}},
            },
            risk_level="high",
            capabilities=["artifact.publish", "workspace.write"],
        ),
        tools.publish_artifact,
    )
    registry.register(
        ToolSpec(
            name="search_files",
            description="按文件名和文本内容搜索 workspace。",
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
            capabilities=["file.search", "workspace.read"],
        ),
        tools.search_files,
    )
    registry.register(
        ToolSpec(
            name="file_info",
            description="读取 workspace 内文件或目录的元信息。",
            input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
            risk_level="low",
            capabilities=["file.info", "workspace.read"],
        ),
        tools.file_info,
    )
    return registry
