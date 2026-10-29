"""MCP 服务启动器（stdio 传输）。

Notes
-----
被 MCP 客户端以子进程方式拉起，例如 Claude Desktop 配置：

.. code-block:: json

    {
      "mcpServers": {
        "xinghuo-zhijiao": {
          "command": "python",
          "args": ["C:/path/to/xinghuo-zhijiao/mcp_server.py"],
          "env": {"XHZH_MODE": "mock"}
        }
      }
    }

**stdout 只输出 JSON-RPC 帧**，本文件的所有提示一律走 stderr。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 允许从仓库根直接运行（python mcp_server.py），无需 pip install
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xhzh.mcp.server import serve_stdio  # noqa: E402  - 需先注入 sys.path
from xhzh import config  # noqa: E402


def _parse_scopes(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated scope list.

    Parameters
    ----------
    raw : str
        e.g. ``"read,generate"``; empty means default scopes.

    Returns
    -------
    tuple of str
        Valid scopes only; unknown names are ignored with a warning.
    """
    known = {"read", "generate", "admin"}
    items = tuple(item.strip() for item in raw.split(",") if item.strip())
    return tuple(item for item in items if item in known)


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Parameters
    ----------
    argv : list of str or None
        Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="mcp_server.py",
        description="星火智教 MCP 服务（stdio 传输）：把教学包生成工作流暴露给 MCP 客户端。",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio"],
        help="传输方式，当前仅支持 stdio（Streamable HTTP 仅在设计文档中保留）。",
    )
    parser.add_argument("--token", default="", help="认证令牌；留空为本地单用户模式（默认授予 read+generate）。")
    parser.add_argument("--log-level", default="INFO", help="日志级别：DEBUG/INFO/WARNING/ERROR，仅写 stderr。")
    parser.add_argument(
        "--scope",
        default=os.getenv("XHZH_MCP_SCOPES", ""),
        help="授予的权限，逗号分隔：read,generate,admin（默认 read,generate；仅作只读演示时可传 read）。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {config.MCP_SERVER_VERSION}")
    args = parser.parse_args(argv)

    token = (args.token or config.MCP_TOKEN or "").strip()
    scopes = _parse_scopes(args.scope) or config.MCP_DEFAULT_SCOPES
    print(
        f"[星火智教 MCP] stdio 就绪 · 模式={config.MODE} · scope={','.join(scopes)} · "
        f"令牌={'已设置' if token else '本地模式'}",
        file=sys.stderr,
    )
    return serve_stdio(token=token, log_level=args.log_level, scopes=scopes)


if __name__ == "__main__":
    raise SystemExit(main())
