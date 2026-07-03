"""Code-capable sub-agent backed by a sandbox runner."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from ..sandbox import SandboxRunner, SandboxRunRequest, SandboxRunResult
from ..state import TaskBrief
from ..tools import ToolRegistry
from .registry import SubAgentResult, SubAgentSpec


CODE_PATH_RE = re.compile(
    r"`([^`]+)`|([\w\u4e00-\u9fff .\\/-]+\.(?:py|js|jsx|ts|tsx|json|csv|xlsx|xls|txt|md|ya?ml|html|css))",
    re.IGNORECASE,
)
CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
DATA_SUFFIXES = (".csv", ".json", ".xlsx", ".xls", ".txt", ".md")
CODE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".json", ".yml", ".yaml")
PYTHON_DEPENDENCIES = ("pandas", "numpy", "openpyxl", "matplotlib", "seaborn")


@dataclass(slots=True)
class CodeTask:
    task_type: str
    path: str = ""
    paths: list[str] = field(default_factory=list)
    user_code: str = ""
    dependencies: list[str] = field(default_factory=list)
    artifact_expected: bool = False
    language: str = "python"
    execution_mode: str = "analyze"
    run_id: str = ""


@dataclass(slots=True)
class CodeAgent:
    """Use code in a sandbox for analysis, computation, and artifacts."""

    CAPABILITY = SubAgentSpec(
        name="code_agent",
        description="通过配置的 code_sandbox 后端使用 Python 完成数据分析、图表生成、代码/项目分析、代码生成和受控脚本执行。",
        handles=["数据分析", "图表生成", "代码结构分析", "项目结构分析", "代码生成", "用户 Python 脚本受控执行"],
        does_not_handle=["未确认的 workspace 写入", "宿主机命令执行", "删除或重命名文件", "绕过 code_sandbox 后端的执行", "非 Python 运行时执行"],
        capabilities=[
            "code.execute.sandboxed",
            "data.analyze",
            "chart.generate",
            "code.analyze",
            "project.analyze",
            "code.generate",
            "script.execute",
        ],
        tools=["code_sandbox", "read_file", "search_files"],
        input_contract={"type": "TaskBrief", "fields": ["intent", "user_goal", "context", "constraints", "expected_output"]},
        output_contract={"type": "SubAgentResult", "fields": ["summary", "findings", "evidence", "artifacts", "stdout", "stderr", "tool_calls"]},
        risk_level="medium",
        examples=[
            "分析 data.csv 的数据",
            "给 02.xlsx 生成趋势图",
            "分析 agent/core/main_agent.py 的代码结构",
            "分析整个项目代码结构",
            "运行这段 Python 代码",
        ],
    )

    tools: ToolRegistry
    sandbox: SandboxRunner

    @classmethod
    def capability_spec(cls) -> SubAgentSpec:
        return cls.CAPABILITY

    def handle_brief(self, brief: TaskBrief) -> SubAgentResult:
        task = self._task_from_brief(brief)
        if task.language != "python" and task.task_type == "script_execution":
            return SubAgentResult.failure(
                "CodeAgent 第一版只支持 Python 脚本执行。",
                summary="不支持的脚本语言。",
                next_actions=["请改用 Python，或仅请求代码结构分析。"],
                confidence=0.3,
            )
        if task.task_type in {"data_analysis", "chart_generation", "code_analysis"} and not task.path:
            return SubAgentResult.failure(
                "CodeAgent 需要明确的 workspace 文件路径才能执行该任务。",
                summary="缺少文件路径。",
                next_actions=["请补充要分析的数据文件或代码文件路径。"],
                confidence=0.3,
            )
        if task.task_type == "script_execution" and not task.user_code:
            return SubAgentResult.failure(
                "CodeAgent 需要明确的 Python 代码片段才能执行脚本。",
                summary="缺少可执行代码。",
                next_actions=["请提供 fenced code block，或明确要运行的 Python 代码。"],
                confidence=0.3,
            )
        if task.task_type == "script_execution" and not self.sandbox.config.allow_user_script_execution:
            return SubAgentResult.failure(
                "用户脚本执行未启用。请在 config/llm.yaml 中设置 code_execution.allow_user_script_execution=true。",
                summary="用户脚本执行未启用。",
                next_actions=["确认风险后启用 allow_user_script_execution，或只请求代码生成/分析。"],
                confidence=0.25,
            )

        if task.task_type == "code_generation":
            return self._generate_code_only(brief, task)

        code = self._script_for_task(task, brief)
        request = SandboxRunRequest(
            code=code,
            run_id=task.run_id or None,
            timeout_seconds=self._timeout_for_task(task),
            input_files=self._input_files_for_task(task),
            dependencies=task.dependencies,
            network_required=bool(task.dependencies),
            sync_workspace=task.task_type in {"project_analysis", "script_execution"},
        )
        sandbox_result = self._run_sandbox(request)
        tool_call = self._sandbox_tool_call(task, sandbox_result)
        data = {
            "task_type": task.task_type,
            "path": task.path,
            "paths": task.paths,
            "generated_code": code,
            "dependencies": task.dependencies,
            "sandbox": sandbox_result.as_dict(),
            "artifacts": sandbox_result.artifacts,
        }
        if not sandbox_result.ok:
            return SubAgentResult.failure(
                sandbox_result.error or "CodeAgent 沙箱执行失败。",
                data=data,
                summary=sandbox_result.error or "沙箱执行失败。",
                findings=self._findings_from_sandbox(sandbox_result),
                evidence=self._evidence_from_sandbox(sandbox_result),
                next_actions=self._next_actions_for_failure(sandbox_result),
                confidence=0.2,
                tool_calls=[tool_call],
            )

        summary = self._summary_from_stdout(sandbox_result.stdout) or "CodeAgent 已完成沙箱执行。"
        return SubAgentResult.success(
            summary,
            data=data,
            summary=summary,
            findings=self._findings_from_sandbox(sandbox_result),
            evidence=self._evidence_from_sandbox(sandbox_result),
            confidence=0.78,
            tool_calls=[tool_call],
        )

    def handle_step(self, step: str, *, user_input: str = "") -> SubAgentResult:
        text = f"{step}\n{user_input}".strip()
        brief = TaskBrief(
            intent=self._intent_from_text(text),
            user_goal=text,
            normalized_input=text,
            context={
                "path": self._extract_path(text),
                "user_code": self._extract_user_code(text),
                "language": "python",
            },
            source_policy="workspace_only",
            expected_output="返回代码执行或分析摘要。",
            delegate_to="code_agent",
            confidence=0.6,
        )
        return self.handle_brief(brief)

    def _task_from_brief(self, brief: TaskBrief) -> CodeTask:
        context = brief.context or {}
        text = f"{brief.intent}\n{brief.normalized_input}\n{brief.user_goal}"
        task_type = self._task_type(brief)
        path = str(context.get("path") or self._extract_path(brief.normalized_input or brief.user_goal)).strip()
        paths = [str(item).strip() for item in context.get("paths", []) if str(item).strip()]
        if path and path not in paths:
            paths.insert(0, path)
        user_code = str(context.get("user_code") or self._extract_user_code(brief.normalized_input or brief.user_goal)).strip()
        if user_code and task_type != "code_generation":
            task_type = "script_execution"
        dependencies = self._dependencies_for_task(task_type, context)
        return CodeTask(
            task_type=task_type,
            path=path,
            paths=paths,
            user_code=user_code,
            dependencies=dependencies,
            artifact_expected=bool(context.get("artifact_expected") or task_type == "chart_generation"),
            language=str(context.get("language") or "python").lower(),
            execution_mode=str(context.get("execution_mode") or self._execution_mode_from_text(text)),
            run_id=str(context.get("run_id") or "").strip(),
        )

    def _task_type(self, brief: TaskBrief) -> str:
        intent = brief.intent or self._intent_from_text(brief.normalized_input or brief.user_goal)
        known = {
            "data_analysis",
            "chart_generation",
            "code_analysis",
            "project_analysis",
            "code_generation",
            "script_execution",
            "notebook_like_analysis",
        }
        if intent in known:
            return intent
        return self._intent_from_text(brief.normalized_input or brief.user_goal)

    def _intent_from_text(self, text: str) -> str:
        lowered = text.lower()
        if self._looks_like_script_execution(text):
            return "script_execution"
        if self._looks_like_code_generation(text):
            return "code_generation"
        if any(marker in text for marker in ("整个项目", "项目结构", "代码库", "整个 workspace", "整个workspace")):
            return "project_analysis"
        if any(marker in lowered or marker in text for marker in ("图表", "结果图", "分析结果图", "画图", "趋势图", "chart", "plot")):
            return "chart_generation"
        if any(marker in lowered or marker in text for marker in ("csv", "xlsx", "xls", "表格", "数据", "统计", "分析数据", "excel")):
            return "data_analysis"
        return "code_analysis"

    def _script_for_task(self, task: CodeTask, brief: TaskBrief) -> str:
        safe_path = task.path.replace("\\", "/").strip("/")
        if task.task_type in {"data_analysis", "notebook_like_analysis"}:
            return self._data_script(safe_path)
        if task.task_type == "chart_generation":
            return self._chart_script(safe_path)
        if task.task_type == "project_analysis":
            return self._project_analysis_script(task.paths)
        if task.task_type == "script_execution":
            return self._user_script(task.user_code)
        return self._code_analysis_script(safe_path)

    def _data_script(self, path: str) -> str:
        return f"""
