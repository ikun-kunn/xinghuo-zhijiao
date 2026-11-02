"""Rule-based preflight checks for generated teaching content.

Notes
-----
These deterministic checks catch a narrow set of high-confidence risks and
structural issues. They do not establish factual correctness or replace teacher
review.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from . import kb, rural

_TEXT_FIELDS = {
    "meta": "课题信息",
    "standard_ref": "课标依据",
    "framework": "教学框架",
    "objectives": "教学目标",
    "key_points": "教学重点",
    "difficult_points": "教学难点",
    "stages": "教学环节",
    "goals_basic": "基础目标",
    "goals_extend": "拓展目标",
    "exercises_basic": "基础练习",
    "exercises_extend": "拓展练习",
    "answers": "答案与解析",
    "board": "板书设计",
    "scaffolding": "课堂支持策略",
    "courseware": "课件脚本",
}

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_PHONE_RE = re.compile(
    r"(?:电话|手机|手机号|联系电话|联系方式|联系号码)\s*[：:：\-]?\s*"
    r"(?:\+?86[\s-]?)?1[3-9]\d{9}"
)
_HARM_PATTERNS = (
    re.compile(r"(?:自杀|自残|割腕|服毒).{0,10}(?:详细步骤|具体方法|操作步骤|教程|指导)", re.S),
    re.compile(r"(?:建议|鼓励|教唆).{0,8}(?:自杀|自残|伤害自己|伤害同学)", re.S),
    re.compile(r"(?:制作|自制|配制).{0,8}(?:炸弹|爆炸物|毒药|致命武器)", re.S),
    re.compile(r"(?:如何|怎样|怎么|教你).{0,8}(?:杀死|杀害|伤害).{0,8}(?:同学|老师|他人|某人)", re.S),
    re.compile(r"(?:诱骗|胁迫|要求).{0,8}(?:未成年人|学生).{0,8}(?:裸照|发生性关系|色情)", re.S),
)
_HATE_RE = re.compile(
    r"(?:某民族|某种族|某性别|残障人士|留守儿童|农村人)"
    r".{0,6}(?:都是低等|天生低等|不配受教育|该被消灭|都是垃圾)"
)
_NEGATED_CONTEXT_RE = re.compile(
    r"(?:反对|禁止|不要|防止|识别|辨析|驳斥|批判|反歧视|预防).{0,8}$"
)
_SENSITIVE_TOPIC_RE = re.compile(r"色情|裸露|成人影片|毒品|赌博")
_GRADE_SUBJECT_LABELS = {
    "安全": "安全与不适龄内容",
    "隐私": "个人隐私与敏感信息",
    "课标": "课标与知识库依据",
    "答案": "练习题与答案结构",
    "教学": "教学结构与学段适配",
    "乡土": "乡土情境与数值合理性",
}
_LIMITATIONS = (
    "这是规则式初筛，无法证明学科事实或答案正确，也无法可靠判断所有内容是否适龄；"
    "请教师重点复核标出的项目。姓名、住址等非结构化个人信息不一定能被自动识别。"
)


class ContentReviewBlocked(RuntimeError):
    """Raised when a high-confidence safety or privacy risk blocks delivery."""

    def __init__(self) -> None:
        super().__init__(
            "自动预检发现高风险安全或隐私内容，已阻止教学包交付；请查看预检报告并修改后重试。"
        )


def _collect_texts(result: dict[str, Any]) -> dict[str, str]:
    """Collect content-bearing text fields without serializing unrelated data.

    Parameters
    ----------
    result : dict
        Structured result from the generation workflow.

    Returns
    -------
    dict of str
        Mapping of human-readable field paths to text.
    """
    texts: dict[str, str] = {}

    def visit(value: Any, label: str) -> None:
        if isinstance(value, str):
            if value.strip():
                texts[label] = value.strip()
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{label}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{label}[{index}]")

    for key, label in _TEXT_FIELDS.items():
        if key in result:
            visit(result[key], label)
    return texts


def _strings(value: Any) -> list[str]:
    """Return non-empty string items from a value.

    Parameters
    ----------
    value : Any
        A string or list-like workflow field.

    Returns
    -------
    list of str
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _valid_identity_number(value: str) -> bool:
    """Check that an 18-character identity-number candidate contains a real date.

    Parameters
    ----------
    value : str
        Candidate identity number.

    Returns
    -------
    bool
    """
    try:
        datetime.strptime(value[6:14], "%Y%m%d")
    except ValueError:
        return False
    return True


