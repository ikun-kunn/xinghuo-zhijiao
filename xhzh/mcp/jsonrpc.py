"""MCP 协议原语：JSON-RPC 2.0 报文构造、解析与错误码常量。

Notes
-----
本模块是协议错误的唯一来源，``tools`` / ``resources`` / ``server`` 一律从
此处导入，禁止散落魔法数。两类错误严格分层（设计文档 §F.1）：

- **协议错误**（``-327xx`` / ``-320xx``）→ JSON-RPC ``error`` 对象；
- **业务错误**（``BizCode``）→ ``result.isError = true`` +
  ``structuredContent.error.code``。

判据是"客户端换一组参数重发是否有意义"：有意义（参数非法、任务忙、
模式不可用）走业务错误；无意义（方法不存在、报文残缺）走协议错误。
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any


class ErrorCode:
    """JSON-RPC 2.0 与 MCP 扩展错误码（协议层）。

    Attributes
    ----------
    PARSE_ERROR : int
        报文不是合法 JSON。
    INVALID_REQUEST : int
        报文结构不合法（缺 ``jsonrpc`` / ``method``，或批量数组）。
    METHOD_NOT_FOUND : int
        方法名不存在（如 ``tools/bogus``）。
    INVALID_PARAMS : int
        参数不合法，或 ``tools/call`` 的 ``name`` 不存在。
    INTERNAL_ERROR : int
        服务端未归类异常。
    SERVER_NOT_INITIALIZED : int
        未完成 ``initialize`` 就调用业务方法。
    FORBIDDEN : int
        scope 不足（仅用于 ``resources/read`` 这类无 ``isError`` 的通道）。
    """

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_NOT_INITIALIZED = -32002
    FORBIDDEN = -32003


class BizCode:
    """业务错误码（写进 ``structuredContent.error.code``）。

    Attributes
    ----------
    PARAM_INVALID : str
        参数校验失败（含枚举越界、课题为空）。
    NOT_FOUND : str
        任务或产物不存在。
    FORBIDDEN : str
        scope 不足。
    RATE_LIMITED : str
        触发速率限制。
    BUSY : str
        并发闸满载。
    TIMEOUT : str
        超过请求级或任务级预算。
    MODE_UNAVAILABLE : str
        ``XHZH_MODE=adp`` 未接入、或知识库缺失导致能力不可用。
    INTERNAL : str
        未归类异常。
    """

    PARAM_INVALID = "PARAM_INVALID"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    BUSY = "BUSY"
    TIMEOUT = "TIMEOUT"
    MODE_UNAVAILABLE = "MODE_UNAVAILABLE"
    INTERNAL = "INTERNAL"


# 业务码 → 是否可重试（写进 structuredContent.error.retryable）
BIZ_RETRYABLE = {
    BizCode.PARAM_INVALID: False,
    BizCode.NOT_FOUND: False,
    BizCode.FORBIDDEN: False,
    BizCode.RATE_LIMITED: True,
    BizCode.BUSY: True,
    BizCode.TIMEOUT: True,
    BizCode.MODE_UNAVAILABLE: False,
    BizCode.INTERNAL: True,
}


class ServiceError(Exception):
    """业务异常：由服务层抛出，协议层统一转换为 ``isError`` 结果。

    Parameters
    ----------
    code : str
        :class:`BizCode` 中的一个。
    message : str
        面向调用方的中文说明（不得包含密钥、绝对路径）。
    retry_after_ms : int or None
        建议重试间隔，仅 ``RATE_LIMITED`` / ``BUSY`` 使用。
    """

    def __init__(self, code: str, message: str, retry_after_ms: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_ms = retry_after_ms
        self.retryable = BIZ_RETRYABLE.get(code, True)

    def to_dict(self) -> dict[str, Any]:
        """Render the error payload for ``structuredContent.error``.

        Returns
        -------
        dict
            ``{code, message, retryable, retry_after_ms?}``.
        """
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.retry_after_ms is not None:
            payload["retry_after_ms"] = self.retry_after_ms
        return payload


class JsonRpcError(Exception):
    """协议异常：由报文解析/分发抛出，转换为 JSON-RPC ``error``。

    Parameters
    ----------
    code : int
        :class:`ErrorCode` 中的一个。
    message : str
        英文短描述（便于客户端日志检索）。
    data : dict or None
        附加信息，如 ``{"tool": "no_such_tool"}``。
    """

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def dumps(payload: dict[str, Any]) -> str:
    """Serialize one JSON-RPC frame as a single line.

    Parameters
    ----------
    payload : dict
        Complete JSON-RPC object.

    Returns
    -------
    str
        Single-line JSON ending with ``\\n`` (never wraps, so stdio framing
        stays one-message-per-line).
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def make_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success response.

    Parameters
    ----------
    request_id : Any
        Echoed from the request (``None`` only for parse errors).
    result : dict

    Returns
    -------
    dict
    """
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a JSON-RPC error response.

    Parameters
    ----------
    request_id : Any
        Echoed from the request; ``None`` when the id could not be parsed.
    code : int
    message : str
    data : dict or None

    Returns
    -------
    dict
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def parse_line(raw: str) -> dict[str, Any]:
    """Parse and structurally validate one incoming frame.

    Parameters
    ----------
    raw : str
        One line read from stdin (may be empty).

    Returns
    -------
    dict
        The validated JSON-RPC object.

    Raises
    ------
    JsonRpcError
        ``-32700`` for invalid JSON, ``-32600`` for a non-object frame or a
        batch array, or a missing ``method`` on a request.
    """
    text = raw.strip()
    if not text:
        raise JsonRpcError(ErrorCode.INVALID_REQUEST, "empty frame")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise JsonRpcError(ErrorCode.PARSE_ERROR, f"invalid json: {error.msg}") from error
    if not isinstance(payload, dict):
        # MCP 未定义批量语义，数组一律按非法请求处理，避免静默丢弃
        raise JsonRpcError(ErrorCode.INVALID_REQUEST, "batch or non-object frame")
    if payload.get("jsonrpc") != "2.0":
        raise JsonRpcError(ErrorCode.INVALID_REQUEST, "jsonrpc must be '2.0'")
    if not isinstance(payload.get("method"), str) or not payload["method"]:
        raise JsonRpcError(ErrorCode.INVALID_REQUEST, "method is required")
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "params must be an object")
    return payload


class Writer:
    """Thread-safe single-line writer for the stdio transport.

    Parameters
    ----------
    stream : TextIO or None
        Target stream; defaults to ``sys.stdout``.

    Notes
    -----
    并发任务会同时写 stdout，必须持有同一把锁逐帧写出；日志一律走 stderr，
    否则会污染 JSON-RPC 帧（客户端直接解析失败）。
    """

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        """Write one frame and flush.

        Parameters
        ----------
        payload : dict
            JSON-RPC object (response or notification).

        Returns
        -------
        None
        """
        line = dumps(payload)
        with self._lock:
            self._stream.write(line)
            self._stream.flush()
