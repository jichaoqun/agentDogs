"""Prompt templates and prompt builders used by Agent Dogs."""

from __future__ import annotations

from typing import Any


DEFAULT_SYSTEM_PROMPT = "你是一个专业、可靠的本地智能助手。请准确、简洁地回答用户问题。"

TASK_ANALYSIS_SYSTEM_PROMPT = "你是主 Agent 的任务解析器。"
TASK_PLAN_SYSTEM_PROMPT = "你是主 Agent 的任务规划器。"
SIMPLE_CHAT_TOOL_GUARD_PROMPT = (
    "你是纯聊天 Agent，不具备工具调用能力。"
    "如果用户要求读取 workspace 文件、分析表格、执行代码、生成图表、保存文件或创建目录，"
    "不要声称已经完成，也不要编造文件内容、统计结果、图片或保存路径；"
    "只能说明该请求需要交给工具型 Agent 执行。"
)


def build_simple_chat_system_prompt(base_prompt: str, current_time: str = "") -> str:
    prompt = f"{base_prompt}\n\n{SIMPLE_CHAT_TOOL_GUARD_PROMPT}"
    if current_time:
        prompt = f"{prompt}\n\n{current_time}"
    return prompt


def build_task_analysis_prompt(text: str, current_time: str = "") -> str:
    prompt = (
        "请判断用户任务复杂度，只返回 JSON，不要解释。\n"
        "字段：intent, complexity, task_kind, route_hint, tool_intents, estimated_steps, risk_level, "
        "requires_confirmation, confidence, reason, missing_info, suggested_steps, clarification_questions。\n"
        "complexity 只能是 simple、needs_info、complex。\n"
        "route_hint 只能是 simple_chat、simple_task、clarify、future_task 或空。\n"
        "task_kind 只能是 chat、tool、task、unknown；risk_level 只能是 low、medium、high。\n"
        "simple: 普通问答、解释、闲聊、简单文本生成。\n"
        "simple_task: 目标明确、低风险、可直接使用工具完成，例如列文件、读明确路径文件、搜索文件、查看文件信息。\n"
        "联网查一下、搜索 workspace 中的明确关键词属于 simple_task；调研并整理对比属于 complex。\n"
        "needs_info: 缺少目标文件、范围、输出格式或确认条件。\n"
        "complex: 多步骤、项目/代码/文件操作、需要执行或长期跟踪。\n"
        "写入、删除、重命名、上传下载、命令执行必须视为 high risk 且 requires_confirmation=true。\n"
        "如果 complexity=needs_info，clarification_questions 必须是数组；每项包含 id、question、options、allow_custom、required。\n"
        "每个 options 给 2-4 个简短候选项；不能确定候选项时 options 为空且 allow_custom=true。\n"
        f"用户输入：{text}"
    )
    if current_time:
        prompt = f"{prompt}\n\n{current_time}"
    return prompt


def build_task_plan_prompt(
    *,
    user_input: str,
    clarification_answers: dict[str, Any] | None = None,
    plan_feedback: str = "",
    current_time: str = "",
) -> str:
    answers = clarification_answers or {}
    answer_lines = "\n".join(f"- {key}: {value}" for key, value in answers.items() if value) or "无"
    feedback_text = plan_feedback or "无"
    prompt = (
        "请为用户任务生成一个需要人工确认的执行计划，只返回 JSON，不要解释。\n"
        "字段：summary, steps, risks, requires_confirmation。\n"
        "summary 是一句话目标摘要；steps 是 3-6 个可执行步骤；risks 是 1-4 个风险或确认点；"
        "requires_confirmation 固定为 true。\n"
        "第一阶段只生成计划，不要声称已经执行、修改文件或调用工具。\n\n"
        f"原始任务：{user_input}\n\n"
        f"补充信息：\n{answer_lines}\n\n"
        f"用户对上一版计划的修改意见：{feedback_text}"
    )
    if current_time:
        prompt = f"{prompt}\n\n{current_time}"
    return prompt
