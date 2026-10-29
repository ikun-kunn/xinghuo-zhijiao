"""``tools/list`` 与 ``tools/call`` 分发：参数校验 → 服务层调用 → 结果组装。

Notes
-----
三个约定（与设计文档 §C.0 / §F.1 一致）：

1. 参数不合法 → 抛 :class:`JsonRpcError` ``-32602``（``data.violations`` 列出
   每个越界字段），因为客户端换参数重发是有意义的；
2. 业务失败 → ``isError: true`` + ``structuredContent.error``（含 ``code`` /
   ``retryable``），HTTP 类比 4xx/5xx；
3. 成功 → ``content``（人类可读 Markdown）+ ``structuredContent``（机器可解析）。
"""

from __future__ import annotations

from typing import Any

from .. import service
from . import schema
from .jsonrpc import BizCode, ErrorCode, JsonRpcError, ServiceError


def call_tool(name: str, arguments: dict[str, Any], scopes: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Validate, authorize and execute one tool call.

    Parameters
    ----------
    name : str
        Tool name; unknown names raise ``-32602`` (per MCP, ``-32601`` is for
        unknown *methods* such as ``tools/bogus``).
    arguments : dict
        Raw ``params.arguments``.
    scopes : tuple or list of str
        Granted scopes from the authenticated principal.

    Returns
    -------
    dict
        ``{"content": [...], "structuredContent": {...}, "isError": bool}``.

    Raises
    ------
    JsonRpcError
        ``-32602`` for unknown tool or invalid arguments.
    """
    if name not in schema.TOOLS_BY_NAME:
        raise JsonRpcError(
            ErrorCode.INVALID_PARAMS, "unknown tool", {"tool": name, "available": list(schema.TOOLS_BY_NAME)}
        )
    required_scope = schema.TOOL_SCOPES[name]
    if required_scope not in scopes:
        error = ServiceError(
            BizCode.FORBIDDEN, f"当前凭据缺少 {required_scope} 权限，无法调用 {name}"
        )
        payload = error.to_dict()
        payload["jsonrpc_code"] = ErrorCode.FORBIDDEN
        return _error_result(payload)

    violations = schema.validate(name, arguments)
    if violations:
        raise JsonRpcError(
            ErrorCode.INVALID_PARAMS,
            "invalid arguments",
            {"tool": name, "violations": violations},
        )

    try:
        handler = _HANDLERS[name]
        payload = handler(arguments)
    except ServiceError as error:
        return _error_result(error.to_dict())
    except (OSError, RuntimeError, ValueError) as error:
        return _error_result(
            {"code": BizCode.INTERNAL, "message": str(error) or error.__class__.__name__, "retryable": True}
        )
    return {
        "content": [{"type": "text", "text": _markdown(name, payload)}],
        "structuredContent": payload,
        "isError": False,
    }


def _error_result(error: dict[str, Any]) -> dict[str, Any]:
    """Wrap a business error as an ``isError`` tool result.

    Parameters
    ----------
    error : dict
        ``{code, message, retryable, ...}``

    Returns
    -------
    dict
        Tool result with ``isError: true`` and no ``structuredContent`` data
        beyond the error itself.
    """
    text = f"❌ {error.get('code')}：{error.get('message')}"
    if error.get("retryable"):
        text += "（可重试）"
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"error": error},
        "isError": True,
    }


def _generate(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``generate_teaching_package``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    params = {
        "grade": arguments["grade"],
        "subject": arguments["subject"],
        "textbook": arguments.get("textbook") or "人教版",
        "topic": str(arguments["topic"]).strip(),
    }
    return service.start_generation(params, owner=_current_owner())


def _status(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``get_generation_status``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.get_generation_status(
        int(arguments["generation_id"]), bool(arguments.get("include_plan", False))
    )


def _cancel(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``cancel_generation``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.cancel_generation(int(arguments["generation_id"]))


def _list(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``list_generations``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.list_generations(int(arguments.get("limit", 10)))


def _package(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``get_package``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.get_package(int(arguments["generation_id"]))


def _read(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``read_package_file``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.read_package_file(int(arguments["generation_id"]), str(arguments["kind"]))


def _search(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``search_knowledge_cards``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.search_knowledge_cards(
        str(arguments["subject"]), str(arguments["grade"]), str(arguments["topic"])
    )


def _catalog(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``list_catalog``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.list_catalog()


def _system(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handler for ``check_system_status``.

    Parameters
    ----------
    arguments : dict

    Returns
    -------
    dict
    """
    return service.check_system_status()


_HANDLERS = {
    "generate_teaching_package": _generate,
    "get_generation_status": _status,
    "cancel_generation": _cancel,
    "list_generations": _list,
    "get_package": _package,
    "read_package_file": _read,
    "search_knowledge_cards": _search,
    "list_catalog": _catalog,
    "check_system_status": _system,
}


_OWNER = {"value": "local"}


def set_owner(owner: str) -> None:
    """Set the current principal label used for audit/rate limiting.

    Parameters
    ----------
    owner : str
        ``sha256(token)[:8]`` or ``"local"``.

    Returns
    -------
    None
    """
    _OWNER["value"] = owner


def _current_owner() -> str:
    """Return the current principal label.

    Returns
    -------
    str
    """
    return _OWNER["value"]


def _markdown(name: str, payload: dict[str, Any]) -> str:
    """Render a tool payload as short human-readable Markdown.

    Parameters
    ----------
    name : str
        Tool name.
    payload : dict
        ``structuredContent`` of the call.

    Returns
    -------
    str
        Markdown text for ``content[0].text``.
    """
    if name == "generate_teaching_package":
        return (
            f"已启动生成任务 #{payload['generation_id']}"
            f"（{payload.get('mode')} 模式），预计 20~300 秒。\n"
            f"请用 `get_generation_status` 轮询，建议间隔 {payload.get('poll_after_ms', 1500)}ms。"
        )
    if name == "get_generation_status":
        lines = [
            f"任务 #{payload['generation_id']}：**{payload['status']}**",
            f"课题：{payload.get('topic')}（{payload.get('grade')} {payload.get('subject')}）",
        ]
        for node in payload.get("nodes", []):
            elapsed = node.get("elapsed_s")
            suffix = f" · {elapsed}s" if elapsed is not None else ""
            lines.append(f"- {node.get('name')}：{node.get('status')}{suffix}")
        if payload.get("error"):
            lines.append(f"- 错误：{payload['error']['code']} — {payload['error']['message']}")
        return "\n".join(lines)
    if name == "list_generations":
        lines = [f"最近 {payload['count']} 条生成记录："]
        for item in payload["items"]:
            lines.append(
                f"- #{item['generation_id']} {item['grade']}{item['subject']}《{item['topic']}》— {item['status']}"
            )
        return "\n".join(lines)
    if name == "get_package":
        lines = [f"任务 #{payload['generation_id']} 的产物："]
        for kind, meta in payload["files"].items():
            if meta.get("exists"):
                lines.append(f"- {kind}: {meta['relative_path']}（{meta['bytes']} 字节）")
            else:
                lines.append(f"- {kind}: 文件缺失")
        return "\n".join(lines)
    if name == "read_package_file":
        return (
            f"已读取 {payload['kind']}（{payload['bytes']} 字节，{payload['mime']}），"
            f"内容在 `structuredContent.base64`，sha256={payload['sha256'][:12]}…"
        )
    if name == "search_knowledge_cards":
        if not payload["count"]:
            return "知识库无匹配卡片：暂无课标依据，生成内容仅供参考。"
        lines = [f"命中 {payload['count']} 张知识点卡："]
        lines += [f"- {card['card_id']}：{card['title']}" for card in payload["cards"]]
        return "\n".join(lines)
    if name == "list_catalog":
        return (
            f"年级：{'/'.join(payload['grades'])}\n"
            f"学科：{'/'.join(payload['subjects'])}\n"
            f"教材：{payload['textbook']}\n"
            f"课件页型：{'、'.join(payload['courseware_kinds'])}"
        )
    if name == "check_system_status":
        warnings = payload.get("warnings") or []
        lines = [
            f"{payload['server']} · 模式 {payload['mode']}（可用：{payload['mode_usable']}）",
            f"LLM：{'已配置' if payload['llm_configured'] else '未配置'}；"
            f"知识库：{'就绪' if payload['kb_ready'] else '缺失'}；"
            f"素材库：{'就绪' if payload['assets_ready'] else '缺失'}",
        ]
        lines += [f"- ⚠️ {warning}" for warning in warnings]
        return "\n".join(lines)
    if name == "cancel_generation":
        return f"任务 #{payload['generation_id']} 取消请求：{'已受理' if payload['accepted'] else '未受理'}。{payload.get('note', '')}"
    return f"已完成 {name}"
