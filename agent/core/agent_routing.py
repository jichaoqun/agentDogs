"""Task routing, TaskBrief construction, and LLM planning helpers for MainAgent."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .state import AgentState, ClarificationQuestion, TaskAnalysis, TaskBrief, TaskPlan
from .utils.llm_models import GenerationOptions, ModelSelection
from .utils.prompt import (
    TASK_ANALYSIS_SYSTEM_PROMPT,
    TASK_PLAN_SYSTEM_PROMPT,
    build_task_analysis_prompt,
    build_task_plan_prompt,
)

COMPLEX_MARKERS = (
    "整个项目",
    "项目分析",
    "代码库",
    "修改代码",
    "改代码",
    "仓库",
    "实现",
    "修复",
    "优化",
    "重构",
    "新增",
    "删除",
    "重命名",
    "上传",
    "下载",
    "测试",
    "前端",
    "后端",
    "接口",
    "数据库",
    "文件管理",
    "计划",
    "方案",
    "报告",
    "分阶段",
    "自动执行",
)
MISSING_TARGET_REFS = (
    "这个文件",
    "那个文件",
    "这个文档",
    "那个文档",
    "这个目录",
    "那个目录",
    "这个表格",
    "那个表格",
)
MISSING_TARGET_ACTIONS = (
    "处理",
    "修改",
    "分析",
    "整理",
    "删除",
    "重命名",
    "上传",
    "下载",
    "转换",
    "总结",
    "修复",
)
PATH_HINT = re.compile(
    r"(`[^`]+`|[\w\u4e00-\u9fff .\\/-]+\.(?:md|txt|py|js|jsx|ts|tsx|json|ya?ml|html|css|csv|xlsx|xls|docx|pdf))",
    re.IGNORECASE,
)
SIMPLE_FILE_LIST_MARKERS = (
    "有哪些文件",
    "文件列表",
    "列出文件",
    "列一下文件",
    "查看目录",
    "目录列表",
    "文件树",
    "当前工作目录",
    "当前目录",
)
SIMPLE_FILE_SCOPE_MARKERS = ("当前项目", "当前工作目录", "当前目录", "workspace")
SIMPLE_FILE_READ_MARKERS = ("读取", "查看", "打开", "预览", "内容", "看一下")
SIMPLE_FILE_SEARCH_MARKERS = ("搜索", "查找", "检索", "找一下", "包含")
SIMPLE_FILE_INFO_MARKERS = ("信息", "属性", "大小", "类型", "元信息")
WEB_SEARCH_MARKERS = ("联网", "网上", "网络", "互联网", "web", "Web")
RESEARCH_MARKERS = ("调研", "研究")
RESEARCH_COMPLEX_MARKERS = ("整理", "对比", "报告", "分析", "方案", "总结")
HIGH_RISK_TOOL_MARKERS = ("写入", "保存", "保存到", "存放", "修改", "改写", "删除", "重命名", "创建", "新建", "新增", "覆盖", "移动", "上传", "下载")
REALTIME_MARKERS = ("今天", "现在", "实时", "最新", "今年", "新闻", "天气", "比赛", "赛程", "价格", "预报")
WEATHER_MARKERS = ("天气", "气温", "降雨", "下雨", "预报", "空气质量")
COMMON_LOCATION_MARKERS = ("北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "重庆", "天津", "武汉", "西安")
CODE_TASK_MARKERS = (
    "Python", "python", "代码", "脚本", "函数", "类", "项目结构", "分析代码",
    "数据", "表格", "csv", "CSV", "json", "JSON", "统计", "图表", "画图", "趋势图",
)
CODE_ANALYSIS_MARKERS = ("代码", "函数", "类", "项目结构", "分析代码")
DATA_ANALYSIS_MARKERS = ("数据", "表格", "csv", "CSV", "json", "JSON", "xlsx", "XLSX", "xls", "XLS", "excel", "Excel", "统计")
CHART_MARKERS = ("图表", "结果图", "分析结果图", "画图", "趋势图", "chart", "plot")
SCRIPT_EXECUTION_MARKERS = ("运行这段", "执行这段", "运行以下", "执行以下", "运行下面", "执行下面", "用脚本处理", "运行代码")
CODE_GENERATION_MARKERS = ("生成代码", "写代码", "生成脚本", "写一个脚本")
PROJECT_ANALYSIS_MARKERS = ("项目结构", "代码结构", "代码库", "整个 workspace", "整个workspace")
SEARCH_TYPO_MAP = {
    "搜素": "搜索",
    "查讯": "查询",
}




class AgentRoutingMixin:
    """Task understanding and routing helpers used by MainAgent."""

    def _build_task_brief(self, text: str, analysis: TaskAnalysis, state: AgentState) -> TaskBrief:
        normalized = self._normalize_user_input(text)
        context = self._brief_context(normalized, state)
        delegate_to = self._delegate_for_analysis(analysis, normalized, context)
        source_policy = "requires_fresh_external_info" if self._requires_fresh_external_info(normalized) else "not_required"
        if context.get("source_scope") == "workspace":
            source_policy = "workspace_only"
        return TaskBrief(
            intent=self._brief_intent(normalized, analysis),
            user_goal=text.strip(),
            normalized_input=normalized,
            context=context,
            constraints=self._brief_constraints(normalized, source_policy),
            source_policy=source_policy,
            expected_output=self._brief_expected_output(normalized, analysis),
            delegate_to=delegate_to,
            confidence=analysis.confidence,
        )

    def _normalize_user_input(self, text: str) -> str:
        normalized = text.strip()
        for source, target in SEARCH_TYPO_MAP.items():
            normalized = normalized.replace(source, target)
        return re.sub(r"\s+", " ", normalized)

    def _brief_context(self, text: str, state: AgentState) -> dict[str, Any]:
        context: dict[str, Any] = {}
        current_date = str(state.get("current_time") or "").split("T", 1)[0]
        if "今天" in text and current_date:
            context["relative_time"] = "今天"
            context["date"] = current_date
        if "今年" in text and current_date:
            context["relative_time"] = "今年"
            context["year"] = current_date[:4]
        location = self._extract_location(text)
        if location:
            context["location"] = location
        path = self._extract_path_for_brief(text)
        if path:
            context["path"] = path
        if self._is_code_task(text):
            context["source_scope"] = "workspace"
            context["language"] = "python"
            context["execution_mode"] = self._code_execution_mode(text)
            user_code = self._extract_code_block(text)
            if user_code:
                context["user_code"] = user_code
            if any(marker in text for marker in CHART_MARKERS):
                context["artifact_expected"] = True
        if self._requires_fresh_external_info(text):
            context["source_scope"] = "web"
        elif any(marker in text for marker in SIMPLE_FILE_SCOPE_MARKERS) or "文件" in text or "项目" in text:
            context["source_scope"] = "workspace"
        if self._is_weather_request(text):
            context["domain"] = "weather"
            pieces = [location, context.get("date"), "天气 预报"]
            context["query"] = " ".join(str(item) for item in pieces if item)
        elif self._requires_fresh_external_info(text):
            context["query"] = text
        return {key: value for key, value in context.items() if value not in (None, "", [], {})}

    def _delegate_for_analysis(self, analysis: TaskAnalysis, text: str, context: dict[str, Any]) -> str:
        if self._is_code_task(text):
            return "code_agent"
        if analysis.route_hint == "simple_chat":
            return "simple_chat"
        if analysis.route_hint in {"clarify", "future_task"}:
            return analysis.route_hint
        if self._is_search_delegate(text, context):
            return "search_agent"
        if "list_workspace_tree" in analysis.tool_intents:
            return "simple_task"
        if context.get("path") or analysis.tool_intents and any(intent in analysis.tool_intents for intent in ("read_file", "file_info", "list_workspace_tree")):
            return "file_agent"
        if "workspace_search" in analysis.tool_intents:
            return "search_agent"
        return "simple_task"

    def _brief_intent(self, text: str, analysis: TaskAnalysis) -> str:
        if self._is_code_task(text):
            if any(marker in text for marker in SCRIPT_EXECUTION_MARKERS):
                return "script_execution"
            if self._looks_like_code_generation(text):
                return "code_generation"
            if any(marker in text for marker in PROJECT_ANALYSIS_MARKERS):
                return "project_analysis"
            if any(marker in text for marker in CHART_MARKERS):
                return "chart_generation"
            if any(marker in text for marker in DATA_ANALYSIS_MARKERS):
                return "data_analysis"
            if any(marker in text for marker in ("生成代码", "写代码", "脚本")):
                return "code_generation"
            return "code_analysis"
        if self._is_weather_request(text):
            return "weather_lookup"
        if self._is_search_delegate(text, {}):
            return "search"
        return analysis.task_kind or "chat"

    def _brief_constraints(self, text: str, source_policy: str) -> list[str]:
        constraints: list[str] = []
        if source_policy == "requires_fresh_external_info":
            constraints.append("需要使用新鲜外部信息，不能只依赖模型历史知识。")
        if self._is_code_task(text):
            constraints.extend([
                "必须通过配置的 code_sandbox 后端执行，不能自动回退到宿主机 Python。",
                "workspace 只读，输出只能写入 artifacts。",
                "默认不允许联网或安装依赖。",
            ])
        if any(marker in text for marker in HIGH_RISK_TOOL_MARKERS):
            constraints.append("涉及高风险操作时必须先请求用户确认。")
        return constraints

    def _brief_expected_output(self, text: str, analysis: TaskAnalysis) -> str:
        if self._is_code_task(text):
            if any(marker in text for marker in SCRIPT_EXECUTION_MARKERS):
                return "执行用户提供的 Python 脚本，返回 stdout/stderr、artifacts 和沙箱信息。"
            if self._looks_like_code_generation(text):
                return "只返回代码文本，不执行、不写入 workspace。"
            if any(marker in text for marker in PROJECT_ANALYSIS_MARKERS):
                return "返回项目结构、关键文件、技术栈和代码组织摘要。"
            if any(marker in text for marker in CHART_MARKERS):
                return "返回图表文件、关键摘要和沙箱执行信息。"
            if any(marker in text for marker in DATA_ANALYSIS_MARKERS):
                return "返回数据摘要、关键发现和必要的 artifacts。"
            return "返回代码结构分析、关键发现和证据。"
        if self._is_weather_request(text):
            return "给出简洁天气结论，并说明来源或无法确认的原因。"
        if self._is_search_delegate(text, {}):
            return "返回精炼搜索结论、关键发现和证据来源。"
        if analysis.route_hint == "simple_chat":
            return "直接回答用户问题。"
        return "返回任务执行摘要、关键结果和必要的下一步。"

    def _extract_location(self, text: str) -> str:
        for location in COMMON_LOCATION_MARKERS:
            if location in text:
                return location
        return ""

    def _extract_path_for_brief(self, text: str) -> str:
        match = PATH_HINT.search(text)
        if not match:
            return ""
        raw = match.group(1).strip("`") if match.group(1) else match.group(0).strip("`")
        parts = [item.strip(" `，。；;：:?？!！") for item in raw.split() if item.strip()]
        candidate = parts[-1] if parts else raw
        for prefix in ("请帮我查看", "请帮我读取", "帮我查看", "帮我读取", "请查看", "请读取", "查看", "读取", "打开", "预览", "帮我", "请"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):].strip(" `，。；;：:?？!！")
        for suffix in ("中的内容是什么", "里面的内容是什么", "的内容是什么", "中的内容", "里面的内容", "的内容", "内容是什么"):
            if candidate.endswith(suffix):
                candidate = candidate[:-len(suffix)].strip(" `，。；;：:?？!！")
        return candidate

    def _requires_fresh_external_info(self, text: str) -> bool:
        return any(marker in text for marker in REALTIME_MARKERS)

    def _is_weather_request(self, text: str) -> bool:
        return any(marker in text for marker in WEATHER_MARKERS)

    def _is_code_task(self, text: str) -> bool:
        path_match = PATH_HINT.search(text)
        has_path = path_match is not None
        explicit = any(marker in text for marker in ("Python", "python", "代码", "脚本", "函数", "项目结构", "分析代码", "图表", "结果图", "画图", "趋势图"))
        explicit = explicit or any(marker in text for marker in SCRIPT_EXECUTION_MARKERS + PROJECT_ANALYSIS_MARKERS)
        explicit = explicit or self._looks_like_code_generation(text)
        data_action = any(marker in text for marker in ("分析", "统计", "生成", "处理", "转换"))
        data_target = any(marker in text for marker in DATA_ANALYSIS_MARKERS)
        path = path_match.group(1).strip("`") if path_match else ""
        spreadsheet_read = path.lower().endswith((".xlsx", ".xls")) and any(marker in text for marker in SIMPLE_FILE_READ_MARKERS)
        return explicit or (has_path and data_target and (data_action or spreadsheet_read))

    def _code_execution_mode(self, text: str) -> str:
        if any(marker in text for marker in SCRIPT_EXECUTION_MARKERS):
            return "execute"
        if self._looks_like_code_generation(text):
            return "generate"
        return "analyze"

    def _extract_code_block(self, text: str) -> str:
        match = re.search(r"```(?:python|py)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else ""

    def _looks_like_code_generation(self, text: str) -> bool:
        if any(marker in text for marker in CODE_GENERATION_MARKERS):
            return True
        return any(marker in text for marker in ("生成", "写")) and any(marker in text for marker in ("脚本", "代码", "程序"))

    def _is_code_workspace_write_task(self, text: str) -> bool:
        if not self._is_code_task(text):
            return False
        write_intent = any(marker in text for marker in ("写入", "保存", "保存到", "存放", "创建", "新建", "输出到", "生成到"))
        artifact_intent = any(marker in text for marker in CHART_MARKERS)
        return write_intent and (artifact_intent or PATH_HINT.search(text) is not None)

    def _is_file_execution_request(self, text: str) -> bool:
        if not PATH_HINT.search(text):
            return False
        execution_markers = (
            "读取", "查看", "打开", "预览", "分析", "统计", "处理", "转换",
            "生成", "图表", "结果图", "保存", "保存到", "存放", "创建", "新建",
        )
        return any(marker in text for marker in execution_markers)

    def _is_search_delegate(self, text: str, context: dict[str, Any]) -> bool:
        if context.get("source_scope") == "web":
            return True
        return self._requires_fresh_external_info(text) or any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS + ("查询", "查一下", "搜一下", "搜索"))

    def _rule_analysis(self, text: str) -> TaskAnalysis | None:
        text = self._normalize_user_input(text)
        if self._is_weather_request(text) and not self._extract_location(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="needs_info",
                task_kind="tool",
                route_hint="clarify",
                risk_level="low",
                requires_confirmation=False,
                confidence=0.82,
                reason="天气查询需要明确地点，才能委派搜索 Agent 获取实时信息。",
                missing_info=["天气查询地点。"],
                clarification_questions=[
                    ClarificationQuestion(
                        id="location",
                        question="你想查询哪个城市或地区的天气？",
                        options=["北京", "上海", "广州", "深圳"],
                    )
                ],
            )
        if self._is_code_workspace_write_task(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="complex",
                task_kind="task",
                route_hint="future_task",
                tool_intents=["code_agent"],
                estimated_steps=4,
                risk_level="high",
                requires_confirmation=True,
                confidence=0.88,
                reason="任务包含数据/代码执行以及保存、新建或写入产物要求，需要先确认计划，避免未经确认写入 workspace。",
                suggested_steps=[
                    "确认输入表格路径、分析目标和预期图表类型。",
                    "通过配置的 code_sandbox 后端读取表格并生成数据摘要。",
                    "在 artifacts 中生成分析图表，并返回可下载路径。",
                    "如需写入 workspace 指定目录，先请求用户确认输出位置和覆盖策略。",
                ],
            )
        if self._is_code_task(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="simple",
                task_kind="tool",
                route_hint="simple_task",
                tool_intents=["code_agent"],
                estimated_steps=1,
                risk_level="medium",
                requires_confirmation=False,
                confidence=0.84,
                reason="这是可由 CodeAgent 通过配置的 code_sandbox 后端处理的代码、数据或图表任务。",
            )
        if self._has_missing_target(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="needs_info",
                task_kind="unknown",
                route_hint="clarify",
                risk_level="medium",
                requires_confirmation=True,
                confidence=0.82,
                reason="用户提到了待处理对象，但没有给出明确路径或范围。",
                missing_info=[
                    "要处理的具体文件、目录或对象路径。",
                    "希望执行的具体动作。",
                    "期望的输出形式或完成标准。",
                ],
                clarification_questions=[
                    ClarificationQuestion(
                        id="target",
                        question="要处理的具体文件、目录或对象是什么？",
                        options=["当前选中文件", "workspace 中的某个文件", "整个 workspace"],
                    ),
                    ClarificationQuestion(
                        id="action",
                        question="希望执行什么具体动作？",
                        options=["分析并总结", "修改内容", "转换格式", "检查问题"],
                    ),
                    ClarificationQuestion(
                        id="output",
                        question="期望输出是什么形式？",
                        options=["直接回复结论", "生成 Markdown 文档", "修改原文件", "创建新文件"],
                    ),
                ],
            )
        if self._looks_high_risk_tool_task(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="complex",
                task_kind="tool",
                route_hint="future_task",
                tool_intents=[],
                estimated_steps=3,
                risk_level="high",
                requires_confirmation=True,
                confidence=0.84,
                reason="任务涉及写入、删除、重命名、上传下载等高风险工具操作，需要计划确认。",
                suggested_steps=[
                    "确认操作目标、范围和预期结果。",
                    "读取相关文件或目录现状，生成拟执行方案。",
                    "在真正写入、删除或移动前请求用户确认。",
                ],
            )
        if self._is_complex_research_task(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="complex",
                task_kind="task",
                route_hint="future_task",
                tool_intents=["workspace_search", "web_search"],
                estimated_steps=4,
                risk_level="medium",
                requires_confirmation=True,
                confidence=0.8,
                reason="任务涉及调研、整理或对比，通常需要多步骤搜索和汇总，应先确认计划。",
                suggested_steps=[
                    "确认调研目标、范围和输出格式。",
                    "选择 workspace 搜索或联网搜索来源。",
                    "汇总搜索结果并按主题整理。",
                    "输出对比结论、来源和未覆盖风险。",
                ],
            )
        tool_intents = self._simple_tool_intents(text)
        if tool_intents:
            return TaskAnalysis(
                intent=text[:80],
                complexity="simple",
                task_kind="tool",
                route_hint="simple_task",
                tool_intents=tool_intents,
                estimated_steps=len(tool_intents),
                risk_level="low",
                requires_confirmation=False,
                confidence=0.86,
                reason="这是目标明确、低风险、可由工具直接完成的简单任务。",
            )
        if self._is_explicit_project_report(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="complex",
                task_kind="task",
                route_hint="future_task",
                estimated_steps=5,
                risk_level="medium",
                requires_confirmation=True,
                confidence=0.78,
                reason="任务涉及项目、代码、文件或多步骤执行，需要任务型 Agent 承接。",
                suggested_steps=self._default_plan_steps(text),
            )
        if self._looks_complex(text):
            return None
        if self._looks_simple(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="simple",
                task_kind="chat",
                route_hint="simple_chat",
                confidence=0.75,
                reason="普通问答、解释、闲聊或简单文本生成。",
            )
        return None

    def _llm_analysis(
        self,
        text: str,
        selection: ModelSelection | None,
        current_time: str = "",
    ) -> TaskAnalysis:
        if self.models is None:
            raise RuntimeError("Model manager is not initialized")
        prompt = build_task_analysis_prompt(text, current_time)
        result = self.models.chat(
            [
                SystemMessage(content=TASK_ANALYSIS_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ],
            selection=selection,
            options=GenerationOptions(temperature=0, max_tokens=512),
        )
        data = _extract_json(result.content)
        return TaskAnalysis(**data)

    def _llm_plan(self, state: AgentState) -> TaskPlan:
        if self.models is None:
            raise RuntimeError("Model manager is not initialized")
        prompt = build_task_plan_prompt(
            user_input=state["user_input"],
            clarification_answers=state.get("clarification_answers") or {},
            plan_feedback=state.get("plan_feedback") or "",
            current_time=state.get("current_time_context", ""),
        )
        result = self.models.chat(
            [
                SystemMessage(content=TASK_PLAN_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ],
            selection=state.get("selection"),
            options=GenerationOptions(temperature=0.2, max_tokens=900),
        )
        data = _extract_json(result.content)
        plan = TaskPlan(**data)
        if not plan.steps:
            plan.steps = self._default_plan_steps(state["user_input"])
        if not plan.summary:
            plan.summary = f"完成用户提出的任务：{state['user_input'][:80]}"
        return plan

    def _has_missing_target(self, text: str) -> bool:
        if PATH_HINT.search(text):
            return False
        return (
            any(ref in text for ref in MISSING_TARGET_REFS)
            and any(action in text for action in MISSING_TARGET_ACTIONS)
        )

    def _simple_tool_intents(self, text: str) -> list[str]:
        text = self._normalize_user_input(text)
        intents: list[str] = []
        has_path = PATH_HINT.search(text) is not None
        if self._is_file_list_request(text):
            intents.append("list_workspace_tree")
        if has_path and any(marker in text for marker in SIMPLE_FILE_INFO_MARKERS):
            intents.append("file_info")
        elif has_path and any(marker in text for marker in SIMPLE_FILE_READ_MARKERS):
            intents.append("read_file")
        if self._is_web_search_request(text):
            intents.append("web_search")
        if self._is_file_search_request(text):
            intents.append("workspace_search")
        return intents

    def _is_file_list_request(self, text: str) -> bool:
        if any(marker in text for marker in SIMPLE_FILE_LIST_MARKERS):
            return True
        has_scope = any(marker in text for marker in SIMPLE_FILE_SCOPE_MARKERS)
        return has_scope and "文件" in text and any(marker in text for marker in ("哪些", "有什么", "列表", "列出"))

    def _is_file_search_request(self, text: str) -> bool:
        if self._is_web_search_request(text):
            return False
        if not any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS):
            return False
        cleaned = text
        for marker in SIMPLE_FILE_SEARCH_MARKERS + ("文件", "内容", "workspace", "中", "的", "一下", "包含"):
            cleaned = cleaned.replace(marker, " ")
        return bool(cleaned.strip(" ，,。；;：:"))

    def _is_web_search_request(self, text: str) -> bool:
        text = self._normalize_user_input(text)
        if self._requires_fresh_external_info(text) and any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS + ("查询", "查一下", "搜一下", "搜索", "怎么样")):
            return True
        if not any(marker in text for marker in WEB_SEARCH_MARKERS):
            return False
        return any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS + ("查询", "查一下", "搜一下", "搜索"))

    def _is_complex_research_task(self, text: str) -> bool:
        return (
            any(marker in text for marker in RESEARCH_MARKERS)
            and any(marker in text for marker in RESEARCH_COMPLEX_MARKERS)
        )

    def _looks_high_risk_tool_task(self, text: str) -> bool:
        if not any(marker in text for marker in HIGH_RISK_TOOL_MARKERS):
            return False
        return bool(
            PATH_HINT.search(text)
            or any(marker in text for marker in SIMPLE_FILE_SCOPE_MARKERS)
            or any(marker in text for marker in ("文件", "文档", "目录", "workspace"))
        )

    def _looks_complex(self, text: str) -> bool:
        lower = text.lower()
        return any(marker in lower or marker in text for marker in COMPLEX_MARKERS)

    def _is_explicit_project_report(self, text: str) -> bool:
        return (
            any(marker in text for marker in ("整个项目", "当前项目", "本项目", "项目分析"))
            and any(marker in text for marker in ("报告", "分析", "整理"))
        )

    def _looks_simple(self, text: str) -> bool:
        normalized = text.strip().lower()
        if self._is_file_execution_request(text):
            return False
        if normalized in {"hi", "hello", "你好", "在吗", "早上好", "晚上好"}:
            return True
        simple_prefixes = (
            "解释",
            "说明",
            "什么是",
            "请问",
            "帮我写",
            "改写",
            "翻译",
            "总结这段",
        )
        if text.startswith(simple_prefixes):
            return True
        return len(text) <= 160

    def _default_plan_steps(self, text: str) -> list[str]:
        return [
            "复述并确认任务目标、输入范围和交付结果。",
            "拆解任务步骤，标记需要文件、工具或外部信息的位置。",
            "执行前请求用户确认关键操作，尤其是写文件、删除和长时间运行命令。",
            "分步骤执行并记录中间结果、错误和下一步。",
            "汇总最终结果，说明修改内容、验证方式和未完成风险。",
        ]

    def _clarification_questions(self, analysis: TaskAnalysis) -> list[ClarificationQuestion]:
        if analysis.complexity != "needs_info":
            return []
        if analysis.clarification_questions:
            return [
                self._normalize_question(index, item)
                for index, item in enumerate(analysis.clarification_questions, start=1)
            ]
        missing = analysis.missing_info or ["请补充目标对象、处理范围和期望输出。"]
        return [
            ClarificationQuestion(
                id=f"q{index}",
                question=item,
                options=[],
                allow_custom=True,
                required=True,
            )
            for index, item in enumerate(missing, start=1)
        ]

    def _normalize_question(
        self,
        index: int,
        question: ClarificationQuestion,
    ) -> ClarificationQuestion:
        text = question.question.strip() or f"请补充第 {index} 项信息。"
        options = [item.strip() for item in question.options if item.strip()][:4]
        return ClarificationQuestion(
            id=question.id.strip() or f"q{index}",
            question=text,
            options=options,
            allow_custom=question.allow_custom,
            required=question.required,
        )

    def _plan_from_state(self, state: AgentState) -> TaskPlan:
        existing = state.get("task_plan")
        if isinstance(existing, TaskPlan):
            return existing
        return TaskPlan(
            summary=state.get("plan_summary", "") or f"完成用户提出的任务：{state['user_input'][:80]}",
            steps=state.get("plan_steps") or self._default_plan_steps(state["user_input"]),
            risks=state.get("plan_risks") or ["第一阶段只确认计划，不自动执行任务。"],
            requires_confirmation=True,
        )


def _extract_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("structured analysis must be a JSON object")
    return parsed
