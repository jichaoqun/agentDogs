"""OpenSandbox-backed runner used by CodeAgent.

The runner deliberately has no host-Python fallback. If OpenSandbox is
unavailable, callers receive a structured failure so the safety boundary remains
explicit.
"""

from __future__ import annotations

from datetime import timedelta
import mimetypes
from pathlib import Path, PurePosixPath
import time
from typing import Any, Callable
from uuid import uuid4

from ..utils.llm_config import CodeExecutionConfig, PROJECT_ROOT
from .base import SandboxRunRequest, SandboxRunResult


WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
SANDBOX_SCRIPT_NAME = "task.script"
REMOTE_WORKSPACE = "/workspace"
REMOTE_ARTIFACTS = "/artifacts"


class OpenSandboxRunner:
    """Run Python code in an OpenSandbox sandbox."""

    def __init__(
        self,
        config: CodeExecutionConfig,
        *,
        project_root: Path = PROJECT_ROOT,
        workspace_root: Path = WORKSPACE_ROOT,
        sandbox_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.project_root = project_root.resolve()
        self.workspace_root = workspace_root.resolve()
        artifacts_root = Path(config.artifacts_dir)
        if not artifacts_root.is_absolute():
            artifacts_root = (self.project_root / artifacts_root).resolve()
        self.artifacts_root = artifacts_root
        self._sandbox_factory = sandbox_factory

    def run_python(self, code: str, *, timeout_seconds: int | None = None) -> SandboxRunResult:
        return self.run(SandboxRunRequest(code=code, timeout_seconds=timeout_seconds))

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        run_id = uuid4().hex
        started = time.monotonic()
        if request.language != "python":
            return SandboxRunResult(
                ok=False,
                run_id=run_id,
                error=f"Unsupported sandbox language: {request.language}",
            )
        if not self.config.enabled:
            return SandboxRunResult(
                ok=False,
                run_id=run_id,
                error="Code execution sandbox is disabled by code_execution.enabled.",
            )

        dependencies = self._normalize_dependencies(request.dependencies)
        dependency_error = self._validate_dependencies(dependencies)
        if dependency_error:
            return SandboxRunResult(ok=False, run_id=run_id, error=dependency_error, dependencies=dependencies)

        try:
            workspace_files = self._workspace_files(request)
        except Exception as exc:
            return SandboxRunResult(
                ok=False,
                run_id=run_id,
                error=str(exc) or exc.__class__.__name__,
                duration_ms=self._duration(started),
                dependencies=dependencies,
            )

        command = self._python_command(
            request.timeout_seconds,
            dependencies=dependencies,
        )
        sandbox: Any | None = None
        artifacts: list[dict[str, str]] = []
        try:
            sandbox = self._create_sandbox(
                network_required=request.network_required or bool(dependencies),
                timeout_seconds=request.timeout_seconds,
            )
            self._prepare_remote_dirs(sandbox)
            self._stage_workspace(sandbox, workspace_files)
            sandbox.files.write_file(f"{REMOTE_ARTIFACTS}/{SANDBOX_SCRIPT_NAME}", request.code, mode=644)
            execution = sandbox.commands.run(
                command,
                opts=self._run_command_opts(
                    request.timeout_seconds,
                    dependencies=dependencies,
                    env=request.env,
                ),
            )
            artifacts = self._safe_collect_artifacts(sandbox, run_id)
        except TimeoutError:
            return SandboxRunResult(
                ok=False,
                run_id=run_id,
                error="Sandbox execution timed out.",
                artifacts=self._safe_collect_artifacts(sandbox, run_id),
                duration_ms=self._duration(started),
                command=["opensandbox", "commands.run", command],
                dependencies=dependencies,
            )
        except ImportError as exc:
            return self._failure(
                run_id,
                started,
                "OpenSandbox SDK is not installed. CodeAgent will not fall back to host Python.",
                command,
                dependencies,
                stderr=str(exc),
            )
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            if "timeout" in error.lower():
                message = "Sandbox execution timed out."
            else:
                message = f"OpenSandbox sandbox is not available or execution failed: {error}"
            return SandboxRunResult(
                ok=False,
                run_id=run_id,
                stderr=self._truncate(error),
                error=message,
                artifacts=self._safe_collect_artifacts(sandbox, run_id),
                duration_ms=self._duration(started),
                command=["opensandbox", "commands.run", command],
                dependencies=dependencies,
            )
        finally:
            self._cleanup_sandbox(sandbox)

        stdout = self._truncate(self._stdout_from_execution(execution))
        stderr = self._truncate(self._stderr_from_execution(execution))
        exit_code = getattr(execution, "exit_code", None)
        if exit_code is None and getattr(execution, "error", None) is not None:
            exit_code = 1
        if exit_code is None:
            exit_code = 0
        return SandboxRunResult(
            ok=exit_code == 0,
            run_id=run_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            error=None if exit_code == 0 else "Sandbox execution failed.",
            artifacts=artifacts,
            duration_ms=self._duration(started),
            command=["opensandbox", "commands.run", command],
            dependencies=dependencies,
        )

    def _create_sandbox(self, *, network_required: bool, timeout_seconds: int | None) -> Any:
        if self._sandbox_factory is not None:
            return self._sandbox_factory(
                image=self.config.image,
                resource=self._resource_limits(),
                network_enabled=self.config.network_enabled or network_required,
            )

        from opensandbox.config.connection_sync import ConnectionConfigSync
        from opensandbox.models.sandboxes import NetworkPolicy
        from opensandbox.sync.sandbox import SandboxSync

        connection = ConnectionConfigSync(
            api_key=self.config.opensandbox.api_key or None,
            domain=self.config.opensandbox.domain,
            protocol=self.config.opensandbox.protocol,
            request_timeout=timedelta(seconds=self.config.opensandbox.request_timeout_seconds),
            use_server_proxy=self.config.opensandbox.use_server_proxy,
        )
        network_policy = NetworkPolicy(defaultAction="allow" if (self.config.network_enabled or network_required) else "deny")
        return SandboxSync.create(
            self.config.image,
            timeout=timedelta(seconds=(timeout_seconds or self.config.timeout_seconds) + self.config.install_timeout_seconds + 30),
            ready_timeout=timedelta(seconds=self.config.opensandbox.ready_timeout_seconds),
            resource=self._resource_limits(),
            network_policy=network_policy,
            connection_config=connection,
        )

    def _prepare_remote_dirs(self, sandbox: Any) -> None:
        sandbox.commands.run(f"mkdir -p {self._shell_quote(REMOTE_WORKSPACE)} {self._shell_quote(REMOTE_ARTIFACTS)}")

    def _stage_workspace(self, sandbox: Any, local_files: list[Path]) -> None:
        if not local_files:
            return
        directories = sorted({self._remote_parent(path) for path in local_files})
        self._mkdir_remote(sandbox, directories)
        for local_path in local_files:
            remote_path = self._remote_workspace_path(local_path)
            sandbox.files.write_file(remote_path, local_path.read_bytes(), mode=444)

    def _workspace_files(self, request: SandboxRunRequest) -> list[Path]:
        candidates: list[Path] = []
        if request.sync_workspace:
            candidates = [self.workspace_root]
        else:
            candidates = [self._resolve_workspace_path(path) for path in request.input_files if str(path).strip()]

        result: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if not candidate.exists():
                continue
            if candidate.is_file():
                files = [candidate]
            else:
                files = [path for path in candidate.rglob("*") if path.is_file()]
            for path in files:
                if self._skip_workspace_file(path):
                    continue
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(resolved)
        return sorted(result)

    def _resolve_workspace_path(self, path: str) -> Path:
        clean = str(path).replace("\\", "/").strip()
        target = self.workspace_root if clean in {"", "."} else (self.workspace_root / clean.lstrip("/")).resolve()
        root = self.workspace_root.resolve()
        if target != root and root not in target.parents:
            raise ValueError("sandbox input file escaped workspace root")
        return target

    def _skip_workspace_file(self, path: Path) -> bool:
        blocked = {".git", "__pycache__", "node_modules", ".venv", "venv"}
        return any(part in blocked for part in path.parts)

    def _remote_workspace_path(self, local_path: Path) -> str:
        relative = local_path.resolve().relative_to(self.workspace_root).as_posix()
        return f"{REMOTE_WORKSPACE}/{relative}"

    def _remote_parent(self, local_path: Path) -> str:
        remote = PurePosixPath(self._remote_workspace_path(local_path))
        return str(remote.parent)

    def _mkdir_remote(self, sandbox: Any, directories: list[str]) -> None:
        for index in range(0, len(directories), 40):
            chunk = directories[index:index + 40]
            if not chunk:
                continue
            quoted = " ".join(self._shell_quote(item) for item in chunk)
            sandbox.commands.run(f"mkdir -p {quoted}")

    def _python_command(self, timeout_seconds: int | None, *, dependencies: list[str]) -> str:
        if not dependencies:
            return f"python {self._shell_quote(f'{REMOTE_ARTIFACTS}/{SANDBOX_SCRIPT_NAME}')}"
        packages = " ".join(self._shell_quote(item) for item in dependencies)
        return (
            "mkdir -p /deps && "
            f"python -m pip install --no-cache-dir --disable-pip-version-check --target /deps {packages} && "
            f"PYTHONPATH=/deps python {self._shell_quote(f'{REMOTE_ARTIFACTS}/{SANDBOX_SCRIPT_NAME}')}"
        )

    def _run_command_opts(self, timeout_seconds: int | None, *, dependencies: list[str], env: dict[str, str]) -> Any:
        timeout = timeout_seconds or self.config.timeout_seconds
        if dependencies:
            timeout += self.config.install_timeout_seconds
        envs = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "AGENT_SANDBOX_TIMEOUT": str(timeout),
        }
        for key, value in sorted(env.items()):
            if self._safe_env_name(key):
                envs[key] = value
        try:
            from opensandbox.models.execd import RunCommandOpts

            return RunCommandOpts(
                working_directory=REMOTE_WORKSPACE,
                timeout=timedelta(seconds=timeout),
                envs=envs,
            )
        except ImportError:
            return {
                "working_directory": REMOTE_WORKSPACE,
                "timeout": timedelta(seconds=timeout),
                "envs": envs,
            }

    def _safe_collect_artifacts(self, sandbox: Any | None, run_id: str) -> list[dict[str, str]]:
        if sandbox is None:
            return []
        try:
            return self._collect_artifacts(sandbox, run_id)
        except Exception:
            return []

    def _collect_artifacts(self, sandbox: Any, run_id: str) -> list[dict[str, str]]:
        try:
            from opensandbox.models.filesystem import DirectoryListEntry
            directory_entry: Any = DirectoryListEntry(path=REMOTE_ARTIFACTS, depth=1)
        except ImportError:
            directory_entry = {"path": REMOTE_ARTIFACTS, "depth": 1}

        run_dir = self._run_dir(run_id)
        artifacts: list[dict[str, str]] = []
        entries = sandbox.files.list_directory(directory_entry)
        for entry in sorted(entries, key=lambda item: str(getattr(item, "path", ""))):
            remote_path = str(getattr(entry, "path", ""))
            filename = PurePosixPath(remote_path).name
            if not filename or filename == SANDBOX_SCRIPT_NAME:
                continue
            if len(artifacts) >= self.config.max_artifacts:
                break
            size = int(getattr(entry, "size", 0) or 0)
            if size > self.config.max_artifact_bytes:
                continue
            try:
                content = sandbox.files.read_bytes(remote_path)
            except Exception:
                continue
            if len(content) > self.config.max_artifact_bytes:
                continue
            target = run_dir / filename
            target.write_bytes(content)
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            artifacts.append(
                {
                    "run_id": run_id,
                    "filename": filename,
                    "path": self._artifact_path(run_id, filename),
                    "url": f"/api/v1/artifacts/{run_id}/{filename}",
                    "type": mime_type,
                }
            )
        return artifacts

    def _run_dir(self, run_id: str) -> Path:
        root = self.artifacts_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        run_dir = (root / run_id).resolve()
        if root != run_dir and root not in run_dir.parents:
            raise ValueError("sandbox run directory escaped artifacts root")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _artifact_path(self, run_id: str, filename: str) -> str:
        try:
            root_relative = self.artifacts_root.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            root_relative = self.artifacts_root.as_posix()
        return f"{root_relative.rstrip('/')}/{run_id}/{filename}"

    def _stdout_from_execution(self, execution: Any) -> str:
        chunks = [str(getattr(item, "text", "")) for item in getattr(execution.logs, "stdout", [])]
        for item in getattr(execution, "result", []) or []:
            text = getattr(item, "text", None)
            if text:
                chunks.append(str(text))
        return "".join(chunks)

    def _stderr_from_execution(self, execution: Any) -> str:
        chunks = [str(getattr(item, "text", "")) for item in getattr(execution.logs, "stderr", [])]
        error = getattr(execution, "error", None)
        if error is not None:
            name = str(getattr(error, "name", "") or "Error")
            value = str(getattr(error, "value", "") or "")
            chunks.append(f"{name}: {value}".strip())
        return "".join(chunks)

    def _cleanup_sandbox(self, sandbox: Any | None) -> None:
        if sandbox is None:
            return
        try:
            sandbox.kill()
        except Exception:
            pass
        try:
            sandbox.close()
        except Exception:
            pass

    def _resource_limits(self) -> dict[str, str]:
        return {
            "cpu": str(self.config.cpu_limit),
            "memory": self._memory_limit_for_opensandbox(),
        }

    def _memory_limit_for_opensandbox(self) -> str:
        value = str(self.config.memory_limit).strip()
        lowered = value.lower()
        if lowered.endswith("mi") or lowered.endswith("gi"):
            return value
        if lowered.endswith("m"):
            return f"{value[:-1]}Mi"
        if lowered.endswith("g"):
            return f"{value[:-1]}Gi"
        return value

    def _normalize_dependencies(self, dependencies: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in dependencies:
            package = str(item).strip()
            if package and package not in normalized:
                normalized.append(package)
        return normalized

    def _validate_dependencies(self, dependencies: list[str]) -> str:
        if not dependencies:
            return ""
        if not self.config.dependency_install_enabled:
            return "Dependency installation is disabled by code_execution.dependency_install.enabled."
        allowed = {item.lower() for item in self.config.allowed_packages}
        rejected = [item for item in dependencies if item.lower() not in allowed]
        if rejected:
            return f"Dependency is not allowed: {', '.join(rejected)}"
        return ""

    def _safe_env_name(self, name: str) -> bool:
        return name.replace("_", "").isalnum() and not name.startswith(("PYTHON", "PIP_", "AGENT_"))

    def _truncate(self, text: str) -> str:
        limit = self.config.max_output_chars
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    def _duration(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _failure(
        self,
        run_id: str,
        started: float,
        error: str,
        command: str,
        dependencies: list[str],
        *,
        stderr: str = "",
    ) -> SandboxRunResult:
        return SandboxRunResult(
            ok=False,
            run_id=run_id,
            stderr=self._truncate(stderr),
            error=error,
            duration_ms=self._duration(started),
            command=["opensandbox", "commands.run", command],
            dependencies=dependencies,
        )

    def _shell_quote(self, value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"
