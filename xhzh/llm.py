"""轻量 LLM 客户端：OpenAI 兼容 Chat Completions（标准库实现，零额外依赖）。

Notes
-----
配置三要素（环境变量）：``LLM_BASE_URL``（如 https://api.xxx.com/v1）、
``LLM_API_KEY``、``LLM_MODEL``。后续接 WorkBuddy ADP 时，
:class:`xhzh.adp_client.AdpClient` 走 adp 模式，本模块保留为通用兜底。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from . import config


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns unusable output."""


def is_configured() -> bool:
    """Check whether LLM env credentials are present.

    Returns
    -------
    bool
    """
    return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL"))


def chat(messages: list[dict], temperature: float = 0.4, timeout: int = 120) -> str:
    """Call an OpenAI-compatible chat completion endpoint.

    Parameters
    ----------
    messages : list of dict
        ``[{"role": ..., "content": ...}]``.
    temperature : float
    timeout : int
        Seconds.

    Returns
    -------
    str
        Assistant message content.

    Raises
    ------
    LLMError
        On network failure, non-2xx response, or missing credentials.
    """
    if not is_configured():
        raise LLMError(
            "LLM 未配置：请设置环境变量 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
            "（或使用 XHZH_MODE=mock 演示模式）"
        )
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    payload = {
        "model": os.environ["LLM_MODEL"],
        "messages": messages,
        "temperature": temperature,
        # 显式上限：节点③ 的 JSON（题目+答案+板书）较长，默认值过小会截断输出
        "max_tokens": config.LLM_MAX_TOKENS,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise LLMError(f"LLM HTTP {error.code}: {error.read().decode('utf-8', 'ignore')[:300]}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise LLMError(f"LLM 调用失败：{error}") from error
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise LLMError(f"LLM 响应结构异常：{json.dumps(body, ensure_ascii=False)[:300]}") from error
    if not isinstance(content, str) or not content.strip():
        raise LLMError(
            f"LLM 返回内容为空（可能触发工具调用或被安全策略拦截）：{json.dumps(body, ensure_ascii=False)[:300]}"
        )
    # 输出被 max_tokens 截断时明确报错，避免误诊为"JSON 语法错误"
    finish_reason = (body.get("choices") or [{}])[0].get("finish_reason")
    if finish_reason == "length":
        raise LLMError(
            f"LLM 输出被截断（finish_reason=length，max_tokens={config.LLM_MAX_TOKENS}）："
            "请调大 XHZH_LLM_MAX_TOKENS 或精简提示词要求的输出长度"
        )
    return content


def _find_balanced_json(text: str) -> str | None:
    """Find the first complete, brace-balanced JSON object in text.

    Parameters
    ----------
    text : str

    Returns
    -------
    str or None
        The candidate JSON substring, or ``None`` if no balanced object
        exists (e.g. truncated output).
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _repair_stray_quotes(text: str) -> str:
    """Escape double quotes that appear inside JSON string values.

    LLMs frequently wrap Chinese phrases with ASCII double quotes inside a
    JSON string (e.g. ``"能解决如"配套问题"等"``), which terminates the
    string early and breaks parsing. This function walks the text like a
    JSON lexer: a quote is treated as a *closing* quote only when the next
    non-space character is a structural delimiter (``, : } ]``); otherwise
    it is escaped as ``\\"``.

    Parameters
    ----------
    text : str
        Raw model output (may contain prose, code fences, or broken JSON).

    Returns
    -------
    str
        Text with stray inner quotes escaped; unchanged when already valid.
    """
    out: list[str] = []
    in_string = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            out.append(text[index:index + 2])
            index += 2
            continue
        if char == '"':
            if not in_string:
                in_string = True
                out.append(char)
            else:
                probe = index + 1
                while probe < length and text[probe] in " \t\r\n":
                    probe += 1
                following = text[probe] if probe < length else ""
                if following in ",:}]":
                    in_string = False
                    out.append(char)
                else:
                    out.append('\\"')  # 字符串内部的裸引号，转义保留
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM response.

    Parameters
    ----------
    text : str
        Raw assistant content (may contain prose or code fences).

    Returns
    -------
    dict

    Raises
    ------
    LLMError
        If no parseable JSON object is found.

    Notes
    -----
    先按原文解析；失败则对"字符串内裸引号"做修复后重试——模型在中文语境
    常用 ASCII 双引号做强调，会提前终止 JSON 字符串。
    """
    last_error: Exception | None = None
    for variant in (text, _repair_stray_quotes(text)):
        candidate = _find_balanced_json(variant)
        if candidate is None:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
            continue
        if not isinstance(parsed, dict):
            raise LLMError("LLM JSON 顶层不是对象")
        return parsed
    if last_error is not None:
        raise LLMError(f"LLM JSON 解析失败（含引号修复重试）：{last_error}") from last_error
    raise LLMError(f"LLM 输出中未找到 JSON：{text[:200]}")
