"""Final response synthesis helpers for MainAgent."""

from __future__ import annotations

import re
from typing import Any

from .state import TaskBrief


class AgentResponseSynthesizerMixin:
    """Convert sub-agent results into user-facing final responses."""

    def _findings_from_raw_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "title": item.get("title") or item.get("path") or item.get("url") or "结果",
                "summary": str(item.get("summary") or item.get("content_excerpt") or "")[:240],
                "source": item.get("source") or item.get("url") or item.get("path") or "",
            }
            for item in results[:5]
        ]

    def _synthesize_task_result(self, state: AgentState, result: Any, agent_name: str) -> str:
        brief = state.get("task_brief")
        if not getattr(result, "ok", False):
            return str(getattr(result, "error", None) or getattr(result, "content", "") or "任务执行失败。")

        if agent_name == "SearchAgent":
            return self._synthesize_search_result(brief if isinstance(brief, TaskBrief) else None, result)
        if agent_name == "CodeAgent":
            return self._synthesize_code_result(result)
        return str(getattr(result, "content", "") or getattr(result, "summary", "") or "任务已完成。")

    def _synthesize_code_result(self, result: Any) -> str:
        summary = str(getattr(result, "summary", "") or getattr(result, "content", "") or "").strip()
        data = getattr(result, "data", None) or {}
        if isinstance(data, dict) and data.get("task_type") == "code_generation":
            return str(getattr(result, "content", "") or summary or "已生成代码文本。")
        sandbox = data.get("sandbox", {}) if isinstance(data, dict) else {}
        artifacts = data.get("artifacts", []) if isinstance(data, dict) else []
        lines = [summary or "CodeAgent 已完成处理。"]
        if isinstance(sandbox, dict):
            if sandbox.get("exit_code") is not None:
                lines.append(f"沙箱退出码：{sandbox.get('exit_code')}")
            if sandbox.get("duration_ms") is not None:
                lines.append(f"耗时：{sandbox.get('duration_ms')} ms")
        if artifacts:
            lines.append("生成的 artifacts：")
            for item in artifacts[:5]:
                if isinstance(item, dict):
                    lines.append(f"- {item.get('filename') or item.get('path')} ({item.get('url') or item.get('path')})")
        lines.append("详细 stdout、stderr、生成代码和沙箱参数已保留在调试信息中。")
        return "\n".join(line for line in lines if line)

    def _synthesize_search_result(self, brief: TaskBrief | None, result: Any) -> str:
        content = str(getattr(result, "content", "") or "")
        summary = str(getattr(result, "summary", "") or "")
        data = getattr(result, "data", None) or {}
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            return summary or content or "没有找到可汇总的搜索结果。"

        intent = brief.intent if brief else ""
        if intent == "weather_lookup":
            return self._synthesize_weather_result(brief, results, summary or content)

        findings = getattr(result, "findings", None) or self._findings_from_raw_results(results)
        lines = [f"我找到 {len(results)} 条相关结果，先给你一个简要汇总："]
        for index, item in enumerate(findings[:3], start=1):
            title = str(item.get("title") or "结果").strip()
            item_summary = str(item.get("summary") or "").strip()
            source = str(item.get("source") or item.get("url") or item.get("path") or "").strip()
            detail = f"{index}. {title}"
            if item_summary:
                detail = f"{detail}：{item_summary[:160]}"
            if source:
                detail = f"{detail}（来源：{source}）"
            lines.append(detail)
        if len(results) > 3:
            lines.append("更多原始搜索结果可以在调试信息里查看。")
        return "\n".join(lines)

    def _synthesize_weather_result(self, brief: TaskBrief | None, results: list[dict[str, Any]], fallback: str) -> str:
        context = brief.context if brief else {}
        location = str(context.get("location") or "").strip() or "目标地区"
        date = str(context.get("date") or context.get("relative_time") or "").strip()
        best = self._best_weather_result(results, date)
        combined = self._combined_result_text([best] + [item for item in results if item is not best])
        condition = self._extract_weather_condition(combined)
        temperature = self._extract_temperature_range(combined)
        air_quality = self._extract_air_quality(combined)
        source = str(best.get("source") or "").strip()
        url = str(best.get("url") or best.get("path") or "").strip()

        pieces = []
        if condition:
            pieces.append(condition)
        if temperature:
            pieces.append(f"气温约 {temperature}")
        if air_quality:
            pieces.append(f"空气质量{air_quality}")

        when = f"{date} " if date else ""
        if pieces:
            answer = f"{location}{when}天气：{'，'.join(pieces)}。"
        else:
            short = self._compact_search_excerpt(best) or fallback
            answer = f"我找到了{location}{when}天气相关结果，但没有稳定提取出完整天气字段。{short[:220]}"

        if source or url:
            answer = f"{answer}\n来源：{source or '搜索结果'}{f'，{url}' if url else ''}"
        answer = f"{answer}\n原始搜索结果已保留在调试信息中。"
        return answer

    def _best_weather_result(self, results: list[dict[str, Any]], date: str = "") -> dict[str, Any]:
        if not results:
            return {}
        compact_date = date.replace("-", "")
        zh_date = ""
        if re.match(r"\d{4}-\d{2}-\d{2}$", date):
            year, month, day = date.split("-")
            zh_date = f"{year}年{int(month):02d}月{int(day):02d}日"
        preferred_sources = ("weather.com.cn", "cma.gov.cn", "tianqi.com")

        def score(item: dict[str, Any]) -> int:
            haystack = self._combined_result_text([item])
            identity = f"{item.get('source') or ''} {item.get('url') or ''} {item.get('title') or ''}".lower()
            value = 0
            if date and date in haystack:
                value += 5
            if compact_date and compact_date in identity:
                value += 5
            if zh_date and zh_date in haystack:
                value += 5
            if self._extract_temperature_range(haystack):
                value += 3
            if self._extract_weather_condition(haystack):
                value += 2
            for index, source in enumerate(preferred_sources):
                if source in identity:
                    value += len(preferred_sources) - index
            return value

        return max(results, key=score)

    def _combined_result_text(self, results: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        for item in results[:5]:
            for key in ("title", "summary", "content_excerpt"):
                value = str(item.get(key) or "").strip()
                if value:
                    chunks.append(value)
        return "\n".join(chunks)

    def _compact_search_excerpt(self, item: dict[str, Any]) -> str:
        for key in ("summary", "content_excerpt", "title"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    def _extract_temperature_range(self, text: str) -> str:
        match = re.search(r"(-?\d{1,2})\s*[~～\-—至到]\s*(-?\d{1,2})\s*[℃°]?", text)
        if match:
            return f"{match.group(1)}~{match.group(2)}℃"
        high = re.search(r"最高(?:气温|温度)?\D{0,8}(-?\d{1,2})\s*[℃°]?", text)
        low = re.search(r"最低(?:气温|温度)?\D{0,8}(-?\d{1,2})\s*[℃°]?", text)
        if high and low:
            return f"{low.group(1)}~{high.group(1)}℃"
        return ""

    def _extract_weather_condition(self, text: str) -> str:
        conditions = (
            "雷阵雨",
            "阵雨",
            "小雨",
            "中雨",
            "大雨",
            "暴雨",
            "多云",
            "晴",
            "阴",
            "雨夹雪",
            "小雪",
            "中雪",
            "大雪",
            "雾",
            "霾",
        )
        for condition in conditions:
            if condition in text:
                return condition
        return ""

    def _extract_air_quality(self, text: str) -> str:
        levels = "优|良|轻度污染|中度污染|重度污染|严重污染"
        match = re.search(rf"(?:空气质量|空气|AQI)[^\n，。:：]{{0,20}}({levels})", text)
        if match:
            return match.group(1)
        match = re.search(rf"\b\d{{1,3}}\s*({levels})\b", text)
        if match:
            return match.group(1)
        return ""