def _safety_privacy_findings(texts: dict[str, str]) -> list[dict[str, str]]:
    """Find high-confidence safety and privacy patterns without retaining matches.

    Parameters
    ----------
    texts : dict of str
        Labeled text fields to inspect.

    Returns
    -------
    list of dict
        Findings contain field labels and guidance, never matched text.
    """
    findings: list[dict[str, str]] = []

    def add(
        severity: str, category: str, code: str, title: str, recommendation: str,
        fields: list[str] | None = None,
    ) -> None:
        findings.append(
            {
                "severity": severity,
                "category": category,
                "code": code,
                "title": title,
                "recommendation": recommendation,
                "fields": "、".join(fields or []),
            }
        )

    normalized_texts = {
        field: text.translate(_FULLWIDTH_DIGITS) for field, text in texts.items()
    }
    privacy_fields: set[str] = set()
    for field, text in normalized_texts.items():
        if _PHONE_RE.search(text) or _EMAIL_RE.search(text):
            privacy_fields.add(field)
        if any(_valid_identity_number(match.group(0)) for match in _ID_RE.finditer(text)):
            privacy_fields.add(field)
    if privacy_fields:
        add(
            "high", "隐私", "personal_data_detected", "疑似包含可识别的联系方式或证件号码",
            "删除或替换为虚构占位信息后重新生成；系统不会在报告中回显匹配内容。",
            sorted(privacy_fields),
        )

    harm_fields: set[str] = set()
    hate_fields: set[str] = set()
    contextual_fields: set[str] = set()
    sensitive_fields: set[str] = set()
    for field, text in normalized_texts.items():
        harm_matches = [
            match for pattern in _HARM_PATTERNS for match in pattern.finditer(text)
        ]
        hate_match = _HATE_RE.search(text)
        high_context_matches = harm_matches + ([hate_match] if hate_match else [])
        if any(
            _NEGATED_CONTEXT_RE.search(text[max(0, match.start() - 12):match.start()])
            for match in high_context_matches
        ):
            contextual_fields.add(field)
        if any(
            not _NEGATED_CONTEXT_RE.search(text[max(0, match.start() - 12):match.start()])
            for match in harm_matches
        ):
            harm_fields.add(field)
        if hate_match and not _NEGATED_CONTEXT_RE.search(
            text[max(0, hate_match.start() - 12):hate_match.start()]
        ):
            hate_fields.add(field)
        if _SENSITIVE_TOPIC_RE.search(text):
            sensitive_fields.add(field)
    if harm_fields:
        add(
            "high", "安全", "actionable_harm", "疑似包含可执行的自伤、伤人或剥削指导",
            "已阻断交付；删除相关指导并由专业人员复核后再生成。",
            sorted(harm_fields),
        )
    if hate_fields:
        add(
            "high", "安全", "targeted_degradation", "疑似包含针对群体的贬损或伤害性表述",
            "已阻断交付；改为中性、尊重且符合教育场景的表述。",
            sorted(hate_fields),
        )
    if contextual_fields:
        add(
            "medium", "安全", "sensitive_quoted_context", "在可能的教育/否定语境中提到高风险内容",
            "确认材料是在预防、辨析或批判该内容，而非提供可执行指导。",
            sorted(contextual_fields),
        )
    if sensitive_fields:
        add(
            "medium", "安全", "sensitive_topic", "出现需要结合教学语境判断的敏感主题",
            "确认内容确有教学必要且符合初中年龄特点；必要时改为安全、非细节化表达。",
            sorted(sensitive_fields),
        )
    return findings


