"""运行配置：环境变量优先，全部带可运行的默认值。

Notes
-----
密钥一律从环境变量读取，禁止写死在代码里。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` pairs from a .env file into ``os.environ``.

    Existing process environment variables take precedence (``setdefault``),
    so ``XHZH_MODE=mock python app.py`` can still override the file.

    Parameters
    ----------
    path : Path
        Typically ``<project>/.env``; missing file is a no-op.

    Returns
    -------
    None
    """
    if os.getenv("XHZH_SKIP_DOTENV"):
        return  # 测试/CI 需要纯净环境：跳过 .env 注入（先设 XHZH_SKIP_DOTENV=1 再 reload）
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("XHZH_DATA_DIR", BASE_DIR / "data"))
OUTPUT_DIR = DATA_DIR / "packages"
DB_PATH = DATA_DIR / "xinghuo.db"

# WorkBuddy（腾讯云 ADP）凭据占位：真实接入时通过环境变量注入
ADP_SECRET_ID = os.getenv("ADP_SECRET_ID", "")
ADP_SECRET_KEY = os.getenv("ADP_SECRET_KEY", "")

# LLM 调用参数：重试次数（工作流设计 D2：首发 + 重试 ≤2）与单次超时。
# 超时按 PRD"生成 ≤5 分钟"预算取 90s：3 节点 × (1+2) 次 × 90s 为理论最坏值。
# 非法值一律回退默认（与 PORT 同一防护口径），避免 import 期崩溃。
try:
    LLM_MAX_RETRIES = max(0, int(os.getenv("XHZH_LLM_MAX_RETRIES", "2")))
except ValueError:
    LLM_MAX_RETRIES = 2
try:
    LLM_TIMEOUT = float(os.getenv("XHZH_LLM_TIMEOUT", "90"))
    if LLM_TIMEOUT <= 0:
        LLM_TIMEOUT = 90.0
except ValueError:
    LLM_TIMEOUT = 90.0

# 单次输出 token 上限：节点③ 返回"题目+答案+板书"的 JSON，默认值过小会导致
# 输出被截断、JSON 解析失败（症状为 "Expecting ',' delimiter"）。
try:
    LLM_MAX_TOKENS = int(os.getenv("XHZH_LLM_MAX_TOKENS", "4096"))
    if LLM_MAX_TOKENS <= 0:
        LLM_MAX_TOKENS = 4096
except ValueError:
    LLM_MAX_TOKENS = 4096

# 工作流模式：mock（演示）/ llm（知识库检索 + LLM 生成）/ adp（WorkBuddy 接入，待实现）
MODE = os.getenv("XHZH_MODE", "mock").lower()

# mock=1 时等价于 XHZH_MODE=mock（向后兼容旧开关）
if os.getenv("XHZH_MOCK", "1") in ("0", "false", "False") and MODE == "mock":
    MODE = "llm"

# Gradio 公开分享链接（评委远程访问用）：XHZH_SHARE=1 时 launch(share=True)
SHARE = os.getenv("XHZH_SHARE", "0") in ("1", "true", "True")

# 监听端口，可通过环境变量覆盖（默认 7860；非法值回退默认，避免 import 期崩溃）
try:
    PORT = int(os.getenv("XHZH_PORT", "7860"))
except ValueError:
    PORT = 7860

SUBJECTS = ["语文", "数学", "英语"]
GRADES = ["七年级", "八年级", "九年级"]
TEXTBOOK = "人教版"

NODE_NAMES = ["检索与生成", "学情适配", "教具生成", "组装输出"]

# ---------------------------------------------------------------------------
# MCP 服务（仅 stdio 传输；HTTP 仅在设计文档中保留，不实现）
# 所有数值项一律"有默认值 + 非法值回退"，import 期不得崩溃（与 PORT 同口径）。
# ---------------------------------------------------------------------------
MCP_PROTOCOL_VERSION = os.getenv("XHZH_MCP_PROTOCOL_VERSION", "2025-06-18")
# 可接受的历史版本：老客户端仍能握手，未知/更高版本回自身首选版本而不报错
MCP_SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MCP_SERVER_NAME = "xinghuo-zhijiao"
MCP_SERVER_VERSION = "0.1.0"

# 认证令牌：空字符串表示本地单用户模式（默认授予 read + generate）
MCP_TOKEN = os.getenv("XHZH_MCP_TOKEN", "").strip()
MCP_DEFAULT_SCOPES = ("read", "generate")
MCP_ADMIN_SCOPES = ("read", "generate", "admin")

# 单次产物读取上限（字节）：防止客户端把整包 base64 拖垮进程
MCP_MAX_READ_BYTES = 8 * 1024 * 1024
MCP_AUDIT_DIR = DATA_DIR / "audit"

try:
    MCP_MAX_WORKERS = int(os.getenv("XHZH_MCP_MAX_WORKERS", "2"))
    if MCP_MAX_WORKERS < 1:
        MCP_MAX_WORKERS = 1
except ValueError:
    MCP_MAX_WORKERS = 2
try:
    MCP_MAX_QUEUE = int(os.getenv("XHZH_MCP_MAX_QUEUE", "4"))
    if MCP_MAX_QUEUE < 0:
        MCP_MAX_QUEUE = 0
except ValueError:
    MCP_MAX_QUEUE = 4
try:
    MCP_TASK_TIMEOUT = float(os.getenv("XHZH_MCP_TASK_TIMEOUT", "300"))
    if MCP_TASK_TIMEOUT <= 0:
        MCP_TASK_TIMEOUT = 300.0
except ValueError:
    MCP_TASK_TIMEOUT = 300.0
try:
    MCP_RATE_LIMIT_PER_MIN = int(os.getenv("XHZH_MCP_RATE_LIMIT_PER_MIN", "6"))
    if MCP_RATE_LIMIT_PER_MIN < 1:
        MCP_RATE_LIMIT_PER_MIN = 6
except ValueError:
    MCP_RATE_LIMIT_PER_MIN = 6

# 请求级预算（秒）：与设计文档 §G.1 一致
MCP_TIMEOUT_DEFAULT = 2.0
MCP_TIMEOUT_SEARCH = 3.0
MCP_TIMEOUT_READ = 5.0


def ensure_dirs() -> None:
    """Ensure data and output directories exist.

    Returns
    -------
    None
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
