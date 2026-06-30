"""Shared sandbox request/result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class SandboxRunRequest:
    code: str
    language: str = "python"
    timeout_seconds: int | None = None
    input_files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    network_required: bool = False
    sync_workspace: bool = False


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


class SandboxRunner(Protocol):
    def run_python(self, code: str, *, timeout_seconds: int | None = None) -> SandboxRunResult:
        ...

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        ...
