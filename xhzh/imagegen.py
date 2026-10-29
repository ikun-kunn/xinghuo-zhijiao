"""可选"运行时生图"适配器：素材库缺失时按需调用 OpenAI 兼容文生图接口。

Notes
-----
PRD 合规要求：AIGC 配图必须经人工审核，未经审核的图片不得进入课堂。
因此本模块**默认关闭**，只有同时满足下列两个条件才会真正发起请求：

1. 三件套齐备（:func:`is_configured`）：
   ``XHZH_IMAGE_BASE_URL`` / ``XHZH_IMAGE_API_KEY`` / ``XHZH_IMAGE_MODEL``；
2. 显式开关（:func:`is_enabled`）：``XHZH_IMAGE_ALLOW=1``。

日常路径是 :func:`xhzh.visuals.scene_image` 直接命中 ``assets/visuals/``
下**经审核**的素材库；本模块只作为素材缺失时的补充手段，且生成结果同样
需要人工过目后才建议入课堂。

设计约束：

- 只用标准库（``urllib`` / ``json`` / ``hashlib`` / ``base64`` / ``logging``），
  不新增任何第三方依赖——县乡低带宽、离线环境要能 import；
- 任何失败（未配置、网络不可达、返回体缺字段）一律 ``logging.warning``
  后返回 ``None``，**禁止向上抛异常**，避免拖垮整条课件生成链路；
- 同一提示词命中文生图缓存（``data/images/scene-{theme}-{sha1[:12]}.jpg``）
  时直接复用本地文件，不重复计费。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path

from . import config

LOGGER = logging.getLogger(__name__)

#: 首发 + 重试次数（合计请求次数，与工作流 D2 口径一致：重试 ≤1）
_MAX_ATTEMPTS = 2

#: 生成图片的目标边长（正方形，与素材库 1280×854 的裁切填充兼容）
_IMAGE_SIZE = "1024x1024"

#: 统一画风后缀：与 ``assets/visuals/`` 已有素材保持一致
_STYLE_SUFFIX = (
    "扁平矢量插画风格，圆角几何色块，线条简洁；"
    "暖米白背景 #FDF7EF，主色为乡土绿 #2E7D5B、暖橙 #E4862B、湖蓝 #2F6FB5；"
    "构图居中、留白充足，无任何文字、数字、字母与标识，不出现真实人脸与真实品牌。"
)

#: 主题 → 画面描述（与 :data:`xhzh.visuals.SCENE_THEMES` 一一对应）
SCENE_PROMPTS: dict[str, str] = {
    "market": (
        "中国县域乡村的露天集市：木质摊位、遮阳棚、摆放整齐的蔬菜瓜果与"
        "竹编小筐，一位不露脸的中年摊主剪影在整理货物，远处是低矮的乡镇楼房与远山。"
    ),
    "farm": (
        "中国南方丘陵地带的田间作物景观：成排的玉米与稻秧、田埂与灌溉小水渠，"
        "远处有一顶草帽与锄头立在田边，天上有柔和的太阳。"
    ),
    "livestock": (
        "中国农家院坝养殖场景：竹篱笆围成的鸡舍、几只母鸡与竹编蛋筐，"
        "旁边是砖墙与瓦片屋檐，地上散落谷粒。"
    ),
    "school": (
        "中国乡村小学校园：白墙灰瓦的教学楼、升起的国旗、操场与乒乓球台，"
        "几棵樟树，天空清朗，画面中不出现人物。"
    ),
    "tool": (
        "中国农具静物组合：锄头、镰刀、铁锹与竹编簸箕靠放在土墙边，"
        "一旁有木桶与麻绳，地面有少量泥土与麦秸。"
    ),
    "travel": (
        "中国县乡公路与赶集路上：一辆乡土风格的橙色班车行驶在乡间公路上，"
        "路边是稻田、电线杆与路牌（牌面留白无文字），远处青山。"
    ),
}

#: 未知主题兜底描述（保证任何 theme 都有可用提示词）
_FALLBACK_SCENE = "中国县域乡村教学情境插画：简化的乡村生活与学习场景组合。"


def _env(name: str, default: str = "") -> str:
    """Read an environment variable and strip surrounding whitespace.

    Parameters
    ----------
    name : str
        Environment variable name.
    default : str, optional
        Value returned when the variable is unset or blank.

    Returns
    -------
    str
        Stripped value, or ``default``.
    """
    return os.getenv(name, "").strip() or default


def _timeout() -> float:
    """Resolve the per-request timeout in seconds.

    Returns
    -------
    float
        ``XHZH_IMAGE_TIMEOUT`` when it parses to a positive number,
        otherwise ``60.0``.

    Notes
    -----
    非法值（非数字 / ≤0）静默回退默认值，与 :mod:`xhzh.config` 中
    ``PORT`` / ``LLM_TIMEOUT`` 的防护口径一致。
    """
    try:
        value = float(_env("XHZH_IMAGE_TIMEOUT", "60"))
    except ValueError:
        return 60.0
    return value if value > 0 else 60.0


def is_configured() -> bool:
    """Check whether the text-to-image endpoint credentials are complete.

    Returns
    -------
    bool
        ``True`` only when ``XHZH_IMAGE_BASE_URL`` / ``XHZH_IMAGE_API_KEY`` /
        ``XHZH_IMAGE_MODEL`` are all non-empty.

    Examples
    --------
    >>> is_configured()  # doctest: +SKIP
    False
    """
    return all(_env(key) for key in ("XHZH_IMAGE_BASE_URL", "XHZH_IMAGE_API_KEY", "XHZH_IMAGE_MODEL"))


def is_enabled() -> bool:
    """Check whether runtime image generation is explicitly switched on.

    Returns
    -------
    bool
        ``True`` only when ``XHZH_IMAGE_ALLOW`` equals ``"1"``.

    Notes
    -----
    默认关闭是合规要求而非保守：AIGC 配图必须人工审核后才能进课堂，
    因此"有凭据"不等于"允许自动生图"。
    """
    return _env("XHZH_IMAGE_ALLOW", "0") == "1"


def build_prompt(theme: str, hint: str = "") -> str:
    """Compose the generation prompt for one scene theme.

    Parameters
    ----------
    theme : str
        Scene theme key, ideally one of :data:`SCENE_PROMPTS`.
    hint : str, optional
        Extra page-specific wording (e.g. 课题/情境描述) appended as a
        supplementary requirement.

    Returns
    -------
    str
        Prompt text ending with the shared style suffix.

    Notes
    -----
    未知主题用 :data:`_FALLBACK_SCENE` 兜底，保证提示词永不为空。
    """
    key = str(theme or "").strip().lower()
    description = SCENE_PROMPTS.get(key, _FALLBACK_SCENE)
    prompt = f"{description}{_STYLE_SUFFIX}"
    extra = str(hint or "").strip()
    if extra:
        prompt = f"{prompt}补充要求：{extra[:120]}。"
    return prompt


def cache_path(theme: str, prompt: str) -> Path:
    """Build the deterministic cache path for one generated scene.

    Parameters
    ----------
    theme : str
        Scene theme key.
    prompt : str
        Full prompt text (hashed into the file name).

    Returns
    -------
    Path
        ``<DATA_DIR>/images/scene-{theme}-{sha1(prompt)[:12]}.jpg``.

    Notes
    -----
    目录不在此处创建，由 :func:`generate_scene` 落盘时按需 ``mkdir``。
    """
    digest = hashlib.sha1(str(prompt).encode("utf-8")).hexdigest()[:12]
    key = str(theme or "scene").strip().lower() or "scene"
    return config.DATA_DIR / "images" / f"scene-{key}-{digest}.jpg"


def generate_scene(theme: str, hint: str = "") -> Path | None:
    """Generate (or reuse) one scene illustration via the image API.

    Parameters
    ----------
    theme : str
        Scene theme key (``market`` / ``farm`` / ``livestock`` / ``school``
        / ``tool`` / ``travel``; unknown keys use a generic prompt).
    hint : str, optional
        Extra wording injected into the prompt (课题关键词等).

    Returns
    -------
    Path or None
        Local JPEG path on success (including cache hit), ``None`` when the
        feature is disabled / unconfigured / any step failed.

    Raises
    ------
    None
        No exception is ever raised: failures are logged and swallowed so a
        broken image endpoint can never break the courseware pipeline.

    Notes
    -----
    返回 ``None`` 时调用方（:func:`xhzh.visuals.scene_image`）会自动降级到
    内置矢量场景，课件版面不会因此留空。
    """
    if not is_enabled() or not is_configured():
        return None
    theme_key = str(theme or "").strip().lower()
    if not theme_key:
        return None
    prompt = build_prompt(theme_key, hint)
    target = cache_path(theme_key, prompt)
    try:
        if target.is_file() and target.stat().st_size > 0:
            LOGGER.info("生图缓存命中：%s", target.name)
            return target
    except OSError as error:  # 缓存探测失败也只是一次普通失败
        LOGGER.warning("生图缓存探测失败：%s", error)
        return None
    try:
        payload = _request_images(
            _env("XHZH_IMAGE_BASE_URL"),
            _env("XHZH_IMAGE_API_KEY"),
            _env("XHZH_IMAGE_MODEL"),
            prompt,
            _timeout(),
        )
        if payload is None:
            return None
        if _store_payload(payload, target, _timeout()):
            return target
    except Exception as error:  # noqa: BLE001 - 生图失败绝不能影响主链路
        LOGGER.warning("生图失败（theme=%s）：%s", theme_key, error)
    return None


def _request_images(base_url: str, api_key: str, model: str, prompt: str, timeout: float) -> dict | None:
    """POST one OpenAI-compatible ``/images/generations`` request (with retry).

    Parameters
    ----------
    base_url : str
        API root, e.g. ``https://example.com/v1``.
    api_key : str
        Bearer token.
    model : str
        Image model name.
    prompt : str
        Full prompt text.
    timeout : float
        Per-attempt timeout in seconds.

    Returns
    -------
    dict or None
        Parsed JSON response object, or ``None`` after all attempts failed.

    Notes
    -----
    请求体固定 ``size=1024x1024`` / ``n=1``：课件单页只插图一张，多图返回
    既浪费额度也无版面可用。重试次数由 :data:`_MAX_ATTEMPTS` 控制。
    """
    url = f"{base_url.rstrip('/')}/images/generations"
    body = json.dumps(
        {"model": model, "prompt": prompt, "size": _IMAGE_SIZE, "n": 1},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
        },
    )
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
            LOGGER.warning("生图返回非 JSON 对象（第 %s/%s 次）", attempt, _MAX_ATTEMPTS)
        except Exception as error:  # noqa: BLE001 - 网络/解析错误统一降级
            LOGGER.warning("生图请求失败（第 %s/%s 次）：%s", attempt, _MAX_ATTEMPTS, error)
    return None


def _store_payload(payload: dict, target: Path, timeout: float) -> bool:
    """Persist the first image of a generation response to ``target``.

    Parameters
    ----------
    payload : dict
        Parsed API response; ``data[0]`` may carry ``b64_json`` or ``url``.
    target : Path
        Destination JPEG path.
    timeout : float
        Timeout used when downloading a picture URL.

    Returns
    -------
    bool
        ``True`` when the file was written; ``False`` when the payload
        carried no usable image.

    Raises
    ------
    OSError
        Propagated from the filesystem write; :func:`generate_scene` catches
        it and degrades to ``None``.
    """
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        LOGGER.warning("生图返回体缺少 data 数组")
        return False
    first = items[0] if isinstance(items[0], dict) else {}
    blob: bytes | None = None
    encoded = first.get("b64_json")
    if isinstance(encoded, str) and encoded.strip():
        try:
            blob = base64.b64decode(encoded)
        except Exception as error:  # noqa: BLE001 - Base64 脏数据按缺图处理
            LOGGER.warning("生图 b64_json 解码失败：%s", error)
            blob = None
    if not blob:
        link = first.get("url")
        if isinstance(link, str) and link.startswith(("http://", "https://")):
            try:
                with urllib.request.urlopen(link, timeout=timeout) as response:
                    blob = response.read()
            except Exception as error:  # noqa: BLE001 - 下载失败按缺图处理
                LOGGER.warning("生图图片下载失败：%s", error)
                blob = None
    if not blob:
        LOGGER.warning("生图返回体不含可用图片（b64_json / url 均缺失）")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.part")
    partial.write_bytes(blob)
    partial.replace(target)  # 原子替换：避免半截文件被当成缓存命中
    return True
