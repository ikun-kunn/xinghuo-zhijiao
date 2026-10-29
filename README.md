# 星火智教 · AI 教学包生成助手（大赛版）

> 县乡教师 AI 教学包生成助手：选年级 + 学科 + 教材版本 + 课题，一键生成「教案 Word + 课件 PPT」教学包。
> 设计蓝图见项目目录下五份方案文档；技术栈见《技术栈选型方案-赛事版-v0.2.md》。

## 快速开始

```bash
pip install -r requirements.txt
python preflight.py            # 演示前自检：依赖/知识库/测试套件/端口，全绿再启动
python app.py                  # 浏览器打开 http://127.0.0.1:7860（.env 已配 llm 真实生成）
XHZH_MODE=mock python app.py   # 演示兜底模式（不调用外部服务，无 .env 时的默认）
XHZH_SHARE=1 python app.py     # 生成公开链接（评委远程访问用，控制台会打印 xxxx.gradio.live）
XHZH_PORT=7861 python app.py   # 换端口（默认 7860 被占用时）
```

凭据配置：复制 `.env.example` 为 `.env` 填入 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（`python app.py` 自动加载；进程环境变量优先）。`.env` 已在 `.gitignore` 中，**禁止提交或外传**。

## 技术栈边界（赛事版原则）

> 复杂度压在 WorkBuddy 平台侧，本地只写平台替代不了的那部分。

| 层 | 原则 | 本地现状 |
|---|---|---|
| AI 编排 | 平台侧 Workflow / RAG / Skill 为主 | `adp` 模式预留 SDK 接入点；`llm` 模式为**演示兜底**（自建检索 + 提示词），不作为平台 RAG 的替代方案 |
| 前端 | Gradio 二选一，不做第三套 | `app.py`（Gradio 6.x，支持 `XHZH_SHARE=1` 一键公开链接） |
| 数据 | SQLite 够用就不上 PG | `data/xinghuo.db` 两表；ChromaDB 仅当需要本地向量检索时再引入，当前未启用 |
| 代码规范 | numpydoc 写 docstring，不搭 Sphinx | 29/29 公开函数合规（可用 AST 脚本复核） |

## 三种运行模式

| 模式 | 开关 | 说明 |
|---|---|---|
| `mock` | 无 `.env` 时默认（或 `XHZH_MODE=mock`） | 本地模拟数据，全链路可跑，演示兜底 |
| `llm` | `XHZH_MODE=llm` + `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 知识点卡检索（`xhzh/kb.py`）+ OpenAI 兼容接口生成，节点输出为结构化 JSON |
| `adp` | `XHZH_MODE=adp` + `ADP_SECRET_ID` / `ADP_SECRET_KEY` | WorkBuddy（腾讯云 ADP）真实接入，待按官方文档实现 `adp_client.call_node` 真实分支 |

llm 模式要点：
- 知识库：解析工作区《知识库-人教版初中知识点-v0.1.md》（159 张卡），可用 `XHZH_KB_PATH` 指定其他路径；
- 检索无匹配时，节点①输出会如实声明"知识库暂无课标依据"（PRD EARS-3.2），不虚构；
- 练习经 `rural.py` 乡土词典替换 + 金额合理性校验后输出。

## MCP 服务（stdio）

把同一套工作流暴露给任意 MCP 客户端（Claude Desktop / IDE / 自研 Agent），**零新增依赖**（标准库手写 JSON-RPC 2.0）：

```bash
python mcp_server.py                 # stdio 传输，本地单用户模式
python mcp_server.py --scope read    # 只读演示（生成类工具返回 FORBIDDEN）
```

客户端配置：

```json
{
  "mcpServers": {
    "xinghuo-zhijiao": {
      "command": "python",
      "args": ["<项目绝对路径>/mcp_server.py"],
      "env": { "XHZH_MODE": "mock" }
    }
  }
}
```

**9 个工具**：`generate_teaching_package`（异步起任务，立即返回 `generation_id`）、`get_generation_status`（轮询四节点进度，`include_plan=true` 附带教案预览）、`cancel_generation`、`list_generations`、`get_package`、`read_package_file`、`search_knowledge_cards`、`list_catalog`、`check_system_status`。

**7 类资源**：`kb://catalog`、`kb://cards/{card_id}`、`xinghuo://generations[/{id}]`、`package://{id}/{kind}`、`xinghuo://audit/recent`（需 admin）。