from pathlib import Path
import os
import json
from collections import Counter

workspace = Path(os.environ.get('AGENT_WORKSPACE_DIR', '/workspace'))
path = workspace / {path!r}
if not path.is_file():
    raise FileNotFoundError(f'workspace file not found: {{path}}')
suffix = path.suffix.lower()
print('CodeAgent 数据分析摘要')
print(f'文件: {{path.relative_to(workspace)}}')
print(f'大小: {{path.stat().st_size}} bytes')

if suffix in {{'.csv', '.xlsx', '.xls', '.json'}}:
    import pandas as pd
    # Excel support is provided by pandas through openpyxl when available.
    if suffix == '.csv':
        df = pd.read_csv(path)
    elif suffix in {{'.xlsx', '.xls'}}:
        df = pd.read_excel(path)
    else:
        try:
            df = pd.read_json(path)
        except ValueError:
            data = json.loads(path.read_text(encoding='utf-8'))
            df = pd.json_normalize(data)
    print(f'行数: {{len(df)}}')
    print(f'列数: {{len(df.columns)}}')
    print('列: ' + ', '.join(str(item) for item in df.columns))
    missing = df.isna().sum()
    missing = missing[missing > 0]
    print('缺失值: ' + (', '.join(f'{{key}}={{value}}' for key, value in missing.items()) if len(missing) else '未发现空值'))
    numeric = df.select_dtypes(include='number')
    if not numeric.empty:
        print('数值列统计:')
        desc = numeric.describe().round(4)
        print(desc.to_string())
        if len(numeric.columns) > 1:
            print('相关性矩阵:')
            print(numeric.corr(numeric_only=True).round(4).to_string())
    categorical = df.select_dtypes(exclude='number')
    for column in list(categorical.columns)[:8]:
        values = df[column].dropna().astype(str)
        if not values.empty:
            top = values.value_counts().head(5)
            print(f'类别列 {{column}} Top5: ' + ', '.join(f'{{index}}={{value}}' for index, value in top.items()))
