"""Local subprocess runner for trusted, human-approved code execution.

This backend is not a strong security sandbox. It is intended for local
development or trusted tasks on machines where OpenSandbox is unavailable.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Sequence
from uuid import uuid4

from ..utils.llm_config import CodeExecutionConfig, PROJECT_ROOT
from .base import SandboxRunRequest, SandboxRunResult


WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
SCRIPT_NAME = "task.script"
BACKEND = "local_process"
ISOLATION = "none"
WARNINGS = ["\u672c\u5730\u8fdb\u7a0b\u6267\u884c\u4e0d\u662f\u5f3a\u5b89\u5168\u6c99\u7bb1\uff0c\u8bf7\u52ff\u8fd0\u884c\u4e0d\u53ef\u4fe1\u4ee3\u7801\u3002"]


class LocalProcessRunner:
    """Run Python code in a local subprocess after external approval."""

    def __init__(
        self,
        config: CodeExecutionConfig,
        *,
        project_root: Path = PROJECT_ROOT,
        workspace_root: Path = WORKSPACE_ROOT,
    ) -> None:
        self.config = config
        self.project_root = project_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.artifacts_root = self._resolve_under_project(config.artifacts_dir)
        self.runs_root = self._resolve_under_project(config.local_process.runs_dir)
        self.deps_root = self._resolve_under_project(config.local_process.deps_dir)

    def run_python(self, code: str, *, timeout_seconds: int | None = None) -> SandboxRunResult:
        return self.run(SandboxRunRequest(code=code, timeout_seconds=timeout_seconds))

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        run_id = request.run_id or uuid4().hex
        started = time.monotonic()
        if request.language != "python":
            return self._result(False, run_id, started, error=f"Unsupported sandbox language: {request.language}")
        if not self.config.enabled:
            return self._result(False, run_id, started, error="Code execution sandbox is disabled by code_execution.enabled.")

        dependencies = self._normalize_dependencies(request.dependencies)
        dependency_error = self._validate_dependencies(dependencies)
        if dependency_error:
            return self._result(False, run_id, started, error=dependency_error, dependencies=dependencies)

        try:
            workspace_files = self._workspace_files(request)
            run_dir = self._run_dir(run_id)
            staged_workspace = run_dir / "workspace"
            staged_artifacts = run_dir / "artifacts"
            staged_workspace.mkdir(parents=True, exist_ok=True)
            staged_artifacts.mkdir(parents=True, exist_ok=True)
            self._stage_workspace(workspace_files, staged_workspace)
            script = run_dir / SCRIPT_NAME
            script.write_text(request.code, encoding="utf-8")
        except Exception as exc:
            return self._result(False, run_id, started, error=str(exc) or exc.__class__.__name__, dependencies=dependencies)

        timeout = request.timeout_seconds or self.config.timeout_seconds
        env = self._execution_env(request.env, staged_workspace, staged_artifacts)
        command = [self._python_executable(), str(script)]
        try:
            if dependencies:
                install_result = self._install_dependencies(run_id, dependencies, env, timeout)
                if not install_result["ok"]:
                    return self._result(
                        False,
                        run_id,
                        started,
                        exit_code=install_result["exit_code"],
                        stdout=install_result["stdout"],
                        stderr=install_result["stderr"],
                        error=install_result["error"],
                        command=install_result["command"],
                        dependencies=dependencies,
                    )
                env["PYTHONPATH"] = str(self._deps_dir(run_id))

            exit_code, stdout, stderr, timed_out = self._run_process(command, cwd=staged_workspace, env=env, timeout=timeout)
            artifacts = self._safe_collect_artifacts(staged_artifacts, run_id)
            if timed_out:
                return self._result(
                    False,
                    run_id,
                    started,
                    stdout=stdout,
                    stderr=stderr,
                    error="Sandbox execution timed out.",
                    artifacts=artifacts,
                    command=["local_process", *command],
                    dependencies=dependencies,
                )
            return self._result(
                exit_code == 0,
                run_id,
                started,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error=None if exit_code == 0 else "Sandbox execution failed.",
                artifacts=artifacts,
                command=["local_process", *command],
                dependencies=dependencies,
            )
        except Exception as exc:
            return self._result(
                False,
                run_id,
                started,
                stderr=str(exc),
                error=f"Local process execution failed: {exc}",
                artifacts=self._safe_collect_artifacts(staged_artifacts, run_id),
                command=["local_process", *command],
                dependencies=dependencies,
            )
        finally:
            if self.config.local_process.cleanup_runs:
                self._cleanup_run_dir(run_id)

    def _resolve_under_project(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    def _python_executable(self) -> str:
        configured = self.config.local_process.python_executable.strip()
        return configured or sys.executable

    def _run_dir(self, run_id: str) -> Path:
        root = self.runs_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        run_dir = (root / run_id).resolve()
        if root != run_dir and root not in run_dir.parents:
            raise ValueError("local process run directory escaped runs root")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _deps_dir(self, run_id: str) -> Path:
        root = self.deps_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        deps_dir = (root / run_id).resolve()
        if root != deps_dir and root not in deps_dir.parents:
            raise ValueError("local process dependency directory escaped deps root")
        return deps_dir

    def _workspace_files(self, request: SandboxRunRequest) -> list[Path]:
        candidates: list[Path]
        if request.sync_workspace:
            candidates = [self.workspace_root]
        else:
            candidates = [self._resolve_workspace_path(path) for path in request.input_files if str(path).strip()]

        result: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if not candidate.exists():
                continue
            files = [candidate] if candidate.is_file() else [path for path in candidate.rglob("*") if path.is_file()]
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

    def _stage_workspace(self, files: list[Path], staged_workspace: Path) -> None:
        staged_root = staged_workspace.resolve()
        for source in files:
            relative = source.resolve().relative_to(self.workspace_root)
            target = (staged_workspace / relative).resolve()
            if staged_root not in target.parents and target != staged_root:
                raise ValueError("staged workspace file escaped run workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _execution_env(self, request_env: dict[str, str], workspace: Path, artifacts: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "AGENT_CODE_BACKEND": BACKEND,
                "AGENT_WORKSPACE_DIR": str(workspace),
                "AGENT_ARTIFACTS_DIR": str(artifacts),
                "AGENT_SANDBOX_TIMEOUT": str(self.config.timeout_seconds),
            }
        )
        for key, value in sorted(request_env.items()):
            if self._safe_env_name(key):
                env[key] = value
        return env

    def _install_dependencies(self, run_id: str, dependencies: list[str], env: dict[str, str], timeout: int) -> dict[str, object]:
        deps_dir = self._deps_dir(run_id)
        deps_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self._python_executable(),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--target",
            str(deps_dir),
            *dependencies,
        ]
        exit_code, stdout, stderr, timed_out = self._run_process(
            command,
            cwd=self.project_root,
            env=env,
            timeout=timeout + self.config.install_timeout_seconds,
        )
        error = "Dependency installation timed out." if timed_out else "Dependency installation failed."
        return {
            "ok": exit_code == 0 and not timed_out,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "error": None if exit_code == 0 and not timed_out else error,
            "command": ["local_process", *command],
        }

    def _run_process(self, command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int | None, str, str, bool]:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, self._truncate(stdout), self._truncate(stderr), False
        except subprocess.TimeoutExpired:
            self._kill_process_tree(process)
            stdout, stderr = process.communicate()
            return None, self._truncate(stdout), self._truncate(stderr), True

    def _kill_process_tree(self, process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        try:
            os.killpg(process.pid, 9)
        except Exception:
            process.kill()

    def _safe_collect_artifacts(self, source_dir: Path, run_id: str) -> list[dict[str, str]]:
        try:
            return self._collect_artifacts(source_dir, run_id)
        except Exception:
            return []

    def _collect_artifacts(self, source_dir: Path, run_id: str) -> list[dict[str, str]]:
        run_dir = self._artifact_run_dir(run_id)
        artifacts: list[dict[str, str]] = []
        for source in sorted(source_dir.iterdir()):
            if not source.is_file():
                continue
            if len(artifacts) >= self.config.max_artifacts:
                break
            size = source.stat().st_size
            if size > self.config.max_artifact_bytes:
                continue
            target = run_dir / source.name
            if run_dir != target.resolve().parent:
                continue
            shutil.copy2(source, target)
            mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            artifacts.append(
                {
                    "run_id": run_id,
                    "filename": source.name,
                    "path": self._artifact_path(run_id, source.name),
                    "url": f"/api/v1/artifacts/{run_id}/{source.name}",
                    "type": mime_type,
                }
            )
        return artifacts

    def _artifact_run_dir(self, run_id: str) -> Path:
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

    def _cleanup_run_dir(self, run_id: str) -> None:
        try:
            run_dir = (self.runs_root.resolve() / run_id).resolve()
            if self.runs_root.resolve() == run_dir or self.runs_root.resolve() in run_dir.parents:
                shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:
            pass

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

    def _result(
        self,
        ok: bool,
        run_id: str,
        started: float,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
        artifacts: list[dict[str, str]] | None = None,
        command: list[str] | None = None,
        dependencies: list[str] | None = None,
    ) -> SandboxRunResult:
        return SandboxRunResult(
            ok=ok,
            run_id=run_id,
            exit_code=exit_code,
            stdout=self._truncate(stdout),
            stderr=self._truncate(stderr),
            error=error,
            artifacts=artifacts or [],
            duration_ms=self._duration(started),
            command=command or [],
            dependencies=dependencies or [],
            backend=BACKEND,
            isolation=ISOLATION,
            warnings=list(WARNINGS),
        )
