from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import mimetypes
from pathlib import Path
import shutil
from uuid import uuid4
import zipfile
import xml.etree.ElementTree as ET

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import BaseMessage

from agent.core.utils.llm_config import AppConfig, load_config
from agent.core.utils.llm_models import (
    GenerationOptions,
    LLMError,
    ModelInvocationError,
    ModelNotFound,
    ModelSelection,
    ProviderNotFound,
)

from .schemas import (
    BackendStatus,
    ChatRequest,
    ChatResponse,
    FailureOut,
    FileContentOut,
    FileCreate,
    FileNodeOut,
    FileSave,
    FileUpdate,
    MessageOut,
    ModelInfoOut,
    SessionCreate,
    SessionOut,
    StatusResponse,
)
from .sessions import ChatSession, SessionManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = PROJECT_ROOT / "GUI" / "dist"
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
TRASH_DIR_NAME = ".trash"
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
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
ALLOWED_UPLOAD_SUFFIXES = {
    *EDITABLE_SUFFIXES,
    ".pdf",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
}


def _mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _is_editable(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in EDITABLE_SUFFIXES


def _relative_path(path: Path) -> str:
    root = WORKSPACE_ROOT.resolve()
    resolved = path.resolve()
    if resolved == root:
        return ""
    return resolved.relative_to(root).as_posix()


def _reject_trash_path(path: Path) -> None:
    relative = _relative_path(path)
    first = relative.split("/", 1)[0] if relative else ""
    if first == TRASH_DIR_NAME:
        raise HTTPException(status_code=403, detail="不能直接操作回收区")


def _resolve_workspace_path(path: str = "", *, allow_root: bool = True) -> Path:
    raw = (path or "").replace("\\", "/").strip("/")
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="非法路径")
    if not raw and not allow_root:
        raise HTTPException(status_code=400, detail="路径不能为空")
    root = WORKSPACE_ROOT.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise HTTPException(status_code=400, detail="路径超出 workspace")
    return resolved


def _safe_child_name(name: str) -> str:
    clean = name.strip().replace("\\", "/")
    if not clean or "/" in clean or clean in {".", ".."}:
        raise HTTPException(status_code=400, detail="非法名称")
    if Path(clean).drive:
        raise HTTPException(status_code=400, detail="非法名称")
    return clean


def _file_node(path: Path) -> FileNodeOut:
    stat = path.stat()
    is_dir = path.is_dir()
    children: list[FileNodeOut] = []
    if is_dir:
        items = [
            item for item in path.iterdir()
            if not (_relative_path(item) == TRASH_DIR_NAME or _relative_path(item).startswith(f"{TRASH_DIR_NAME}/"))
        ]
        for item in sorted(items, key=lambda item: (not item.is_dir(), item.name.lower())):
            children.append(_file_node(item))
    return FileNodeOut(
        path=_relative_path(path),
        name=path.name if path != WORKSPACE_ROOT.resolve() else "workspace",
        type="directory" if is_dir else "file",
        size=0 if is_dir else stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        mime_type="inode/directory" if is_dir else _mime_type(path),
        editable=_is_editable(path),
        children=children,
    )


