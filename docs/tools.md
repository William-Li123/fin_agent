# 工具、终端与数据边界

工具层只有一个公开入口：`finagent.tools.ResearchWorkspace`。Agent 通过 `execute(name, arguments)` 调用金融工具或终端，收到统一的 `success / structured_content / error` 结果。没有第二套受限 Python AST 执行器，也没有隐藏的框架工具注册路径。

## 工具目录

`workspace.catalog()` 返回 OpenAI function tool 格式的 JSON Schema。默认有以下 10 个工具；配置 MCP 后增加 `finance_mcp`。

| 工具 | 作用 | 主要参数 |
|---|---|---|
| `finance_list_documents` | 列出当前允许访问的文档和内容哈希 | 无 |
| `finance_read_document` | 分页读取正文段落和表格单元格，附稳定证据 ID | `document_id`, `offset`, `max_chars` |
| `finance_search_evidence` | 词法检索正文、表格及相邻段落 | `query`, `document_id`, `top_k` |
| `finance_read_table` | 按行列读取表格，保留单元格位置和来源 | `document_id`, `row_start`, `row_end`, `columns` |
| `finance_calculate` | 使用 Decimal 执行有界算术并保存计算回执 | `expression`, `variables`, `evidence_ids` |
| `finance_normalize_value` | 数字文本、金额规模、百分比和基点转换 | `value`, `unit`, `target_unit`, `currency`, `evidence_ids` |
| `finance_compare_periods` | 计算差额和 `(current-previous)/previous*100` | `current`, `previous`, `evidence_ids` |
| `finance_render_chart` | 生成附来源清单的 SVG 柱状图 | `title`, `labels`, `values`, `evidence_ids`, `filename` |
| `finance_write_report` | 生成 Markdown 报告及引用清单 | `title`, `body`, `evidence_ids`, `filename` |
| `finance_terminal` | Python、Shell、文件和后台进程 | `action` 及该动作需要的参数 |

表格行列从 0 开始，`row_end` 不包含末行；省略它可以读取剩余行。`read_document` 默认最多返回约 40,000 个证据内容字符；`has_more=true` 时用 `next_offset` 继续读取，单条记录不会被静默截断。检索是确定性的词法检索，不是向量检索。

计算支持 `+ - * / **` 和 `abs/min/max`，不调用 `eval`。缺失值不能当成零，非有限数、除零、过大指数会返回错误；上一期为零时，同比百分比明确为未定义。金额规模转换不会自动兑换币种。计算器验证算术和参数，不会根据变量名猜测该数字是否对应正确的会计科目，科目、期间和口径选择由研究流程与技能检查。

## 不调用模型的本地使用

在仓库根目录安装包后，可以直接检查工具；以下代码不产生模型费用。

```python
from pathlib import Path
from finagent.tools import ResearchWorkspace

with ResearchWorkspace(
    [Path("examples/company.json")],
    Path("output/tool-demo"),
) as workspace:
    result = workspace.execute("finance_read_document", {})
    assert result["success"], result["error"]
    print(result["structured_content"])
    print(workspace.execute("finance_terminal", {"action": "status"}))
```

输入为经过导入处理的五字段 JSON：`id / question / pre_text / post_text / table`。工具层拒绝夹带额外答案字段的 JSON；`raw`、`gold` 目录不能作为研究输入。每次运行复制输入快照，保留原始 JSON 字节的 SHA256；同一输出目录不能混用不同文档范围。多文档场景应传入明确的 `document_id`。

文件导入与 Agent 的命令分别是：

```powershell
finagent ingest .\report.pdf --output .\data\report --question "本期营收增长是多少？"
finagent doctor
finagent ask "本期营收增长是多少？" --input .\examples\company.json --session research
```

`ingest` 和 `doctor` 不调用模型。`ask` 需要已配置的模型凭据和预算账本，按 README 的预算初始化流程执行。

## Docker 终端

```powershell
docker build -t finance-agent-terminal:local -f docker/Dockerfile .
```

默认使用 Docker。Docker 或本地镜像不可用时，执行失败并返回原因，不会自动在宿主机运行。镜像包含 pandas、pypdf、openpyxl、matplotlib 和 requests；镜像没有模型 SDK，也不接收宿主机 API 密钥。

