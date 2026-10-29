"""学生向课件脚本：把教案（教师语言）改写为能吸引学生的分页课件脚本。

Notes
-----
教案是给教师看的（目标、重难点、环节、练习），直接搬到幻灯片上会得到
一叠文字墙，学生不容易被吸引。本模块在组装输出前插入一步"课件脚本"：
把同样的教学内容重新包装成学生视角的页面序列——悬念开场、任务闯关、
互动提问、分层挑战、答案揭晓、小结作业。

- llm 模式：调用大模型生成脚本，解析失败或超时自动降级；
- mock 模式：使用本地模板脚本（同样具备学生向结构，保证演示效果）。

脚本是增强项：任何异常都不得中断主链路（教案 docx 必须照常产出）。
"""

from __future__ import annotations

import logging
from typing import Any

from . import config, prompts
from .visuals import infer_icons, infer_theme

logger = logging.getLogger(__name__)

# 页型枚举：渲染器据此决定主色与版面（cover 为封面半版，route 为横向流程）
KINDS = (
    "cover",
    "hook",
    "quest",
    "explore",
    "route",
    "practice",
    "game",
    "answer",
    "board",
    "summary",
)

# 单页要点上限：超过会挤压版面，渲染器亦按此截断
MAX_POINTS = 4
# 单条要点字数上限（中文字符计）
MAX_POINT_CHARS = 46

DEFAULT_KIND = "explore"