def _read_text_file(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 2MB，暂不支持直接编辑")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _extract_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml_bytes = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=415, detail="无法预览该 DOCX 文件") from exc
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise HTTPException(status_code=415, detail="无法预览该 DOCX 文件") from exc
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _trash_target(path: Path) -> Path:
    trash = WORKSPACE_ROOT / TRASH_DIR_NAME
    trash.mkdir(parents=True, exist_ok=True)
    relative = _relative_path(path).replace("/", "__") or path.name
    candidate = trash / relative
    if not candidate.exists():
        return candidate
    return trash / f"{candidate.stem}-{uuid4().hex[:8]}{candidate.suffix}"


def _message_out(message: BaseMessage) -> MessageOut:
    role = "assistant" if message.type == "ai" else "user" if message.type == "human" else message.type
    content = message.content if isinstance(message.content, str) else str(message.content)
    return MessageOut(role=role, content=content)


def _session_out(session: ChatSession, include_messages: bool = True) -> SessionOut:
    messages = [_message_out(item) for item in session.messages()] if include_messages else []
    return SessionOut(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=messages,
    )


def create_app(config: AppConfig | None = None, manager: SessionManager | None = None) -> FastAPI:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    active_config = config or load_config()
    sessions = manager or SessionManager(active_config)
    application = FastAPI(title="agentDogs API", version="0.1.0")
    application.state.config = active_config
    application.state.sessions = sessions
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/v1/status", response_model=StatusResponse)
    def backend_status() -> StatusResponse:
        return StatusResponse(
            status="ok",
            default_provider=active_config.default_provider,
            default_model=active_config.default_model,
            backends=[
                BackendStatus(name=name, enabled=item.enabled, model=item.model)
                for name, item in active_config.providers.items()
            ],
        )

    @application.get("/api/v1/models", response_model=list[ModelInfoOut])
    def list_models(provider: str | None = None) -> list[ModelInfoOut]:
        try:
            models = sessions.models.list_models(provider)
        except LLMError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return [
            ModelInfoOut(
                provider=item.provider,
                model=item.model,
                display_name=item.display_name,
                supports_thinking=item.supports_thinking,
            )
            for item in models
        ]

    @application.get("/api/v1/files/tree", response_model=FileNodeOut)
    def file_tree() -> FileNodeOut:
        return _file_node(WORKSPACE_ROOT.resolve())

    @application.get("/api/v1/files/content", response_model=FileContentOut)
    def file_content(path: str) -> FileContentOut:
        target = _resolve_workspace_path(path, allow_root=False)
        _reject_trash_path(target)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        suffix = target.suffix.lower()
        if _is_editable(target):
            content = _read_text_file(target)
            editable = True
        elif suffix == ".docx":
            content = _extract_docx_text(target)
            editable = False
        else:
            raise HTTPException(status_code=415, detail="该文件类型不支持文本预览")
        return FileContentOut(
            path=_relative_path(target),
            name=target.name,
            content=content,
            editable=editable,
            mime_type=_mime_type(target),
        )

    @application.put("/api/v1/files/content", response_model=FileContentOut)
    def save_file_content(payload: FileSave) -> FileContentOut:
        target = _resolve_workspace_path(payload.path, allow_root=False)
        _reject_trash_path(target)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        if not _is_editable(target):
            raise HTTPException(status_code=415, detail="该文件类型不支持编辑")
        encoded = payload.content.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise HTTPException(status_code=413, detail="文件超过 2MB，暂不支持保存")
        target.write_bytes(encoded)
        return FileContentOut(
            path=_relative_path(target),
            name=target.name,
            content=payload.content,
            editable=True,
            mime_type=_mime_type(target),
        )

    @application.get("/api/v1/files/raw")
    def raw_file(path: str) -> FileResponse:
        target = _resolve_workspace_path(path, allow_root=False)
        _reject_trash_path(target)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(target, media_type=_mime_type(target))

    @application.get("/api/v1/files/download")
    def download_file(path: str) -> FileResponse:
        target = _resolve_workspace_path(path, allow_root=False)
        _reject_trash_path(target)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(
            target,
            media_type="application/octet-stream",
            filename=target.name,
        )

    @application.post("/api/v1/files", response_model=FileNodeOut, status_code=status.HTTP_201_CREATED)
    def create_file_item(payload: FileCreate) -> FileNodeOut:
        parent = _resolve_workspace_path(payload.path)
        _reject_trash_path(parent)
        if not parent.is_dir():
            raise HTTPException(status_code=404, detail="目标文件夹不存在")
        name = _safe_child_name(payload.name)
        target = parent / name
        if target.exists():
            raise HTTPException(status_code=409, detail="同名文件或文件夹已存在")
        if payload.type == "directory":
            target.mkdir()
        else:
            target.touch()
        return _file_node(target)

    @application.patch("/api/v1/files", response_model=FileNodeOut)
    def rename_file_item(payload: FileUpdate) -> FileNodeOut:
        target = _resolve_workspace_path(payload.path, allow_root=False)
        _reject_trash_path(target)
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件或文件夹不存在")
        name = _safe_child_name(payload.name)
        destination = target.parent / name
        if destination.exists():
            raise HTTPException(status_code=409, detail="同名文件或文件夹已存在")
        target.rename(destination)
        return _file_node(destination)

    @application.delete(
        "/api/v1/files",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_file_item(path: str) -> Response:
        target = _resolve_workspace_path(path, allow_root=False)
        _reject_trash_path(target)
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件或文件夹不存在")
        shutil.move(str(target), str(_trash_target(target)))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post("/api/v1/files/upload", response_model=FileNodeOut, status_code=status.HTTP_201_CREATED)
    async def upload_file_item(path: str = "", file: UploadFile = File(...)) -> FileNodeOut:
        parent = _resolve_workspace_path(path)
        _reject_trash_path(parent)
        if not parent.is_dir():
            raise HTTPException(status_code=404, detail="目标文件夹不存在")
        filename = _safe_child_name(file.filename or "")
        if Path(filename).suffix.lower() not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=415, detail="不支持的上传文件类型")
        target = parent / filename
        if target.exists():
            raise HTTPException(status_code=409, detail="同名文件已存在")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="上传文件超过 50MB")
        target.write_bytes(data)
        return _file_node(target)

    @application.post("/api/v1/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
    def create_session(payload: SessionCreate) -> SessionOut:
        return _session_out(sessions.create(payload.title))

    @application.get("/api/v1/sessions", response_model=list[SessionOut])
    def list_sessions() -> list[SessionOut]:
        return [_session_out(item, include_messages=False) for item in sessions.list()]

    @application.get("/api/v1/sessions/{session_id}", response_model=SessionOut)
    def get_session(session_id: str) -> SessionOut:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return _session_out(session)

    @application.delete(
        "/api/v1/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_session(session_id: str) -> Response:
        if not sessions.delete(session_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post("/api/v1/sessions/{session_id}/messages", response_model=ChatResponse)
    async def send_message(session_id: str, payload: ChatRequest) -> ChatResponse:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        selected_provider = payload.provider or active_config.default_provider
        if payload.model:
            selected_model = payload.model
        elif selected_provider == active_config.default_provider:
            selected_model = active_config.default_model
        elif selected_provider == "builtin":
            selected_model = "builtin"
        else:
            provider_config = active_config.providers.get(selected_provider)
            selected_model = provider_config.model if provider_config is not None else ""
        selection = ModelSelection(selected_provider, selected_model)
        options = GenerationOptions(
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
            thinking_enabled=payload.thinking_enabled,
        )
        try:
            result = await run_in_threadpool(
                session.agent.chat,
                payload.message,
                selection=selection,
                options=options,
            )
        except (ProviderNotFound, ModelNotFound, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ModelInvocationError as exc:
            raise HTTPException(
                status_code=503,
                detail={"message": "模型调用失败", "reason": str(exc)},
            ) from exc
        sessions.touch(session, first_message=payload.message)
        return ChatResponse(
            session_id=session.id,
            message=MessageOut(role="assistant", content=result.content),
            backend=result.backend,
            provider=result.provider,
            model=result.model,
            failures=[FailureOut(backend=name, reason=reason) for name, reason in result.failures],
            reasoning=result.reasoning,
            thinking_enabled=payload.thinking_enabled,
        )

    if FRONTEND_DIST.is_dir():
        assets = FRONTEND_DIST / "assets"
        if assets.is_dir():
            application.mount("/assets", StaticFiles(directory=assets), name="assets")

        @application.get("/{path:path}", include_in_schema=False)
        def frontend(request: Request, path: str) -> FileResponse:
            candidate = (FRONTEND_DIST / path).resolve()
            if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return application


app = create_app()
