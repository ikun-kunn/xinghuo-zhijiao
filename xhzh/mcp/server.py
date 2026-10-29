"""MCP stdio 传输与生命周期：握手、能力声明、鉴权、方法分发、进度通知。

Notes
-----
stdio 传输的三条铁律：

1. **stdout 只写 JSON-RPC 帧**，每帧一行、``\\n`` 结尾；任何日志走 stderr，
   否则客户端会解析失败（这是自研 MCP 服务最常见的故障）；
2. **通知不回包**（``notifications/*`` 没有 id）；
3. **进程不能因为单条坏报文退出**：解析失败回 ``-32700`` 后继续读下一行。

取消是协作式的：``cancel_generation`` 只置标记，工作线程在节点边界检查，
不在协议层强杀线程。
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, TextIO

from .. import config, service, storage
from . import resources, tools
from .jsonrpc import ErrorCode, JsonRpcError, Writer, make_error, make_result, parse_line
from .schema import list_tools

LOGGER = logging.getLogger(__name__)

# 未协商前的默认版本（客户端若不发 initialize 直接调用，会先被 -32002 拦下）
_FALLBACK_VERSION = config.MCP_PROTOCOL_VERSION
# outputSchema 自 2025-06-18 起被规范支持，老版本协商时不下发
_OUTPUT_SCHEMA_SINCE = "2025-06-18"


class Server:
    """One MCP session over a text stream pair.

    Parameters
    ----------
    writer : Writer
        JSON-RPC frame writer (stdout, lock-protected).
    token : str
        Authentication token; empty means local single-user mode.
    scopes : tuple of str or None
        Granted scopes; defaults to :data:`xhzh.config.MCP_DEFAULT_SCOPES`.
    """

    def __init__(self, writer: Writer, token: str = "", scopes: tuple[str, ...] | None = None) -> None:
        self.writer = writer
        self.token = token
        self.scopes = tuple(scopes or config.MCP_DEFAULT_SCOPES)
        self.owner = "local" if not token else __import__("hashlib").sha256(token.encode()).hexdigest()[:8]
        self.initialized = False
        self.protocol_version = _FALLBACK_VERSION
        tools.set_owner(self.owner)

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    def handle(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one validated frame.

        Parameters
        ----------
        payload : dict
            Output of :func:`xhzh.mcp.jsonrpc.parse_line`.

        Returns
        -------
        dict or None
            Response frame, or ``None`` for notifications.
        """
        method = payload["method"]
        params = payload.get("params") or {}
        request_id = payload.get("id")
        is_notification = "id" not in payload

        if method == "notifications/initialized":
            LOGGER.debug("client initialized")
            return None
        if not self.initialized and method not in ("initialize", "ping"):
            return make_error(request_id, ErrorCode.SERVER_NOT_INITIALIZED, "server not initialized")
        if method == "ping":
            return None if is_notification else make_result(request_id, {})
        if method == "initialize":
            return make_result(request_id, self._initialize(params))
        if method == "logging/setLevel":
            return make_result(request_id, {})
        if method == "tools/list":
            include = _version_at_least(self.protocol_version, _OUTPUT_SCHEMA_SINCE)
            return make_result(request_id, {"tools": list_tools(include_output_schema=include)})
        if method == "tools/call":
            return make_result(request_id, self._call_tool(params))
        if method == "resources/list":
            return make_result(request_id, {"resources": resources.list_resources()})
        if method == "resources/read":
            return make_result(request_id, resources.read_resource(str(params.get("uri") or ""), self.scopes))
        raise JsonRpcError(ErrorCode.METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Negotiate the protocol version and declare capabilities.

        Parameters
        ----------
        params : dict
            Client ``initialize`` params.

        Returns
        -------
        dict
            ``protocolVersion`` / ``capabilities`` / ``serverInfo`` / ``instructions``.

        Notes
        -----
        客户端请求未知或更高版本时，按规范回本端首选版本，**不报错**，
        由客户端决定是否继续。
        """
        requested = str(params.get("protocolVersion") or "")
        if requested in config.MCP_SUPPORTED_VERSIONS:
            self.protocol_version = requested
        else:
            self.protocol_version = config.MCP_PROTOCOL_VERSION
        self.initialized = True
        LOGGER.info(
            "initialize: client=%s requested=%s negotiated=%s",
            (params.get("clientInfo") or {}).get("name", "?"),
            requested or "-",
            self.protocol_version,
        )
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "logging": {},
            },
            "serverInfo": {
                "name": config.MCP_SERVER_NAME,
                "version": config.MCP_SERVER_VERSION,
            },
            "instructions": (
                "星火智教 MCP 服务：用 generate_teaching_package 发起教学包生成（异步），"
                "再用 get_generation_status 轮询；产物可用 get_package 取元信息、read_package_file 取内容。"
            ),
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call and emit optional progress notifications.

        Parameters
        ----------
        params : dict
            ``{"name": ..., "arguments": {...}, "_meta": {"progressToken": ...}}``.

        Returns
        -------
        dict
            Tool result (``content`` / ``structuredContent`` / ``isError``).

        Raises
        ------
        JsonRpcError
            ``-32602`` for missing name or invalid arguments.
        """
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(ErrorCode.INVALID_PARAMS, "arguments must be an object", {"tool": name})
        result = tools.call_tool(name, arguments, self.scopes)
        progress_token = (params.get("_meta") or {}).get("progressToken")
        if (
            progress_token is not None
            and name == "generate_teaching_package"
            and not result.get("isError")
        ):
            generation_id = int(result["structuredContent"]["generation_id"])
            threading.Thread(
                target=self._watch_progress,
                args=(progress_token, generation_id),
                daemon=True,
                name=f"progress-{generation_id}",
            ).start()
        return result

    def _watch_progress(self, progress_token: Any, generation_id: int, total: int = 4) -> None:
        """Emit ``notifications/progress`` until the task reaches a terminal state.

        Parameters
        ----------
        progress_token : Any
            Client-provided token (echoed verbatim).
        generation_id : int
        total : int
            Node count used for the percentage.

        Returns
        -------
        None

        Notes
        -----
        仅当客户端给了 ``progressToken`` 才会走到这里；没给就一条通知都不发
        （避免 stdout 上出现客户端未预期的帧）。
        """
        last = -1
        while True:
            try:
                status = service.get_generation_status(generation_id)
            except service.ServiceError:
                return
            done = sum(1 for node in status.get("nodes") or [] if node.get("status") == "done")
            terminal = status.get("status") in ("done", "failed", "cancelled")
            progress = 100 if terminal else max(5, int(done / total * 100))
            if progress != last:
                last = progress
                self.writer.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": progress_token,
                            "progress": progress,
                            "total": 100,
                        },
                    }
                )
            if terminal:
                return
            threading.Event().wait(0.5)


