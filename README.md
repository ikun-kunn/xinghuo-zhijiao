# 星火智教 · AI 教学包生成助手（大赛版）

> 面向初中七至九年级、语文/数学/英语的县乡教师 AI 教学包生成助手：选择年级 + 学科 + 教材版本 + 课题，一键生成「教案 Word + 课件 PPT + 配图」教学包。
> 当前需求基线：[PRD v0.2](../PRD-星火智教-AI教学包生成助手-v0.2.md)；测试与发布边界：[测试报告 v1.2](../测试与缺陷报告-v1.2.md)。技术栈目标见《技术栈选型方案-赛事版-v0.2.md》，实现差异见[技术栈合规核对表 v1.1](../技术栈合规核对表-v1.1.md)。

## 快速开始

Windows（使用项目独立虚拟环境；需先安装 [uv](https://docs.astral.sh/uv/)）：

```powershell
uv venv --python 3.13 .venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
$env:PYTHONUTF8 = "1"
python preflight.py
```

```bash
pip install -r requirements.txt
python preflight.py            # 演示前自检：依赖/知识库/测试套件/端口，全绿再启动
python web_app.py              # 最终前端：打开 http://127.0.0.1:7860，服务根目录的 index.html
XHZH_MODE=mock python web_app.py # 演示兜底模式（不调用外部服务，无 .env 时的默认）
XHZH_WEB_PORT=7861 python web_app.py # 换端口
python app.py                  # 旧版 Gradio 界面，仅作为备用入口
```

凭据配置：复制 `.env.example` 为 `.env` 填入 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（两个入口都会自动加载；进程环境变量优先）。`.env` 已在 `.gitignore` 中，**禁止提交或外传**。

最终前端由 `web_app.py` 在同一地址提供页面和 `/api/catalog`、`/api/generations`、`/api/history`、文件下载接口。课程选项来自知识库，仅显示后端支持的七至九年级语文、数学、英语及人教版课题；生成进度、教案预览和下载均来自服务层。下载有效期为 24 小时。服务仅监听本机 `127.0.0.1`。

## 技术栈边界（赛事版原则）

> 复杂度压在 WorkBuddy 平台侧，本地只写平台替代不了的那部分。

| 层 | 原则 | 本地现状 |
|---|---|---|
| AI 编排 | 平台侧 Workflow / RAG / Skill 为主 | `adp` 模式预留 SDK 接入点；`llm` 模式为**演示兜底**（自建检索 + 提示词），不作为平台 RAG 的替代方案 |
| 前端 | Gradio 二选一，不做第三套 | `app.py`（Gradio 6.x，支持 `XHZH_SHARE=1` 一键公开链接） |
| 数据 | SQLite 够用就不上 PG | `data/xinghuo.db` 两表；ChromaDB 仅当需要本地向量检索时再引入，当前未启用 |
| 代码规范 | numpydoc 写 docstring，不搭 Sphinx | 按现有 docstring 约定维护；未作为自动化发布门禁 |

## 三种运行模式

| 模式 | 开关 | 说明 |
|---|---|---|
| `mock` | 无 `.env` 时默认（或 `XHZH_MODE=mock`） | 本地模拟数据，全链路可跑；题目答案为参考要点，不是经验证的标准答案 |
| `llm` | `XHZH_MODE=llm` + `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 知识点卡检索（`xhzh/kb.py`）+ OpenAI 兼容接口生成，节点输出为结构化 JSON |
| `adp` | `XHZH_MODE=adp` | 尚未接入；明确返回 `MODE_UNAVAILABLE`，配置密钥不会启用未实现的接口 |

llm 模式要点：
- 知识库：解析工作区《知识库-人教版初中知识点-v0.1.md》（159 张卡），可用 `XHZH_KB_PATH` 指定其他路径；
- 检索无匹配时，节点①输出会如实声明"知识库暂无课标依据"（PRD EARS-3.2），不虚构；
- 节点③要求基础题、拓展题各不少于 4 道，并为每题生成答案与解析；未达标会按配置重试，仍不足则明确失败，不交付不达标教学包；
- 课件为 9~12 页，至少包含 3 张主题插图；ZIP 同时包含 `配图/` 文件夹；
- 练习经 `rural.py` 乡土词典替换 + 金额合理性校验后输出。
- mock 答案是课题相关参考要点/教学提示；任何模式的教学内容均应由教师审核后使用。

## 自动内容预检

生成前会先检查教师输入的课题：识别到带明确联系方式的电话号码、邮箱、有效格式证件号或高风险伤害指导时，直接拦截；输入不会保存到历史记录，也不会发送给模型。生成后、组装教学包前再检查全部内容：

- **高风险阻断**：疑似个人联系方式/证件号码、明确可执行的自伤/伤人/剥削指导、明确群体贬损。阻断时不生成可下载包；报告只标出内容所在字段，不回显敏感文本。
- **标记教师复核**：知识库依据缺失或范围不匹配、练习与答案数量不一致、目标/课时结构缺失、敏感主题、已有乡土金额告警或未替换的常见城市情境。
- 报告显示在「审核与生成明细」，同时写入 Word 教案、ZIP 内 `自动预检报告.md`；MCP 任务状态也会返回预检结果。

这是无额外模型调用的规则式初筛，可能漏检或误报；不能判断学科事实与答案是否正确、课标是否权威、所有内容是否适龄，也不能可靠识别姓名/住址。教师仍需重点审核标出的项目；“未发现明显问题”不等于内容已获审核批准。

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
| 错误分流 | 协议错误走 JSON-RPC `error`（-32700/-32600/-32601/-32602/-32002/-32003）；业务错误走 `isError:true` + `structuredContent.error.code`（含不可重试的 `CONTENT_BLOCKED`） |
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

## 24 小时历史恢复下载

在 Gradio 页面展开「历史记录」，刷新后可选择最近 24 小时内成功生成且产物仍存在的教学包，并重新下载 zip / Word / PPT。超过期限、失败或文件缺失的记录不会出现在可恢复列表中；服务层也会拒绝过期恢复请求。此功能是**当前 Gradio 实例内的全局历史恢复**，不是可转发的签名链接；文件不会因超过 24 小时而自动从磁盘删除。当前没有登录或用户隔离，**不要将其作为公网多租户服务部署**。

## 真实模式（接 WorkBuddy / 腾讯云 ADP）

`adp` 真实接入尚未实现。当前设置 `XHZH_MODE=adp` 会明确返回 `MODE_UNAVAILABLE`；仅配置密钥或安装 SDK 不会启用 WorkBuddy。待取得官方 SDK/API 契约后，再实现 `xhzh/adp_client.py` 的真实调用分支。

## 测试

```bash
python tests/test_suite.py          # 全量（151 项：含 content-review 规则与输入阻断回归）
python tests/test_suite.py kb llm   # 只跑指定分组
python tests/test_mcp_suite.py      # MCP 协议级回归（76 项：含 CONTENT_BLOCKED 隐私拦截，独立临时库）
python preflight.py                 # 发布前依赖、知识库、主套件、端口自检
```

测试使用标准库运行器（不用 pytest）；主套件失败时返回非零退出码。当前基线和已知限制见项目父目录《测试与缺陷报告-v1.2.md》。

## 目录结构

```
xinghuo-zhijiao/
├── app.py               # Gradio 前端（左栏表单、右栏步骤/教案/下载，明细与历史默认收起；单实例无登录隔离）
├── mcp_server.py        # MCP 服务启动器（stdio；--token/--scope/--log-level）
├── preflight.py         # 演示前自检（依赖/知识库/MCP 协议层/测试套件/端口，exit 0 全绿）
├── .env.example         # 环境变量模板（复制为 .env 填真实值，勿提交）
├── tests/
│   ├── test_suite.py     # 标准库测试套件（151 项，见上）
│   └── test_mcp_suite.py # MCP 协议级回归（76 项，subprocess 拉起 stdio 服务）
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
│   ├── content_review.py # 规则式内容预检（安全/隐私阻断与教学质量复核提示）
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
