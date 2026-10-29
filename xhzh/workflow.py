"""四节点工作流：检索与生成 → 学情适配 → 教具生成 → 组装输出。

Notes
-----
以生成器形式逐步 yield 进度事件，供 Gradio 前端流式渲染；
节点内容来自 :class:`xhzh.adp_client.AdpClient`（mock 或真实 ADP 调用）。
"""

from __future__ import annotations

import time
from typing import Any, Iterator

from . import config, rural
from .adp_client import AdpClient
from .assembly import build_package
from .courseware import as_str_list, build_script

# 教学字段归一化（原实现位于本模块，存在 assembly 反向依赖；为打断循环导入
# 统一收敛到 courseware.as_str_list）。保留旧名，供 assembly 与测试继续引用。
_as_str_list = as_str_list

NODES = config.NODE_NAMES


def run_generation(
    grade: str, subject: str, textbook: str, topic: str, client: AdpClient | None = None
) -> Iterator[dict[str, Any]]:
    """Run the four-node workflow and yield progress events.

    Parameters
    ----------
    grade : str
        e.g. ``"七年级"``.
    subject : str
        One of :data:`xhzh.config.SUBJECTS`.
    textbook : str
        Textbook edition, e.g. ``"人教版"``.
    topic : str
        Lesson topic.
    client : AdpClient or None
        Defaults to a new mock-aware :class:`AdpClient`.

    Yields
    ------
    dict
        ``{"type": "progress", ...}`` while running, then a final
        ``{"type": "done", "result": ...}`` event with all artifacts.
    """
    client = client or AdpClient()
    params = {"grade": grade, "subject": subject, "textbook": textbook, "topic": topic}
    context: dict[str, Any] = {"params": params}
    result: dict[str, Any] = {"meta": params}

    for index, name in enumerate(NODES[:3]):
        started = time.time()
        yield {"type": "progress", "node": index, "name": name, "status": "running"}
        output = client.call_node(index, params, context)
        if index == 0 and "framework" not in output:
            output["framework"] = _render_framework(output)
        if index == 2:  # 教具生成节点：先完成乡土案例转换 + 校验，再统一写入结果
            # llm 模式下字段可能缺失或类型漂移（数组→字符串），先规整再消费
            output["exercises_basic"] = [
                rural.ruralize(t) for t in _as_str_list(output.get("exercises_basic"))
            ]
            output["exercises_extend"] = [
                rural.ruralize(t) for t in _as_str_list(output.get("exercises_extend"))
            ]
            output["board"] = str(output.get("board") or "").strip()
            output["rural_warnings"] = rural.check_money_sanity(
                " ".join(output["exercises_basic"] + output["exercises_extend"])
            )
        context[f"node{index}"] = output
        result.update(output)
        yield {
            "type": "progress",
            "node": index,
            "name": name,
            "status": "done",
            "elapsed": round(time.time() - started, 2),
        }

    yield {
        "type": "progress",
        "node": 3,
        "name": NODES[3],
        "status": "running",
        "detail": "生成学生向课件脚本",
    }
    started = time.time()
    # 课件脚本：把教案改写成"学生看得进去"的分页脚本（悬念/闯关/挑战/揭晓）。
    # 属增强项，内部任何失败都会降级为模板脚本，不会中断打包。
    result["courseware"] = build_script(params, result, client.mode)
    package = build_package(result)
    result["package"] = package
    yield {
        "type": "progress",
        "node": 3,
        "name": NODES[3],
        "status": "done",
        "elapsed": round(time.time() - started, 2),
    }
    yield {"type": "done", "result": result}


def _render_framework(node0: dict[str, Any]) -> str:
    """Render the node-0 structured output into a framework text block.

    Parameters
    ----------
    node0 : dict
        Output of node 0 (mock or llm schema).

    Returns
    -------
    str
    """
    lines: list[str] = []
    lines += [f"教学目标：{i + 1}. {text}" for i, text in enumerate(_as_str_list(node0.get("objectives")))]
    lines += [f"教学重点：{text}" for text in _as_str_list(node0.get("key_points"))]
    lines += [f"教学难点：{text}" for text in _as_str_list(node0.get("difficult_points"))]
    # 教学环节：保留时长与活动描述（此前只拼环节名，导致教案缺课时安排与教学活动设计）
    stage_lines: list[str] = []
    for stage in node0.get("stages") or []:
        if isinstance(stage, dict):
            name = str(stage.get("name", "")).strip()
            if not name:
                continue
            minutes = stage.get("minutes")
            desc = str(stage.get("desc", "")).strip()
            time_part = f"（{minutes} 分钟）" if minutes else ""
            desc_part = f"：{desc}" if desc else ""
            stage_lines.append(f"{name}{time_part}{desc_part}")
        elif str(stage).strip():
            stage_lines.append(str(stage).strip())
    lines.append("教学环节：")
    lines += [f"- {line}" for line in stage_lines]
    return "\n".join(lines)


def render_plan_text(result: dict[str, Any]) -> str:
    """Render a readable plan preview from the workflow result.

    Parameters
    ----------
    result : dict
        Final ``result`` of :func:`run_generation`.

    Returns
    -------
    str
        Markdown-ish plain text preview.
    """
    meta = result["meta"]
    lines = [
        f"# {meta['topic']}（{meta['grade']} {meta['subject']} · {meta['textbook']}）",
        "",
        f"课标依据：{result.get('standard_ref', '待补充')}",
        "",
        "## 教学框架",
        result.get("framework", ""),
        "",
        "## 教学目标",
        "### 基础巩固",
    ]
    lines += [f"- {g}" for g in _as_str_list(result.get("goals_basic"))]
    lines.append("### 拓展提升")
    lines += [f"- {g}" for g in _as_str_list(result.get("goals_extend"))]
    lines += ["", "## 随堂练习（乡土化后）", "### 基础巩固"]
    lines += [f"- {t}" for t in _as_str_list(result.get("exercises_basic"))]
    lines.append("### 拓展提升")
    lines += [f"- {t}" for t in _as_str_list(result.get("exercises_extend"))]
    answers = _as_str_list(result.get("answers"))
    if answers:
        lines += ["", "### 参考答案与简解"]
        lines += [f"- {a}" for a in answers]
    for warning in result.get("rural_warnings", []):
        lines.append(f"> ⚠️ {warning}")
    lines += ["", "## 板书设计", result.get("board", "")]
    script = [s for s in (result.get("courseware") or []) if isinstance(s, dict)]
    if script:
        lines += ["", "## 课件脚本（学生向，随 PPT 一同生成）"]
        for index, spec in enumerate(script):
            tag = str(spec.get("tag") or "").strip()
            lead = str(spec.get("lead") or "").strip()
            suffix = f"｜{tag}" if tag else ""
            lines.append(f"- 第 {index + 1} 页 · {spec.get('title', '')}{suffix}：{lead}")
    if result.get("scaffolding"):
        lines += ["", "## 课堂支持策略（学情适配）", result["scaffolding"]]
    if result.get("kb_card_ids"):
        lines += ["", f"知识点卡引用：{'、'.join(result['kb_card_ids'])}"]
    return "\n".join(lines)