def _make_report(
    findings: list[dict[str, str]], labels: dict[str, str]
) -> dict[str, Any]:
    """Build common status, counts, and category summaries.

    Parameters
    ----------
    findings : list of dict
        Review findings.
    labels : dict of str
        Category keys and user-facing labels to summarize.

    Returns
    -------
    dict
        Report structure.
    """
    counts = {
        severity: sum(item["severity"] == severity for item in findings)
        for severity in ("high", "medium", "low")
    }
    blocked = counts["high"] > 0
    status = "blocked" if blocked else ("review" if counts["medium"] else "clear")
    checks = []
    for category, label in labels.items():
        category_findings = [
            item for item in findings if item["category"] == category
        ]
        checks.append(
            {
                "category": category,
                "label": label,
                "status": (
                    "blocked"
                    if any(item["severity"] == "high" for item in category_findings)
                    else "review"
                    if category_findings
                    else "clear"
                ),
                "finding_count": len(category_findings),
            }
        )
    return {
        "status": status,
        "blocked": blocked,
        "counts": counts,
        "checks": checks,
        "findings": findings,
        "limitations": _LIMITATIONS,
    }


def review_input(params: dict[str, Any]) -> dict[str, Any]:
    """Pre-screen user-provided topic text before saving or sending it to a model.

    Parameters
    ----------
    params : dict
        Generation parameters; only user-provided text fields are inspected.

    Returns
    -------
    dict
        Safety/privacy report suitable for an early block.
    """
    topic = params.get("topic")
    texts = {"课题": topic} if isinstance(topic, str) else {}
    return _make_report(
        _safety_privacy_findings(texts),
        {"安全": "安全与不适龄内容", "隐私": "个人隐私与敏感信息"},
    )


