from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.core.sandbox import DockerSandboxRunner, SandboxRunRequest
from agent.core.utils.llm_config import CodeExecutionConfig


class DockerSandboxRunnerTests(unittest.TestCase):
    def make_runner(self, directory: str, *, enabled: bool = True) -> DockerSandboxRunner:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        return DockerSandboxRunner(
            CodeExecutionConfig(
                enabled=enabled,
                artifacts_dir=str(root / "artifacts"),
                timeout_seconds=3,
                max_output_chars=20,
            ),
            project_root=root,
            workspace_root=workspace,
        )

    def test_disabled_sandbox_does_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(directory, enabled=False)

            result = runner.run_python("print('hi')")

        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error)
        self.assertEqual(result.command, [])

    def test_docker_unavailable_does_not_fallback_to_host_python(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(directory)

            with patch("agent.core.sandbox.docker_runner.subprocess.run", side_effect=FileNotFoundError):
                result = runner.run_python("print('hi')")

        self.assertFalse(result.ok)
        self.assertIn("Docker is not available", result.error)
        self.assertTrue(result.command)

    def test_success_collects_artifacts_and_truncates_output(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(directory)

            def fake_run(command, **kwargs):
                run_mount = command[command.index("-v") + 3]
                run_dir = Path(run_mount.split(":/artifacts", 1)[0])
                (run_dir / "chart.png").write_bytes(b"png")
                return subprocess.CompletedProcess(command, 0, stdout="x" * 30, stderr="")

            with patch("agent.core.sandbox.docker_runner.subprocess.run", side_effect=fake_run):
                result = runner.run(SandboxRunRequest(code="print('hi')"))

        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "x" * 20 + "...")
        self.assertEqual(result.artifacts[0]["filename"], "chart.png")
        self.assertIn("--network", result.command)
        self.assertIn("none", result.command)
        self.assertIn("--read-only", result.command)
        self.assertNotIn("/artifacts/task.py", " ".join(result.command))
        self.assertFalse(any(Path(item["path"]).name == "task.py" for item in result.artifacts))

    def test_dependency_install_requires_allowlist_and_enables_network(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DockerSandboxRunner(
                CodeExecutionConfig(
                    enabled=True,
                    dependency_install_enabled=True,
                    allowed_packages=["pandas"],
                    artifacts_dir=str(Path(directory) / "artifacts"),
                ),
                project_root=Path(directory),
                workspace_root=Path(directory) / "workspace",
            )
            runner.workspace_root.mkdir()

            def fake_run(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            with patch("agent.core.sandbox.docker_runner.subprocess.run", side_effect=fake_run):
                result = runner.run(SandboxRunRequest(code="print('ok')", dependencies=["pandas"]))

        self.assertTrue(result.ok)
        command_text = " ".join(result.command)
        self.assertIn("bridge", result.command)
        self.assertIn("pip install", command_text)
        self.assertIn("--target /deps", command_text)
        self.assertIn("PYTHONPATH=/deps", command_text)
        self.assertIn("runtime\\sandbox_deps", command_text)
        self.assertEqual(result.dependencies, ["pandas"])

    def test_dependency_install_rejects_unallowed_package(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(directory)
            runner.config.dependency_install_enabled = True
            runner.config.allowed_packages = ["pandas"]

            result = runner.run(SandboxRunRequest(code="print('ok')", dependencies=["requests"]))

        self.assertFalse(result.ok)
        self.assertIn("not allowed", result.error)
        self.assertEqual(result.command, [])


if __name__ == "__main__":
    unittest.main()