def as_str_list(value: Any) -> list[str]:
    """Normalize an LLM output field into a clean list of strings.

    Parameters
    ----------
    value : Any
        Raw field value; may be a list, a single string, a nested dict
        (e.g. ``{"basic": [...], "advanced": [...]}``), or garbage.

    Returns
    -------
    list of str
        Non-empty string items; strings become one-element lists so a
        drifted field never gets iterated character by character. Dict
        values are flattened in key order (schema drift tolerance).

    Notes
    -----
    与工作流共用同一口径（原先定义在 ``workflow`` 中）；此处为唯一实现，
    :mod:`xhzh.workflow` 与 :mod:`xhzh.assembly` 均从本模块导入，避免循环导入。
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        flattened: list[str] = []
        for item in value.values():
            flattened.extend(as_str_list(item))
        return flattened
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def build_script(params: dict[str, Any], result: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    """Build the student-facing courseware script for one lesson.

    Parameters
    ----------
    params : dict
        User inputs: grade / subject / textbook / topic.
    result : dict
        Workflow result produced by nodes 0-2 (framework, goals, exercises...).
    mode : str
        ``"llm"`` calls the model; anything else uses the local template.

    Returns
    -------
    list of dict
        Slide specs: ``{"kind", "title", "lead", "points", "tip", "tag"}``.

    Notes
    -----
    脚本为增强项：模型调用或 JSON 解析失败时记录告警并返回本地模板脚本，
    绝不向上抛异常（否则整条生成链路会因"锦上添花"而失败）。
    """
    if mode == "llm":
        try:
            from . import llm

            messages = prompts.build_courseware_messages(params, result)
            data = llm.extract_json(llm.chat(messages, timeout=config.LLM_TIMEOUT))
            script = _normalize(data.get("slides"))
            if script:
                return script
            logger.warning("课件脚本为空，降级为模板脚本")
        except Exception as error:  # noqa: BLE001 - 增强项，任何失败都降级
            logger.warning("课件脚本生成失败，降级为模板脚本：%s", error)
    return _fallback_script(params, result)


def _normalize(raw_slides: Any) -> list[dict[str, Any]]:
    """Validate and clean model-produced slide specs.

    Parameters
    ----------
    raw_slides : Any
        ``slides`` field of the model JSON; may be missing or malformed.

    Returns
    -------
    list of dict
        Cleaned specs, or ``[]`` when nothing usable is present.
    """
    if not isinstance(raw_slides, list):
        return []
    slides: list[dict[str, Any]] = []
    for item in raw_slides:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        kind = str(item.get("kind") or DEFAULT_KIND).strip().lower()
        if kind not in KINDS:
            kind = DEFAULT_KIND
        points = [p[:MAX_POINT_CHARS] for p in as_str_list(item.get("points"))][:MAX_POINTS]
        visual = item.get("visual") if isinstance(item.get("visual"), dict) else None
        slides.append({
            "kind": kind,
            "title": title[:28],
            "lead": str(item.get("lead") or "").strip()[:40],
            "points": points,
            "tip": str(item.get("tip") or "").strip()[:60],
            "tag": str(item.get("tag") or "").strip()[:10],
            "visual": visual,
        })
    # 封面缺失时补一张，保证课件开头完整
    if slides and slides[0]["kind"] != "cover":
        slides.insert(0, _cover_spec(slides[0].get("title", ""), "", ""))
    return slides


def _cover_spec(topic: str, subtitle: str, lead: str) -> dict[str, Any]:
    """Build a cover slide spec.

    Parameters
    ----------
    topic : str
        Lesson topic.
    subtitle : str
        Grade / subject / textbook line.
    lead : str
        Hook sentence shown on the cover.

    Returns
    -------
    dict
        Cover slide spec.
    """
    return {
        "kind": "cover",
        "title": topic,
        "lead": lead,
        "points": [subtitle] if subtitle else [],
        "tip": "",
        "tag": "课堂闯关",
    }


def _fallback_script(params: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a student-facing script locally, without any model call.

    Parameters
    ----------
    params : dict
        User inputs.
    result : dict
        Workflow result.

    Returns
    -------
    list of dict
        Slide specs derived from the lesson plan content.

    Notes
    -----
    覆盖 mock 演示与 llm 降级两条路径：结构（悬念→任务→锦囊→闯关→
    挑战→揭晓→板书→小结）与模型版一致，只是措辞由模板生成。
    """
    topic = str(params.get("topic", "")).strip()
    subtitle = f"{params.get('grade', '')} {params.get('subject', '')} · {params.get('textbook', '')}"
    stages = [s for s in (result.get("stages") or []) if isinstance(s, dict) and str(s.get("name", "")).strip()]
    goals_basic = as_str_list(result.get("goals_basic"))
    goals_extend = as_str_list(result.get("goals_extend"))
    basic = as_str_list(result.get("exercises_basic"))
    extend = as_str_list(result.get("exercises_extend"))
    answers = as_str_list(result.get("answers"))
    key_points = as_str_list(result.get("key_points"))
    difficult_points = as_str_list(result.get("difficult_points"))
    # 情境线索：首个练习题干用于封面/悬念页的场景推断
    scene_text = " ".join([topic, basic[0] if basic else "", extend[0] if extend else ""])

    slides: list[dict[str, Any]] = [
        {**_cover_spec(topic, subtitle, "今天这节课，我们来解决一个身边的真问题"),
         "visual": {"type": "scene", "theme": infer_theme(scene_text)}},
    ]

    hook_desc = str(stages[0].get("desc", "")).strip() if stages else ""
    hook_points = [
        hook_desc or f"想一想：生活中哪里会遇到《{topic}》？",
        "提示：把问题里的『不知道的数』先记下来",
    ]
    slides.append({
        "kind": "hook",
        "title": "开场悬念",
        "lead": "先别急着翻书，看这道题你能不能秒答",
        "points": hook_points,
        "tip": "让学生先猜再算，猜错没关系，重点是引出未知数。",
        "tag": "第 1 关",
        "visual": {"type": "scene", "theme": infer_theme(" ".join(hook_points))},
    })

    if goals_basic or goals_extend:
        goal_points = (goals_basic + goals_extend)[:MAX_POINTS]
        slides.append({
            "kind": "quest",
            "title": "本关任务",
            "lead": "学完这节课，你要能拿下这些能力",
            "points": goal_points,
            "tip": "任务逐条打勾，留守儿童优先回答基础档，建立信心。",
            "tag": "任务清单",
            "visual": {"type": "iconrow", "icons": infer_icons(goal_points)},
        })

    if key_points or difficult_points:
        tip_points = (
            [f"重点 · {text}" for text in key_points]
            + [f"难点 · {text}" for text in difficult_points]
        )[:MAX_POINTS]
        slides.append({
            "kind": "explore",
            "title": "闯关锦囊",
            "lead": "抓住这两点，题目就难不住你",
            "points": tip_points,
            "tip": "难点处放慢，先让学生用自己的话说一遍再纠正。",
            "tag": "锦囊",
            "visual": {"type": "iconrow", "icons": infer_icons(tip_points)},
        })

    if stages:
        slides.append({
            "kind": "route",
            "title": "闯关路线",
            "lead": "四步走完，这节课就通关了",
            "points": [
                f"{stage['name']}（{stage['minutes']} 分钟）" if stage.get("minutes") else str(stage["name"])
                for stage in stages
            ][:MAX_POINTS],
            "tip": "按路线控制节奏，超时优先压缩巩固练习环节。",
            "tag": "路线图",
        })

    for index in range(0, len(basic), 2):
        chunk = basic[index:index + 2]
        slides.append({
            "kind": "practice",
            "title": f"基础挑战 {index // 2 + 1}",
            "lead": "先设未知数，再列方程，别直接口算",
            "points": chunk,
            "tip": "先独立做 2 分钟，再同桌互评；板演时要求口述设未知数的过程。",
            "tag": f"第 {index // 2 + 2} 关",
            "visual": _visual_for_exercise(chunk[0], "bar"),
        })

    for index in range(0, len(extend), 2):
        chunk = extend[index:index + 2]
        slides.append({
            "kind": "game",
            "title": f"终极挑战 {index // 2 + 1}",
            "lead": "这关有点难，小组讨论再出手",
            "points": chunk,
            "tip": "开放题允许不同设法，重点评『等量关系找得对不对』。",
            "tag": "终极关",
            "visual": _visual_for_exercise(chunk[0], "seg"),
        })

    for index in range(0, len(answers), 3):
        slides.append({
            "kind": "answer",
            "title": f"答案揭晓 {index // 3 + 1}",
            "lead": "对一对，看看你的方程列对了吗",
            "points": answers[index:index + 3],
            "tip": "公布答案前先统计举手人数，错得多的题当场回代验算。",
            "tag": "揭晓",
        })

    board = str(result.get("board") or "").strip()
    board_points = [board] if board else ["核心概念 → 方法步骤 → 易错提醒"]
    slides.append({
        "kind": "board",
        "title": "本课方法卡",
        "lead": "记下这条思路，下次直接套用",
        "points": board_points,
        "tip": "板书边讲边写，留白处让学生上台补全。",
        "tag": "板书",
        "visual": {"type": "iconrow", "icons": infer_icons(board_points)},
    })

    slides.append({
        "kind": "summary",
        "title": "通关小结与作业",
        "lead": "今天你拿下了什么？",
        "points": [
            "小结：设未知数 → 找等量关系 → 列方程 → 检验作答",
            "作业：基础题 2 道必做，拓展题 1 道选做",
            "选做：把今天的方法讲给家里人听一遍",
        ],
        "tip": "小结让学生自己说，教师只补漏；作业分层布置。",
        "tag": "通关",
        "visual": {"type": "scene", "theme": infer_theme(scene_text)},
    })
    return slides


def _visual_for_exercise(stem: str, default_type: str) -> dict[str, Any] | None:
    """Plan a diagram (or fallback scene) for one exercise slide.

    Parameters
    ----------
    stem : str
        First exercise text of the page (numbers are extracted from it).
    default_type : str
        Preferred diagram type when wording gives no signal.

    Returns
    -------
    dict or None
        Diagram spec, scene spec fallback, or ``None`` when the stem has
        fewer than two usable numbers.
    """
    from . import visuals

    nums = visuals.extract_nums(stem)
    if len(nums) >= 2:
        return {
            "type": "diagram",
            "diagram_type": visuals._infer_diagram(stem, nums) or default_type,
            "nums": nums,
            "labels": [],
        }
    return {"type": "scene", "theme": visuals.infer_theme(stem)}