def review_content(result: dict[str, Any]) -> dict[str, Any]:
    """Inspect generated content and return a non-generative review report.

    Parameters
    ----------
    result : dict
        Structured output from the four-node workflow, before package assembly.

    Returns
    -------
    dict
        Review status, category checks, findings, and limitations. Findings
        never include the matched sensitive text itself.
    """
    texts = _collect_texts(result)
    findings = _safety_privacy_findings(texts)

    def add(
        severity: str, category: str, code: str, title: str, recommendation: str,
        fields: list[str] | None = None,
    ) -> None:
        findings.append(
            {
                "severity": severity,
                "category": category,
                "code": code,
                "title": title,
                "recommendation": recommendation,
                "fields": "、".join(fields or []),
            }
        )

    params = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    subject = str(params.get("subject") or "")
    grade = str(params.get("grade") or "")
    card_ids = _strings(result.get("kb_card_ids"))
    known_cards = {card.card_id: card for card in kb.load_cards()}
    expected_subject = kb.SUBJECT_CODES.get(subject)
    expected_grades = kb.GRADE_CODES.get(grade, ())
    invalid_cards = [
        card_id
        for card_id in card_ids
        if card_id not in known_cards
        or known_cards[card_id].subject_code != expected_subject
        or known_cards[card_id].grade_code not in expected_grades
    ]
    standard_ref = str(result.get("standard_ref") or "").strip()
    if not card_ids or result.get("kb_empty") is True:
        add(
            "medium", "课标", "no_knowledge_reference", "未找到可核验的知识点卡依据",
            "请学科教师核对课标与教材内容；不要把生成文本当作已核实的官方课标引用。",
            ["课标依据"],
        )
    elif invalid_cards:
        add(
            "medium", "课标", "invalid_knowledge_reference", "知识点卡引用不存在或与所选学科/年级不匹配",
            "请核对引用卡片及适用年级，必要时更正后重新生成。",
            ["课标依据"],
        )
    if not standard_ref or any(
        marker in standard_ref for marker in ("待补充", "暂无课标依据", "未收录")
    ):
        if card_ids and not invalid_cards:
            add(
                "medium", "课标", "standard_reference_incomplete", "课标依据说明缺失或明确标记为未核验",
                "请教师补充或确认对应课程标准条目。",
                ["课标依据"],
            )

    basic = _strings(result.get("exercises_basic"))
    extended = _strings(result.get("exercises_extend"))
    answers = _strings(result.get("answers"))
    if len(basic) < 4 or len(extended) < 4 or len(answers) != len(basic) + len(extended):
        add(
            "medium", "答案", "exercise_answer_count_mismatch",
            "分层练习数量不足，或答案/解析未与题目逐题对应",
            "补足基础题和拓展题各至少 4 题，并逐题核对答案与解析。",
            ["基础练习", "拓展练习", "答案与解析"],
        )

    if not _strings(result.get("goals_basic")) or not _strings(result.get("goals_extend")):
        add(
            "medium", "教学", "differentiated_goals_missing", "基础或拓展目标缺失",
            "补齐两档教学目标，并确认练习与目标相互对应。",
            ["教学目标"],
        )
    stages = result.get("stages")
    stage_minutes = [
        stage.get("minutes")
        for stage in stages
        if isinstance(stage, dict)
        and isinstance(stage.get("minutes"), (int, float))
        and stage["minutes"] > 0
    ] if isinstance(stages, list) else []
    if not stage_minutes or not 35 <= sum(stage_minutes) <= 55:
        add(
            "medium", "教学", "lesson_timing_review", "教学环节时长缺失或总时长偏离常规课时",
            "请教师确认各环节安排及总课时；自动预检不能判断教学节奏是否合适。",
            ["教学环节"],
        )
    courseware = result.get("courseware")
    if not isinstance(courseware, list) or not 9 <= len(courseware) <= 12:
        add(
            "medium", "教学", "courseware_structure_review", "课件页数不在 9–12 页范围",
            "确认课件包含导入、讲解、练习、反馈和总结等必要环节。",
            ["课件脚本"],
        )

    rural_findings: set[str] = set()
    if result.get("rural_warnings"):
        rural_findings.add("乡土金额校验")
    exercise_text = "\n".join(basic + extended)
    for urban_term in rural.REPLACE_MAP:
        if urban_term in exercise_text:
            rural_findings.add("乡土化后的练习")
    if rural_findings:
        add(
            "medium", "乡土", "rural_context_review", "存在金额合理性告警或尚未转换的城市化情境",
            "核对当地价格、单位与生活场景；若改写会破坏题意，应保留原题并说明原因。",
            sorted(rural_findings),
        )

    labels = {
        "安全": "安全",
        "隐私": "隐私",
        "课标": "课标依据",
        "答案": "题目与答案",
        "教学": "教学结构",
        "乡土": "乡土情境",
    }
    return _make_report(findings, labels)


def render_report_markdown(report: dict[str, Any]) -> str:
    """Render a review report for the UI, lesson plan, and ZIP package.

    Parameters
    ----------
    report : dict
        Value returned by :func:`review_content`.

    Returns
    -------
    str
        Markdown report without matched sensitive content.
    """
    status = report.get("status")
    status_label = {
        "blocked": "⛔ 已阻断交付",
        "review": "⚠️ 有项目需要复核",
        "clear": "✅ 未发现规则可识别的明显问题",
    }.get(status, "⚠️ 预检状态未知")
    counts = report.get("counts") or {}
    lines = [
        "## 自动预检报告",
        f"**结论**：{status_label}（高风险 {counts.get('high', 0)} 项；需复核 {counts.get('medium', 0)} 项）",
    ]
    for check in report.get("checks") or []:
        state = check.get("status")
        icon = "⛔" if state == "blocked" else "⚠️" if state == "review" else "✅"
        lines.append(f"- {icon} {check.get('label', '检查项')}")
    for finding in report.get("findings") or []:
        severity = "高风险" if finding.get("severity") == "high" else "需复核"
        fields = f"（位置：{finding['fields']}）" if finding.get("fields") else ""
        lines.append(
            f"- **{severity} · {finding.get('title', '检查发现')}**{fields}："
            f"{finding.get('recommendation', '')}"
        )
    limitations = report.get("limitations")
    if limitations:
        lines.append(f"> {limitations}")
    return "\n".join(lines)