else:
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    words = text.split()
    print(f'行数: {{len(lines)}}')
    print(f'字符数: {{len(text)}}')
    print(f'词数: {{len(words)}}')
    print('前 5 行:')
    for line in lines[:5]:
        print(line[:200])
"""

    def _chart_script(self, path: str) -> str:
        return f"""
from pathlib import Path
import os

workspace = Path(os.environ.get('AGENT_WORKSPACE_DIR', '/workspace'))
artifacts = Path(os.environ.get('AGENT_ARTIFACTS_DIR', '/artifacts'))
path = workspace / {path!r}
if not path.is_file():
    raise FileNotFoundError(f'workspace file not found: {{path}}')
import pandas as pd
# Excel support is provided by pandas through openpyxl when available.
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

suffix = path.suffix.lower()
if suffix == '.csv':
    df = pd.read_csv(path)
elif suffix in {{'.xlsx', '.xls'}}:
    df = pd.read_excel(path)
elif suffix == '.json':
    df = pd.read_json(path)
else:
    raise RuntimeError(f'当前图表生成仅支持 csv/xlsx/xls/json，收到: {{suffix}}')
if df.empty:
    raise ValueError('没有可绘制的数据行。')
numeric = df.select_dtypes(include='number')
if numeric.empty:
    raise ValueError('表格中没有可绘制的数值列。')
