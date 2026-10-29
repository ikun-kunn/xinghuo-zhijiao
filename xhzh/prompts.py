"""三节点 LLM 提示词模板。

Notes
-----
规则来源：PRD EARS-3.1/4.1/4.2/5.3、语料方案 §2.2 通用编写要求。
所有节点要求 JSON 输出，字段与 mock 输出保持一致，前端无感切换。
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = (
    "你是'星火智教'的县乡教育教研专家。你的服务对象是县乡初中教师："
    "学生基础相对薄弱、留守儿童多、家庭辅导缺位。生成内容必须：\n"
    "1. 教学目标拆分为'基础巩固'与'拓展提升'两部分（EARS-4.1）；\n"
    "2. 情境与例题优先使用县乡生活场景（农贸市场、田间、赶集等），禁止高档商场、地铁等城市情境；\n"
    "3. 只使用提供的知识点卡内容，不得虚构课标条目；知识库无匹配时如实声明（EARS-3.2）；\n"
    "4. 严格按指定的 JSON 结构输出，不要输出 JSON 以外的任何文字。\n"
    "5. 每个字段的值必须是可直接使用的最终成品内容（如完整题干、课标原文）；"
    "严禁把填写说明、字数要求、场景要求等元描述写进字段值。\n"
    "6. JSON 输出规范：所有字段值必须严格遵循给定结构（objectives 等一律为字符串数组，不得改为嵌套对象）；"
    "字符串内容中**禁止使用英文双引号**（需要强调时用中文引号「」或不加引号），否则会破坏 JSON 结构。"
)

NODE0_INSTRUCTION = """\
## 任务：节点① 检索与生成
基于下方知识点卡，生成《{topic}》的基础教案框架。

## 知识点卡
{cards}

## 字段填写规则
- standard_ref：只引用与本课题**直接相关**的那一张知识点卡的 ID 与课标条目原文，不要罗列其他章节的卡片；
 知识点卡无课标映射时，该字段写：知识库暂无课标依据，内容仅供参考。不得虚构。

## 输出 JSON 结构（示例值为内容样式，须替换为《{topic}》的真实内容）
{{
  "standard_ref": "MAT-7A-3-0-K01 一元一次方程的解法与应用；能根据具体问题中的数量关系列出方程",
  "objectives": ["理解一元一次方程的概念，能识别方程中的未知数与系数", "能列出简单实际问题中的等量关系方程"],
  "key_points": ["找等量关系并列出方程"],
  "difficult_points": ["从文字情境中抽象出等量关系"],
  "stages": [{{"name": "情境导入", "minutes": 5, "desc": "用村头小卖部购买文具的情境引出问题"}}]
}}
要求：4 个环节共 45 分钟（情境导入/新知探究/巩固练习/小结作业）。
"""

NODE1_INSTRUCTION = """\
## 任务：节点② 学情适配
基于下方教案框架，为县乡学情（基础薄弱、留守儿童多、家庭辅导缺位）拆解教学目标。

## 教案框架
{framework}

## 知识点卡
{cards}

## 输出 JSON 结构（示例值为内容样式，须替换为本课的真实内容）
{{
  "goals_basic": ["能说出本课的核心概念", "能独立完成 2 道基础练习题"],
  "goals_extend": ["能将本课知识迁移到家庭生产情境中解决问题", "能提出 1 个开放性问题并尝试解答"],
  "scaffolding": "课堂上优先提问基础性问题建立信心；作业分层布置，留守儿童由课后服务时段辅导。"
}}
"""

NODE2_INSTRUCTION = """\
## 任务：节点③ 教具生成
基于下方框架与目标，生成随堂练习与板书设计。

**本课学科约束（最高优先级）**：本课课题是《{topic}》，练习必须严格匹配该课题所属的知识点。
数学类课题若为一元一次方程，所有题目**只设一个未知数 x**、只列一元一次方程；
不得引入第二个未知数或方程组，不得超出本课知识点范围。

## 教案框架
{framework}

## 分层目标
{goals}

## 知识点卡
{cards}

