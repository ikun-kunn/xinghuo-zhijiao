"""服务层：任务生命周期、业务编排与异常归类（MCP 与 Gradio 共用入口）。

Notes
-----
本模块**不认识任何协议概念**（没有 JSON-RPC、没有 tools/call），只做三件事：

1. 任务生命周期：落库 ``generations`` / ``packages``，维护内存态取消标记；
2. 业务编排：把 :func:`xhzh.workflow.run_generation` 的生成器包装成可轮询任务；
3. 异常归类：把 ``RuntimeError/LLMError/OSError`` 归类为 :class:`BizCode`。

抽出这一层的目的是让 Gradio 与 MCP 走**同一段编排代码**，避免两套流程漂移
（历史上详情页与知识库的课标口径就曾互相矛盾）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config, kb, storage, workflow
from .mcp.jsonrpc import BizCode, ServiceError

LOGGER = logging.getLogger(__name__)

# 任务表状态：running / done / failed / cancelled
TERMINAL_STATUSES = ("done", "failed", "cancelled")


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Returns
    -------
    str
        e.g. ``2026-10-29T06:12:33Z``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Task:
    """In-memory state for one running generation task.

    Parameters
    ----------
    generation_id : int
        Row id in ``generations``.
    params : dict
        grade / subject / textbook / topic.
    owner : str
        ``sha256(token)[:8]`` 或 ``"local"``，用于审计与限流分桶。
    """

    def __init__(self, generation_id: int, params: dict[str, Any], owner: str) -> None:
        self.generation_id = generation_id
        self.params = params
        self.owner = owner
        self.started_at = time.time()
        self.cancel_requested = False
        self.nodes: list[dict[str, Any]] = []
        self.lock = threading.Lock()


class _Registry:
    """Thread-safe registry of running tasks.

    Notes
    -----
    取消是**协作式**的：``request_cancel`` 只置标记，工作线程在节点边界检查，
    不强行终止线程（Python 无法安全 kill 线程，硬中断需改 workflow 签名）。
    """

    def __init__(self) -> None:
        self._tasks: dict[int, _Task] = {}
        self._lock = threading.Lock()

    def add(self, task: _Task) -> None:
        """Register a running task.

        Parameters
        ----------
        task : _Task

        Returns
        -------
        None
        """
        with self._lock:
            self._tasks[task.generation_id] = task

    def get(self, generation_id: int) -> _Task | None:
        """Look up a running task.

        Parameters
        ----------
        generation_id : int

        Returns
        -------
        _Task or None
        """
        with self._lock:
            return self._tasks.get(generation_id)

    def pop(self, generation_id: int) -> _Task | None:
        """Remove a finished task and return it.

        Parameters
        ----------
        generation_id : int

        Returns
        -------
        _Task or None
        """
        with self._lock:
            return self._tasks.pop(generation_id, None)

    def request_cancel(self, generation_id: int) -> bool:
        """Mark a task for cooperative cancellation.

        Parameters
        ----------
        generation_id : int

        Returns
        -------
        bool
            ``True`` if the task exists and was running.
        """
        task = self.get(generation_id)
        if task is None:
            return False
        with task.lock:
            task.cancel_requested = True
        return True

    def snapshot(self) -> list[int]:
        """List running generation ids.

        Returns
        -------
        list of int
        """
        with self._lock:
            return list(self._tasks)


REGISTRY = _Registry()

# 并发闸：工作线程数 + 排队上限（超限直接 BUSY，不无限排队）
_EXECUTOR = ThreadPoolExecutor(
    max_workers=config.MCP_MAX_WORKERS, thread_name_prefix="xhzh-gen"
)
_QUEUE_SLOT = threading.Semaphore(config.MCP_MAX_WORKERS + config.MCP_MAX_QUEUE)

# 限流：每个 owner 最近 60s 的起任务时间戳
_RATE_HISTORY: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = threading.Lock()


def _check_rate(owner: str) -> None:
    """Enforce the per-owner generation rate limit.

    Parameters
    ----------
    owner : str

    Returns
    -------
    None

    Raises
    ------
    ServiceError
        ``RATE_LIMITED`` with ``retry_after_ms`` when the quota is exhausted.
    """
    now = time.time()
    with _RATE_LOCK:
        bucket = _RATE_HISTORY[owner]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= config.MCP_RATE_LIMIT_PER_MIN:
            retry_after = int(max(1.0, 60 - (now - bucket[0])) * 1000)
            raise ServiceError(
                BizCode.RATE_LIMITED,
                f"触发速率限制：每分钟最多 {config.MCP_RATE_LIMIT_PER_MIN} 次生成",
                retry_after_ms=retry_after,
            )
        bucket.append(now)


def _audit(event: str, **fields: Any) -> None:
    """Append one audit record to ``data/audit/mcp-YYYYMMDD.jsonl``.

    Parameters
    ----------
    event : str
        动作名，如 ``generate.start`` / ``generate.done``。
    **fields : Any
        附加上下文字段（owner / generation_id / tool / code 等）。

    Returns
    -------
    None

    Notes
    -----
    审计永不影响主流程：写失败只降级为 warning。绝不记录密钥。
    """
    try:
        config.MCP_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.MCP_AUDIT_DIR / f"mcp-{datetime.now().strftime('%Y%m%d')}.jsonl"
        record = {"ts": _now_iso(), "event": event}
        record.update(fields)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as error:  # 审计属旁路，失败不影响业务
        LOGGER.warning("audit write failed: %s", error)


def _classify(error: BaseException) -> ServiceError:
    """Map a business exception to :class:`ServiceError`.

    Parameters
    ----------
    error : BaseException

    Returns
    -------
    ServiceError
        Already-classified errors pass through unchanged.
    """
    if isinstance(error, ServiceError):
        return error
    text = str(error)
    if "adp 模式未接入" in text or "LLM 未配置" in text:
        return ServiceError(BizCode.MODE_UNAVAILABLE, text)
    if "finish_reason=length" in text or "输出被截断" in text:
        return ServiceError(BizCode.INTERNAL, "模型输出被截断，请重试或缩减课题范围")
    if isinstance(error, (TimeoutError,)):
        return ServiceError(BizCode.TIMEOUT, "生成超时，请稍后重试")
    return ServiceError(BizCode.INTERNAL, text or error.__class__.__name__)


def _atomic_drive(
    generation_id: int,
    params: dict[str, Any],
    task: _Task | None,
    client: Any = None,
) -> Iterator[dict[str, Any]]:
    """Drive ``workflow.run_generation`` and persist progress / terminal state.

    Parameters
    ----------
    generation_id : int
        Row id created by the caller.
    params : dict
        grade / subject / textbook / topic.
    task : _Task or None
        In-memory task for cancellation checks; ``None`` disables cancellation.
    client : xhzh.adp_client.AdpClient or None
        Injected workflow client (tests use mock).

    Yields
    ------
    dict
        Workflow progress events, then the final ``{"type": "done"}`` event.

    Raises
    ------
    ServiceError
        On cancellation, task timeout, or classified business failure. The
        failed/cancelled status is written **before** raising, so客户端轮询
        能看到终态（也是 Gradio 失败 UI 的来源）。
    """
    started = time.time()
    plan_text = ""
    nodes_seen: list[dict[str, Any]] = []
    try:
        for event in workflow.run_generation(
            params["grade"], params["subject"], params["textbook"], params["topic"], client
        ):
            if task is not None and task.cancel_requested:
                raise ServiceError(BizCode.TIMEOUT, "任务已被取消")
            if time.time() - started > config.MCP_TASK_TIMEOUT:
                raise ServiceError(
                    BizCode.TIMEOUT,
                    f"生成超过 {int(config.MCP_TASK_TIMEOUT)} 秒预算，已终止",
                )
            if event.get("type") == "progress":
                node = {
                    "node": event.get("node"),
                    "name": event.get("name"),
                    "status": event.get("status"),
                    "elapsed_s": event.get("elapsed"),
                }
                # 同节点 running → done 覆盖为最新一条，其余节点保留（否则只留最后一节点）
                nodes_seen = [n for n in nodes_seen if n["node"] != node["node"]] + [node]
                if task is not None:
                    with task.lock:
                        task.nodes = list(nodes_seen)
                storage.update_generation_status(
                    generation_id, "running", detail={"nodes": nodes_seen}
                )
            elif event.get("type") == "done":
                result = event.get("result") or {}
                package = result.get("package") or {}
                if package:
                    storage.save_package(generation_id, package)
                plan_text = workflow.render_plan_text(result)
                storage.update_generation_status(
                    generation_id,
                    "done",
                    detail={
                        "elapsed_s": round(time.time() - started, 1),
                        "plan_text": plan_text,
                        "warnings": result.get("rural_warnings") or [],
                    },
                )
                _audit("generate.done", generation_id=generation_id, elapsed_s=round(time.time() - started, 1))
            yield event
    except ServiceError as error:
        cancelled = "取消" in error.message
        status = "cancelled" if cancelled else "failed"
        storage.update_generation_status(
            generation_id,
            status,
            detail={"error": error.message, "error_code": error.code},
        )
        _audit(f"generate.{status}", generation_id=generation_id, code=error.code)
        raise
    except BaseException as raw_error:  # noqa: BLE001 - 统一归类后重抛
        classified = _classify(raw_error)
        storage.update_generation_status(
            generation_id,
            "failed",
            detail={"error": classified.message, "error_code": classified.code},
        )
        _audit("generate.failed", generation_id=generation_id, code=classified.code)
        # 带堆栈，便于定位"只读数据库/权限/磁盘"类环境问题
        LOGGER.exception("task %s failed: %s", generation_id, classified.message)
        raise classified from raw_error


def stream_generation(
    params: dict[str, Any], client: Any = None, owner: str = "local"
) -> Iterator[dict[str, Any]]:
    """Run one generation synchronously as an event stream (Gradio 入口).

    Parameters
    ----------
    params : dict
        grade / subject / textbook / topic.
    client : xhzh.adp_client.AdpClient or None
    owner : str
        审计与限流分桶标识。

    Yields
    ------
    dict
        首事件 ``{"type": "started", "generation_id": N}``，随后是工作流的
        progress/done 事件（与 :func:`xhzh.workflow.run_generation` 同构）。

    Raises
    ------
    ServiceError
        任一业务失败；失败记录已先行落库。
    """
    _check_rate(owner)
    if not _QUEUE_SLOT.acquire(blocking=False):
        raise ServiceError(BizCode.BUSY, "并发任务已满，请稍后重试", retry_after_ms=3000)
    generation_id = storage.save_generation(params, status="running", detail={"owner": owner})
    task = _Task(generation_id, params, owner)
    REGISTRY.add(task)
    _audit("generate.start", generation_id=generation_id, owner=owner, topic=params.get("topic"))
    try:
        yield {"type": "started", "generation_id": generation_id}
        for event in _atomic_drive(generation_id, params, task, client):
            yield event
    finally:
        REGISTRY.pop(generation_id)
        _QUEUE_SLOT.release()


def start_generation(params: dict[str, Any], owner: str = "local") -> dict[str, Any]:
    """Start a generation task in the background and return immediately.

    Parameters
    ----------
    params : dict
        grade / subject / textbook / topic.
    owner : str

    Returns
    -------
    dict
        ``{generation_id, status, created_at, poll_after_ms, mode, nodes}``.

    Raises
    ------
    ServiceError
        ``RATE_LIMITED`` / ``BUSY`` / ``MODE_UNAVAILABLE``.
    """
    _check_rate(owner)
    if not _QUEUE_SLOT.acquire(blocking=False):
        raise ServiceError(BizCode.BUSY, "并发任务已满，请稍后重试", retry_after_ms=3000)
    generation_id = storage.save_generation(params, status="running", detail={"owner": owner})
    task = _Task(generation_id, params, owner)
    REGISTRY.add(task)
    _audit("generate.start", generation_id=generation_id, owner=owner, topic=params.get("topic"))

    def _worker() -> None:
        try:
            for _ in _atomic_drive(generation_id, params, task, None):
                pass
        except ServiceError:
            pass  # 终态已由 _atomic_drive 落库
        finally:
            REGISTRY.pop(generation_id)
            _QUEUE_SLOT.release()

    try:
        _EXECUTOR.submit(_worker)
    except RuntimeError as error:  # 执行器已关闭
        REGISTRY.pop(generation_id)
        _QUEUE_SLOT.release()
        raise ServiceError(BizCode.INTERNAL, f"任务提交失败：{error}") from error

    record = storage.get_generation(generation_id) or {}
    created_at = _iso_from_sql(record.get("created_at"))
    return {
        "generation_id": generation_id,
        "status": "running",
        "created_at": created_at,
        "poll_after_ms": 1500,
        "mode": config.MODE,
        "nodes": list(config.NODE_NAMES),
    }


def _iso_from_sql(value: Any) -> str:
    """Convert a SQLite ``CURRENT_TIMESTAMP`` value to ISO-8601 UTC.

    Parameters
    ----------
    value : Any
        ``"2026-10-29 06:12:33"`` or ``None``.

    Returns
    -------
    str
        ISO string with ``Z`` suffix; falls back to now when unparsable.
    """
    if not value:
        return _now_iso()
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return _now_iso()


def get_generation_status(generation_id: int, include_plan: bool = False) -> dict[str, Any]:
    """Return the current status, node progress and (optionally) plan preview.

    Parameters
    ----------
    generation_id : int
    include_plan : bool
        为 True 且任务已完成时附带 ``plan_preview``（Markdown 教案预览）。

    Returns
    -------
    dict
        ``{generation_id, status, topic, grade, subject, textbook, nodes,
        created_at, elapsed_s?, package?, error?, plan_preview?}``.

    Raises
    ------
    ServiceError
        ``NOT_FOUND`` when the id does not exist.
    """
    record = storage.get_generation(generation_id)
    if record is None:
        raise ServiceError(BizCode.NOT_FOUND, f"生成任务 #{generation_id} 不存在")
    detail = record.get("detail") or {}
    nodes = detail.get("nodes") or []
    payload: dict[str, Any] = {
        "generation_id": record["id"],
        "status": record["status"],
        "topic": record["topic"],
        "grade": record["grade"],
        "subject": record["subject"],
        "textbook": record["textbook"],
        "nodes": nodes,
        "created_at": _iso_from_sql(record.get("created_at")),
    }
    if detail.get("elapsed_s") is not None:
        payload["elapsed_s"] = detail["elapsed_s"]
    if record["status"] == "done":
        package = storage.get_package(record["id"])
        if package:
            payload["package"] = {
                "docx": _file_meta(record["id"], package["docx"], "docx"),
                "pptx": _file_meta(record["id"], package["pptx"], "pptx"),
                "zip": _file_meta(record["id"], package["zip"], "zip"),
            }
        if include_plan and detail.get("plan_text"):
            payload["plan_preview"] = detail["plan_text"]
    if record["status"] in ("failed", "cancelled"):
        payload["error"] = {
            "code": detail.get("error_code") or BizCode.INTERNAL,
            "message": detail.get("error") or "生成失败",
            "retryable": detail.get("error_code") in (BizCode.TIMEOUT, BizCode.INTERNAL, BizCode.BUSY),
        }
    return payload


def cancel_generation(generation_id: int) -> dict[str, Any]:
    """Request cooperative cancellation of a running task.

    Parameters
    ----------
    generation_id : int

    Returns
    -------
    dict
        ``{generation_id, accepted, status}``；对已终态任务 ``accepted=False``。

    Raises
    ------
    ServiceError
        ``NOT_FOUND`` when the id does not exist.
    """
    record = storage.get_generation(generation_id)
    if record is None:
        raise ServiceError(BizCode.NOT_FOUND, f"生成任务 #{generation_id} 不存在")
    accepted = REGISTRY.request_cancel(generation_id)
    _audit("generate.cancel", generation_id=generation_id, accepted=accepted)
    return {
        "generation_id": generation_id,
        "accepted": accepted,
        "status": record["status"],
        "note": "取消在节点边界生效，最坏需等待当前节点结束" if accepted else "任务已结束或不在运行中",
    }


def list_generations(limit: int = 10) -> dict[str, Any]:
    """List recent generation records.

    Parameters
    ----------
    limit : int
        1~50，超出按上限截断。

    Returns
    -------
    dict
        ``{"items": [{generation_id, grade, subject, topic, status, created_at}], "count"}``.
    """
    limit = max(1, min(int(limit), 50))
    rows = storage.list_recent_generations(limit)
    items = [
        {
            "generation_id": row[0],
            "grade": row[1],
            "subject": row[2],
            "topic": row[3],
            "status": row[4],
            "created_at": _iso_from_sql(row[5]),
        }
        for row in rows
    ]
    return {"items": items, "count": len(items)}


def _safe_package_path(generation_id: int, kind: str) -> Path:
    """Resolve one package file with a strict whitelist check.

    Parameters
    ----------
    generation_id : int
    kind : str
        ``docx`` / ``pptx`` / ``zip``.

    Returns
    -------
    Path
        Existing file inside ``config.OUTPUT_DIR``.

    Raises
    ------
    ServiceError
        ``NOT_FOUND`` when the task has no package, the kind is unknown, or
        the stored path escapes the output directory (path traversal).
    """
    if kind not in ("docx", "pptx", "zip"):
        raise ServiceError(BizCode.PARAM_INVALID, f"未知产物类型：{kind}（可选 docx/pptx/zip）")
    package = storage.get_package(generation_id)
    if package is None:
        raise ServiceError(BizCode.NOT_FOUND, f"任务 #{generation_id} 暂无产物")
    raw = Path(str(package[kind]))
    try:
        resolved = raw.resolve(strict=False)
        root = config.OUTPUT_DIR.resolve()
    except OSError as error:
        raise ServiceError(BizCode.NOT_FOUND, f"产物路径不可解析：{error}") from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ServiceError(BizCode.NOT_FOUND, "产物不存在或路径非法")
    return resolved


def _file_meta(generation_id: int, raw_path: str, kind: str) -> dict[str, Any]:
    """Build metadata for one package file without leaking absolute paths.

    Parameters
    ----------
    generation_id : int
    raw_path : str
        Stored absolute path.
    kind : str
        ``docx`` / ``pptx`` / ``zip``.

    Returns
    -------
    dict
        ``{kind, relative_path, bytes, sha256, download_uri}``；文件缺失时
        ``exists=False`` 且不含大小与摘要。
    """
    name = Path(raw_path).name
    relative = f"data/packages/{name}"
    uri = f"package://{generation_id}/{kind}"
    try:
        path = Path(raw_path).resolve()
        if not path.is_file():
            return {"kind": kind, "relative_path": relative, "download_uri": uri, "exists": False}
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "kind": kind,
            "relative_path": relative,
            "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
            "download_uri": uri,
            "exists": True,
        }
    except OSError:
        return {"kind": kind, "relative_path": relative, "download_uri": uri, "exists": False}


def get_package(generation_id: int) -> dict[str, Any]:
    """Return package file metadata (relative paths + size + digest).

    Parameters
    ----------
    generation_id : int

    Returns
    -------
    dict
        ``{generation_id, files: {docx, pptx, zip}}``.

    Raises
    ------
    ServiceError
        ``NOT_FOUND`` when the task or its package does not exist.
    """
    record = storage.get_generation(generation_id)
    if record is None:
        raise ServiceError(BizCode.NOT_FOUND, f"生成任务 #{generation_id} 不存在")
    package = storage.get_package(generation_id)
    if package is None:
        raise ServiceError(BizCode.NOT_FOUND, f"任务 #{generation_id} 暂无产物（状态：{record['status']}）")
    return {
        "generation_id": generation_id,
        "files": {kind: _file_meta(generation_id, package[kind], kind) for kind in ("docx", "pptx", "zip")},
    }


def read_package_file(generation_id: int, kind: str) -> dict[str, Any]:
    """Read one package file as base64 (guarded by size and path checks).

    Parameters
    ----------
    generation_id : int
    kind : str
        ``docx`` / ``pptx`` / ``zip``.

    Returns
    -------
    dict
        ``{generation_id, kind, mime, bytes, sha256, base64}``.

    Raises
    ------
    ServiceError
        ``NOT_FOUND`` for missing/invalid path, ``PARAM_INVALID`` for a file
        above ``config.MCP_MAX_READ_BYTES``.
    """
    path = _safe_package_path(generation_id, kind)
    size = path.stat().st_size
    if size > config.MCP_MAX_READ_BYTES:
        raise ServiceError(
            BizCode.PARAM_INVALID,
            f"产物 {size} 字节超过单次读取上限 {config.MCP_MAX_READ_BYTES}，请改用 get_package 取元信息",
        )
    data = path.read_bytes()
    mime = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "zip": "application/zip",
    }[kind]
    return {
        "generation_id": generation_id,
        "kind": kind,
        "mime": mime,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "base64": base64.b64encode(data).decode("ascii"),
    }


def search_knowledge_cards(subject: str, grade: str, topic: str) -> dict[str, Any]:
    """Search knowledge cards for a lesson topic.

    Parameters
    ----------
    subject : str
    grade : str
    topic : str

    Returns
    -------
    dict
        ``{query, count, standard_ref_missing, cards: [{card_id, title, summary,
        standard_ref, uri}]}``.

    Raises
    ------
    ServiceError
        ``MODE_UNAVAILABLE`` when the knowledge base file is missing.
    """
    try:
        cards = kb.retrieve(subject, grade, topic)
    except FileNotFoundError as error:
        raise ServiceError(BizCode.MODE_UNAVAILABLE, f"知识库不可用：{error}") from error
    items = []
    for card in cards:
        items.append(
            {
                "card_id": card.card_id,
                "title": getattr(card, "title", "") or card.card_id,
                "summary": str(card.render())[:400],
                "uri": f"kb://cards/{card.card_id}",
            }
        )
    return {
        "query": {"subject": subject, "grade": grade, "topic": topic},
        "count": len(items),
        "standard_ref_missing": len(items) == 0,
        "cards": items,
    }


def list_catalog() -> dict[str, Any]:
    """Return enumerations used to render client-side forms.

    Returns
    -------
    dict
        ``{grades, subjects, textbook, nodes, courseware_kinds, modes}``.
    """
    from .courseware import KINDS as COURSEWARE_KINDS

    return {
        "grades": list(config.GRADES),
        "subjects": list(config.SUBJECTS),
        "textbook": config.TEXTBOOK,
        "nodes": list(config.NODE_NAMES),
        "courseware_kinds": list(COURSEWARE_KINDS),
        "modes": ["mock", "llm", "adp"],
        "topic_max_length": 60,
    }


def check_system_status() -> dict[str, Any]:
    """Report runtime readiness for diagnostics.

    Returns
    -------
    dict
        ``{server, mode, mode_usable, llm_configured, kb_ready, assets_ready,
        task_timeout_s, max_workers, warnings}``（不含密钥与绝对路径）。
    """
    from . import llm, visuals

    warnings: list[str] = []
    mode_usable = config.MODE in ("mock", "llm")
    if config.MODE == "adp":
        warnings.append("adp 模式尚未接入：需接入 WorkBuddy ADP SDK 后才能生成")
    elif config.MODE == "llm" and not llm.is_configured():
        mode_usable = False
        warnings.append("llm 模式缺少 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 配置")
    kb_ready = False
    try:
        kb_ready = bool(kb._default_kb_path().is_file())  # noqa: SLF001 - 复用同一路径解析
    except OSError:
        kb_ready = False
    if not kb_ready:
        warnings.append("知识库文件缺失，检索将降级为无匹配")
    try:
        assets_ready = bool(list(visuals.ASSET_DIR.glob("*.jpg")))
    except OSError:
        assets_ready = False
    if not assets_ready:
        warnings.append("插画素材库为空，课件将回退为矢量绘制")
    return {
        "server": f"{config.MCP_SERVER_NAME} {config.MCP_SERVER_VERSION}",
        "mode": config.MODE,
        "mode_usable": mode_usable,
        "llm_configured": llm.is_configured(),
        "kb_ready": kb_ready,
        "assets_ready": assets_ready,
        "task_timeout_s": config.MCP_TASK_TIMEOUT,
        "max_workers": config.MCP_MAX_WORKERS,
        "warnings": warnings,
    }
