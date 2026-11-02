"""Serve the final teacher interface and its same-origin HTTP API."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from xhzh import config, kb, service, storage
from xhzh.mcp.jsonrpc import ServiceError

INDEX = Path(os.getenv("XHZH_WEB_INDEX", config.BASE_DIR.parent / "index.html"))
ROUTE = re.compile(r"^/api/generations/([1-9]\d*)(?:/files/(zip|docx|pptx))?$")


def catalog() -> dict:
    """Return only combinations backed by the current knowledge base."""
    topics: dict[str, dict[str, list[str]]] = {}
    for card in kb.load_cards():
        grade = next((g for g, codes in kb.GRADE_CODES.items() if card.grade_code in codes), None)
        subject = next((s for s, code in kb.SUBJECT_CODES.items() if card.subject_code == code), None)
        if grade and subject:
            names = topics.setdefault(grade, {}).setdefault(subject, [])
            if card.name not in names:
                names.append(card.name)
    return {"grades": [g for g in config.GRADES if g in topics],
            "subjects": config.SUBJECTS, "textbook": config.TEXTBOOK, "topics": topics,
            "mode": config.MODE}


def public_status(generation_id: int) -> dict:
    """Return progress and preview without paths or raw service errors."""
    status = service.get_generation_status(generation_id, include_plan=True)
    status.pop("package", None)
    if status.get("error"):
        status["error"] = {"message": "生成未完成，请稍后重试或联系管理员。"}
    if status["status"] == "done":
        try:
            service.get_recent_package_files(generation_id)
        except ServiceError:
            status["downloads"] = {}
        else:
            status["downloads"] = {
                kind: f"/api/generations/{generation_id}/files/{kind}"
                for kind in ("zip", "docx", "pptx")
            }
    return status


class Handler(BaseHTTPRequestHandler):
    """Small local-only HTTP adapter around the existing generation service."""

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: Exception) -> None:
        if isinstance(error, ServiceError):
            code = 404 if error.code == "NOT_FOUND" else 429 if error.code in ("BUSY", "RATE_LIMITED") else 400
            messages = {
                "CONTENT_BLOCKED": "课题未通过自动预检，请更换课题。",
                "BUSY": "当前任务较多，请稍后重试。",
                "RATE_LIMITED": "操作过于频繁，请稍后重试。",
                "NOT_FOUND": "记录或文件不存在，或已超过下载有效期。",
                "MODE_UNAVAILABLE": "生成服务暂不可用，请检查配置。",
            }
            self._json(code, {"error": messages.get(error.code, "请求未完成，请检查选择后重试。")})
        else:
            self.log_error("request failed: %r", error)
            self._json(500, {"error": "服务暂不可用，请稍后重试。"})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                body = INDEX.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/catalog":
                self._json(200, catalog())
            elif path == "/api/history":
                self._json(200, {"items": service.list_recent_downloads(20)})
            elif (match := ROUTE.fullmatch(path)):
                generation_id = int(match.group(1))
                kind = match.group(2)
                if kind:
                    file_path = Path(service.get_recent_package_files(generation_id)["files"][kind])
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(file_path.name))
                    self.send_header("Content-Length", str(file_path.stat().st_size))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    with file_path.open("rb") as source:
                        while chunk := source.read(65536):
                            self.wfile.write(chunk)
                else:
                    self._json(200, public_status(generation_id))
            else:
                self._json(404, {"error": "页面不存在。"})
        except (ServiceError, OSError, ValueError) as error:
            self._error(error)

    def _origin_allowed(self, origin: str) -> bool:
        """
        判断 POST 是否来自本站页面。

        反向代理场景下不能用「Origin == http://{Host}」判定：
        网关会把请求转给容器内服务，Host 头与浏览器看到的 Origin 域名不一定一致
        （实测 CloudStudio Gateway 会把 Host 改写成
        ``3000-<sandbox>.sandbox.cloudstudio.club``，公网域名根本传不到应用层，
        导致合法同源请求被 403 拦下）。

        采用两级判定，任意一条通过即放行：
          1. Sec-Fetch-Site（现代浏览器必带、不可被页面 JS 伪造）：
             same-origin / none 视为本站，cross-site / same-site 视为跨站；
          2. 域名比对：Origin 的 host 与 Host / X-Forwarded-Host /
             XHZH_ALLOWED_ORIGINS 白名单任一一致即视为本站。
        """
        if not origin:
            return True  # 非浏览器客户端（如 curl / 集成调用）不带 Origin
        try:
            origin_host = urlsplit(origin).netloc.lower()
        except ValueError:
            return False
        if not origin_host:
            return False

        # 1) Sec-Fetch-Site 优先
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site:
            return fetch_site in ("same-origin", "none")

        # 2) 域名白名单
        candidates = {
            (self.headers.get("Host") or "").lower(),
            (self.headers.get("X-Forwarded-Host") or "").lower(),
        }
        for extra in (os.getenv("XHZH_ALLOWED_ORIGINS") or "").split(","):
            extra = extra.strip().lower()
            if extra:
                candidates.add(extra)
        candidates.discard("")
        return origin_host in candidates

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/generations":
            return self._json(404, {"error": "接口不存在。"})
        try:
            if not self._origin_allowed(self.headers.get("Origin") or ""):
                return self._json(403, {"error": "仅允许从当前页面发起生成。"})
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return self._json(400, {"error": "请求格式无效。"})
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16384:
                return self._json(400, {"error": "请求内容无效。"})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("invalid payload")
            grade, subject, textbook, topic = (data.get(key) for key in ("grade", "subject", "textbook", "topic"))
            choices = catalog()["topics"].get(grade, {}).get(subject, [])
            if textbook != config.TEXTBOOK or topic not in choices or not isinstance(topic, str) or len(topic) > 60:
                return self._json(400, {"error": "课程选项无效，请刷新页面后重新选择。"})
            started = service.start_generation(
                {"grade": grade, "subject": subject, "textbook": textbook, "topic": topic},
                owner="web",
            )
            self._json(202, {"generation_id": started["generation_id"], "poll_after_ms": started["poll_after_ms"]})
        except (ServiceError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._error(error)


if __name__ == "__main__":
    storage.mark_stale_running_failed()
    # 优先用平台注入的 $PORT（线上部署）；回退 XHZH_WEB_PORT / 7860。
    # 必须绑定 0.0.0.0 —— 只监听 127.0.0.1 时反向代理无法访问容器内服务。
    port = int(os.getenv("PORT") or os.getenv("XHZH_WEB_PORT") or "7860")
    print(f"星火智教最终前端：http://0.0.0.0:{port}/", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