def _version_at_least(version: str, threshold: str) -> bool:
    """Compare two MCP date-style versions.

    Parameters
    ----------
    version : str
        e.g. ``"2025-03-26"``.
    threshold : str
        e.g. ``"2025-06-18"``.

    Returns
    -------
    bool
        ``True`` when ``version`` is at least ``threshold`` (lexicographic
        comparison is valid for ``YYYY-MM-DD``).
    """
    return version >= threshold


def serve_stdio(
    token: str = "",
    log_level: str = "INFO",
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    scopes: tuple[str, ...] | None = None,
) -> int:
    """Run the MCP server loop until EOF.

    Parameters
    ----------
    token : str
        Authentication token (empty = local mode).
    log_level : str
        Logging level name; logs always go to stderr.
    stdin : TextIO or None
        Defaults to ``sys.stdin``.
    stdout : TextIO or None
        Defaults to ``sys.stdout``.
    scopes : tuple of str or None
        Override granted scopes (tests use ``("read",)`` to check FORBIDDEN).

    Returns
    -------
    int
        Process exit code: ``0`` on EOF, ``1`` on unrecoverable transport error.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    input_stream = stdin or sys.stdin
    writer = Writer(stdout or sys.stdout)
    server = Server(writer, token=token, scopes=scopes)
    try:
        storage.mark_stale_running_failed("MCP 服务启动，清理上次中断的任务")
    except Exception as error:  # noqa: BLE001 - 启动清理失败不应阻止服务
        LOGGER.warning("stale cleanup failed: %s", error)
    LOGGER.info("MCP server ready (mode=%s, owner=%s)", config.MODE, server.owner)
    while True:
        try:
            line = input_stream.readline()
        except KeyboardInterrupt:  # pragma: no cover - 交互式中断
            break
        if not line:
            break
        try:
            payload = parse_line(line)
        except JsonRpcError as error:
            writer.send(make_error(None, error.code, error.message, error.data))
            continue
        try:
            response = server.handle(payload)
        except JsonRpcError as error:
            response = make_error(payload.get("id"), error.code, error.message, error.data)
        except Exception as error:  # noqa: BLE001 - 单条请求异常不得终止进程
            LOGGER.exception("unhandled error")
            response = make_error(
                payload.get("id"), ErrorCode.INTERNAL_ERROR, "internal error", {"detail": str(error)[:200]}
            )
        if response is not None and "id" in payload:
            writer.send(response)
    LOGGER.info("MCP server stopped (EOF)")
    return 0
