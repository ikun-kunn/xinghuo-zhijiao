"""星火智教 · 演示前自检（preflight）。

Run
---
    python preflight.py          # 全量自检：依赖 / 知识库 / 测试套件 / 端口
    python preflight.py --fast   # 跳过测试套件，只做秒级环境检查

Notes
-----
背景：2026-10-25 曾发生 Python 升级导致 gradio/docx/pptx 依赖被清空、
服务无法启动的环境事故。本脚本把"演示前 30 秒体检"固化为一条命令，
任何一项失败以非零退出码结束，可直接放进 CI 或演示 checklist。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# 自检结果收集：(名称, 是否通过, 说明)
results: list[tuple[str, bool, str]] = []


def check_dependencies() -> None:
    """Verify required third-party packages are importable.

    Returns
    -------
    None
    """
    for name, import_name in [
        ("gradio", "gradio"),
        ("python-docx", "docx"),
        ("python-pptx", "pptx"),
    ]:
        try:
            module = __import__(import_name)
            version = getattr(module, "__version__", "版本未知")
            results.append((f"依赖 {name}", True, str(version)))
        except ImportError as error:
            results.append((f"依赖 {name}", False, f"未安装：{error}；执行 pip install -r requirements.txt"))


def check_python_version() -> None:
    """Require Python 3.9+ (numpydoc / Gradio baseline).

    Returns
    -------
    None
    """
    version = sys.version_info
    ok = version >= (3, 9)
    results.append(("Python 版本", ok, f"{version.major}.{version.minor}.{version.micro}"))


def check_mcp() -> None:
    """Verify the MCP protocol layer imports and its config is not leaking secrets.

    Returns
    -------
    None

    Notes
    -----
    两项检查：① ``xhzh.mcp.server`` 可导入且能取到协议版本（防止打包漏文件）；
    ② ``.env.example`` 中 ``XHZH_MCP_TOKEN`` 必须为空——示例文件曾把真实令牌
    带进仓库的情况在同类项目中并不罕见。
    """
    try:
        from xhzh import config
        from xhzh.mcp import server as mcp_server
        from xhzh.mcp.schema import TOOLS

        ok = mcp_server.Server is not None and bool(config.MCP_PROTOCOL_VERSION)
        results.append(("MCP 协议层", ok, f"{len(TOOLS)} 个工具 · 协议 {config.MCP_PROTOCOL_VERSION}"))
    except Exception as error:  # noqa: BLE001 —— 自检不得因异常崩溃
        results.append(("MCP 协议层", False, f"导入失败：{error}"))
        return
    example = BASE_DIR / ".env.example"
    if not example.is_file():
        results.append(("MCP 令牌示例", False, "缺少 .env.example"))
        return
    content = example.read_text(encoding="utf-8")
    leaked = any(
        line.split("=", 1)[1].strip()
        for line in content.splitlines()
        if line.strip().startswith("XHZH_MCP_TOKEN=")
    )
    results.append(("MCP 令牌示例", not leaked, "XHZH_MCP_TOKEN 未在示例中填写真实值" if not leaked else "示例文件包含真实令牌"))


def check_knowledge_base() -> None:
    """Verify the knowledge-base markdown exists and parses to enough cards.

    Returns
    -------
    None
    """
    try:
        from xhzh import kb

        cards = kb.load_cards()
        ok = len(cards) >= 100
        results.append(("知识库卡片", ok, f"{len(cards)} 张（期望 ≥100）"))
    except FileNotFoundError as error:
        results.append(("知识库卡片", False, str(error)))
    except Exception as error:  # noqa: BLE001 —— 自检必须把异常转化为结果，而不是崩溃
        results.append(("知识库卡片", False, f"解析异常：{error}"))


def check_test_suite() -> None:
    """Run the zero-dependency test suite as a subprocess.

    Returns
    -------
    None
    """
    suite = BASE_DIR / "tests" / "test_suite.py"
    if not suite.exists():
        results.append(("测试套件", False, f"缺失：{suite}"))
        return
    completed = subprocess.run(
        [sys.executable, "-W", "error::UserWarning", str(suite)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(BASE_DIR),
    )
    tail = (completed.stdout or "").strip().splitlines()[-1] if completed.stdout else ""
    results.append(
        ("测试套件", completed.returncode == 0, tail or (completed.stderr or "无输出")[:120])
    )


def check_port(port: int) -> None:
    """Report whether the Gradio port is free (informational).

    Parameters
    ----------
    port : int
        Target port.

    Returns
    -------
    None
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        in_use = sock.connect_ex(("127.0.0.1", port)) == 0
    if in_use:
        results.append((f"端口 {port}", True, "已被占用（服务可能已在运行；如需重启先停旧进程）"))
    else:
        results.append((f"端口 {port}", True, "空闲，可直接 python app.py 启动"))


def main() -> int:
    """Run all preflight checks and print a summary.

    Returns
    -------
    int
        ``0`` when every check passes, ``1`` otherwise.
    """
    fast = "--fast" in sys.argv
    print("=" * 52)
    print("星火智教 · 演示前自检" + ("（快速模式）" if fast else ""))
    print("=" * 52)

    check_python_version()
    check_dependencies()
    check_knowledge_base()
    check_mcp()
    if not fast:
        check_test_suite()
    check_port(int(os.getenv("XHZH_PORT", "7860")))

    failed = 0
    for name, ok, detail in results:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name:14s} {detail}")
        if not ok:
            failed += 1

    print("-" * 52)
    if failed:
        print(f"结论：{failed} 项未通过，请先处理后再演示。")
        return 1
    print("结论：全部通过，可以启动演示。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
