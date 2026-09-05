"""LLM Provider 抽象层：DeepSeek / Minimax / Kimi 三家 OpenAI 兼容 chat/completions。

设计要点：
- 一份 evaluator 共用同套 system / user prompt（沿用 inspiration_house.py 的措辞）。
- 每个 Provider 只声明 endpoint / 默认 model / 是否需要任何额外 header。
- 单条算子打分由 evaluate_one(...) 返回 (score: int, reason: str)。
- 失败/超时降级为 (0, "error reason")，不抛异常，避免大批量并发时一个坏调用拖垮整批。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

import requests

logger = logging.getLogger(__name__)


# ---------- Provider 定义 ----------

@dataclass(frozen=True)
class Provider:
    key: str            # UI 显示名/下拉值
    label: str          # 中文标签
    base_url: str
    default_model: str
    models: tuple[str, ...]


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        key="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/chat/completions",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "minimax": Provider(
        key="minimax",
        label="MiniMax",
        # OpenAI 兼容端点（官方文档 2026-08-09 确认）：https://api.minimaxi.com/v1
        # chat/completions 为国内站；国际站为 api.minimax.io/v1
        base_url="https://api.minimaxi.com/v1/chat/completions",
        default_model="MiniMax-M3",
        models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed",
                "MiniMax-M2.5", "MiniMax-M2.5-highspeed",
                "MiniMax-M2.1", "MiniMax-M2.1-highspeed", "MiniMax-M2"),
    ),
    "kimi": Provider(
        key="kimi",
        label="Kimi（Moonshot）",
        base_url="https://api.moonshot.cn/v1/chat/completions",
        default_model="moonshot-v1-8k",
        models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
    ),
}


# ---------- Prompt 模板（沿用 inspiration_house.py 系统提示词） ----------

SYSTEM_PROMPT = """You are an expert quantitative researcher and data scientist specializing in financial data analysis and algorithmic trading. Your task is to evaluate how well a specific BRAIN operator could be ADDED to the current expression to help achieve a given quant research target.

IMPORTANT: You are NOT suggesting replacements for the current expression. You are evaluating how each operator could be COMBINED with or applied AFTER the current expression to move closer to the target.

Your evaluation should be based on:
1. How the operator could be applied AFTER the current expression (e.g., ts_rank(current_expression))
2. The operator's mathematical and statistical properties when combined with the current expression
3. How this combination would better achieve the stated research target
4. The synergistic effects of applying this operator to the current expression

You must provide:
1. A score from 0-10 (where 10 is perfect synergistic addition to achieve the target)
2. A clear, concise reason explaining how this operator could be added to improve the current expression

Scoring Guidelines:
- 8-10: Excellent addition - operator would significantly enhance the current expression for the target
- 6-7: Good addition - operator would meaningfully improve the current expression
- 4-5: Moderate addition - operator could provide some benefit when combined
- 2-3: Weak addition - operator would add little value to the current expression
- 0-1: No value - operator would not help or might even hurt the current expression

Response Format:
Score: [number from 0-10]
Reason: [provide a detailed explanation of how this operator could be added to the current expression to better achieve the target. Include specific examples of how the combination would work and why it would be effective.]