| 约定 | 说明 |
|---|---|
| 协议版本 | 首选 `2025-06-18`；可接受 `2025-03-26` / `2024-11-05`；未知版本回本端首选，不报错 |
| 能力声明 | 仅 `tools` / `resources` / `logging`（未实现的 `prompts`/`sampling` 一律不声明） |
| 错误分流 | 协议错误走 JSON-RPC `error`（-32700/-32600/-32601/-32602/-32002/-32003）；业务错误走 `isError:true` + `structuredContent.error.code`（`PARAM_INVALID/NOT_FOUND/FORBIDDEN/RATE_LIMITED/BUSY/TIMEOUT/MODE_UNAVAILABLE/INTERNAL`） |
| 权限 | scope 三档 `read` / `generate` / `admin`；默认无令牌授予 `read+generate`；路径白名单 + 单文件 8MB 读取上限 |
| 超时 | 请求级 1~5s；任务级 300s（`XHZH_MCP_TASK_TIMEOUT`），超时置 `failed` 并回 `TIMEOUT` |
| 并发 | 工作线程 2 + 队列 4（可配），超限回 `BUSY` + `retry_after_ms` |
| 审计 | `data/audit/mcp-YYYYMMDD.jsonl`（只记录 owner 的 sha256 前 8 位，不落明文令牌） |

接口契约与报文示例见《MCP服务设计-星火智教-v0.1.md》；协议级回归见 `tests/test_mcp_suite.py`。

## 课件视觉元素（插画素材库 + 可选生图）

课件（学生向 PPT）会按页面主题自动配场景插图，图片来源有明确优先级链：

| 顺序 | 来源 | 说明 |
|---|---|---|
| ① | `assets/visuals/{theme}.jpg` → `{theme}.png` | **AIGC 生成并经人工过目的插画素材库**（默认随包分发，运行时优先使用；生图 API 默认关闭）。6 个主题：乡村集市 / 田间作物 / 农家养殖 / 乡村校园 / 农具 / 赶集路上，jpg 约 90~130KB，县乡低带宽友好；`assets/visuals/src/` 的 PNG 源图仅存档，运行时不读取 |
| ② | 运行时生图（`xhzh/imagegen.py`） | **默认关闭**。`XHZH_IMAGE_BASE_URL` / `XHZH_IMAGE_API_KEY` / `XHZH_IMAGE_MODEL` 三件套齐备 **且** `XHZH_IMAGE_ALLOW=1` 时才调用 OpenAI 兼容 `POST {base}/images/generations`，结果缓存在 `data/images/` |
| ③ | 内置矢量场景（`xhzh/visuals.py`） | 纯 python-pptx 原生形状绘制，离线可用；素材缺失、生图失败或离线时自动兜底，版面永不留白 |

实现说明：

- **无变形裁切填充**：`_add_picture_fill()` 先按目标框宽高比挑出原图中比例完全一致的最大可见窗口，再交给 python-pptx 的 `crop_*`——图片严格占满目标框，不拉伸、不越界；
- **水印裁切守卫**：素材源图右下角带平台水印「AI生成 WORKBUDDY」（实测占 x≥0.923W、y≥0.941H），因此右侧与底部**各强制裁掉 ≥9%**（`visuals.WATERMARK_GUARD`），多出的裁切量从对侧补回以维持比例——水印在任何目标框比例下都不会被投影出来。测试不写死该数值：断言从常量派生（`crop_right/crop_bottom ≥ WATERMARK_GUARD`），并另设实测下限护栏（`WATERMARK_MIN_GUARD = 1 - 0.923 = 7.7%`），因此把守卫调小（例如回退到 6%）会直接测试失败；

- **体积守卫**：命中素材 >400KB 时用 Pillow（python-pptx 既有依赖）等比降采样到宽 1280 / JPEG q88 并缓存到 `data/images/`，防止单张源图击穿 3MB 教学包预算。

合规约定：

