"""星火智教 · Gradio 前端入口（赛事版技术栈 · 路径 A）。

Run
---
    python app.py                 # .env 配置的模式（默认 llm；无 .env 时为 mock），http://127.0.0.1:7860
    XHZH_MODE=mock python app.py  # 强制演示兜底（不调用外部服务）

页面结构（简洁主线）：左栏输入表单；右栏自上而下为四节点步骤条 → 教案预览
→ 下载教学包（生成完成后自动展开）→ 生成明细与历史记录（默认收起）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

import gradio as gr

from xhzh import config, content_review, kb, service, storage
from xhzh.adp_client import AdpClient
from xhzh.workflow import render_plan_text

# ---------------------------------------------------------------------------
# 演示用快捷课题（均已验证可命中知识库；三科各一，点击即填）
# ---------------------------------------------------------------------------
EXAMPLES: list[list[str]] = [
    ["七年级", "数学", "一元一次方程的实际应用"],
    ["七年级", "语文", "比喻拟人修辞"],
    ["九年级", "英语", "宾语从句"],
]

_MODE_BADGE = {
    "mock": ("演示模式 · 未接大模型", "#a85a22", "#fff1df"),
    "llm": ("真实生成模式 · 知识库检索增强", "#35684d", "#eaf3eb"),
    "adp": ("ADP 模式 · 待接入", "#526879", "#edf2f5"),
}

_STEP_ICONS = {"pending": "⚪", "running": "🔄", "done": "✅", "failed": "❌"}

_CUSTOM_CSS = """
footer {display: none !important;}
.gradio-container {
    max-width: 1180px !important;
    margin: 0 auto !important;
    padding: 24px clamp(14px, 3vw, 36px) 40px !important;
    background: #f7f5f0;
}
body {background: #f7f5f0 !important;}
#app-header {
    margin: 0 0 22px;
    padding: 18px 22px 16px;
    background: linear-gradient(115deg, #fffdf9 0%, #f6efe2 100%);
    border: 1px solid #eadfce;
    border-radius: 16px;
    box-shadow: 0 5px 18px rgba(76, 58, 34, .06);
}
.brand-lockup {display: flex; flex-wrap: wrap; align-items: center; gap: 4px;}
.brand-title {font-size: 23px; font-weight: 800; color: #315342; letter-spacing: .02em;}
.brand-sub {font-size: 13px; color: #69766d; margin: 7px 0 0; line-height: 1.7;}
.badge {
    display: inline-block; padding: 5px 11px; border-radius: 999px;
    font-size: 12px; font-weight: 600; margin-left: 0; vertical-align: 3px;
}
.form-col, .result-col {
    background: #fff;
    border: 1px solid #e8e4db;
    border-radius: 16px;
    padding: 20px !important;
    box-shadow: 0 5px 20px rgba(57, 66, 52, .055);
}
.form-col {position: sticky; top: 18px; align-self: flex-start;}
.gen-btn button {
    background: #c96a32 !important;
    border-color: #c96a32 !important;
    box-shadow: 0 4px 10px rgba(201, 106, 50, .2);
}
.gen-btn button:hover {background: #ad5525 !important; border-color: #ad5525 !important;}
.form-col input:focus, .form-col textarea:focus, .form-col [role="combobox"]:focus {
    border-color: #c96a32 !important;
    box-shadow: 0 0 0 2px rgba(201, 106, 50, .14) !important;
}
/* ---- 步骤条（节点进度卡片） ---- */
.steps {display: flex; flex-direction: column; gap: 7px; margin: 2px 0 14px;}
.step {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; border-radius: 10px;
    background: #f8f7f3; font-size: 14px; color: #58645e;
    border: 1px solid #efede6; transition: all .2s;
}
.step-running {background: #fff3e5; border-color: #e6b17f; font-weight: 600; color: #9a4e20;}
.step-done {background: #edf5ee; border-color: #b6cfb8; color: #35684d;}
.step-failed {background: #fbeeee; border-color: #dfaaaa; color: #a03030;}
.elapsed {margin-left: auto; color: #838b80; font-size: 12px;}
/* ---- 教案容器：限高滚动，长教案不把下载区挤出视口 ---- */
.plan-box {
    max-height: min(56vh, 620px);
    overflow-y: auto;
    border: 1px solid #e8e4db;
    border-radius: 12px;
    padding: 6px 16px;
    background: #fffdf9;
}
/* ---- 占位提示 ---- */
.placeholder-hint {color: #898f83; font-size: 13px; padding: 8px 2px;}
.result-col a {color: #a95c2c;}
.result-col a:hover {color: #7f421f;}
.result-col [data-testid="accordion"] {border-color: #e8e4db !important;}
/* ---- 响应式：窄屏（手机）双栏堆叠为单列 ---- */
@media (max-width: 720px) {
    .gradio-container {padding: 12px 12px 24px !important;}
    #app-header {padding: 15px 16px; margin-bottom: 14px;}
    .brand-title {font-size: 21px;}
    .main-row {flex-direction: column !important;}
    .main-row > div {flex: 1 1 auto !important; width: 100% !important; min-width: 0 !important;}
    .gen-btn {width: 100% !important;}
    .form-col, .result-col {padding: 15px !important; border-radius: 13px;}
    .form-col {position: static;}
    .plan-box {max-height: 60vh;}
}
/* 统一浅色界面；Gradio 会跟随系统深色模式，需覆盖其组件变量。 */
body, body.dark {background: #f2f6f2 !important;}
.gradio-container {
    --body-background-fill: #f2f6f2;
    --body-text-color: #23382e;
    --body-text-color-subdued: #52675a;
    --block-background-fill: #ffffff;
    --block-border-color: #d6e2d9;
    --block-label-text-color: #294638;
    --block-info-text-color: #52675a;
    --input-background-fill: #f8faf8;
    --input-border-color: #cbdacf;
    --button-secondary-background-fill: #eaf2eb;
    --button-secondary-text-color: #294638;
    --color-accent-soft: #e5efe7;
    color: #23382e !important;
    background: #f2f6f2 !important;
}
#app-header {
    padding: 27px 30px 25px;
    border: 1px solid #d7e6d9;
    background: linear-gradient(120deg, #ffffff 0%, #edf6ef 100%);
    box-shadow: 0 12px 30px rgba(34, 74, 48, .07);
}
.brand-title {font-size: 28px; color: #174c35;}
.brand-sub {font-size: 14px; color: #455d4d;}
.form-col, .result-col {
    border-color: #d8e4da;
    box-shadow: 0 10px 28px rgba(38, 67, 47, .07);
    color: #23382e !important;
}
.section-title {font-size: 18px; font-weight: 750; color: #214d36; margin: 0 0 4px;}
.section-subtitle {font-size: 13px; color: #52675a; margin: 0 0 16px; line-height: 1.6;}
.form-col span[data-testid="block-info"] {
    display: block; background: transparent !important; color: #294638 !important;
    font-size: 13px; font-weight: 700; padding: 0 0 6px;
}
.form-col input, .form-col [role="combobox"] {
    color: #243b2e !important; background: #f8faf8 !important;
}
.form-col .wrap, .form-col .wrap-inner, .form-col .secondary-wrap {
    background: #f8faf8 !important; color: #243b2e !important;
}
.form-col .wrap {border: 1px solid #cbdacf !important; border-radius: 10px !important;}
.form-col [role="listbox"], .form-col [role="option"] {
    background: #fff !important; color: #243b2e !important;
}
.form-col [role="option"]:hover {background: #eaf3eb !important;}
.form-col .info, .form-col [data-testid="block-label"] {color: #52675a !important;}
.result-col button:not(.gen-btn button) {color: #294638 !important;}
.result-col [data-testid="accordion"] {background: #f7faf7 !important;}
.placeholder-hint {color: #52675a;}
.plan-box {color: #23382e !important; background: #fcfdfb;}
.plan-box p, .plan-box li, .result-col p {color: #34483b;}
.plan-box h1, .plan-box h2, .plan-box h3 {color: #1f5139;}
.gen-btn button {font-weight: 750 !important; min-height: 48px; border-radius: 10px !important;}
@media (max-width: 720px) {
    #app-header {padding: 20px;}
    .brand-title {font-size: 24px;}
}
"""


def _header_html() -> str:
    """Build the page header with the current mode badge.

    Returns
    -------
    str
        HTML fragment.
    """
    label, color, bg = _MODE_BADGE.get(config.MODE, ("#69766d", "#f0ede6"))
    return (
        "<div class='brand-lockup'><span class='brand-title'>🔥 星火智教</span>"
        f"<span class='badge' style='color:{color};background:{bg}'>{label}</span></div>"
        "<div class='brand-sub'>县乡教师 AI 教学包生成助手 · 选年级、学科与课题，一键生成教案 Word + 课件 PPT</div>"
    )


def _render_steps(states: list[str], elapsed: dict[int, float]) -> str:
    """Render the four workflow nodes as a styled step list.

    Parameters
    ----------
    states : list of str
        Per-node state: ``pending`` / ``running`` / ``done`` / ``failed``.
    elapsed : dict
        Node index -> seconds.

    Returns
    -------
    str
        HTML fragment.
    """
    items: list[str] = []
    for index, name in enumerate(config.NODE_NAMES):
        state = states[index] if index < len(states) else "pending"
        time_html = (
            f"<span class='elapsed'>{elapsed[index]:.1f}s</span>" if index in elapsed else ""
        )
        items.append(
            f"<div class='step step-{state}'>"
            f"{_STEP_ICONS.get(state, '⚪')} "
            f"<span>{index + 1}. {name}</span>{time_html}</div>"
        )
    return f"<div class='steps'>{''.join(items)}</div>"


def _to_local_time(value: str | None) -> str:
    """Convert a SQLite UTC timestamp to local time for display.

    Parameters
    ----------
    value : str or None
        ``YYYY-MM-DD HH:MM:SS`` in UTC (SQLite ``CURRENT_TIMESTAMP``).

    Returns
    -------
    str
        Local ``MM-DD HH:MM``;原值 returned unchanged if unparseable.
    """
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(value)
    return parsed.astimezone().strftime("%m-%d %H:%M")


def _render_history() -> str:
    """Render the recent generations table (traceability panel).

    Returns
    -------
    str
        Markdown table; stored UTC timestamps are shown in local time and
        pipe characters in user input are escaped to keep the table intact.
    """
    rows = storage.list_recent_generations(10)
    if not rows:
        return "_暂无生成记录_"
    icons = {"done": "✅", "failed": "❌", "running": "⏳"}
    lines = ["| # | 时间 | 年级学科 | 课题 | 状态 |", "|---|---|---|---|---|"]
    for rid, grade, subject, topic, status, created_at in rows:
        icon = icons.get(status, "·")
        safe_topic = str(topic).replace("|", "\\|")
        lines.append(
            f"| {rid} | {_to_local_time(created_at)} | {grade}{subject} | {safe_topic} | {icon} {status} |"
        )
    return "\n".join(lines)


def _recent_download_choices() -> list[tuple[str, int]]:
    """Build Gradio choices for completed packages still within 24 hours.

    Returns
    -------
    list of tuple
        Display label and generation id pairs.
    """
    return [
        (item["label"], item["generation_id"])
        for item in service.list_recent_downloads()
    ]


def _topic_choices(grade: str, subject: str) -> list[str]:
    """List knowledge-base topics for the selected grade and subject."""
    try:
        names = [
            card.name for card in kb.load_cards()
            if card.grade_code in kb.GRADE_CODES.get(grade, ())
            and card.subject_code == kb.SUBJECT_CODES.get(subject)
        ]
    except OSError:
        names = [topic for g, s, topic in EXAMPLES if g == grade and s == subject]
    return list(dict.fromkeys(names))


def _update_topics(grade: str, subject: str) -> dict[str, Any]:
    """Clear the old topic when grade or subject changes."""
    return gr.update(choices=_topic_choices(grade, subject), value=None)


def _friendly_error(error: Exception) -> str:
    """Keep implementation details and raw model responses off the page."""
    if isinstance(error, service.ServiceError):
        messages = {
            "CONTENT_BLOCKED": "输入或生成内容未通过自动预检，请查看下方审核提示。",
            "MODE_UNAVAILABLE": "生成服务暂不可用，请检查配置后重试。",
            "TIMEOUT": "生成超时，请稍后重试。",
            "BUSY": "当前生成任务较多，请稍后重试。",
            "PARAM_INVALID": "所选信息无效，请重新选择。",
        }
        if error.code in messages:
            return messages[error.code]
    return "生成未完成，请稍后重试；如持续失败，请联系管理员查看服务日志。"


def refresh_recent_downloads() -> tuple[dict[str, Any], str]:
    """Refresh the list of packages available for 24-hour retrieval.

    Returns
    -------
    tuple
        Gradio update for the selector and a user-facing status message.
    """
    choices = _recent_download_choices()
    if not choices:
        return gr.update(choices=[], value=None), "_最近 24 小时内没有可重新下载的教学包。_"
    return (
        gr.update(choices=choices, value=None),
        f"找到 {len(choices)} 个仍在 24 小时有效期内的教学包。",
    )


def retrieve_recent_package(generation_id: int | None) -> tuple[str | None, str | None, str | None, str]:
    """Load an unexpired teaching package into the download components.

    Parameters
    ----------
    generation_id : int or None
        Selected generation id from the recent-package list.

    Returns
    -------
    tuple
        Zip, Word and PowerPoint paths plus a status message.
    """
    if generation_id is None:
        return None, None, None, "❗ 请先选择一条生成记录。"
    try:
        package = service.get_recent_package_files(int(generation_id))
    except (service.ServiceError, ValueError) as error:
        message = error.message if isinstance(error, service.ServiceError) else str(error)
        return None, None, None, f"❌ 无法恢复下载：{message}"
    files = package["files"]
    return (
        files["zip"],
        files["docx"],
        files["pptx"],
        "✅ 已恢复下载；该教学包将在 24 小时有效期结束后不可再取回。",
    )


def _render_detail(result: dict[str, Any]) -> str:
    """Render generation metadata (KB refs, rural checks, timings).

    Parameters
    ----------
    result : dict
        Final ``result`` of :func:`xhzh.workflow.run_generation`.

    Returns
    -------
    str
        Markdown fragment.
    """
    lines = [f"**运行模式**：{config.MODE} ｜ **教材**：{result['meta']['textbook']}"]
    card_ids = result.get("kb_card_ids") or []
    if card_ids:
        lines.append(f"**知识点卡引用**：{', '.join(card_ids)}")
    else:
        lines.append("**知识点卡引用**：无匹配 —— 已按 EARS-3.2 声明“暂无课标依据”，内容仅供参考")
    warnings = result.get("rural_warnings") or []
    if warnings:
        lines.append("**乡土化数值复核**：")
        lines += [f"- ⚠️ {warning}" for warning in warnings]
    else:
        lines.append("**乡土化数值复核**：✅ 通过")
    report = result.get("content_review")
    if isinstance(report, dict):
        lines += ["", content_review.render_report_markdown(report)]
    return "\n\n".join(lines)


def generate(
    grade: str, subject: str, textbook: str, topic: str
) -> Iterator[tuple]:
    """Gradio handler: stream node progress, then deliver the package.

    Parameters
    ----------
    grade, subject, textbook, topic : str
        Form inputs from the UI.

    Yields
    ------
    tuple
        ``(steps_html, plan_markdown, detail_markdown, history_markdown,
        zip_file, docx_file, pptx_file, button_update, download_update)``;
        files are ``None`` until done. ``button_update`` toggles the
        interactive state (prevents duplicate submits); ``download_update``
        auto-expands the download accordion once the package is ready.
    """
    idle_button = gr.update(interactive=True, value="🚀 生成教学包")
    busy_button = gr.update(interactive=False, value="⏳ 生成中…")

    if not topic or not topic.strip():
        yield (
            _render_steps(["pending"] * 4, {}),
            "",
            "❗ **请先选择课题，再点击生成**",
            _render_history(),
            None, None, None,
            idle_button, gr.update(),
        )
        return
    topic = topic.strip()  # 规范化后再入库与组装（避免首尾空格进文件名/数据库）

    states = ["pending"] * 4
    elapsed: dict[int, float] = {}
    generation_id = 0
    review_report: dict[str, Any] | None = None
    try:
        yield _render_steps(["pending"] * 4, {}), "", "", _render_history(), None, None, None, busy_button, gr.update()
        # 与 MCP 通道共用同一段编排（service 内部落库、记录节点进度、归类异常）
        for event in service.stream_generation(
            {"grade": grade, "subject": subject, "textbook": textbook, "topic": topic},
            client=AdpClient(),
        ):
            if event["type"] == "started":
                generation_id = int(event["generation_id"])
                continue
            if event["type"] == "review":
                report = event.get("report")
                review_report = report if isinstance(report, dict) else None
                review_text = (
                    content_review.render_report_markdown(review_report)
                    if review_report is not None
                    else "⚠️ 自动预检报告不可用。"
                )
                yield (
                    _render_steps(states, elapsed),
                    "",
                    review_text,
                    _render_history(),
                    None, None, None,
                    gr.update(),
                    gr.update(),
                )
                continue
            if event["type"] == "progress":
                states[event["node"]] = event["status"]
                if "elapsed" in event:
                    elapsed[event["node"]] = event["elapsed"]
                yield _render_steps(states, elapsed), "", "", _render_history(), None, None, None, gr.update(), gr.update()
                continue
            result = event["result"]
            package = result["package"]
            yield (
                _render_steps(["done"] * 4, elapsed),
                render_plan_text(result),
                _render_detail(result),
                _render_history(),
                package["zip"], package["docx"], package["pptx"],
                idle_button,
                gr.update(open=True),  # 产物就绪，自动展开下载区
            )
    except Exception as error:  # noqa: BLE001 —— 单点失败要回显给教师，不能静默
        # 生成期间失败由 service 落库；输入预检拦截发生在落库前。
        # 失败节点标红而不是把步骤条重置为全 pending。
        if isinstance(error, service.ServiceError) and error.content_review is not None:
            review_report = error.content_review
        fail_states = ["failed" if state == "running" else state for state in states]
        record = f"#{generation_id}" if generation_id else "未落库"
        review_text = (
            content_review.render_report_markdown(review_report)
            if review_report is not None
            else ""
        )
        detail_text = f"❌ **生成失败**：{_friendly_error(error)}\n\n记录 {record}。"
        if review_text:
            detail_text = f"{detail_text}\n\n{review_text}"
        yield (
            _render_steps(fail_states, elapsed),
            "",
            detail_text,
            _render_history(),
            None, None, None,
            idle_button,  # 恢复可点，允许重试
            gr.update(),
        )


def build_ui(theme: Any = None, css: str | None = None) -> gr.Blocks:
    """Assemble the Gradio Blocks UI.

    Parameters
    ----------
    theme : gr.themes.Theme or None
        Gradio 6.0+ applies theme at launch; kept as parameter for callers.
    css : str or None
        Custom CSS, applied at launch in Gradio 6.0+.

    Returns
    -------
    gr.Blocks
    """
    with gr.Blocks(title="星火智教 · AI 教学包生成") as ui:
        gr.HTML(_header_html(), elem_id="app-header")
        with gr.Row(elem_classes=["main-row"]):
            # 左栏：只放输入与触发，一屏可见
            with gr.Column(scale=2, min_width=270, elem_classes=["form-col"]):
                gr.HTML("<div class='section-title'>创建教学包</div><div class='section-subtitle'>按顺序选择课程信息，即可开始生成。</div>")
                with gr.Row():
                    grade = gr.Dropdown(choices=config.GRADES, value=config.GRADES[0], label="年级", min_width=110)
                    subject = gr.Dropdown(
                        choices=config.SUBJECTS, value=config.SUBJECTS[0], label="学科", min_width=110
                    )
                textbook = gr.Dropdown(
                    choices=[config.TEXTBOOK], value=config.TEXTBOOK, label="教材版本"
                )
                topic = gr.Dropdown(
                    choices=_topic_choices(config.GRADES[0], config.SUBJECTS[0]),
                    value=None,
                    label="课题",
                    info="先选择年级和学科，再从对应课题中选择",
                )
                button = gr.Button(
                    "🚀 生成教学包", variant="primary", size="lg", elem_classes=["gen-btn"]
                )
            # 右栏：一条主线（步骤 → 教案 → 下载），明细与历史默认收起
            with gr.Column(scale=3, min_width=360, elem_classes=["result-col"]):
                gr.HTML("<div class='section-title'>生成结果</div><div class='section-subtitle'>实时查看进度、预览教案并下载文件。</div>")
                steps = gr.HTML(
                    value="<div class='placeholder-hint'>⏳ 点击「生成教学包」后，这里显示四个节点的实时进度</div>"
                )
                plan = gr.Markdown(
                    value="_点击左侧「生成教学包」，教案将显示在这里_",
                    elem_classes=["plan-box"],
                )
                with gr.Accordion("📥 下载教学包", open=False) as download:
                    file_zip = gr.File(label="完整教学包（zip）")
                    with gr.Row():
                        file_docx = gr.File(label="教案（Word）")
                        file_pptx = gr.File(label="课件（PPT）")
                with gr.Accordion("审核与生成明细（自动预检）", open=False):
                    detail = gr.Markdown(value="_生成后在此展示明细_")
                with gr.Accordion("历史记录", open=False):
                    history = gr.Markdown(value=_render_history())
                    gr.Markdown("最近 24 小时内生成成功的教学包可在此恢复下载。")
                    recent_downloads = gr.Dropdown(
                        choices=_recent_download_choices(),
                        value=None,
                        label="选择教学包",
                    )
                    with gr.Row():
                        refresh_downloads = gr.Button("刷新记录")
                        retrieve_download = gr.Button("恢复下载", variant="secondary")
                    retrieval_status = gr.Markdown(value="")
                    with gr.Row():
                        recent_zip = gr.File(label="教学包（zip）")
                    with gr.Row():
                        recent_docx = gr.File(label="教案（Word）")
                        recent_pptx = gr.File(label="课件（PPT）")
        grade.change(_update_topics, inputs=[grade, subject], outputs=topic)
        subject.change(_update_topics, inputs=[grade, subject], outputs=topic)
        button.click(
            generate,
            inputs=[grade, subject, textbook, topic],
            outputs=[
                steps, plan, detail, history, file_zip, file_docx, file_pptx,
                button, download,
            ],
        )
        refresh_downloads.click(
            refresh_recent_downloads,
            outputs=[recent_downloads, retrieval_status],
        )
        retrieve_download.click(
            retrieve_recent_package,
            inputs=[recent_downloads],
            outputs=[recent_zip, recent_docx, recent_pptx, retrieval_status],
        )
    return ui


if __name__ == "__main__":
    stale = storage.mark_stale_running_failed()
    if stale:
        print(f"[startup] 清理残留 running 记录 {stale} 条（已标记 failed）")
    # Gradio 6.0+：theme / css 在 launch() 传入
    # XHZH_SHARE=1 时生成公开链接（评委远程访问，链接有效期由 Gradio 隧道决定）
    build_ui().launch(
        theme=gr.themes.Soft(primary_hue="orange", neutral_hue="stone"),
        css=_CUSTOM_CSS,
        share=config.SHARE,
        server_port=config.PORT,
        show_error=False,
        # Gradio 6 默认只服务 cwd 内文件；产物目录显式放行，避免换目录启动时下载 403
        allowed_paths=[str(config.OUTPUT_DIR)],
    )