`finance_terminal` 的动作如下：

| 动作 | 参数和行为 |
|---|---|
| `python` | `code`；启动一次真实 Python 进程，可选 `timeout_seconds` |
| `shell` | `command`；Docker 中使用 `/bin/sh`，可选 `timeout_seconds` |
| `write_file` | `path`, `content`；相对当前工作区写入 UTF-8 文件 |
| `read_file` | `path`；读取工作区文件，返回截断标志 |
| `list_files` | 可选 `path`，默认 `.`；最多列出 200 项 |
| `start_job` | `code` 或 `command` 二选一；立即返回 `job_id` |
| `poll_job` | `job_id`；查询当前状态、日志摘录和结果 |
| `terminate_job` | `job_id`；结束本会话拥有的进程树 |
| `read_record` | `path`, `offset`, `limit`；通过宿主代理分页读取获准的运行记录 |
| `status` | 后端、可用性、执行额度及本会话作业列表 |

文件在同一次研究运行的工作区内持续存在。每次 Python / Shell 执行都会创建新进程，Python 全局变量、Shell 环境修改不会自动延续。用文件在调用之间传递 DataFrame、脚本或中间结果。后台作业属于当前工作区对象；关闭对象会清理其进程，不能跨 Agent 进程重启续跑。

### 批量调用金融 SDK

Python 自动注入 `finance`、`mcp`、`records`、`emit`；脚本文件也可显式导入：

```python
from finance_runtime_sdk import finance, mcp, records, emit

documents = finance.list_documents()["documents"]
document_id = documents[0]["document_id"]
hits = finance.search_evidence(query="revenue", document_id=document_id, top_k=5)
table = finance.read_table(document_id=document_id)
emit(hits)
emit(table)
```

确认所选单元格的科目和期间后，可以在同一段代码中批量读取、计算和生成产物：

```python
# 示例表：第 1 行为营收，第 1/2 列分别为上一期/本期。
cells = finance.read_table(row_start=1, row_end=2)["rows"][0]["cells"]
refs = [cells[1]["evidence_id"], cells[2]["evidence_id"]]
result = finance.calculate(
    expression="(current-previous)/previous*100",
    variables={"current": cells[2]["text"], "previous": cells[1]["text"]},
    evidence_ids=refs,
)
emit(result)
emit(finance.render_chart(
    title="Revenue", labels=["Previous", "Current"],
    values=[cells[1]["text"], cells[2]["text"]], evidence_ids=refs,
))
emit(finance.write_report(
    title="Revenue analysis", body=f"Revenue changed by {result['value']} percent.",
    evidence_ids=refs,
))
```

`finance.read_document(document_id=..., offset=0, max_chars=40000)` 也可在脚本中使用。`read_table` 和 `search_evidence` 默认紧凑化返回数据，保留数值、证据 ID 和位置；需要完整宿主响应时可传 `compact=False`。

`emit` 返回有限大小的 JSON 结果，便于模型读取；普通 `print` 进入 stdout。完整 stdout/stderr、代码哈希、执行配置、SDK 调用记录和退出状态保存在宿主运行目录。输出摘要截断不会删除完整日志。失败执行已经写出的文件和完成的计算可能仍存在，诊断中会保留，不能将整次失败执行当作已验证的研究结论。

### 读取文件与读取记录

`read_file` 只接受工作区内相对路径，拒绝 `..`、绝对路径和符号链接越界。`read_record` / `records.read` 用于读取宿主保存的上下文归档和执行日志，只允许当前运行目录及已绑定的当前 session/scope 记忆记录，不挂载整个宿主目录。

```python
# path 应使用工具返回的 record_path、stdout_path 或上下文归档路径。
page = records.read(path="tools.jsonl", offset=0, limit=12000)
emit(page)
# has_more 为真时，用 next_offset 继续；偏移和长度按字节计算。
```

### 执行限制

默认容器关闭网络、根文件系统只读、移除 Linux capabilities，启用 `no-new-privileges`；仅挂载本次工作区和只读的净化输入。默认内存 512 MB、1 CPU、64 个进程，临时目录 64 MB。单次执行默认 30 秒、可请求最多 120 秒，最多 4 个并行作业。

