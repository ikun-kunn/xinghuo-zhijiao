"""MCP 协议层：JSON-RPC 2.0 over stdio（标准库实现，零第三方依赖）。

Notes
-----
包内模块职责：
- :mod:`xhzh.mcp.jsonrpc`：报文构造/解析、协议码与业务码常量；
- :mod:`xhzh.mcp.schema`：9 个工具的 JSON Schema 与轻量校验器；
- :mod:`xhzh.mcp.tools`：``tools/list`` / ``tools/call`` 分发；
- :mod:`xhzh.mcp.resources`：``resources/list`` / ``resources/read``；
- :mod:`xhzh.mcp.server`：stdio 传输循环、握手、鉴权、生命周期。

本模块刻意保持极简：``serve_stdio`` 内部延迟导入 :mod:`xhzh.mcp.server`，
避免 ``service`` 导入 ``mcp.jsonrpc`` 时触发包级循环导入。
"""

from __future__ import annotations

from typing import Any


def serve_stdio(token: str = "", log_level: str = "INFO", **kwargs: Any) -> int:
    """Run the MCP server over stdio until EOF.

    Parameters
    ----------
    token : str
        认证令牌；空字符串表示本地单用户模式。
    log_level : str
        日志级别名（DEBUG/INFO/WARNING/ERROR）。
    **kwargs : Any
        透传给 :func:`xhzh.mcp.server.serve_stdio`。

    Returns
    -------
    int
        进程退出码（正常 EOF 为 0）。
    """
    from .server import serve_stdio as _serve

    return _serve(token=token, log_level=log_level, **kwargs)