Focus on combination effects and how the operator would enhance the current expression rather than replace it. Be thorough in your explanation."""


def build_user_prompt(
    operator: dict, research_target: str, current_expression: str, expression_context: str
) -> str:
    op_name = operator.get("name", "Unknown")
    op_cat = operator.get("category", "Unknown")
    op_desc = operator.get("description", "No description available")
    return (
        "Please evaluate how well this BRAIN operator could be ADDED to the current "
        "expression to help achieve the research target.\n\n"
        f"Operator Name: {op_name}\n"
        f"Operator Category: {op_cat}\n"
        f"Operator Description: {op_desc}\n\n"
        f"Research Target: {research_target}\n"
        f"Current Expression: {current_expression or 'None provided'}\n"
        f"Expression Context: {expression_context or 'None provided'}\n\n"
        f"IMPORTANT: Consider how this operator could be applied AFTER the current "
        f"expression (e.g., {op_name}(current_expression)) to enhance the strategy. "
        "Do NOT suggest replacing the current expression.\n\n"
        "Think about:\n"
        "- How would applying this operator to the current expression improve the strategy?\n"
        "- What synergistic effects would this combination create?\n"
        "- How would this addition move us closer to the research target?\n\n"
        "Response Format:\n"
        "Score: [number from 0-10]\n"
        "Reason: [provide a detailed explanation of how this operator could be added to "
        "the current expression to better achieve the target. Include specific examples "
        "of how the combination would work and why it would be effective.]"
    )


# ---------- 响应解析（沿用 inspiration_house.py parse_evaluation_response） ----------

def parse_evaluation_response(response: str) -> tuple[int, str]:
    """从 LLM 文本响应里抠出 Score / Reason。"""
    score = 0
    reason = "No reason provided"
    in_reason = False
    reason_lines: list[str] = []
    for raw in (response or "").splitlines():
        line = raw.strip()
        if line.lower().startswith("score:"):
            try:
                txt = line.split(":", 1)[1].strip()
                # 容错：支持 "8"、"7.5"、"7.5/10"、"8." 等格式；
                # 原 int(txt.split()[0]) 对 "7.5/10" 会 ValueError → 误判 0 分
                m = re.match(r"(\d+(?:\.\d+)?)", txt)
                score = max(0, min(10, int(float(m.group(1))))) if m else 0
            except (ValueError, IndexError):
                score = 0
        elif line.lower().startswith("reason:"):
            in_reason = True
            tail = line.split(":", 1)[1].strip()
            if tail:
                reason_lines.append(tail)
        elif in_reason and line:
            if line.lower().startswith(("operator:", "category:")):
                break
            if line.lower().startswith("score:"):
                # 形如 "Score: 8" 的新评分块才结束 reason；否则视为 reason 正文
                if re.match(r"score:\s*\d", line.lower()):
                    break
            reason_lines.append(line)
    if reason_lines:
        reason = " ".join(reason_lines)
    return score, reason


# ---------- 单条算子评估 ----------

def evaluate_one(
    *,
    api_key: str,
    provider_key: str,
    model_name: str,
    operator: dict,
    research_target: str,
    current_expression: str,
    expression_context: str,
    timeout: int = 200,
    base_url: str | None = None,
) -> dict:
    """给一个算子打分。失败时 score=0 reason 含错误信息，不抛异常。

    base_url 可选：覆盖 Provider 默认端点（设置弹窗里用户自定义 Base URL 时用）。
    """
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        return {"operator": operator.get("name", "?"), "category": operator.get("category", "?"),
                "score": 0, "reason": f"unknown provider: {provider_key}"}
    if not api_key:
        return {"operator": operator.get("name", "?"), "category": operator.get("category", "?"),
                "score": 0, "reason": "missing api key"}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model_name or provider.default_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(
                operator, research_target, current_expression, expression_context)},
        ],
        "temperature": 1,
        "stream": False,
    }
    endpoint = (base_url or provider.base_url).rstrip("/")
    try:
        resp = requests.post(endpoint, headers=headers, json=body, timeout=timeout)
    except requests.exceptions.Timeout:
        return _err(operator, "API timeout")
    except requests.exceptions.RequestException as e:
        return _err(operator, f"network error: {e}")
    except Exception as e:
        return _err(operator, f"unexpected: {e}")

    if resp.status_code != 200:
        snippet = (resp.text or "")[:200].replace("\n", " ")
        return _err(operator, f"HTTP {resp.status_code}: {snippet}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return _err(operator, f"bad response shape: {e}")

    score, reason = parse_evaluation_response(content)
    return {
        "operator": operator.get("name", "?"),
        "category": operator.get("category", "?"),
        "score": score,
        "reason": reason,
    }


def _err(operator: dict, msg: str) -> dict:
    return {
        "operator": operator.get("name", "?"),
        "category": operator.get("category", "?"),
        "score": 0,
        "reason": msg,
    }


# ---------- 并发批量 ----------

def batch_evaluate(
    operators: list[dict],
    *,
    api_key: str,
    provider_key: str,
    model_name: str,
    research_target: str,
    current_expression: str,
    expression_context: str,
    max_workers: int = 20,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """并发评估多个算子，返回的 list 与输入同顺序（按 score 排序在调用方做）。"""
    total = len(operators)
    results: list[dict | None] = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(
                evaluate_one,
                api_key=api_key,
                provider_key=provider_key,
                model_name=model_name,
                operator=op,
                research_target=research_target,
                current_expression=current_expression,
                expression_context=expression_context,
            ): i
            for i, op in enumerate(operators)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                op = operators[i]
                results[i] = _err(op, f"worker exception: {e}")
            done += 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass
    # 任何被 None 的（理论上不会有）补一条 error
    for i, r in enumerate(results):
        if r is None:
            results[i] = _err(operators[i], "no result")
    return results  # type: ignore[return-value]