单次执行最多 32 次业务 RPC，整个工作区最多 64 次；金融、MCP 和记录读取共用额度。`emit` 最多 64 条、合计 64,000 个 JSON 字节；文件写入上限 1 MB，stdout/stderr 摘录各 12,000 字符，日志总量上限 20 MB。监督线程独立运行，即使没有继续轮询也会终止超时作业和子进程。

`trusted-local` 必须明确选择，会以当前用户身份在宿主机执行，清理环境变量和文件路径校验不构成沙箱。默认研究和真实验收均使用 Docker；自动化测试另用该显式选项快速验证进程管理。

## 证据与计算回执

以下三个集合由宿主维护：

- `observed_evidence_ids`：成功的正文读取、表格读取或检索实际返回的来源。
- `observed_calculations`：宿主金融工具生成的计算 ID 与完整回执。
- `observed_artifacts`：宿主生成的图表、报告及来源清单。

SDK 通过进程间 JSON 消息调用宿主金融工具，不接触模型客户端。引用证据前必须读取它。工作区直接调用与后续终端调用可以共享已验证的来源；失败终端执行中的部分结果不会进入这些已完成结果集合。

终端输出随意打印 `ev_...` 或 `calc_...` 不会建立来源。`last_trusted_observation` 供记忆模块使用，来自宿主工具和 SDK 对象，避免把代码伪造的 stdout / `emit` 当成真实计算记录。宿主审计日志会脱敏形似 API key 的字符串和常见凭据字段；业务文件与原始终端输出仍需按实际数据敏感性管理。

## 可选 MCP

MCP 是宿主工具代理的连接方式。终端提供代码组合能力；外部 MCP 工具仍可通过同一代理调用，无需把账户凭据交给容器。

```json
{
  "servers": {
    "research": {
      "command": ["python", "C:/path/to/your_mcp_server.py"],
      "allowed_tools": ["search_filings", "get_company_profile"],
      "env_names": ["RESEARCH_VENDOR_TOKEN"],
      "timeout_seconds": 20
    }
  }
}
```

以上是配置模板，服务器脚本和账户由使用者提供。通过 `finagent ask ... --mcp-config mcp.json` 启用；服务器必须同时被配置且公布相应工具，只有白名单工具可调用。默认没有 MCP 服务器，没有外部行情账户。

```python
emit(mcp.list_tools("research"))
emit(mcp.call("research", "get_company_profile", {"symbol": "EXAMPLE"}))
```

也可以通过 `finance_mcp` 直接调用，参数为 `server / operation / tool / arguments`。目前支持 stdio 传输，不含远程 HTTP、OAuth、服务端 sampling 或 elicitation。MCP 返回数据不会自动升级成本地财报证据或已验证的计算回执；正式引用仍需要建立可追溯的本地文档。

## 已验证范围

工具、终端、连接器、记忆与工作区测试共 **87 项通过、1 项跳过**，包含真实 Docker 集成测试；跳过项是在当前 Windows 环境无法建立的符号链接夹具。Docker 验收实际检查了分析库导入、宿主密钥不进入容器、净化输入挂载、财务 SDK 计算、SVG/Markdown 产物及跨执行文件持久化。

MCP 测试使用本地真实 stdio 子进程，验证初始化、工具发现、白名单、错误返回、超时关闭及重连。尚未验证任何商业数据服务、实盘交易或生产金融账户。

```powershell
$env:FINAGENT_DOCKER_TESTS = "1"
python -m pytest tests/test_tools_finance.py tests/test_tools_connectors.py tests/test_tools_workspace.py tests/test_memory_store.py tests/test_terminal_runtime.py tests/test_terminal_docker.py -q
```

实现文件按职责拆分在 `src/finagent/tools`；会话与证据记忆独立位于 `src/finagent/memory/store.py`。终端、金融工具和记忆的基础实现迁自本项目之前的 `my_agent/backend/finance` 工作版本，移除了原框架导入、遥测依赖和特定题型的变量名规则。
