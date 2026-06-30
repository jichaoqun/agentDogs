from __future__ import annotations

import os
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

from agent.core.sandbox import OpenSandboxRunner, SandboxRunRequest
from agent.core.utils.llm_config import CodeExecutionConfig


class FakeFiles:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def write_file(self, path, data, **kwargs):
        self.store[str(path)] = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def read_bytes(self, path):
        return self.store[str(path)]

    def list_directory(self, entry):
        return [
            SimpleNamespace(path=path, size=len(data))
            for path, data in self.store.items()
            if path.startswith("/artifacts/")
        ]


class FakeCommands:
    def __init__(self, files: FakeFiles, *, fail: Exception | None = None) -> None:
        self.files = files
        self.fail = fail
        self.commands: list[tuple[str, object | None]] = []

    def run(self, command, *, opts=None):
        self.commands.append((command, opts))
        if command.startswith("mkdir"):
            return self._execution(0, "")
        if self.fail is not None:
            raise self.fail
        self.files.store["/artifacts/chart.png"] = b"png"
        return self._execution(0, "x" * 30)

    def _execution(self, exit_code: int, stdout: str):
        return SimpleNamespace(
            exit_code=exit_code,
            error=None,
            result=[],
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=stdout)] if stdout else [],
                stderr=[],
            ),
        )


class FakeSandbox:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.files = FakeFiles()
        self.commands = FakeCommands(self.files, fail=fail)
        self.killed = False
        self.closed = False

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True


class OpenSandboxRunnerTests(unittest.TestCase):
    def make_runner(
        self,
        directory: str,
        *,
        enabled: bool = True,
        fake: FakeSandbox | None = None,
    ) -> OpenSandboxRunner:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        fake = fake or FakeSandbox()
        return OpenSandboxRunner(
            CodeExecutionConfig(
                enabled=enabled,
                artifacts_dir=str(root / "artifacts"),
                timeout_seconds=3,
                max_output_chars=20,
            ),
            project_root=root,
            workspace_root=workspace,
            sandbox_factory=lambda **kwargs: fake,
        )

    def test_disabled_sandbox_does_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(directory, enabled=False)

            result = runner.run_python("print('hi')")

        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error)
        self.assertEqual(result.command, [])

    def test_opensandbox_unavailable_does_not_fallback_to_host_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runner = OpenSandboxRunner(
                CodeExecutionConfig(enabled=True, artifacts_dir=str(root / "artifacts")),
                project_root=root,
                workspace_root=workspace,
                sandbox_factory=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("service down")),
            )

            result = runner.run_python("print('hi')")

        self.assertFalse(result.ok)
        self.assertIn("OpenSandbox", result.error)
        self.assertTrue(result.command)

    def test_success_stages_input_collects_artifacts_and_truncates_output(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeSandbox()
            runner = self.make_runner(directory, fake=fake)
            workspace_file = Path(directory) / "workspace" / "data.csv"
            workspace_file.write_bytes(b"a,b\n1,2\n")

            result = runner.run(SandboxRunRequest(code="print('hi')", input_files=["data.csv"]))

        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "x" * 20 + "...")
        self.assertEqual(result.artifacts[0]["filename"], "chart.png")
        self.assertEqual(fake.files.store["/workspace/data.csv"], b"a,b\n1,2\n")
        self.assertIn("opensandbox", result.command[0])
        self.assertTrue(fake.killed)
        self.assertTrue(fake.closed)

    def test_dependency_install_requires_allowlist_and_enables_network(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeSandbox()
            runner = OpenSandboxRunner(
                CodeExecutionConfig(
                    enabled=True,
                    dependency_install_enabled=True,
                    allowed_packages=["pandas"],
                    artifacts_dir=str(Path(directory) / "artifacts"),
                ),
                project_root=Path(directory),
                workspace_root=Path(directory) / "workspace",
                sandbox_factory=lambda **kwargs: fake,
            )
            runner.workspace_root.mkdir()

            result = runner.run(SandboxRunRequest(code="print('ok')", dependencies=["pandas"]))

        self.assertTrue(result.ok)
        command_text = result.command[-1]
        self.assertIn("pip install", command_text)
        self.assertIn("--target /deps", command_text)
        self.assertIn("PYTHONPATH=/deps", command_text)
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

    def test_workspace_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(directory)

            result = runner.run(SandboxRunRequest(code="print('ok')", input_files=["../secret.txt"]))

        self.assertFalse(result.ok)
        self.assertIn("escaped workspace", result.error)

    def test_timeout_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeSandbox(fail=TimeoutError())
            runner = self.make_runner(directory, fake=fake)

            result = runner.run_python("while True: pass")

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

    @unittest.skipUnless(os.getenv("RUN_OPENSANDBOX_INTEGRATION") == "1", "OpenSandbox integration test is opt-in")
    def test_real_opensandbox_executes_python_and_collects_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "input.txt").write_text("hello", encoding="utf-8")
            runner = OpenSandboxRunner(
                CodeExecutionConfig(
                    enabled=True,
                    artifacts_dir=str(root / "artifacts"),
                    timeout_seconds=10,
                    max_output_chars=2000,
                ),
                project_root=root,
                workspace_root=workspace,
            )

            result = runner.run(
                SandboxRunRequest(
                    code="from pathlib import Path\nprint(Path('/workspace/input.txt').read_text())\nPath('/artifacts/out.txt').write_text('done')",
                    input_files=["input.txt"],
                )
            )

        self.assertTrue(result.ok, result.stderr or result.error)
        self.assertIn("hello", result.stdout)
        self.assertEqual(result.artifacts[0]["filename"], "out.txt")


if __name__ == "__main__":
    unittest.main()