## 输出 JSON 结构（示例值为内容样式，须替换为本课的真实题目）
{{
  "exercises_basic": ["张大爷买苹果，每千克 4 元，付 50 元找回 10 元，他买了多少千克？", "王叔叔买化肥，每袋 50 元，买了若干袋共付 200 元，买了几袋？"],
  "exercises_extend": ["鸡兔同笼共 10 个头、26 只脚，鸡和兔各几只？"],
  "answers": ["设买了 x 千克：4x+10=50，解得 x=10（千克）", "设买了 x 袋：50x=200，解得 x=4（袋）", "设鸡 x 只：2x+4(10-x)=26，解得 x=7，兔 3 只"],
  "board": "一元一次方程 → 找等量关系 → 列方程 → 易错：单位与未知数含义"
}}
要求：
1. 每条练习必须**必须先设未知数、再列方程**才能求解（禁止一步乘法/除法口算题）；
2. **只能设一个未知数 x**（本课为一元一次方程）：严禁出现第二个未知数（y/p/q 等）、严禁方程组；
   若情境含两个量，用"其中一个量用含 x 的式子表示"的方式转化为一元方程；
3. 题干 ≤40 字，包含完整数量关系；数值必须自洽（用整数解优先，确保数据能算出唯一合理答案）；
4. answers 与 exercises_basic+exercises_extend **条数一致、顺序对应**，每条写"设…→方程→解得…"的简解，
   且必须验证答案唯一（一元一次方程有唯一解，不得出现凑解）；
5. 每题训练本课学科能力（数学练建模与求解，语文练修辞辨识/仿写），不得生成与本学科无关的题目；
6. 单价不超百元级，情境用县乡生活场景；
7. **输出前逐题回代验算**：把解出的 x 代回原方程与题干，确认等式成立、数值自洽、解为整数或合理小数；
   若验算不通过，重新设计题干数值后重算，严禁保留不成立的题目与答案。
"""


COURSEWARE_SYSTEM = (
    "你是'星火智教'的县乡初中课堂设计师。你的读者是**学生**，不是教师："
    "学生基础相对薄弱、容易走神、缺少家庭辅导。你要把教案改写成能让学生"
    "抬起头来的课堂课件脚本。写作要求：\n"
    "1. 用第二人称'你'对学生说话，口语化、有画面感、有悬念，禁止写'教学目标''重难点''环节设计'这类教师用语；\n"
    "2. 情境一律用县乡生活场景（集市、田间、小卖部、养殖、农机、赶集路费等），禁止高档商场、地铁、写字楼；\n"
    "3. 练习题干与数值**原样照搬教案**，不得改写数据、不得增减题目，包装成'关卡/挑战'即可；\n"
    "4. 每页只讲一件事：lead 是画面感的一句话，points 是学生真正要读的短句或题目；\n"
    "5. 严格控制字数：title ≤14 字，lead ≤30 字，每条 point ≤46 字，每页 points ≤4 条；\n"
    "6. 只输出 JSON，字符串内容中禁止使用英文双引号（强调请用「」），否则会破坏 JSON 结构。"
)

COURSEWARE_INSTRUCTION = """\
## 任务：把教案改写成学生向课件脚本
课题：《{topic}》（{grade} {subject} · {textbook}）

## 教案内容（教师语言，仅供你取材，不得照抄其措辞）
{plan}

## 页型说明（kind 必须从下列枚举中选取）
cover=封面；hook=开场悬念；quest=本关任务；explore=闯关锦囊/方法指导；
route=闯关路线（学习步骤）；practice=基础挑战；game=终极挑战；
answer=答案揭晓；board=本课方法卡/板书；summary=通关小结与作业。

## 页面编排要求
1. 共 9~12 页，顺序建议：cover → hook → quest → explore/route → practice → game → answer → board → summary；
2. 第一页必须是 cover，最后一页必须是 summary；board 页要写成'方法卡'式的短句，不要照抄整段板书文字；
3. 练习分两类呈现：practice 页放基础题、game 页放拓展题，每页 **最多 2 题**，题干原样保留；
4. answer 页放参考答案与简解，每页最多 3 条，与题目顺序对应；
5. 每页都要有 tag（≤6 字，如 第 1 关 / 锦囊 / 终极关）和 tip（给教师的 30 字内提示：怎么组织互动、怎么提问）；
6. hook 页要有真悬念：先给一个学生凭直觉会答错的小问题，再说'别急，学完这节课你就知道'；
7. **每页须规划视觉元素**，写入 visual 字段，类型按页面主题选择：
   - scene（场景插图）：cover / hook / summary 页用；theme 从枚举中选最贴切的：
     market（集市/小卖部）· farm（田间/种植/收获）· livestock（养殖/家禽）
     · school（校园/课堂）· tool（农具/工具）· travel（赶集出行/交通）
   - iconrow（图标短句行）：quest / explore / board 页用；icons 数组与 points 一一对应，
     每项从枚举中选：eye（观察）· bulb（思路）· calc（计算）· check（检验）
     · flag（目标）· hand（动手）· book（阅读）· chart（比较）
   - diagram（数量关系图）：practice / game 页用；diagram_type 从枚举中选：
     seg（线段图：从总量中减去已知量求剩余）· balance（天平图：两边相等的等量关系）
     · bar（条形图：几个量的大小比较）；nums 给 2~3 个数字，**必须从该页题干中原样提取**，不得改数；
     labels 用 ≤6 字短词标注（如 单价/数量/总价/共花/找回）
   - none：该页不需要视觉元素（answer 页固定用 none，避免与解题过程抢版面）
