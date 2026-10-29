"""知识点卡知识库：解析与检索。

Notes
-----
解析《知识库-人教版初中知识点-v0.1.md》中的 ``### ID 名称`` 结构化卡片；
检索采用字符 bigram 重叠打分 + 学科/年级过滤，赛事规模（百余张卡）足够。
后续迁移平台知识库时，本模块只需替换 ``load_cards`` 的数据源。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config

SUBJECT_CODES = {"语文": "CHI", "数学": "MAT", "英语": "ENG"}
GRADE_CODES = {"七年级": ("7",), "八年级": ("8",), "九年级": ("9",)}


@dataclass
class KnowledgeCard:
    """One structured knowledge card parsed from the markdown KB.

    Parameters
    ----------
    card_id : str
        e.g. ``"MAT-7A-3-0-K01"``.
    name : str
        Card title.
    subject_code : str
        e.g. ``"MAT"``.
    grade_code : str
        e.g. ``"7"``.
    definition : str
        概念定义.
    key_points : str
        重点难点 text.
    rural_hint : str
        乡土化建议.
    extra : dict
        Other raw fields, reserved.
    """

    card_id: str
    name: str
    subject_code: str
    grade_code: str
    definition: str = ""
    key_points: str = ""
    rural_hint: str = ""
    extra: dict = field(default_factory=dict)

    def render(self) -> str:
        """Render the card as prompt context text.

        Returns
        -------
        str
        """
        parts = [f"[{self.card_id}] {self.name}", f"概念定义：{self.definition}"]
        if self.key_points:
            parts.append(f"重点难点：{self.key_points}")
        if self.rural_hint:
            parts.append(f"乡土化建议：{self.rural_hint}")
        return "\n".join(parts)


def _default_kb_path() -> Path:
    """Resolve the KB markdown path (env override first).

    Returns
    -------
    Path
    """
    env_path = os.getenv("XHZH_KB_PATH", "")
    if env_path:
        return Path(env_path)
    return config.BASE_DIR.parent / "知识库-人教版初中知识点-v0.1.md"


def _extract_field(block: str, label: str) -> str:
    """Extract ``- <label>：<content>`` (possibly multi-line) from a card block.

    Parameters
    ----------
    block : str
        Card markdown block.
    label : str
        Field label without colon, e.g. ``"概念定义"``.

    Returns
    -------
    str
        Field content; empty string if absent.
    """
    pattern = rf"- {label}：\s*(.+?)(?=\n- |\n###|\Z)"
    match = re.search(pattern, block, flags=re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def parse_cards(md_text: str) -> list[KnowledgeCard]:
    """Parse all ``### <ID> <name>`` cards from markdown text.

    Parameters
    ----------
    md_text : str
        Full KB markdown.

    Returns
    -------
    list of KnowledgeCard
        Cards in document order; malformed cards are skipped.
    """
    cards: list[KnowledgeCard] = []
    blocks = re.split(r"\n(?=### )", md_text)
    header_re = re.compile(r"^###\s+([A-Z]{3}-\d[A-ZQ]?-[\dS]+-[\d]+-K\d+)\s+(.+)$")
    for block in blocks:
        first_line = block.splitlines()[0].strip() if block.splitlines() else ""
        header = header_re.match(first_line)
        if not header:
            continue
        card_id, name = header.group(1), header.group(2).strip()
        subject_code = card_id.split("-")[0]
        grade_code = card_id.split("-")[1][0]
        cards.append(
            KnowledgeCard(
                card_id=card_id,
                name=name,
                subject_code=subject_code,
                grade_code=grade_code,
                definition=_extract_field(block, "概念定义"),
                key_points=_extract_field(block, "重点难点"),
                rural_hint=_extract_field(block, "乡土化建议"),
            )
        )
    return cards


def _bigrams(text: str) -> set[str]:
    """Compute character bigrams of a text.

    Parameters
    ----------
    text : str

    Returns
    -------
    set of str
    """
    cleaned = re.sub(r"[\s，。、《》""''：；？！的与和及]", "", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} if len(cleaned) > 1 else {cleaned}


def _clean_text(text: str) -> str:
    """Strip whitespace and punctuation for substring matching.

    Parameters
    ----------
    text : str
        Raw text.

    Returns
    -------
    str
        Cleaned text with separators removed.
    """
    return re.sub(r"[\s，。、《》""''：；？！的与和及]", "", text)


def retrieve(subject: str, grade: str, topic: str, top_k: int = 3) -> list[KnowledgeCard]:
    """Retrieve the most relevant knowledge cards for a request.

    Parameters
    ----------
    subject : str
        e.g. ``"数学"``.
    grade : str
        e.g. ``"七年级"``.
    topic : str
        Lesson topic text.
    top_k : int
        Max cards to return.

    Returns
    -------
    list of KnowledgeCard
        Ranked by relevance; empty list means no match (caller must
        follow PRD EARS-3.2 and declare missing standard reference).

    Notes
    -----
    命中规则（按序满足其一）：

    1. **名称子串**：清洗后的课题是卡片名称的连续子串（长度≥2）——
       如"方程"⊂"一元一次方程的解法与应用"，最精准，专治超短课题；
    2. **短课题词面命中**：课题 bigram ≤2 个时，重叠 ≥1 即命中；
    3. **常规阈值**：overlap ≥2 且查询侧覆盖率 ≥0.2——防"虚构课题误命中"
       的底线（虚构课题 overlap=1/覆盖率 0.111，过不了此规则）。
    """
    cards = load_cards()
    subject_code = SUBJECT_CODES.get(subject, "")
    grade_codes = GRADE_CODES.get(grade, ())
    cleaned_topic = _clean_text(topic)
    topic_grams = _bigrams(topic)
    is_short_query = len(topic_grams) <= 2

    scored: list[tuple[float, KnowledgeCard]] = []
    for card in cards:
        if subject_code and card.subject_code != subject_code:
            continue
        if grade_codes and card.grade_code not in grade_codes:
            continue
        # 只用名称+定义打分：card_id 的字符（7A/K01 等）与课题无关，纳入会产生噪音命中
        card_grams = _bigrams(card.name + card.definition)
        overlap = len(topic_grams & card_grams)
        coverage = overlap / max(len(topic_grams), 1)
        # 规则 1：名称子串直接命中
        name_hit = len(cleaned_topic) >= 2 and cleaned_topic in _clean_text(card.name)
        # 规则 2/3：词面阈值
        gram_hit = (is_short_query and overlap >= 1) or (overlap >= 2 and coverage >= 0.2)
        if name_hit or gram_hit:
            # 子串命中略微加分，保证短课题时最贴切的卡排在前面
            scored.append((float(overlap) + (10.0 if name_hit else 0.0), card))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [card for _, card in scored[:top_k]]


_CARDS_CACHE: list[KnowledgeCard] | None = None


def load_cards() -> list[KnowledgeCard]:
    """Load and cache all knowledge cards.

    Returns
    -------
    list of KnowledgeCard
    """
    global _CARDS_CACHE
    if _CARDS_CACHE is None:
        kb_path = _default_kb_path()
        if not kb_path.exists():
            raise FileNotFoundError(f"知识库文件不存在：{kb_path}（可用 XHZH_KB_PATH 指定）")
        _CARDS_CACHE = parse_cards(kb_path.read_text(encoding="utf-8"))
    return _CARDS_CACHE