- AIGC 配图须人工过目后才用于课堂，故生图**默认关闭**（PRD 合规要求），任何生图失败只记 warning 并静默降级，不影响教学包生成；
- 凡使用生成式配图的页面，PPT 右下角自动标注 `AI 生成配图` 水印。

## 真实模式（接 WorkBuddy / 腾讯云 ADP）

1. `requirements.txt` 中取消 `tencentcloud-sdk-python-adp` 注释并安装；
2. 设置环境变量 `ADP_SECRET_ID` / `ADP_SECRET_KEY`，`XHZH_MODE=adp`；
3. 在 `xhzh/adp_client.py` 的 `call_node` 真实分支按 **ADP 官方文档** 实现调用（接口签名以官方文档为准，勿臆造）。

## 测试

```bash
python tests/test_suite.py          # 全量（117 项：config/kb/llm/rural/workflow/assembly/visuals/storage/app）
python tests/test_suite.py kb llm   # 只跑指定分组
python tests/test_mcp_suite.py      # MCP 协议级回归（74 项：握手/发现/调用/资源/权限/并发/进度，独立临时库）
```

零依赖（不用 pytest），失败非零退出，可直接用于演示前自检。

## 目录结构

```
xinghuo-zhijiao/
├── app.py               # Gradio 前端（星火暖橙 + 乡土绿；手机优先；左栏表单、右栏步骤/教案/下载，明细与历史默认收起）
├── mcp_server.py        # MCP 服务启动器（stdio；--token/--scope/--log-level）
├── preflight.py         # 演示前自检（依赖/知识库/MCP 协议层/测试套件/端口，exit 0 全绿）
├── .env.example         # 环境变量模板（复制为 .env 填真实值，勿提交）
├── tests/
│   ├── test_suite.py    # 零依赖测试套件（117 项，见上）
│   └── test_mcp_suite.py# MCP 协议级回归（74 项，subprocess 拉起真实 stdio 服务）
├── assets/
│   └── visuals/         # AIGC 生成并经人工过目的场景插画素材库（6 主题 jpg 运行时用；src/ 为 PNG 源图存档）
├── xhzh/
│   ├── config.py        # 配置（模式/密钥/重试/端口/MCP 上限均走环境变量，mock 默认）
│   ├── service.py       # 服务层：任务生命周期 + 线程池 + 异常归类 + 限流/审计（Gradio 与 MCP 共用）
│   ├── mcp/             # 协议层：jsonrpc（错误码）/ schema（9 工具契约）/ tools / resources / server
│   ├── kb.py            # 知识点卡解析与 bigram 检索（知识库 md 为数据源）
│   ├── prompts.py       # 三节点提示词模板（编码 PRD EARS 规则）
│   ├── llm.py           # OpenAI 兼容客户端（标准库实现，零额外依赖）
│   ├── adp_client.py    # 工作流客户端：mock / llm / adp 三模式
│   ├── workflow.py      # 四节点工作流（生成器逐步 yield 进度）
│   ├── courseware.py    # 教案 → 学生向课件脚本（9~12 页，含 kind/visual 字段）
│   ├── visuals.py       # 课件视觉元素（审核插画 > 可选生图 > 矢量兜底；图标徽章/数量关系图）
│   ├── imagegen.py      # 可选运行时生图适配器（默认关闭，OpenAI 兼容 images/generations）
│   ├── assembly.py      # python-docx / python-pptx 教学包组装
│   ├── rural.py         # 乡土案例转换（规则版占位 + 数值合理性校验）
│   └── storage.py       # SQLite（generations / packages）
└── data/                # 运行时生成：SQLite + 教学包输出（+ images/ 生图缓存、audit/ 审计 JSONL）
```

## 约定

- 所有对外函数按 **numpydoc** 格式写 docstring（不搭 Sphinx 站点，只保证规范）；
- 冒烟测试：`python -c "from xhzh.workflow import run_generation; ..."`（见 README 下方命令）；
- 乡土转换当前为词典规则版，接 WorkBuddy Skill 后退化为结果校验器。

## 冒烟测试

```bash
python - <<'EOF'
from xhzh.workflow import run_generation
events = list(run_generation("七年级", "数学", "人教版", "一元一次方程的实际应用"))
done = events[-1]
assert done["type"] == "done"
print(done["result"]["package"])
EOF
```