8. 视觉规划原则：与该页文字内容直接相关、帮助学生理解而非纯装饰；拿不准时选 none。

## 输出 JSON 结构
{{
  "slides": [
    {{
      "kind": "cover",
      "title": "一元一次方程的实际应用",
      "lead": "今天这节课，我们来解决一个集市上的真问题",
      "points": ["七年级 数学 · 人教版"],
      "tip": "",
      "tag": "课堂闯关",
      "visual": {{"type": "scene", "theme": "market"}}
    }},
    {{
      "kind": "practice",
      "title": "基础挑战 1",
      "lead": "先设未知数，再列方程",
      "points": ["村头小卖部买化肥，每袋 50 元，3 袋一共多少钱？"],
      "tip": "让学生口述设未知数的过程",
      "tag": "基础关",
      "visual": {{"type": "diagram", "diagram_type": "bar", "nums": [50, 3], "labels": ["单价", "袋数"]}}
    }}
  ]
}}
"""


def build_courseware_messages(params: dict[str, Any], result: dict[str, Any]) -> list[dict]:
    """Build chat messages for the student-facing courseware script.

    Parameters
    ----------
    params : dict
        User inputs: grade / subject / textbook / topic.
    result : dict
        Workflow result of nodes 0-2; only text-bearing fields are forwarded.

    Returns
    -------
    list of dict
        OpenAI-style ``[{"role": ..., "content": ...}]`` messages.
    """
    plan = {
        "topic": params.get("topic", ""),
        "stages": result.get("stages") or [],
        "goals_basic": result.get("goals_basic") or [],
        "goals_extend": result.get("goals_extend") or [],
        "key_points": result.get("key_points") or [],
        "difficult_points": result.get("difficult_points") or [],
        "exercises_basic": result.get("exercises_basic") or [],
        "exercises_extend": result.get("exercises_extend") or [],
        "answers": result.get("answers") or [],
        "board": result.get("board") or "",
    }
    user = COURSEWARE_INSTRUCTION.format(
        topic=params.get("topic", ""),
        grade=params.get("grade", ""),
        subject=params.get("subject", ""),
        textbook=params.get("textbook", ""),
        plan=json.dumps(plan, ensure_ascii=False, indent=1),
    )
    return [
        {"role": "system", "content": COURSEWARE_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_node_messages(node: int, params: dict[str, Any], context: dict[str, Any], cards: list[Any]) -> list[dict]:
    """Build chat messages for one node.

    Parameters
    ----------
    node : int
        0=检索与生成, 1=学情适配, 2=教具生成.
    params : dict
        User inputs.
    context : dict
        Previous node outputs (``node0`` / ``node1`` keys).
    cards : list of xhzh.kb.KnowledgeCard
        Retrieved cards; may be empty.

    Returns
    -------
    list of dict
        OpenAI-style ``[{"role": ..., "content": ...}]`` messages.
    """
    cards_text = "\n\n".join(card.render() for card in cards) if cards else "（知识库无匹配卡片，请如实声明）"
    if node == 0:
        user = NODE0_INSTRUCTION.format(topic=params["topic"], cards=cards_text)
    elif node == 1:
        user = NODE1_INSTRUCTION.format(
            framework=json.dumps(context.get("node0", {}), ensure_ascii=False, indent=1),
            cards=cards_text,
        )
    elif node == 2:
        user = NODE2_INSTRUCTION.format(
            topic=params["topic"],
            framework=json.dumps(context.get("node0", {}), ensure_ascii=False, indent=1),
            goals=json.dumps(context.get("node1", {}), ensure_ascii=False, indent=1),
            cards=cards_text,
        )
    else:
        raise ValueError(f"unknown node {node}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