datetime_columns = []
for column in df.columns:
    converted = pd.to_datetime(df[column], errors='coerce')
    if converted.notna().sum() >= max(2, len(df) // 3):
        datetime_columns.append((column, converted))
category_columns = [column for column in df.columns if column not in numeric.columns and column not in [item[0] for item in datetime_columns]]

plt.figure(figsize=(10, 5))
chart_kind = 'line'
if datetime_columns:
    x_name, x_values = datetime_columns[0]
    y_name = numeric.columns[0]
    order = x_values.argsort()
    plt.plot(x_values.iloc[order], df[y_name].iloc[order], marker='o')
    plt.xlabel(str(x_name))
    plt.ylabel(str(y_name))
    plt.title(f'{{y_name}} trend by {{x_name}}')
elif category_columns:
    category = category_columns[0]
    y_name = numeric.columns[0]
    grouped = df.groupby(category)[y_name].mean().sort_values(ascending=False).head(20)
    grouped.plot(kind='bar')
    plt.ylabel(str(y_name))
    plt.title(f'Average {{y_name}} by {{category}}')
    chart_kind = 'bar'
elif len(numeric.columns) >= 2:
    plt.scatter(df[numeric.columns[0]], df[numeric.columns[1]])
    plt.xlabel(str(numeric.columns[0]))
    plt.ylabel(str(numeric.columns[1]))
    plt.title(f'{{numeric.columns[0]}} vs {{numeric.columns[1]}}')
    chart_kind = 'scatter'
else:
    numeric[numeric.columns[0]].dropna().plot(kind='hist', bins=20)
    plt.xlabel(str(numeric.columns[0]))
    plt.title(f'Distribution of {{numeric.columns[0]}}')
    chart_kind = 'hist'
plt.tight_layout()
artifacts.mkdir(parents=True, exist_ok=True)
target = artifacts / 'chart.png'
plt.savefig(target, dpi=150)
print('CodeAgent 图表生成完成')
print(f'图表: {{target}}')
print(f'图表类型: {{chart_kind}}')
print(f'数据行数: {{len(df)}}')
"""

    def _code_analysis_script(self, path: str) -> str:
        return f"""
from pathlib import Path
import os
import ast
import json

workspace = Path(os.environ.get('AGENT_WORKSPACE_DIR', '/workspace'))
path = workspace / {path!r}
if not path.is_file():
    raise FileNotFoundError(f'workspace file not found: {{path}}')
text = path.read_text(encoding='utf-8', errors='replace')
print('CodeAgent 代码分析摘要')
print(f'文件: {{path.relative_to(workspace)}}')
print(f'行数: {{len(text.splitlines())}}')
print(f'大小: {{path.stat().st_size}} bytes')
if path.suffix.lower() == '.py':
    tree = ast.parse(text)
    classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    functions = [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or '')
    print(f'类: {{", ".join(classes[:20]) if classes else "无"}}')
    print(f'函数: {{", ".join(functions[:30]) if functions else "无"}}')
    print(f'导入: {{", ".join(imports[:30]) if imports else "无"}}')
elif path.suffix.lower() == '.json':
    data = json.loads(text)
    print(f'JSON 类型: {{type(data).__name__}}')
    if isinstance(data, dict):
        print('字段: ' + ', '.join(list(data.keys())[:30]))
else:
    keywords = ['function ', 'class ', 'const ', 'let ', 'var ', 'export ', 'import ', 'def ']
    for keyword in keywords:
        count = text.count(keyword)
        if count:
            print(f'关键词 {{keyword.strip()}}: {{count}}')
"""

    def _project_analysis_script(self, paths: list[str]) -> str:
        roots = paths or ["."]
        roots_literal = ", ".join(repr(item.replace("\\", "/").strip("/")) for item in roots)
        return f"""
from pathlib import Path
import os
from collections import Counter

workspace = Path(os.environ.get('AGENT_WORKSPACE_DIR', '/workspace'))
roots = [{roots_literal}]
allowed_suffixes = {{'.py', '.js', '.jsx', '.ts', '.tsx', '.json', '.yaml', '.yml', '.md', '.txt', '.html', '.css'}}
files = []
for item in roots:
    root = (workspace / item).resolve()
    if workspace not in root.parents and root != workspace:
        raise ValueError(f'path escaped workspace: {{item}}')
    if root.is_file():
        candidates = [root]
    else:
        candidates = [path for path in root.rglob('*') if path.is_file()]
    for path in candidates:
        if any(part in {{'.git', '__pycache__', 'node_modules', '.venv', 'venv'}} for part in path.parts):
            continue
        if path.suffix.lower() in allowed_suffixes:
            files.append(path)
print('CodeAgent 项目分析摘要')
print(f'文件数: {{len(files)}}')
suffixes = Counter(path.suffix.lower() or '<none>' for path in files)
print('文件类型: ' + ', '.join(f'{{key}}={{value}}' for key, value in suffixes.most_common(12)))
for name in ['requirements.txt', 'pyproject.toml', 'package.json', 'config/llm.yaml', 'README.md']:
    target = workspace / name
    if target.exists():
        print(f'发现关键文件: {{name}}')
print('示例文件:')
for path in files[:30]:
    print('- ' + str(path.relative_to(workspace)))
"""

    def _user_script(self, user_code: str) -> str:
        return f"""
from pathlib import Path
import os

workspace = Path(os.environ.get('AGENT_WORKSPACE_DIR', '/workspace'))
artifacts = Path(os.environ.get('AGENT_ARTIFACTS_DIR', '/artifacts'))
os.chdir(workspace)
artifacts.mkdir(parents=True, exist_ok=True)
print('CodeAgent user script started')
print(f'workspace is staged read-only; write outputs to {{artifacts}}')

{user_code}

print('CodeAgent user script finished')
"""

    def _generate_code_only(self, brief: TaskBrief, task: CodeTask) -> SubAgentResult:
        path = task.path or "input.csv"
        content = (
            "下面是一段可在 CodeAgent 沙箱中运行的 Python 示例代码。"
            "本步骤只生成代码文本，不会执行，也不会写入 workspace。\n\n"
            "```python\n"
            "from pathlib import Path\n"
            "import os\n"
            "import pandas as pd\n\n"
            "workspace = Path(os.environ.get('AGENT_WORKSPACE_DIR', '/workspace'))\n"
            f"path = workspace / {path!r}\n"
            "df = pd.read_csv(path) if path.suffix == '.csv' else pd.read_excel(path)\n"
            "print(df.head())\n"
            "print(df.describe(include='all'))\n"
            "```\n"
        )
        return SubAgentResult.success(
            content,
            data={"task_type": "code_generation", "path": path, "generated_code": content},
            summary="已生成代码文本，未执行沙箱。",
            next_actions=["如需执行，请明确要求运行这段 Python 代码。"],
            confidence=0.72,
        )

    def _extract_path(self, text: str) -> str:
        match = CODE_PATH_RE.search(text)
        if not match:
            return ""
        raw = (match.group(1) or match.group(2) or "").strip(" `，。；;：:?？!！")
        parts = [item.strip(" `，。；;：:?？!！") for item in raw.split() if item.strip()]
        return parts[-1] if parts else raw

    def _extract_user_code(self, text: str) -> str:
        match = CODE_BLOCK_RE.search(text)
        if match:
            return match.group(1).strip()
        markers = ("运行这段 Python", "执行这段 Python", "运行以下 Python", "执行下面代码", "运行下面代码")
        for marker in markers:
            if marker in text:
                return text.split(marker, 1)[1].strip(" ：:\n")
        return ""

    def _dependencies_for_task(self, task_type: str, context: dict[str, Any]) -> list[str]:
        requested = [str(item).strip() for item in context.get("dependencies", []) if str(item).strip()]
        if requested:
            return self._unique(requested)
        if task_type in {"data_analysis", "chart_generation", "notebook_like_analysis"}:
            dependencies = ["pandas", "numpy"]
            if task_type == "chart_generation":
                dependencies.extend(["matplotlib", "seaborn"])
            return self._unique([*dependencies, "openpyxl"])
        return []

    def _input_files_for_task(self, task: CodeTask) -> list[str]:
        if task.task_type in {"project_analysis", "script_execution"}:
            return task.paths
        return task.paths or ([task.path] if task.path else [])

    def _execution_mode_from_text(self, text: str) -> str:
        if self._looks_like_script_execution(text):
            return "execute"
        if "生成" in text or "写" in text:
            return "generate"
        return "analyze"

    def _looks_like_script_execution(self, text: str) -> bool:
        return any(marker in text for marker in ("运行这段", "执行这段", "运行以下", "执行以下", "运行下面", "执行下面", "用脚本处理", "运行代码"))

    def _looks_like_code_generation(self, text: str) -> bool:
        if any(marker in text for marker in ("生成代码", "写代码", "生成脚本", "写一个脚本")):
            return True
        return any(marker in text for marker in ("生成", "写")) and any(marker in text for marker in ("脚本", "代码", "程序"))

    def _timeout_for_task(self, task: CodeTask) -> int | None:
        config = getattr(self.sandbox, "config", None)
        if task.dependencies and config is not None:
            return self.sandbox.config.timeout_seconds + self.sandbox.config.install_timeout_seconds
        return None

    def _run_sandbox(self, request: SandboxRunRequest) -> SandboxRunResult:
        runner = getattr(self.sandbox, "run", None)
        if callable(runner):
            return runner(request)
        try:
            return self.sandbox.run_python(request.code, timeout_seconds=request.timeout_seconds)
        except TypeError:
            return self.sandbox.run_python(request.code)

    def _unique(self, items: list[str]) -> list[str]:
        result: list[str] = []
        for item in items:
            if item and item not in result:
                result.append(item)
        return result

    def _summary_from_stdout(self, stdout: str) -> str:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        return "\n".join(lines[:16])

    def _findings_from_sandbox(self, result: SandboxRunResult) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if result.stdout:
            findings.append({"title": "stdout", "summary": result.stdout[:1200]})
        if result.stderr:
            findings.append({"title": "stderr", "summary": result.stderr[:1200]})
        if result.artifacts:
            findings.append({"title": "artifacts", "summary": json.dumps(result.artifacts, ensure_ascii=False)})
        return findings

    def _evidence_from_sandbox(self, result: SandboxRunResult) -> list[dict[str, Any]]:
        return [
            {"title": "sandbox", "run_id": result.run_id, "exit_code": result.exit_code, "duration_ms": result.duration_ms},
            *[{"title": item.get("filename", "artifact"), **item} for item in result.artifacts],
        ]

    def _next_actions_for_failure(self, result: SandboxRunResult) -> list[str]:
        error = result.error or ""
        if "Local process" in error:
            return ["local_process is not a strong sandbox. Check the configured Python executable, approval decision, and runtime/local_runs details."]
        if "Dependency installation is disabled" in error:
            return ["在 config/llm.yaml 中启用 code_execution.dependency_install.enabled，或换用已包含依赖的执行环境。"]
        if "Dependency is not allowed" in error:
            return ["把需要的包加入 code_execution.dependency_install.allowed_packages，确认风险后再执行。"]
        if "OpenSandbox" in error:
            return ["确认 OpenSandbox Server 已启动，并检查 code_execution.opensandbox 的 domain/protocol/api_key 配置。"]
        return ["查看调试信息中的 stdout/stderr，修正输入文件、依赖或脚本后重试。"]

    def _sandbox_tool_call(self, task: CodeTask, result: SandboxRunResult) -> dict[str, Any]:
        config = getattr(self.sandbox, "config", None)
        backend = result.backend or getattr(config, "backend", "opensandbox")
        return {
            "tool": "code_sandbox",
            "payload": {
                "backend": backend,
                "isolation": result.isolation,
                "warnings": result.warnings,
                "task_type": task.task_type,
                "path": task.path,
                "paths": task.paths,
                "run_id": result.run_id,
                "dependencies": task.dependencies,
                "network": bool(task.dependencies),
                "workspace": "readonly",
            },
            "ok": result.ok,
            "error": result.error,
        }
