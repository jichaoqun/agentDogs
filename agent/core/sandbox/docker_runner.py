"""Docker-backed sandbox runner used by CodeAgent.

The runner deliberately has no host-Python fallback. If Docker is unavailable,
callers receive a structured failure so the safety boundary remains explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import mimetypes
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from ..utils.llm_config import CodeExecutionConfig, PROJECT_ROOT


WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
SANDBOX_SCRIPT_NAME = "task.script"


@dataclass(slots=True)
class SandboxRunRequest:
    code: str
    language: str = "python"
    timeout_seconds: int | None = None
    input_files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    network_required: bool = False


@dataclass(slots=True)
class SandboxRunResult:
    ok: bool
    run_id: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    artifacts: list[dict[str, str]] = field(default_factory=list)
    duration_ms: int = 0
    command: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "artifacts": self.artifacts,
            "duration_ms": self.duration_ms,
            "command": self.command,
            "dependencies": self.dependencies,
        }


class DockerSandboxRunner:
    """Run Python code in a constrained Docker container."""

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
        artifacts_root = Path(config.artifacts_dir)
        if not artifacts_root.is_absolute():
            artifacts_root = (self.project_root / artifacts_root).resolve()
        self.artifacts_root = artifacts_root
        self.deps_root = (self.project_root / "runtime" / "sandbox_deps").resolve()

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

        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        script_path = run_dir / SANDBOX_SCRIPT_NAME
        script_path.write_text(request.code, encoding="utf-8")
        command = self._docker_command(
            run_dir,
            request.timeout_seconds,
            dependencies=dependencies,
            network_required=request.network_required,
            env=request.env,
        )

        try:
            completed = subprocess.run(
                command,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=(request.timeout_seconds or self.config.timeout_seconds) + 5,
                check=False,
            )
        except FileNotFoundError:
            return self._failure(
                run_id,
                started,
                "Docker is not available. CodeAgent will not fall back to host Python.",
                command,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._truncate(exc.stdout or "")
            stderr = self._truncate(exc.stderr or "")
            return SandboxRunResult(
                ok=False,
                run_id=run_id,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                error="Sandbox execution timed out.",
                artifacts=self._collect_artifacts(run_dir, run_id),
                duration_ms=self._duration(started),
                command=command,
                dependencies=dependencies,
            )

        stdout = self._truncate(completed.stdout or "")
        stderr = self._truncate(completed.stderr or "")
        return SandboxRunResult(
            ok=completed.returncode == 0,
            run_id=run_id,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            error=None if completed.returncode == 0 else "Sandbox execution failed.",
            artifacts=self._collect_artifacts(run_dir, run_id),
            duration_ms=self._duration(started),
            command=command,
            dependencies=dependencies,
        )

    def _docker_command(
        self,
        run_dir: Path,
        timeout_seconds: int | None = None,
        *,
        dependencies: list[str] | None = None,
        network_required: bool = False,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        workspace_mode = "ro" if self.config.workspace_readonly else "rw"
        timeout = str(timeout_seconds or self.config.timeout_seconds)
        dependencies = dependencies or []
        deps_dir = self._dependency_dir(dependencies) if dependencies else None
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge" if (self.config.network_enabled or network_required or dependencies) else "none",
            "--memory",
            self.config.memory_limit,
            "--cpus",
            str(self.config.cpu_limit),
            "--user",
            "1000:1000",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            f"AGENT_SANDBOX_TIMEOUT={timeout}",
        ]
        for key, value in sorted((env or {}).items()):
            if self._safe_env_name(key):
                command.extend(["-e", f"{key}={value}"])
        command.extend([
            "-v",
            f"{self.workspace_root}:/workspace:{workspace_mode}",
            "-v",
            f"{run_dir}:/artifacts:rw",
        ])
        if deps_dir is not None:
            command.extend(["-v", f"{deps_dir}:/deps:rw"])
        command.extend([
            "-w",
            "/workspace",
            self.config.image,
        ])
        if dependencies:
            packages = " ".join(self._shell_quote(item) for item in dependencies)
            command.extend([
                "sh",
                "-c",
                (
                    "if [ ! -f /deps/.agent_dogs_ready ]; then "
                    f"python -m pip install --no-cache-dir --disable-pip-version-check --target /deps {packages} && "
                    "touch /deps/.agent_dogs_ready; "
                    "fi && "
                    f"PYTHONPATH=/deps python /artifacts/{SANDBOX_SCRIPT_NAME}"
                ),
            ])
        else:
            command.extend(["python", f"/artifacts/{SANDBOX_SCRIPT_NAME}"])
        return [str(item) for item in command]

    def _run_dir(self, run_id: str) -> Path:
        root = self.artifacts_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        run_dir = (root / run_id).resolve()
        if root != run_dir and root not in run_dir.parents:
            raise ValueError("sandbox run directory escaped artifacts root")
        return run_dir

    def _dependency_dir(self, dependencies: list[str]) -> Path:
        key = "|".join([self.config.image, *dependencies])
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        target = (self.deps_root / digest).resolve()
        self.deps_root.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        if self.deps_root != target and self.deps_root not in target.parents:
            raise ValueError("sandbox dependency directory escaped dependency root")
        return target

    def _collect_artifacts(self, run_dir: Path, run_id: str) -> list[dict[str, str]]:
        artifacts: list[dict[str, str]] = []
        for path in sorted(run_dir.iterdir()):
            if not path.is_file() or path.name == SANDBOX_SCRIPT_NAME:
                continue
            if len(artifacts) >= self.config.max_artifacts:
                break
            if path.stat().st_size > self.config.max_artifact_bytes:
                continue
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            artifacts.append(
                {
                    "run_id": run_id,
                    "filename": path.name,
                    "path": f"runtime/artifacts/{run_id}/{path.name}",
                    "url": f"/api/v1/artifacts/{run_id}/{path.name}",
                    "type": mime_type,
                }
            )
        return artifacts

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
        command: list[str],
    ) -> SandboxRunResult:
        return SandboxRunResult(
            ok=False,
            run_id=run_id,
            error=error,
            duration_ms=self._duration(started),
            command=command,
        )

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

    def _shell_quote(self, value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"
