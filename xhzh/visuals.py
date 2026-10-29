"""课件视觉元素 Skill：按页面主题自动加入场景插图、图标行与数量关系图。

Notes
-----
全部使用 python-pptx 原生自选图形绘制，不依赖任何外部图标字体或网络
资源——县乡学校低带宽、离线投影环境即开即用；配色与卡片式版式
（:mod:`xhzh.assembly`）共用同一套色板，保证整套课件风格统一。

场景插图的图片来源有明确的优先级链（见 :func:`scene_image`）：
① ``assets/visuals/`` 下**AIGC 生成并经人工过目**的插画素材库（随包分发，
运行时只用 jpg；``src/`` 为 PNG 源图存档）→ ② 若显式开启
:mod:`xhzh.imagegen` 则尝试运行时生图（默认关闭，AIGC 图须过目）→
③ 内置矢量场景兜底。因此即使素材缺失、网络不可达，课件版面也永远
不会留白。

视觉类型（由课件脚本 ``visual`` 字段驱动，缺失或非法时按页型自动推断）：

- ``scene``   场景插图：审核插画（首选）或简笔矢量场景（集市/田间/家禽/
  校园/农具/出行），用于封面、悬念页与带情境的练习页；
- ``iconrow`` 图标徽章行：每个要点卡片右侧一枚"单字徽章"（看/想/算/
  验/标/做/读/比），用于任务、锦囊与方法卡页；
- ``diagram`` 数量关系图：线段图 / 天平图 / 条形图，数值从题干原样
  提取，用于练习与挑战页。

页型与视觉类型的兼容关系由 :data:`_ALLOWED` 固化：渲染器只为
hook / practice / game 页预留侧边栏，其余页型自动约束到可融入现有
版面的类型，避免视觉元素与文字抢版面。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# 与 assembly 卡片式版式共用的色板（复制而非导入，避免与渲染器循环依赖）
BG = "FDF7EF"
INK = "2C2A26"
GREEN = "2E7D5B"
ORANGE = "E4862B"
BLUE = "2F6FB5"
CARD = "FFFFFF"
CARD_LINE = "EFE3D2"
MUTED = "9A9088"
FONT = "微软雅黑"

# 主色 → 浅色底（视觉卡片背景），与 _KIND_STYLE 的 soft 保持一致
SOFT = {GREEN: "E7F1EB", BLUE: "E8F0F9", ORANGE: "FCEEDD"}

VISUAL_KINDS = ("scene", "iconrow", "diagram")
SCENE_THEMES = ("market", "farm", "livestock", "school", "tool", "travel")
DIAGRAM_TYPES = ("seg", "balance", "bar")

#: 场景插画素材库目录（项目根 ``assets/visuals/``）：运行时只用目录下的 ``.jpg``。
#: ``assets/visuals/src/`` 存放 AIGC 源图（PNG，1.5~1.8MB/张）仅作存档，
#: 不参与运行时解析——否则单张源图就会击穿 3MB 教学包预算。
#: 刻意在运行时按模块全局名解析，便于测试用 monkeypatch 指向空目录验证降级。
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "visuals"

#: 素材文件后缀优先级：``.jpg`` 体积小（<300KB），低带宽优先；``.png`` 为兜底
IMAGE_SUFFIXES = (".jpg", ".png")

#: 单个素材文件体积上限（字节）：超过则等比降采样后缓存复用，保证包体可控
MAX_ASSET_BYTES = 400 * 1024

#: 降采样目标宽度（像素）与 JPEG 质量：与现有素材库同规格（1280 宽 / q88）
DOWNSAMPLE_WIDTH = 1280
DOWNSAMPLE_QUALITY = 88

#: 第三方水印裁切守卫：右侧与底部各至少裁掉的比例。
#: 素材右下角带平台水印「AI生成 WORKBUDDY」（实测占 x≥0.92W、y≥0.94H），
#: 6% 仍会切进"AI"字头，故取 9%（含安全余量），使水印永远落在可见区之外。
WATERMARK_GUARD = 0.09

#: 主题 → 中文名（供图注、日志与后续扩展使用）
SCENE_LABELS = {
    "market": "乡村集市",
    "farm": "田间作物",
    "livestock": "农家养殖",
    "school": "乡村校园",
    "tool": "农具",
    "travel": "赶集路上",
}

#: AIGC 配图的合规水印文案（PPT 页面上标注，见 :func:`_scene_picture`）
AIGC_LABEL = "AI 生成配图"

# 图标名 → 徽章字符（微软雅黑必含的常用字，投影可读性优先于抽象符号）
ICON_CHAR = {
    "eye": "看",
    "bulb": "想",
    "calc": "算",
    "check": "验",
    "flag": "标",
    "hand": "做",
    "book": "读",
    "chart": "比",
}

# 页型 → 允许的视觉类型（决定渲染器如何腾出版面）
_ALLOWED = {
    "cover": ("scene",),
    "hook": ("scene", "diagram"),
    "quest": ("iconrow",),
    "explore": ("iconrow",),
    "route": (),
    "practice": ("diagram", "scene"),
    "game": ("diagram", "scene"),
    "answer": (),
    "board": ("iconrow",),
    "summary": (),
}

# 场景主题关键词（按序匹配，命中即用；县乡语境下 market 作兜底最贴切）
_THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "market": ("集市", "小卖部", "商店", "超市", "买", "卖", "价格", "化肥", "种子", "元"),
    "farm": ("田", "种植", "收", "玉米", "麦", "稻", "菜", "果园", "亩", "秧"),
    "livestock": ("鸡", "鸭", "鹅", "兔", "猪", "羊", "牛", "养殖", "蛋", "禽"),
    "school": ("学校", "教室", "课堂", "同学", "操场", "图书馆", "课本例题"),
    "tool": ("农具", "锄", "镰", "铲", "工具", "维修", "器械"),
    "travel": ("车", "路", "赶集", "出行", "坐", "公里", "速度", "步行"),
}

# 图标关键词（要点文本命中即映射到对应徽章）
_ICON_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("eye", ("观察", "看图", "审题", "找一找", "发现")),
    ("bulb", ("思路", "想", "理解", "明白", "为什么", "分析")),
    ("calc", ("算", "计算", "方程", "解", "列式", "求解")),
    ("check", ("检验", "验算", "检查", "回顾", "核对", "对答案")),
    ("flag", ("目标", "能", "会", "任务", "学会")),
    ("hand", ("动手", "做一做", "画", "写", "操作", "测量", "摆")),
    ("book", ("读", "阅读", "课本", "书", "资料", "预习")),
    ("chart", ("比较", "比一比", "大小", "多少", "统计", "排序")),
]


def plan_visual(spec: dict[str, Any], kind: str, points: list[str]) -> dict[str, Any] | None:
    """Plan the visual element for one slide (model output + local inference).

    Parameters
    ----------
    spec : dict
        Slide spec; may carry a model-produced ``visual`` dict.
    kind : str
        Slide kind (cover / hook / quest / practice / ...).
    points : list of str
        Slide bullet texts; used for keyword and number extraction.

    Returns
    -------
    dict or None
        ``{"type": "scene", "theme": ...}`` / ``{"type": "iconrow",
        "icons": [...]}`` / ``{"type": "diagram", "diagram_type": ...,
        "nums": [...], "labels": [...]}``, or ``None`` when the page
        should stay text-only (answer / route / summary).

    Notes
    -----
    模型的 visual 输出先按页型白名单约束（不兼容的类型直接丢弃），
    载荷缺失部分由本地推断补齐；练习页图表数值优先取模型给的数字，
    缺失时从题干正则提取——保证图表数字与题干永远一致。
    """
    allowed = _ALLOWED.get(kind, ())
    if not allowed:
        return None
    raw = spec.get("visual") if isinstance(spec.get("visual"), dict) else {}
    vtype = str(raw.get("type") or "").strip().lower()
    if vtype not in allowed:
        vtype = ""
    text = " ".join([str(spec.get("title") or ""), str(spec.get("lead") or ""), *map(str, points)])

    if "scene" in allowed and (vtype == "scene" or (not vtype and "scene" in allowed and "iconrow" not in allowed)):
        theme = str(raw.get("theme") or "").strip().lower()
        if theme not in SCENE_THEMES:
            theme = infer_theme(text)
        return {"type": "scene", "theme": theme}
    if vtype == "scene":
        theme = str(raw.get("theme") or "").strip().lower()
        return {"type": "scene", "theme": theme if theme in SCENE_THEMES else infer_theme(text)}
    if vtype == "iconrow" or "iconrow" in allowed:
        icons = [str(i).strip().lower() for i in (raw.get("icons") or []) if str(i).strip().lower() in ICON_CHAR]
        if not icons or len(icons) != len(points):
            icons = infer_icons(points)
        return {"type": "iconrow", "icons": icons[:max(1, len(points))]}
    # 剩余情况：practice / game / hook 的 diagram 分支
    nums = [float(n) for n in (raw.get("nums") or []) if _is_num(n)]
    if not nums:
        nums = extract_nums(text)
    labels = [str(l).strip()[:6] for l in (raw.get("labels") or []) if str(l).strip()]
    dtype = str(raw.get("diagram_type") or "").strip().lower()
    if vtype == "diagram" and dtype in DIAGRAM_TYPES and len(nums) >= 2:
        return {"type": "diagram", "diagram_type": dtype, "nums": nums[:3], "labels": labels}
    if len(nums) >= 2:
        return {"type": "diagram", "diagram_type": _infer_diagram(text, nums), "nums": nums[:3], "labels": labels}
    # 数值不足无法画图：练习/挑战页退化为情境插图，保证版面不空
    if kind in ("practice", "game"):
        return {"type": "scene", "theme": infer_theme(text)}
    return None


def infer_theme(text: str) -> str:
    """Infer a scene theme from page text via keyword matching.

    Parameters
    ----------
    text : str
        Combined title / lead / points text.

    Returns
    -------
    str
        One of :data:`SCENE_THEMES`; ``"market"`` as the county-default.
    """
    for theme, keywords in _THEME_KEYWORDS.items():
        if any(word in text for word in keywords):
            return theme
    return "market"


def infer_icons(points: list[str]) -> list[str]:
    """Infer one icon name per point via keyword matching.

    Parameters
    ----------
    points : list of str
        Bullet texts.

    Returns
    -------
    list of str
        Icon names, same length as ``points``; unmatched points get
        ``"flag"``.
    """
    icons: list[str] = []
    for point in points:
        matched = "flag"
        for name, keywords in _ICON_KEYWORDS:
            if any(word in str(point) for word in keywords):
                matched = name
                break
        icons.append(matched)
    return icons or ["flag"]


def extract_nums(text: str) -> list[float]:
    """Extract up to three positive numbers from text.

    Parameters
    ----------
    text : str
        Usually the exercise stem.

    Returns
    -------
    list of float
        Numbers in order of appearance, zeros excluded.
    """
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text) if _is_num(float(n))][:3]


def _is_num(value: Any) -> bool:
    """Check that a value is a usable positive diagram number.

    Parameters
    ----------
    value : Any

    Returns
    -------
    bool
    """
    return isinstance(value, (int, float)) and 0 < float(value) < 1_000_000


def _infer_diagram(text: str, nums: list[float]) -> str:
    """Infer the diagram type from exercise wording.

    Parameters
    ----------
    text : str
        Exercise stem.
    nums : list of float
        Extracted numbers.

    Returns
    -------
    str
        ``"seg"`` / ``"balance"`` / ``"bar"``.
    """
    if any(word in text for word in ("找回", "还剩", "剩下", "比", "多", "少")):
        return "seg"
    if any(word in text for word in ("共", "总价", "一样", "相等", "等于")):
        return "balance"
    return "bar"


# ----------------------------------------------------------------------
# 绘制入口
# ----------------------------------------------------------------------
def draw_side_visual(slide, visual: dict[str, Any], x: float, y: float, w: float, h: float, accent: str) -> bool:
    """Draw a scene or diagram card in the side region of a slide.

    Parameters
    ----------
    slide : pptx.slide.Slide
    visual : dict
        Output of :func:`plan_visual` (scene or diagram).
    x, y, w, h : float
        Region geometry in inches.
    accent : str
        Page accent hex color.

    Returns
    -------
    bool
        Whether anything was drawn.
    """
    if w < 2.6 or h < 1.6:
        return False
    vtype = visual.get("type")
    if vtype == "scene":
        _scene_card(slide, str(visual.get("theme") or "market"), x, y, w, h, accent)
        return True
    if vtype == "diagram":
        return _diagram_card(
            slide,
            str(visual.get("diagram_type") or "bar"),
            [float(n) for n in visual.get("nums") or []],
            [str(l) for l in visual.get("labels") or []],
            x, y, w, h, accent,
        )
    return False


def draw_cover_scene(slide, theme: str, x: float, y: float, w: float, h: float) -> bool:
    """Draw a theme scene on the cover slide (picture first, vector fallback).

    Parameters
    ----------
    slide : pptx.slide.Slide
    theme : str
        One of :data:`SCENE_THEMES`.
    x, y, w, h : float
        Region geometry in inches.

    Returns
    -------
    bool
        Whether anything was drawn.

    Notes
    -----
    封面不留圆角底板之外的空白：命中审核插画（或可选生图）时用
    "浅色圆角底板 + 居中裁切填充 + AIGC 水印"，否则用内置矢量场景，
    两条路径都保证版面完整。
    """
    if theme not in SCENE_THEMES or w < 2.2 or h < 1.4:
        return False
    image = scene_image(theme)
    if image is not None and _scene_picture(slide, image, x, y, w, h, GREEN):
        return True
    _SCENES[theme](slide, x, y, w, h)
    return True


def draw_icon_badge(slide, icon: str, x: float, y: float, accent: str, size: float = 0.44) -> None:
    """Draw one character icon badge (accent circle + white glyph).

    Parameters
    ----------
    slide : pptx.slide.Slide
    icon : str
        Icon name from :data:`ICON_CHAR`.
    x, y : float
        Top-left of the badge in inches.
    accent : str
        Badge fill color.
    size : float
        Badge diameter in inches.
    """
    _oval(slide, x, y, size, size, accent)
    _text(
        slide, x, y - 0.015, size, size, ICON_CHAR.get(icon, "标"),
        size=13, bold=True, color="FFFFFF", align="center", anchor="middle",
    )


# ----------------------------------------------------------------------
# 插画素材库（审核过的图片优先，矢量兜底）
# ----------------------------------------------------------------------
def scene_image(theme: str) -> Path | None:
    """Resolve the best available illustration for one scene theme.

    Parameters
    ----------
    theme : str
        Theme key; must be one of :data:`SCENE_THEMES`.

    Returns
    -------
    Path or None
        Local image path, or ``None`` when no source is available.

    Raises
    ------
    None
        All probes are guarded: a corrupt素材/网络异常只会退化为 ``None``，
        由调用方切到内置矢量场景。

    Notes
    -----
    解析优先级链（命中即返回，不再继续尝试）：

    1. ``ASSET_DIR/{theme}.jpg``——AIGC 生成并经人工过目的素材库，体积小、
       低带宽优先；
    2. ``ASSET_DIR/{theme}.png``——同主题兜底（防御性保留；源图已归档到
       ``ASSET_DIR/src/``，故正常运行不会命中）；
    3. ``xhzh.imagegen`` 已显式开启（``XHZH_IMAGE_ALLOW=1``）且凭据齐备时
       尝试运行时生图；
    4. 以上都不可用 → ``None``（渲染器降级为矢量场景）。

    体积守卫：命中文件 > :data:`MAX_ASSET_BYTES`（400KB）时先经
    :func:`_lightweight_copy` 等比降采样为 1280 宽 JPEG 缓存再返回，
    避免单张大图击穿教学包体积预算；降采样失败返回 ``None``。

    未知 theme 直接返回 ``None``——不猜主题、不糊图。
    """
    key = str(theme or "").strip().lower()
    if key not in SCENE_THEMES:
        return None
    for suffix in IMAGE_SUFFIXES:
        candidate = ASSET_DIR / f"{key}{suffix}"
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return _lightweight_copy(candidate)
        except OSError as error:  # 权限/坏链接等：继续尝试下一个来源
            LOGGER.warning("插画素材探测失败（%s）：%s", candidate, error)
    try:
        from . import imagegen

        if imagegen.is_enabled() and imagegen.is_configured():
            generated = imagegen.generate_scene(key, SCENE_LABELS.get(key, ""))
            if generated is not None and Path(generated).is_file():
                return _lightweight_copy(Path(generated))
    except Exception as error:  # noqa: BLE001 - 生图不可用必须静默降级
        LOGGER.warning("运行时生图不可用（theme=%s）：%s", key, error)
    return None


def _lightweight_copy(path: Path) -> Path | None:
    """Return a small (<= :data:`MAX_ASSET_BYTES`) JPEG for an oversized asset.

    Parameters
    ----------
    path : Path
        Candidate asset file (``.jpg`` / ``.png``).

    Returns
    -------
    Path or None
        The original path when it is already small enough; otherwise the
        cached downsampled JPEG path; ``None`` when downsampling failed
        (caller then falls back to the vector scene).

    Raises
    ------
    None
        Pillow / 文件系统异常一律 ``logging.warning`` 后返回 ``None``。

    Notes
    -----
    体积守卫的存在理由：``assets/visuals/src/`` 里的 AIGC 源图单张
    1.5~1.8MB，一旦被嵌入课件，整包体积会从 ~0.25MB 涨到 1.6MB 以上，
    超出"县乡低带宽 ≤3MB"预算。降采样只用 Pillow（python-pptx 的既有
    依赖，不新增要求），结果按
    ``DATA_DIR/images/asset-{stem}-{size}-w1280-q88.jpg`` 缓存复用。
    """
    try:
        size = path.stat().st_size
    except OSError as error:
        LOGGER.warning("素材体积读取失败（%s）：%s", path, error)
        return None
    if size <= MAX_ASSET_BYTES:
        return path
    try:
        from . import config

        target = (
            config.DATA_DIR / "images"
            / f"asset-{path.stem}-{size}-w{DOWNSAMPLE_WIDTH}-q{DOWNSAMPLE_QUALITY}.jpg"
        )
        if target.is_file() and target.stat().st_size > 0:
            return target
        Image = _pillow_image()
        if Image is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as source:
            picture = source.convert("RGB")
            width, height = picture.size
            if width > DOWNSAMPLE_WIDTH:
                new_height = max(1, round(height * DOWNSAMPLE_WIDTH / width))
                resample = getattr(Image, "Resampling", Image).LANCZOS
                picture = picture.resize((DOWNSAMPLE_WIDTH, new_height), resample)
            picture.save(target, "JPEG", quality=DOWNSAMPLE_QUALITY, optimize=True)
        if target.is_file() and target.stat().st_size > 0:
            LOGGER.info("素材降采样完成：%s -> %s（%s → %s 字节）", path.name, target.name, size, target.stat().st_size)
            return target
        return None
    except Exception as error:  # noqa: BLE001 - 降采样失败即走矢量兜底
        LOGGER.warning("素材降采样失败（%s）：%s", path, error)
        return None


def _pillow_image():
    """Import Pillow lazily, tolerating environments without it.

    Returns
    -------
    module or None
        ``PIL.Image`` module, or ``None`` when Pillow is unavailable.
    """
    try:
        from PIL import Image
    except Exception as error:  # noqa: BLE001 - 缺 Pillow 时降级为矢量场景
        LOGGER.warning("Pillow 不可用，跳过素材降采样：%s", error)
        return None
    return Image


def _add_picture_fill(slide, path: Path, x: float, y: float, w: float, h: float):
    """Insert a picture cropped to fill a target box without distortion.

    Parameters
    ----------
    slide : pptx.slide.Slide
    path : Path
        Local image file (``.jpg`` / ``.png``).
    x, y, w, h : float
        Target box geometry in inches.

    Returns
    -------
    pptx.shapes.picture.Picture or None
        The inserted shape, or ``None`` when the image could not be inserted.

    Raises
    ------
    None

    Notes
    -----
    裁切规则分两步：

    1. **等比例裁切**：按目标框宽高比挑出原图中的最大可见窗口，窗口宽高比
       严格等于目标框，因此 python-pptx 把窗口拉伸到框内时**不产生任何变形**；
    2. **水印守卫（合规）**：素材右下角带平台水印「AI生成 WORKBUDDY」，
       必须始终落在可见区之外。故强制 ``crop_right >= WATERMARK_GUARD`` 且
       ``crop_bottom >= WATERMARK_GUARD``（9%）。若原图比例与目标框一致、
       自然裁切为 0，则按守卫值先裁右侧/底部，再把多出的量从对侧（左/上）
       补回，使可见窗口仍满足第 1 步的比例约束。

    实现上先解出可见窗口比例（``keep_y`` / ``keep_x``），再把裁切量在各侧
    分配：``crop_right = max(total_x / 2, GUARD)``、``crop_left = total_x - crop_right``
    （上下同理），故裁切量既满足守卫又是左右/上下尽量对称的，构图偏移
    不超过一个守卫值。各侧裁切量上限由 ``keep <= 1 - GUARD`` 自动保证
    ``crop_left + crop_right < 1``（否则 python-pptx 会写出非法 XML）。
    """
    from pptx.util import Inches

    if w <= 0.1 or h <= 0.1:
        return None
    try:
        picture = slide.shapes.add_picture(str(path), Inches(x), Inches(y), Inches(w), Inches(h))
    except Exception as error:  # noqa: BLE001 - 坏图/非图片文件
        LOGGER.warning("插画插入失败（%s）：%s", path, error)
        return None
    try:
        native_w, native_h = picture.image.size
        image_ratio = float(native_w) / float(native_h)
    except Exception as error:  # noqa: BLE001 - 取不到原始尺寸就按原样放置
        LOGGER.warning("插画尺寸读取失败（%s）：%s", path, error)
        return picture
    if image_ratio <= 0:
        return picture
    box_ratio = float(w) / float(h)
    # 目标框相对原图的"缩放系数"：>1 表示框更扁（该裁上下），<1 表示框更瘦（该裁左右）
    scale = box_ratio / image_ratio
    limit = 1.0 - WATERMARK_GUARD  # 单侧可见比例上限，确保右/底各留 9% 裁切量
    keep_y = limit / scale if scale >= 1.0 else limit
    keep_x = scale * keep_y  # 严格由目标框比例导出 → 可见窗口与被填充框同比例（不变形）
    keep_x = max(min(keep_x, limit), 0.02)
    keep_y = max(min(keep_y, limit), 0.02)
    total_x = 1.0 - keep_x
    total_y = 1.0 - keep_y
    # 先在右/底保住守卫值，剩余量给对侧：构图偏移不超过一个守卫值
    crop_right = max(total_x / 2.0, WATERMARK_GUARD)
    crop_left = total_x - crop_right
    crop_bottom = max(total_y / 2.0, WATERMARK_GUARD)
    crop_top = total_y - crop_bottom
    picture.crop_left = crop_left
    picture.crop_right = crop_right
    picture.crop_top = crop_top
    picture.crop_bottom = crop_bottom
    return picture


def _scene_picture(slide, image: Path, x: float, y: float, w: float, h: float, accent: str) -> bool:
    """Draw "soft card + cropped picture + AIGC watermark" for a scene region.

    Parameters
    ----------
    slide : pptx.slide.Slide
    image : Path
        AIGC 素材库图片或运行时生成的插画。
    x, y, w, h : float
        Card geometry in inches.
    accent : str
        Page accent color (selects the soft background).

    Returns
    -------
    bool
        ``False`` when the picture could not be placed (caller then falls
        back to the vector scene).

    Notes
    -----
    AIGC 水印是 PRD 合规项：凡出现生成式配图，右下角固定标注
    ``AI 生成配图``（8pt 灰字 + 白色小胶囊，保证在任何画面上都可读）。
    """
    pad = 0.1  # 卡片内边距（英寸）
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    if inner_w <= 0.3 or inner_h <= 0.3:
        return False
    _round(slide, x, y, w, h, SOFT.get(accent, "E7F1EB"))
    if _add_picture_fill(slide, image, x + pad, y + pad, inner_w, inner_h) is None:
        return False
    label_w, label_h = 1.18, 0.24
    label_x = x + w - pad - label_w
    label_y = y + h - pad - label_h
    _round(slide, label_x, label_y, label_w, label_h, CARD, line=CARD_LINE)
    _text(
        slide, label_x, label_y - 0.01, label_w, label_h, AIGC_LABEL,
        size=8, color=MUTED, align="center", anchor="middle",
    )
    return True


# ----------------------------------------------------------------------
# 场景插图（简笔矢量，全部原生形状）
# ----------------------------------------------------------------------
def _scene_card(slide, theme: str, x: float, y: float, w: float, h: float, accent: str) -> None:
    """Draw a scene on a soft rounded card (picture first, vector fallback).

    Parameters
    ----------
    slide : pptx.slide.Slide
    theme : str
    x, y, w, h : float
        Region geometry in inches.
    accent : str
        Page accent color (selects the card background).

    Returns
    -------
    None

    Notes
    -----
    有可用插画时走 :func:`_scene_picture`（含 AIGC 水印）；否则用矢量
    场景——**降级必须可用**是硬约束，素材库缺失也不得抛异常。
    """
    key = theme if theme in _SCENES else "market"
    image = scene_image(key)
    if image is not None and _scene_picture(slide, image, x, y, w, h, accent):
        return
    _round(slide, x, y, w, h, SOFT.get(accent, "E7F1EB"))
    _SCENES[key](slide, x + 0.18, y + 0.18, w - 0.36, h - 0.36)


def _market(slide, x: float, y: float, w: float, h: float) -> None:
    """Draw a market-stall scene.

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Scene region in inches.
    """
    u, v = w / 10, h / 8
    _rect(slide, x + 0.6 * u, y + 6.6 * v, 8.8 * u, 0.22 * v, "D9C9B4")
    for offset in (1.1, 8.6):
        _rect(slide, x + offset * u, y + 3.1 * v, 0.16 * u, 3.5 * v, "8A7A66")
    _rect(slide, x + 1.0 * u, y + 5.1 * v, 7.9 * u, 0.3 * v, "8A7A66")
    for index in range(5):
        _rect(
            slide, x + (0.7 + index * 1.75) * u, y + 2.3 * v, 1.75 * u, 0.8 * v,
            (ORANGE, CARD, GREEN, CARD, ORANGE)[index], line="D9C9B4",
        )
    for offset, color in ((2.0, ORANGE), (3.4, GREEN), (4.8, BLUE)):
        _oval(slide, x + offset * u, y + 4.0 * v, 0.95 * u, 0.95 * u, color)


def _farm(slide, x: float, y: float, w: float, h: float) -> None:
    """Draw a field scene (sun, sprouts, crop rows).

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Scene region in inches.
    """
    u, v = w / 10, h / 8
    _oval(slide, x + 7.7 * u, y + 0.5 * v, 1.6 * u, 1.6 * v, ORANGE)
    for index, color in enumerate(("7FA98F", "5E8F72", GREEN)):
        _rect(slide, x + 0.6 * u, y + (4.6 + index * 1.05) * v, (8.8 - index * 0.7) * u, 0.26 * v, color)
    for index in range(4):
        _tri(slide, x + (1.3 + index * 2.1) * u, y + 3.3 * v, 0.5 * u, 1.0 * v, GREEN)


def _livestock(slide, x: float, y: float, w: float, h: float) -> None:
    """Draw a farmyard scene (fence, hen, egg).

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Scene region in inches.
    """
    u, v = w / 10, h / 8
    for offset in (0.5, 1.7, 2.9):
        _rect(slide, x + offset * u, y + 3.0 * v, 0.12 * u, 1.9 * v, "8A7A66")
    _rect(slide, x + 0.4 * u, y + 4.5 * v, 3.2 * u, 0.14 * v, "8A7A66")
    _rect(slide, x + 0.6 * u, y + 6.7 * v, 8.8 * u, 0.2 * v, "D9C9B4")
    _oval(slide, x + 4.6 * u, y + 4.1 * v, 2.7 * u, 1.9 * v, CARD, line="D9C9B4")
    _oval(slide, x + 6.8 * u, y + 3.3 * v, 1.2 * u, 1.2 * v, CARD, line="D9C9B4")
    _rect(slide, x + 7.15 * u, y + 3.0 * v, 0.5 * u, 0.3 * v, ORANGE)
    _tri(slide, x + 7.9 * u, y + 3.6 * v, 0.5 * u, 0.42 * v, ORANGE)
    _oval(slide, x + 7.5 * u, y + 3.7 * v, 0.18 * u, 0.18 * v, INK)
    for offset in (5.2, 6.2):
        _rect(slide, x + offset * u, y + 5.9 * v, 0.1 * u, 0.8 * v, ORANGE)
    _oval(slide, x + 8.7 * u, y + 6.0 * v, 0.75 * u, 0.9 * v, "F5E6C8")


def _school(slide, x: float, y: float, w: float, h: float) -> None:
    """Draw a schoolhouse scene (building, roof, door, flag).

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Scene region in inches.
    """
    u, v = w / 10, h / 8
    _rect(slide, x + 0.7 * u, y + 6.7 * v, 8.6 * u, 0.2 * v, "D9C9B4")
    _rect(slide, x + 2.0 * u, y + 3.2 * v, 6.0 * u, 3.5 * v, CARD, line="D9C9B4")
    _tri(slide, x + 1.5 * u, y + 2.1 * v, 7.0 * u, 1.2 * v, GREEN)
    _rect(slide, x + 4.45 * u, y + 4.9 * v, 1.1 * u, 1.8 * v, ORANGE)
    for offset in (2.6, 5.6):
        _rect(slide, x + offset * u, y + 3.7 * v, 1.2 * u, 0.95 * v, "D8E8F5")
    _rect(slide, x + 8.6 * u, y + 1.1 * v, 0.1 * u, 2.6 * v, "8A7A66")
    _rect(slide, x + 8.7 * u, y + 1.2 * v, 1.0 * u, 0.55 * v, ORANGE)


def _tool(slide, x: float, y: float, w: float, h: float) -> None:
    """Draw a tool scene (spade in soil, bucket).

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Scene region in inches.
    """
    u, v = w / 10, h / 8
    _rect(slide, x + 0.8 * u, y + 6.5 * v, 8.4 * u, 0.26 * v, "D9C9B4")
    _oval(slide, x + 4.55 * u, y + 1.5 * v, 0.55 * u, 0.45 * v, "8A7A66")
    _rect(slide, x + 4.72 * u, y + 1.9 * v, 0.2 * u, 3.4 * v, "8A7A66")
    _tri(slide, x + 4.05 * u, y + 5.2 * v, 1.55 * u, 1.3 * v, "6B7F8E", rotation=180)
    _tri(slide, x + 7.4 * u, y + 5.0 * v, 1.9 * u, 1.4 * v, "9AA5AE", rotation=180)
    _rect(slide, x + 7.75 * u, y + 4.3 * v, 1.2 * u, 0.7 * v, "9AA5AE")


def _travel(slide, x: float, y: float, w: float, h: float) -> None:
    """Draw a travel scene (bus on a road).

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Scene region in inches.
    """
    u, v = w / 10, h / 8
    _oval(slide, x + 7.9 * u, y + 0.5 * v, 1.4 * u, 1.4 * v, ORANGE)
    _rect(slide, x + 0.5 * u, y + 6.3 * v, 9.0 * u, 0.8 * v, "D9C9B4")
    for index in range(4):
        _rect(slide, x + (1.2 + index * 2.1) * u, y + 6.62 * v, 0.8 * u, 0.14 * v, CARD)
    _round(slide, x + 1.7 * u, y + 3.4 * v, 5.5 * u, 2.5 * v, ORANGE)
    for index in range(3):
        _rect(slide, x + (2.2 + index * 1.5) * u, y + 3.8 * v, 1.05 * u, 0.75 * v, CARD)
    for offset in (2.5, 5.3):
        _oval(slide, x + offset * u, y + 5.7 * v, 1.0 * u, 1.0 * v, INK)


_SCENES = {
    "market": _market,
    "farm": _farm,
    "livestock": _livestock,
    "school": _school,
    "tool": _tool,
    "travel": _travel,
}


# ----------------------------------------------------------------------
# 数量关系图（数值取自题干）
# ----------------------------------------------------------------------
def _diagram_card(slide, dtype: str, nums: list[float], labels: list[str], x: float, y: float, w: float, h: float, accent: str) -> bool:
    """Draw a diagram on a soft rounded card.

    Parameters
    ----------
    slide : pptx.slide.Slide
    dtype : str
        ``seg`` / ``balance`` / ``bar``.
    nums : list of float
        Values from the exercise stem.
    labels : list of str
        Short labels for each value.
    x, y, w, h : float
        Region geometry in inches.
    accent : str
        Page accent color.

    Returns
    -------
    bool
        Whether a diagram was drawn.
    """
    nums = [n for n in nums if _is_num(n)][:3]
    if len(nums) < 2:
        return False
    _round(slide, x, y, w, h, SOFT.get(accent, "E7F1EB"))
    name = {"seg": "线段图", "balance": "天平图", "bar": "条形图"}[dtype]
    _text(slide, x + 0.32, y + 0.24, w - 0.64, 0.4, f"数量关系 · {name}", size=12, bold=True, color=accent)
    inner_x, inner_y = x + 0.32, y + 0.78
    inner_w, inner_h = w - 0.64, h - 1.06
    if dtype == "bar":
        _bar_chart(slide, nums, labels, inner_x, inner_y, inner_w, inner_h, accent)
    elif dtype == "balance":
        _balance(slide, nums, labels, inner_x, inner_y, inner_w, inner_h, accent)
    else:
        _seg(slide, nums, labels, inner_x, inner_y, inner_w, inner_h, accent)
    return True


def _bar_chart(slide, nums: list[float], labels: list[str], x: float, y: float, w: float, h: float, accent: str) -> None:
    """Draw a vertical bar chart.

    Parameters
    ----------
    slide : pptx.slide.Slide
    nums : list of float
    labels : list of str
    x, y, w, h : float
        Chart area in inches.
    accent : str
    """
    count = len(nums)
    colors = (accent, BLUE, ORANGE)
    bar_w = min(0.95, w * 0.18)
    gap = (w - count * bar_w) / (count + 1)
    base_y, chart_h = y + h - 0.5, h - 0.9
    _rect(slide, x, y + 0.08, 0.04, chart_h + 0.06, "B4B2A9")
    _rect(slide, x, base_y, w, 0.04, "B4B2A9")
    peak = max(nums)
    for index, value in enumerate(nums):
        bar_h = max(0.25, value / peak * chart_h)
        bar_x = x + gap + index * (bar_w + gap)
        _rect(slide, bar_x, base_y - bar_h, bar_w, bar_h, colors[index % 3])
        _text(slide, bar_x - 0.2, base_y - bar_h - 0.4, bar_w + 0.4, 0.32, _fmt(value), size=13, bold=True, color=INK, align="center")
        label = labels[index] if index < len(labels) else f"数量{index + 1}"
        _text(slide, bar_x - 0.25, base_y + 0.08, bar_w + 0.5, 0.32, label, size=11, color=MUTED, align="center")


def _balance(slide, nums: list[float], labels: list[str], x: float, y: float, w: float, h: float, accent: str) -> None:
    """Draw a balance-scale diagram (left pan equals right pan).

    Parameters
    ----------
    slide : pptx.slide.Slide
    nums : list of float
    labels : list of str
    x, y, w, h : float
        Diagram area in inches.
    accent : str
    """
    beam_w = min(3.6, w * 0.72)
    beam_y = y + 0.55
    cx = x + w / 2
    _rect(slide, cx - beam_w / 2, beam_y, beam_w, 0.1, "8A7A66")
    _rect(slide, cx - 0.05, beam_y, 0.1, h - 0.75, "8A7A66")
    _rect(slide, cx - 0.5, beam_y + h - 0.78, 1.0, 0.12, "8A7A66")
    _text(slide, cx - 0.25, beam_y + 0.12, 0.5, 0.4, "＝", size=17, bold=True, color=accent, align="center")
    for side, (value, pan_x) in enumerate((
        (nums[0], cx - beam_w / 2),
        (nums[1], cx + beam_w / 2),
    )):
        for offset in (-0.75, 0.75):
            _rect(slide, pan_x + offset - 0.02, beam_y + 0.1, 0.04, 0.55, "8A7A66")
        _rect(slide, pan_x - 0.85, beam_y + 0.62, 1.7, 0.1, accent)
        chip_y = beam_y + 0.62
        _round(slide, pan_x - 0.62, chip_y - 0.5, 1.24, 0.5, CARD, line="D9C9B4")
        label = labels[side] if side < len(labels) else f"{'左' if side == 0 else '右'}盘"
        _text(slide, pan_x - 0.62, chip_y - 0.5, 1.24, 0.5, f"{label} {_fmt(value)}", size=12, bold=True, color=INK, align="center", anchor="middle")


def _seg(slide, nums: list[float], labels: list[str], x: float, y: float, w: float, h: float, accent: str) -> None:
    """Draw a segment (tape) diagram: total minus known parts.

    Parameters
    ----------
    slide : pptx.slide.Slide
    nums : list of float
        ``[total, part1, (part2)]`` in any order; the largest value is
        treated as the total.
    labels : list of str
    x, y, w, h : float
        Diagram area in inches.
    accent : str
    """
    total = max(nums)
    parts = [n for n in nums if n != total][:2]
    if not parts:
        parts = [nums[0]]
    used = sum(parts)
    remainder = total - used if total > used else 0.0
    top_y, part_y = y + 0.5, y + h * 0.55
    _rect(slide, x, top_y, w, 0.24, accent)
    _text(slide, x, top_y - 0.42, w, 0.34, f"共 {_fmt(total)}", size=13, bold=True, color=INK, align="right")
    cursor = x
    seg_colors = ("8FBBA4", "A8C9B8")
    for index, part in enumerate(parts):
        seg_w = part / total * w
        _rect(slide, cursor, part_y, seg_w, 0.24, seg_colors[index % 2])
        label = labels[index + 1] if index + 1 < len(labels) else "已知"
        _text(slide, cursor - 0.15, part_y + 0.3, seg_w + 0.3, 0.32, f"{label} {_fmt(part)}", size=11, color=MUTED, align="center")
        cursor += seg_w
    if remainder > 0:
        seg_w = remainder / total * w
        _rect(slide, cursor, part_y, seg_w, 0.24, ORANGE)
        _text(slide, cursor - 0.15, part_y + 0.3, seg_w + 0.3, 0.32, "还差？", size=11, bold=True, color=ORANGE, align="center")


def _fmt(value: float) -> str:
    """Format a number without trailing zeros.

    Parameters
    ----------
    value : float

    Returns
    -------
    str
        ``"50"`` for 50.0, ``"2.5"`` for 2.5.
    """
    return f"{value:g}"


# ----------------------------------------------------------------------
# 绘制原语（与 assembly 同口径的本地实现，避免相互依赖）
# ----------------------------------------------------------------------
def _paint(shape, fill: str, line: str | None = None) -> None:
    """Apply solid fill and optional outline to an autoshape.

    Parameters
    ----------
    shape : pptx.shapes.autoshape.Shape
    fill : str
        Hex RGB fill color.
    line : str or None
        Hex RGB outline color.

    Returns
    -------
    None
    """
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(fill)
    if line:
        shape.line.color.rgb = RGBColor.from_string(line)
        shape.line.width = Pt(1)
    else:
        shape.line.fill.background()
    shape.shadow.inherit = False


def _rect(slide, x: float, y: float, w: float, h: float, fill: str, line: str | None = None):
    """Add a rectangle.

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Geometry in inches.
    fill : str
    line : str or None

    Returns
    -------
    pptx.shapes.autoshape.Shape
    """
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    _paint(shape, fill, line)
    return shape


def _round(slide, x: float, y: float, w: float, h: float, fill: str, line: str | None = None):
    """Add a rounded rectangle.

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Geometry in inches.
    fill : str
    line : str or None

    Returns
    -------
    pptx.shapes.autoshape.Shape
    """
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = 0.35 if min(w, h) < 0.5 else 0.06
    _paint(shape, fill, line)
    return shape


def _oval(slide, x: float, y: float, w: float, h: float, fill: str, line: str | None = None):
    """Add an oval.

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Geometry in inches.
    fill : str
    line : str or None

    Returns
    -------
    pptx.shapes.autoshape.Shape
    """
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(y), Inches(w), Inches(h))
    _paint(shape, fill, line)
    return shape


def _tri(slide, x: float, y: float, w: float, h: float, fill: str, rotation: float = 0):
    """Add an isosceles triangle with optional rotation.

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Geometry in inches.
    fill : str
    rotation : float
        Degrees clockwise; 180 flips the apex downward.

    Returns
    -------
    pptx.shapes.autoshape.Shape
    """
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.ISOSCELES_TRIANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    if rotation:
        shape.rotation = rotation
    _paint(shape, fill)
    return shape


def _text(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    size: int = 18,
    bold: bool = False,
    color: str = INK,
    align: str = "left",
    anchor: str = "top",
):
    """Add a text box with Chinese-safe font settings.

    Parameters
    ----------
    slide : pptx.slide.Slide
    x, y, w, h : float
        Geometry in inches.
    text : str
    size : int
    bold : bool
    color : str
    align : str
        ``"left"`` / ``"center"`` / ``"right"``.
    anchor : str
        ``"top"`` / ``"middle"`` / ``"bottom"``.

    Returns
    -------
    pptx.shapes.autoshape.Shape
    """
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches

    aligns = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
    anchors = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchors.get(anchor, MSO_ANCHOR.TOP)
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    for index, line in enumerate(str(text).split("\n")):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = aligns.get(align, PP_ALIGN.LEFT)
        run = paragraph.add_run()
        run.text = line
        _set_run_font(run, size, bold, color)
    return box


def _set_run_font(run, size: int, bold: bool, color: str) -> None:
    """Set size / bold / color / East-Asian typeface on one run.

    Parameters
    ----------
    run : pptx.text.text._Run
    size : int
    bold : bool
    color : str

    Returns
    -------
    None
    """
    from pptx.dml.color import RGBColor
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls, qn
    from pptx.util import Pt

    font = run.font
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = RGBColor.from_string(color)
    font.name = FONT
    rPr = run._r.get_or_add_rPr()
    east_asian = rPr.find(qn("a:ea"))
    if east_asian is None:
        east_asian = parse_xml(f'<a:ea {nsdecls("a")}/>')
        rPr.append(east_asian)
    east_asian.set("typeface", FONT)
