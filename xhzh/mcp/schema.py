"""九个 MCP 工具的 JSON Schema 与轻量校验器（draft-07 子集）。

Notes
-----
只实现本服务实际用到的关键字：``type / enum / pattern / minLength /
maxLength / minimum / maximum / required / additionalProperties / items``。
不引入 ``jsonschema`` 依赖（零新依赖是硬约束），校验失败统一产出
``(pointer, keyword, message)`` 三元组，供协议层组装 ``-32602`` 的
``data.violations``。
"""

from __future__ import annotations

import re
from typing import Any

from .. import config

_DRAFT7 = "http://json-schema.org/draft-07/schema#"


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build a closed object schema.

    Parameters
    ----------
    properties : dict
        字段名 → 字段 schema。
    required : list of str

    Returns
    -------
    dict
    """
    return {
        "$schema": _DRAFT7,
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


_GRADE = {
    "type": "string",
    "enum": list(config.GRADES),
    "description": "年级，枚举取自 xhzh.config.GRADES。",
}
_SUBJECT = {
    "type": "string",
    "enum": list(config.SUBJECTS),
    "description": "学科，枚举取自 xhzh.config.SUBJECTS；范围外学科知识库无卡片。",
}
_TOPIC = {
    "type": "string",
    "minLength": 1,
    "maxLength": 60,
    "pattern": "^[^\\u0000-\\u001F<>:\"/\\\\|?*]+$",
    "description": "课题，1~60 字。禁止控制字符与路径分隔符（课题会进入产物文件名）。",
}
_GENERATION_ID = {
    "type": "integer",
    "minimum": 1,
    "description": "生成任务 ID（generations.id）。",
}
_KIND = {
    "type": "string",
    "enum": ["docx", "pptx", "zip"],
    "description": "产物类型：docx 教案 / pptx 课件 / zip 教学包。",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "generate_teaching_package",
        "title": "生成教学包（教案 Word + 课件 PPT + 打包 zip）",
        "description": "按年级/学科/教材/课题发起一次教学包生成。立即返回 generation_id，用 get_generation_status 轮询进度与结果。单次生成通常 20~300 秒。",
        "inputSchema": _object(
            {
                "grade": _GRADE,
                "subject": _SUBJECT,
                "textbook": {
                    "type": "string",
                    "enum": [config.TEXTBOOK],
                    "default": config.TEXTBOOK,
                    "description": "教材版本，当前仅支持 config.TEXTBOOK。",
                },
                "topic": _TOPIC,
            },
            ["grade", "subject", "topic"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "get_generation_status",
        "title": "查询生成任务状态",
        "description": "轮询一次生成任务的四节点进度、状态与错误。include_plan=true 且任务已完成时，额外返回教案预览 Markdown。",
        "inputSchema": _object(
            {
                "generation_id": _GENERATION_ID,
                "include_plan": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否附带教案预览（仅 done 状态有效）。",
                },
            },
            ["generation_id"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "cancel_generation",
        "title": "取消生成任务",
        "description": "请求取消一个运行中的任务。取消为协作式：在节点边界生效，最坏需等待当前节点结束。",
        "inputSchema": _object({"generation_id": _GENERATION_ID}, ["generation_id"]),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_generations",
        "title": "列出最近生成记录",
        "description": "按时间倒序返回最近的教学包生成记录，用于历史面板或断线重连后恢复任务。",
        "inputSchema": _object(
            {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": "返回条数上限（1~50）。",
                }
            },
            [],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "get_package",
        "title": "获取教学包产物元信息",
        "description": "返回 docx/pptx/zip 的相对路径、字节数、sha256 与下载标识（不泄露绝对路径）。",
        "inputSchema": _object({"generation_id": _GENERATION_ID}, ["generation_id"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "read_package_file",
        "title": "读取教学包产物内容",
        "description": "以 base64 返回指定产物内容；受单文件 8MB 上限与路径白名单保护。大文件请改用 get_package 取元信息。",
        "inputSchema": _object(
            {"generation_id": _GENERATION_ID, "kind": _KIND}, ["generation_id", "kind"]
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "search_knowledge_cards",
        "title": "检索知识点卡",
        "description": "按学科/年级/课题检索知识库卡片（含课标依据）。无匹配时 standard_ref_missing=true，调用方应据此提示“暂无课标依据”。",
        "inputSchema": _object(
            {"subject": _SUBJECT, "grade": _GRADE, "topic": _TOPIC},
            ["subject", "grade", "topic"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_catalog",
        "title": "列出可选项（年级/学科/教材/节点）",
        "description": "返回客户端渲染表单所需的枚举：年级、学科、教材、四节点名、课件页型、可用模式。",
        "inputSchema": _object({}, []),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "check_system_status",
        "title": "服务健康诊断",
        "description": "返回运行模式、LLM 配置、知识库与素材库就绪情况、任务超时与并发上限，用于排障（不含密钥与绝对路径）。",
        "inputSchema": _object({}, []),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]

# 工具名 → 所需 scope（默认最小权限：读类只需 read）
TOOL_SCOPES: dict[str, str] = {
    "generate_teaching_package": "generate",
    "cancel_generation": "generate",
    "get_generation_status": "read",
    "list_generations": "read",
    "get_package": "read",
    "read_package_file": "read",
    "search_knowledge_cards": "read",
    "list_catalog": "read",
    "check_system_status": "read",
}

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def list_tools(include_output_schema: bool = True) -> list[dict[str, Any]]:
    """Return the tool list for ``tools/list``.

    Parameters
    ----------
    include_output_schema : bool
        ``outputSchema`` 仅 2025-06-18 起被规范支持；老客户端协商时置 False。

    Returns
    -------
    list of dict
        Tool descriptors (``annotations`` 始终返回，老客户端会忽略未知字段).
    """
    result: list[dict[str, Any]] = []
    for tool in TOOLS:
        item = dict(tool)
        if not include_output_schema:
            item.pop("outputSchema", None)
        result.append(item)
    return result


def validate(tool_name: str, arguments: dict[str, Any]) -> list[dict[str, str]]:
    """Validate arguments against a tool's ``inputSchema``.

    Parameters
    ----------
    tool_name : str
    arguments : dict

    Returns
    -------
    list of dict
        Violations as ``{"pointer": "/grade", "keyword": "enum", "message": ...}``;
        空列表表示通过。
    """
    schema = TOOLS_BY_NAME[tool_name]["inputSchema"]
    violations: list[dict[str, str]] = []

    def _fail(pointer: str, keyword: str, message: str) -> None:
        violations.append({"pointer": pointer, "keyword": keyword, "message": message})

    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in arguments or arguments[name] in (None, ""):
            _fail(f"/{name}", "required", f"缺少必填字段 {name}")
    if schema.get("additionalProperties") is False:
        for name in arguments:
            if name not in properties:
                _fail(f"/{name}", "additionalProperties", f"未声明的字段 {name}")
    for name, value in arguments.items():
        if name not in properties or value is None:
            continue
        spec = properties[name]
        pointer = f"/{name}"
        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            _fail(pointer, "type", f"{name} 必须是字符串")
            continue
        if expected == "integer" and not isinstance(value, int) or (
            expected == "integer" and isinstance(value, bool)
        ):
            _fail(pointer, "type", f"{name} 必须是整数")
            continue
        if expected == "boolean" and not isinstance(value, bool):
            _fail(pointer, "type", f"{name} 必须是布尔值")
            continue
        if "enum" in spec and value not in spec["enum"]:
            _fail(pointer, "enum", f"{name} 取值必须是 {'/'.join(map(str, spec['enum']))} 之一")
        if isinstance(value, str):
            if "minLength" in spec and len(value) < spec["minLength"]:
                _fail(pointer, "minLength", f"{name} 至少 {spec['minLength']} 个字符")
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                _fail(pointer, "maxLength", f"{name} 最多 {spec['maxLength']} 个字符")
            if "pattern" in spec and value and not re.search(spec["pattern"], value):
                _fail(pointer, "pattern", f"{name} 含有不允许的字符（控制字符或路径分隔符）")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                _fail(pointer, "minimum", f"{name} 不得小于 {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                _fail(pointer, "maximum", f"{name} 不得大于 {spec['maximum']}")
    return violations
