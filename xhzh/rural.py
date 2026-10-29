"""乡土案例转换 Skill：城市化情境 → 县乡情境。

Notes
-----
规则版占位实现（词典替换 + 数值合理性提示）。
赛程内接 WorkBuddy Skill 后，本模块退化为结果校验器。
"""

from __future__ import annotations

import re

# 演示用替换词典：真实效果由平台侧 Skill（LLM 改写）承担
REPLACE_MAP = {
    "高档商场": "镇上的农贸市场",
    "大型商场": "镇上的农贸市场",
    "购物中心": "镇上的农贸市场",
    "地铁站": "乡镇客运站",
    "地铁": "乡镇班车",
    "游乐园": "村头文化广场",
    "西餐厅": "镇上的面馆",
    "超市": "村里的小卖部",
}

# 长词优先：正则按 key 长度降序交替匹配，避免"地铁站"被"地铁"抢先替换成"乡镇班车站"
_URBAN_PATTERN = re.compile(
    "|".join(re.escape(key) for key in sorted(REPLACE_MAP, key=len, reverse=True))
)

# 全角数字/句点 → 半角（教材输入常含全角数字）
_FULLWIDTH_MAP = str.maketrans("０１２３４５６７８９．", "0123456789.")
_MONEY_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")


def ruralize(text: str) -> str:
    """Replace urban contexts with rural ones in the given text.

    Parameters
    ----------
    text : str
        Original text, e.g. an exercise stem.

    Returns
    -------
    str
        Ruralized text; longest dictionary keys win, so compound terms
        like ``地铁站`` become ``乡镇客运站`` instead of ``乡镇班车站``.
    """
    return _URBAN_PATTERN.sub(lambda match: REPLACE_MAP[match.group(0)], text)


def check_money_sanity(text: str) -> list[str]:
    """Flag monetary values that look implausible for rural contexts.

    Parameters
    ----------
    text : str
        Text to inspect, e.g. a ruralized exercise. Full-width digits
        are normalized first so ``３６００元`` is caught as well.

    Returns
    -------
    list of str
        Human-readable warnings; empty list means the text passes.
    """
    warnings: list[str] = []
    normalized = text.translate(_FULLWIDTH_MAP)
    for match in _MONEY_PATTERN.finditer(normalized):
        value = float(match.group(1))
        if value >= 1000:
            warnings.append(f"金额 {match.group(0)} 对县乡消费情境可能不合理，请人工复核")
    return warnings
