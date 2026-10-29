"""MCP 协议级回归：stdio 握手、工具发现、调用、资源、错误、权限、并发。

Run
---
    XHZH_SKIP_DOTENV=1 XHZH_MODE=mock python tests/test_mcp_suite.py

Notes
-----
与 ``tests/test_suite.py`` 相互独立：本文件用 ``subprocess`` 真正拉起
``mcp_server.py``，逐行收发 JSON-RPC，验证**协议行为**而非内部函数。
所有产物写入临时 ``XHZH_DATA_DIR``，不污染项目 ``data/``。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DIR = tempfile.mkdtemp(prefix="xhzh-mcp-")
os.environ["XHZH_SKIP_DOTENV"] = "1"
os.environ.setdefault("XHZH_MODE", "mock")
os.environ["XHZH_DATA_DIR"] = _TMP_DIR

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS = 0
_FAIL = 0
_ERRORS: list[str] = []


def section(title: str) -> None:
    """Print a section header.

    Parameters
    ----------
    title : str

    Returns
    -------
    None
    """
    print(f"\n=== {title} ===")


def check(name: str, condition: bool, detail: str = "") -> bool:
    """Record one assertion.

    Parameters
    ----------
    name : str
    condition : bool
    detail : str
        Extra context printed on failure.

    Returns
    -------
    bool
        ``condition`` (for chaining).
    """
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        message = f"{name} — {detail}" if detail else name
        _ERRORS.append(message)
        print(f"  ❌ {message}")
    return condition


class McpProcess:
    """A running ``mcp_server.py`` child process speaking JSON-RPC over stdio.

    Parameters
    ----------
    env_extra : dict or None
        Extra environment variables merged over the inherited environment.
    args : list of str or None
        Extra CLI arguments (e.g. ``["--scope", "read"]``).
    """

    def __init__(self, env_extra: dict[str, str] | None = None, args: list[str] | None = None) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, "mcp_server.py", *(args or [])],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._drain_err, daemon=True).start()
        self._next_id = 1

    def _drain_err(self) -> None:
        """Consume stderr in the background (keeps the pipe from filling)."""
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line)

    def send(self, payload: dict) -> None:
        """Write one frame.

        Parameters
        ----------
        payload : dict

        Returns
        -------
        None
        """
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def send_raw(self, raw: str) -> None:
        """Write one raw line (for malformed-JSON tests).

        Parameters
        ----------
        raw : str

        Returns
        -------
        None
        """
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        """Read one frame.

        Returns
        -------
        dict
        """
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        self.stdout_lines.append(line)
        return json.loads(line) if line.strip() else {}

    def request(self, method: str, params: dict | None = None) -> dict:
        """Send a request and return its response.

        Parameters
        ----------
        method : str
        params : dict or None

        Returns
        -------
        dict
        """
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)
        return self.recv()

    def call(self, name: str, arguments: dict, progress_token: str | None = None) -> dict:
        """Invoke a tool and return the raw response frame.

        Parameters
        ----------
        name : str
        arguments : dict
        progress_token : str or None

        Returns
        -------
        dict
        """
        params: dict = {"name": name, "arguments": arguments}
        if progress_token is not None:
            params["_meta"] = {"progressToken": progress_token}
        return self.request("tools/call", params)

    def initialize(self, version: str = "2025-06-18") -> dict:
        """Perform the handshake.

        Parameters
        ----------
        version : str

        Returns
        -------
        dict
            ``initialize`` result.
        """
        response = self.request(
            "initialize",
            {"protocolVersion": version, "clientInfo": {"name": "xhzh-test", "version": "1.0"}},
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response.get("result", {})

    def close(self) -> int:
        """Close stdin and wait for exit.

        Returns
        -------
        int
            Exit code.
        """
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()
            return -1


def _wait_done(client: McpProcess, generation_id: int, include_plan: bool = False, limit: int = 100) -> dict:
    """Poll until the task leaves the ``running`` state.

    Parameters
    ----------
    client : McpProcess
    generation_id : int
    include_plan : bool
    limit : int
        Max polls.

    Returns
    -------
    dict
        Final ``structuredContent`` of ``get_generation_status``.
    """
    status: dict = {}
    for _ in range(limit):
        response = client.call(
            "get_generation_status",
            {"generation_id": generation_id, "include_plan": include_plan},
        )
        status = response["result"]["structuredContent"]
        if status["status"] != "running":
            return status
        time.sleep(0.2)
    return status


def test_handshake() -> None:
    """J-1 / J-2 / J-9 / J-10 / J-30 / J-31：握手、能力、纯净 stdout、未知版本。"""
    section("握手与传输")
    client = McpProcess()
    # J-10 未初始化直接调用
    early = client.request("tools/list")
    check("J-10 未初始化调用返回 -32002", early.get("error", {}).get("code") == -32002, str(early))

    result = client.initialize()
    check("J-1 protocolVersion 协商", result.get("protocolVersion") == "2025-06-18", str(result)[:120])
    capabilities = result.get("capabilities") or {}
    check(
        "J-1 capabilities 精确匹配",
        capabilities
        == {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "logging": {},
        },
        str(capabilities),
    )
    check("J-1 不声明 prompts/sampling", "prompts" not in capabilities and "sampling" not in capabilities)
    check("J-1 serverInfo 名称", (result.get("serverInfo") or {}).get("name") == "xinghuo-zhijiao")

    # J-9 非法 JSON 后仍可 ping
    client.send_raw("{not json}")
    parse_error = client.recv()
    check("J-9 非法 JSON 返回 -32700", parse_error.get("error", {}).get("code") == -32700, str(parse_error))
    check("J-9 错误 id 为 null", parse_error.get("id") is None)
    check("J-9 之后 ping 仍可用", client.request("ping").get("result") == {})

    # 未知/更高版本不报错，回自身首选
    other = McpProcess()
    future = other.initialize("2099-01-01")
    check("未知版本回本端首选", future.get("protocolVersion") == "2025-06-18", str(future)[:80])
    other.close()

    check("J-31 进程存活", client.proc.poll() is None)
    exit_code = client.close()
    check("J-31 EOF 退出码 0", exit_code == 0, str(exit_code))
    # J-30 stdout 纯净：每一行都是合法 JSON
    ok = True
    for line in client.stdout_lines:
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            ok = False
            break
    check("J-30 stdout 每行均为合法 JSON", ok, str(client.stdout_lines[:2]))


def test_discovery() -> None:
    """J-3 / J-4 / J-15：工具与资源发现。"""
    section("能力发现")
    client = McpProcess()
    client.initialize()
    tools = client.request("tools/list")["result"]["tools"]
    names = [tool["name"] for tool in tools]
    check("J-3 工具数量为 9", len(tools) == 9, str(names))
    check(
        "J-3 工具名完整",
        set(names)
        == {
            "generate_teaching_package",
            "get_generation_status",
            "cancel_generation",
            "list_generations",
            "get_package",
            "read_package_file",
            "search_knowledge_cards",
            "list_catalog",
            "check_system_status",
        },
        str(names),
    )
    schemas_ok = all(
        tool["inputSchema"].get("$schema") == "http://json-schema.org/draft-07/schema#"
        and tool["inputSchema"].get("additionalProperties") is False
        and "description" in tool
        for tool in tools
    )
    check("J-3 schema 为 draft-07 且封闭", schemas_ok)
    gen = next(tool for tool in tools if tool["name"] == "generate_teaching_package")
    enums = [key for key, spec in gen["inputSchema"]["properties"].items() if "enum" in spec]
    check("J-3 生成参数含 enum", set(enums) == {"grade", "subject", "textbook"}, str(enums))

    catalog = client.call("list_catalog", {})["result"]["structuredContent"]
    check("J-4 年级枚举", catalog["grades"] == ["七年级", "八年级", "九年级"], str(catalog["grades"]))
    check("J-4 学科枚举", catalog["subjects"] == ["语文", "数学", "英语"], str(catalog["subjects"]))
    check("J-4 节点数为 4", len(catalog["nodes"]) == 4)
    check("J-4 课件页型为 10", len(catalog["courseware_kinds"]) == 10, str(len(catalog["courseware_kinds"])))

    resources = client.request("resources/list")["result"]["resources"]
    uris = {item["uri"] for item in resources}
    check(
        "J-15 静态资源 4 项",
        uris == {"kb://catalog", "kb://cards", "xinghuo://generations", "xinghuo://audit/recent"},
        str(uris),
    )
    check("J-15 资源均带 mimeType", all(item.get("mimeType") for item in resources))
    client.close()


def test_generation_flow() -> str:
    """J-5 / J-11 / J-12 / J-13 / J-16 / J-17：生成全链路与产物读取。

    Returns
    -------
    str
        完成的 ``generation_id``（供后续用例复用）。
    """
    section("生成全链路")
    client = McpProcess()
    client.initialize()
    started = time.time()
    response = client.call(
        "generate_teaching_package",
        {"grade": "七年级", "subject": "数学", "topic": "一元一次方程的实际应用"},
    )
    elapsed = time.time() - started
    result = response["result"]
    check("J-5 生成非错误", result.get("isError") is False, str(result)[:160])
    payload = result["structuredContent"]
    check("J-5 立即返回 generation_id", isinstance(payload.get("generation_id"), int), str(payload))
    check("J-5 起任务耗时 ≤2s", elapsed <= 2.0, f"{elapsed:.2f}s")
    check("J-5 返回轮询建议", payload.get("poll_after_ms", 0) > 0)
    generation_id = payload["generation_id"]

    final = _wait_done(client, generation_id, include_plan=True)
    check("J-11 任务完成", final["status"] == "done", str(final.get("error")))
    check("J-11 四节点全 done", len(final["nodes"]) == 4 and all(n["status"] == "done" for n in final["nodes"]))
    check("J-11 含耗时", all(n.get("elapsed_s") is not None for n in final["nodes"]))
    check("J-11 教案预览含标题", "# " in final.get("plan_preview", "") or "课标依据" in final.get("plan_preview", ""))

    package = client.call("get_package", {"generation_id": generation_id})["result"]["structuredContent"]
    files = package["files"]
    check("J-12 三件产物齐全", set(files) == {"docx", "pptx", "zip"} and all(f.get("exists") for f in files.values()))
    check("J-12 相对路径前缀正确", all(str(f["relative_path"]).startswith("data/packages/") for f in files.values()))
    check("J-12 摘要为 64 位", all(len(str(f["sha256"])) == 64 for f in files.values()))
    check("J-12 download_uri 形式", files["zip"]["download_uri"] == f"package://{generation_id}/zip")
    raw = json.dumps(package, ensure_ascii=False)
    check("J-12 不泄露绝对路径", "C:" not in raw and "\\\\" not in raw and "/home/" not in raw, raw[:120])

    read = client.call("read_package_file", {"generation_id": generation_id, "kind": "docx"})["result"]["structuredContent"]
    check("J-13 base64 长度自洽", len(read["base64"]) > 0 and read["bytes"] == files["docx"]["bytes"])
    import base64

    check("J-13 base64 可解码且字节一致", len(base64.b64decode(read["base64"])) == read["bytes"])
    check("J-13 sha256 与元信息一致", read["sha256"] == files["docx"]["sha256"])

    card = client.request("resources/read", {"uri": "kb://cards/MAT-7A-3-0-K01"})["result"]["contents"][0]
    check("J-16 知识点卡为 Markdown", card["mimeType"] == "text/markdown" and "[MAT-7A-3-0-K01]" in card["text"])

    blob = client.request("resources/read", {"uri": f"package://{generation_id}/zip"})["result"]["contents"][0]
    check("J-17 产物为 blob 且无 text", "blob" in blob and "text" not in blob and blob["mimeType"] == "application/zip")

    missing = client.request("resources/read", {"uri": "kb://cards/NOPE-0-0-0-K00"})
    check("J-18 不存在的卡片返回 -32602", missing.get("error", {}).get("code") == -32602, str(missing)[:120])
    check("J-18 回显 uri", str(missing.get("error", {}).get("data", {}).get("uri", "")).endswith("NOPE-0-0-0-K00"))
    client.close()
    return generation_id


def test_errors_and_permissions() -> None:
    """J-6 / J-7 / J-8 / J-14 / J-19 / J-20 / J-26 / J-27：错误分流与权限。"""
    section("错误与权限")
    client = McpProcess()
    client.initialize()
    unknown_method = client.request("tools/bogus")
    check("J-6 未知 method 为 -32601", unknown_method.get("error", {}).get("code") == -32601, str(unknown_method))
    unknown_tool = client.call("no_such_tool", {})
    check("J-7 未知 tool 为 -32602", unknown_tool.get("error", {}).get("code") == -32602, str(unknown_tool))
    check("J-7 回显 tool 名", unknown_tool.get("error", {}).get("data", {}).get("tool") == "no_such_tool")

    bad = client.call("generate_teaching_package", {"grade": "十年级", "subject": "数学", "topic": ""})
    violations = bad.get("error", {}).get("data", {}).get("violations", [])
    pointers = {item["pointer"] for item in violations}
    check("J-8 非法参数为 -32602", bad.get("error", {}).get("code") == -32602, str(bad)[:120])
    check("J-8 同时列出 grade 与 topic", {"/grade", "/topic"} <= pointers, str(pointers))

    # J-14 路径穿越：直接往库里塞一条越界路径
    from xhzh import storage

    params = {"grade": "七年级", "subject": "数学", "textbook": "人教版", "topic": "穿越探针"}
    generation_id = storage.save_generation(params, status="running")
    storage.save_package(generation_id, {"zip": "../../etc/passwd", "docx": "../../etc/passwd", "pptx": "../../etc/passwd"})
    traversal = client.call("read_package_file", {"generation_id": generation_id, "kind": "zip"})
    check("J-14 越界路径被拒", traversal["result"].get("isError") is True, str(traversal)[:140])
    check(
        "J-14 错误码为 NOT_FOUND",
        traversal["result"]["structuredContent"]["error"]["code"] == "NOT_FOUND",
        str(traversal["result"]["structuredContent"]),
    )
    check("J-14 未返回文件内容", "base64" not in traversal["result"]["structuredContent"])

    status = client.call("check_system_status", {})["result"]["structuredContent"]
    check("J-26 诊断含 mode_usable", "mode_usable" in status and status["mode_usable"] is True, str(status)[:120])
    check("诊断不含密钥", "api_key" not in json.dumps(status).lower() and "sk-" not in json.dumps(status))

    search = client.call("search_knowledge_cards", {"subject": "数学", "grade": "七年级", "topic": "一元一次方程"})
    check("J-22 检索在预算内返回", search["result"].get("isError") is False)
    check("检索命中知识点卡", search["result"]["structuredContent"]["count"] >= 1)
    client.close()

    # J-19 只读凭据调用生成 → FORBIDDEN
    readonly = McpProcess(args=["--scope", "read"])
    readonly.initialize()
    denied = readonly.call("generate_teaching_package", {"grade": "七年级", "subject": "数学", "topic": "只读探针"})
    payload = denied["result"]["structuredContent"]
    check("J-19 只读凭据被拒", denied["result"]["isError"] is True, str(payload)[:120])
    check("J-19 业务码 FORBIDDEN", payload["error"]["code"] == "FORBIDDEN", str(payload))
    check("J-19 携带协议码 -32003", payload["error"].get("jsonrpc_code") == -32003, str(payload))
    audit_denied = readonly.request("resources/read", {"uri": "xinghuo://audit/recent"})
    check("J-20 审计资源需 admin", audit_denied.get("error", {}).get("code") == -32003, str(audit_denied))
    readonly.close()

    # J-26 adp 模式不可用
    adp = McpProcess(env_extra={"XHZH_MODE": "adp"})
    adp.initialize()
    started = adp.call("generate_teaching_package", {"grade": "七年级", "subject": "数学", "topic": "adp 探针"})
    if started["result"].get("isError"):
        check("J-26 adp 模式起任务即失败", started["result"]["structuredContent"]["error"]["code"] == "MODE_UNAVAILABLE")
    else:
        final = _wait_done(adp, started["result"]["structuredContent"]["generation_id"])
        check("J-26 adp 模式任务失败", final["status"] == "failed", str(final)[:120])
        check("J-26 错误码 MODE_UNAVAILABLE", final.get("error", {}).get("code") == "MODE_UNAVAILABLE", str(final.get("error")))
    adp_status = adp.call("check_system_status", {})["result"]["structuredContent"]
    check("J-26 诊断标记不可用", adp_status["mode_usable"] is False and adp_status["warnings"])
    adp.close()

    # J-27 知识库缺失
    nokb = McpProcess(env_extra={"XHZH_KB_PATH": str(Path(_TMP_DIR) / "no-such-kb.md")})
    nokb.initialize()
    kb_error = nokb.call("search_knowledge_cards", {"subject": "数学", "grade": "七年级", "topic": "任意课题"})
    payload = kb_error["result"]
    check("J-27 知识库缺失不崩进程", nokb.proc.poll() is None)
    check(
        "J-27 返回 MODE_UNAVAILABLE 或空结果",
        payload.get("isError") is True and payload["structuredContent"]["error"]["code"] == "MODE_UNAVAILABLE"
        or payload.get("structuredContent", {}).get("count") == 0,
        str(payload)[:140],
    )
    nokb.close()


def test_concurrency_and_cancel() -> None:
    """J-23 / J-24 / J-25：取消、并发生成、并发闸。"""
    section("并发与取消")
    client = McpProcess()
    client.initialize()
    first = client.call("generate_teaching_package", {"grade": "七年级", "subject": "数学", "topic": "并发甲"})
    time.sleep(0.05)
    second = client.call("generate_teaching_package", {"grade": "八年级", "subject": "数学", "topic": "并发乙"})
    id_a = first["result"]["structuredContent"]["generation_id"]
    id_b = second["result"]["structuredContent"]["generation_id"]
    check("J-24 并发生成返回不同 id", id_a != id_b, f"{id_a} vs {id_b}")
    final_a = _wait_done(client, id_a)
    final_b = _wait_done(client, id_b)
    check("J-24 两个任务均完成", final_a["status"] == "done" and final_b["status"] == "done", f"{final_a['status']}/{final_b['status']}")

    # 取消：先起一个任务，立刻取消
    started = client.call("generate_teaching_package", {"grade": "七年级", "subject": "数学", "topic": "取消探针"})
    cancel_id = started["result"]["structuredContent"]["generation_id"]
    cancelled = client.call("cancel_generation", {"generation_id": cancel_id})["result"]["structuredContent"]
    check("J-23 取消返回 accepted 字段", "accepted" in cancelled, str(cancelled))
    final = _wait_done(client, cancel_id)
    check("J-23 取消后到达终态", final["status"] in ("cancelled", "done"), final["status"])
    again = client.call("cancel_generation", {"generation_id": cancel_id})["result"]["structuredContent"]
    check("J-23 重复取消幂等", again["accepted"] is False, str(again))
    client.close()

    # J-25 并发闸：1 个 worker + 0 队列，第二个必然 BUSY
    busy = McpProcess(env_extra={"XHZH_MCP_MAX_WORKERS": "1", "XHZH_MCP_MAX_QUEUE": "0"})
    busy.initialize()
    busy.call("generate_teaching_package", {"grade": "七年级", "subject": "数学", "topic": "闸门甲"})
    rejected = busy.call("generate_teaching_package", {"grade": "七年级", "subject": "语文", "topic": "闸门乙"})
    payload = rejected["result"]
    check("J-25 超限返回 BUSY", payload.get("isError") is True and payload["structuredContent"]["error"]["code"] == "BUSY", str(payload)[:140])
    check("J-25 给出重试间隔", payload["structuredContent"]["error"].get("retry_after_ms", 0) > 0)
    busy.close()


def test_progress_notifications() -> None:
    """J-28 / J-29：progressToken 有则推送、无则静默。"""
    section("进度通知")
    client = McpProcess()
    client.initialize()
    response = client.call(
        "generate_teaching_package",
        {"grade": "七年级", "subject": "数学", "topic": "进度探针"},
        progress_token="p1",
    )
    generation_id = response["result"]["structuredContent"]["generation_id"]
    received: list[dict] = []
    deadline = time.time() + 20
    while time.time() < deadline:
        frame = client.recv()
        if frame.get("method") == "notifications/progress":
            received.append(frame["params"])
            if frame["params"].get("progress") == 100:
                break
    check("J-28 收到进度通知", bool(received), str(received[:3]))
    check("J-28 token 原样回显", all(item.get("progressToken") == "p1" for item in received))
    check("J-28 进度递增到 100", received and received[-1]["progress"] == 100, str(received[-1] if received else None))
    client.close()

    silent = McpProcess()
    silent.initialize()
    silent.call("generate_teaching_package", {"grade": "七年级", "subject": "数学", "topic": "静默探针"})
    silent.send({"jsonrpc": "2.0", "id": 999, "method": "ping"})
    frame = silent.recv()
    check("J-29 未给 token 不推送进度", frame.get("id") == 999 and "method" not in frame, str(frame)[:120])
    silent.close()


def main() -> int:
    """Run the whole MCP protocol regression.

    Returns
    -------
    int
        Process exit code (0 = all passed).
    """
    print(f"临时数据目录：{_TMP_DIR}")
    test_handshake()
    test_discovery()
    test_errors_and_permissions()
    test_concurrency_and_cancel()
    test_progress_notifications()
    try:
        test_generation_flow()
    except Exception as error:  # noqa: BLE001 - 任一链路异常都要计入失败
        check("生成全链路执行成功", False, repr(error))

    print("\n" + "=" * 46)
    print(f"总计 {_PASS + _FAIL} 项：✅ {_PASS} 通过 / ❌ {_FAIL} 失败")
    if _FAIL:
        print("\n失败项：")
        for item in _ERRORS:
            print(f"  - {item}")
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
