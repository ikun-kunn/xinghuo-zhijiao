"""``resources/list`` 与 ``resources/read``：知识库、生成记录、产物与审计。

Notes
-----
资源与工具的分工：工具用于**动作**（发起生成、读取产物），资源用于
**可枚举的只读上下文**（客户端可预加载、可引用）。

URI 方案
--------
- ``kb://catalog``               知识库目录（列出全部知识点卡 ID）
- ``kb://cards``                 同上（便于客户端按前缀发现）
- ``kb://cards/{card_id}``       单张知识点卡（Markdown）
- ``xinghuo://generations``      最近生成记录（JSON）
- ``xinghuo://generations/{id}`` 单个任务快照（JSON）
- ``package://{id}/{kind}``      产物二进制（base64 ``blob``，不带 ``text``）
- ``xinghuo://audit/recent``     最近审计记录（需 ``admin`` scope）

``resources/list`` 只列 4 个静态项——按需解析型资源不进列表，避免客户端把
所有生成记录/产物一次性拉进上下文。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .. import config, kb, service
from .jsonrpc import ErrorCode, JsonRpcError

STATIC_RESOURCES = [
    {
        "uri": "kb://catalog",
        "name": "知识点卡目录",
        "description": "列出知识库全部知识点卡 ID 与课标条目，供客户端按需读取单卡。",
        "mimeType": "text/markdown",
    },
    {
        "uri": "kb://cards",
        "name": "知识点卡集合",
        "description": "与 kb://catalog 等价的集合入口（按前缀发现 kb://cards/{card_id}）。",
        "mimeType": "text/markdown",
    },
    {
        "uri": "xinghuo://generations",
        "name": "生成记录",
        "description": "最近 20 条教学包生成记录（JSON），可配合 xinghuo://generations/{id} 查看详情。",
        "mimeType": "application/json",
    },
    {
        "uri": "xinghuo://audit/recent",
        "name": "最近审计记录",
        "description": "最近 50 条 MCP 调用审计（JSONL 摘要），需要 admin scope。",
        "mimeType": "application/json",
    },
]


def list_resources() -> list[dict[str, Any]]:
    """Return the static resource list for ``resources/list``.

    Returns
    -------
    list of dict
        Resource descriptors with ``uri`` / ``name`` / ``description`` / ``mimeType``.
    """
    return [dict(item) for item in STATIC_RESOURCES]


def read_resource(uri: str, scopes: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Read one resource by URI.

    Parameters
    ----------
    uri : str
        Resource URI (see module docstring).
    scopes : tuple or list of str
        Granted scopes; ``xinghuo://audit/recent`` requires ``admin``.

    Returns
    -------
    dict
        ``{"contents": [{"uri", "mimeType", "text" | "blob"}]}``.

    Raises
    ------
    JsonRpcError
        ``-32003`` for insufficient scope, ``-32602`` for unknown URI or
        missing resource (``data.uri`` echoes the request).
    """
    if uri in ("kb://catalog", "kb://cards"):
        return _contents(uri, "text/markdown", _kb_catalog_text())
    if uri.startswith("kb://cards/"):
        card_id = uri[len("kb://cards/"):]
        return _contents(uri, "text/markdown", _kb_card_text(card_id))
    if uri == "xinghuo://generations":
        return _contents(uri, "application/json", json.dumps(service.list_generations(20), ensure_ascii=False))
    if uri.startswith("xinghuo://generations/"):
        raw_id = uri[len("xinghuo://generations/"):]
        try:
            generation_id = int(raw_id)
        except ValueError as error:
            raise JsonRpcError(ErrorCode.INVALID_PARAMS, "invalid generation id", {"uri": uri}) from error
        try:
            payload = service.get_generation_status(generation_id, include_plan=True)
        except service.ServiceError as error:
            raise JsonRpcError(ErrorCode.INVALID_PARAMS, error.message, {"uri": uri}) from error
        return _contents(uri, "application/json", json.dumps(payload, ensure_ascii=False))
    if uri.startswith("package://"):
        return _package_blob(uri)
    if uri == "xinghuo://audit/recent":
        if "admin" not in scopes:
            raise JsonRpcError(
                ErrorCode.FORBIDDEN, "admin scope required", {"uri": uri, "required_scope": "admin"}
            )
        return _contents(uri, "application/json", _audit_tail())
    raise JsonRpcError(ErrorCode.INVALID_PARAMS, "unknown resource uri", {"uri": uri})


def _contents(uri: str, mime: str, text: str) -> dict[str, Any]:
    """Build a text resource payload.

    Parameters
    ----------
    uri : str
    mime : str
    text : str

    Returns
    -------
    dict
    """
    return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}


def _kb_catalog_text() -> str:
    """Render the knowledge-card catalog as Markdown.

    Returns
    -------
    str
    """
    try:
        cards = kb.load_cards()
    except (FileNotFoundError, OSError) as error:
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, f"知识库不可用：{error}", {"uri": "kb://catalog"}) from error
    lines = ["# 知识点卡目录", ""]
    for card in cards:
        lines.append(f"- [{card.card_id}] {getattr(card, 'title', '') or card.card_id}")
    return "\n".join(lines)


def _kb_card_text(card_id: str) -> str:
    """Render one knowledge card as Markdown.

    Parameters
    ----------
    card_id : str

    Returns
    -------
    str

    Raises
    ------
    JsonRpcError
        ``-32602`` when the card does not exist.
    """
    try:
        cards = kb.load_cards()
    except (FileNotFoundError, OSError) as error:
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, f"知识库不可用：{error}", {"uri": f"kb://cards/{card_id}"}) from error
    for card in cards:
        if card.card_id == card_id:
            return card.render()
    raise JsonRpcError(ErrorCode.INVALID_PARAMS, "card not found", {"uri": f"kb://cards/{card_id}"})


def _package_blob(uri: str) -> dict[str, Any]:
    """Read a package file as a base64 ``blob`` resource.

    Parameters
    ----------
    uri : str
        ``package://{generation_id}/{kind}``.

    Returns
    -------
    dict
        ``{"contents": [{"uri", "mimeType", "blob"}]}``（不含 ``text``）.

    Raises
    ------
    JsonRpcError
        ``-32602`` when the URI is malformed or the file is unavailable.
    """
    parts = uri[len("package://"):].split("/")
    if len(parts) != 2:
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "uri must be package://{id}/{kind}", {"uri": uri})
    try:
        generation_id = int(parts[0])
    except ValueError as error:
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "invalid generation id", {"uri": uri}) from error
    try:
        payload = service.read_package_file(generation_id, parts[1])
    except service.ServiceError as error:
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, error.message, {"uri": uri}) from error
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": payload["mime"],
                "blob": payload["base64"],
            }
        ]
    }


def _audit_tail(limit: int = 50) -> str:
    """Return the tail of today's audit log.

    Parameters
    ----------
    limit : int
        Max records.

    Returns
    -------
    str
        JSON array text (empty array when no log exists).
    """
    from datetime import datetime

    path = Path(config.MCP_AUDIT_DIR) / f"mcp-{datetime.now().strftime('%Y%m%d')}.jsonl"
    if not path.is_file():
        return "[]"
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return json.dumps(records, ensure_ascii=False)
