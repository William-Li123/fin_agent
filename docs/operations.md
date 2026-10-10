# 运行与排错

## 状态与产物

默认 `.finagent/` 保存 `budget.sqlite3`、`memory.sqlite3`、`memory_records/`、`skills/`、`learning/` 和交互运行；显式 `--output` 会将本轮产物写入指定目录。每次运行保留：

- `request.json`：问题、配置、文档清单和初始状态，不含凭据。
- `model/step_*.json`：模型响应及计费调用 ID。
- `tools.jsonl`、`trace.json`：工具调用、错误和结果。
- `context/event_*.json`：可分页回读的完整观察。
- `manifest.json`：来源、计算和文件产物回执。
- `result.json`：最终状态、答案、技能、记忆使用、错误及产物路径。

`request.json` 的初始 `status=failed` 不是结论；以 `result.json` 为准。`completed` 表示输出通过结构和证据校验，不代表外部评分一定正确。`tool_errors` 保留被修复的中间错误。

## 费用

`budget init` 只初始化一次。重新运行、API 验收和技能提案都应复用同一个 `--state`，否则无法约束跨任务累计费用。账本在请求前原子预留，在返回 usage 后结算；缓存输入会按对应费率处理。

网络断开或 usage 不可信时，账本保留预留并冻结新请求。此时先核对供应商用量记录，不能删除账本或创建新目录绕过总预算。当前版本没有自动清除未知费用的命令。SDK 不自动重试；提案格式修复属于另一次明确计费的模型调用。

已经获得新的累计预算授权时，使用 `BudgetLedger.open(path).raise_ceiling(new_ceiling_cny, reason)` 追加上调记录。原始 `config` 保持不可修改；`status()` 的 `ceiling_cny` 返回有效上限，`initial_ceiling_cny` 返回初始上限；`authorizations()` 可查询授权历史。该操作禁止下调和重复上调，不会释放未知费用、清除使用记录或解除计费冻结。模型与工具不能自行调用这个宿主预算接口。本机 2026-10-10 的 ¥50→¥100 上调沿用同一个账本。

## Docker

`finagent doctor` 检查 Docker 引擎与 `finance-agent-terminal:local` 镜像，完全离线于模型。若镜像不存在，按 README 构建。若 Docker 引擎报错，先修复 Docker Desktop；工具不会静默回退到宿主执行。

终端默认无网络。要调用外部服务，优先在宿主机配置 MCP 白名单与显式环境变量名；容器从不直接接收模型密钥。`trusted-local` 是明确选用的宿主执行模式，不是安全沙箱。

## 会话恢复

相同 `--session` 和相同文档内容形成同一个记忆范围。更换任何输入内容，范围哈希就变化，旧财报事实不会混入新材料。

若进程意外终止留下租约，从上一轮 `result.json` 或记录取得 scope，再检查：

```powershell
finagent memory status --session demo --scope SCOPE_HASH
finagent memory export --session demo --scope SCOPE_HASH
```

确认原进程已经停止后，可显式把遗留轮次标记失败并释放租约：

```powershell
finagent memory fail-turn --key TURN_ID --value "原进程已退出，保留失败诊断"
```

失败轮次不进入成功事实检索。用户偏好通过 `memory preference --session demo --key language --value Chinese` 设置；可加 `--scope` 限定文档范围。

## 工程边界

上下文按字符预算，模型费用按保守 UTF-8 token 上界预留；过长的固定问题/记忆/技能会在发送前失败。大工具观察会归档并注明不完整，Agent 可调用 `context_read` 回读。

PDF 仅提取文本。扫描 PDF 会要求提供可提取文字的材料；复杂表格不保证结构恢复。XLSX 读取已缓存数值，缺失公式缓存不能当作零。检索是词法匹配，没有伪称向量 RAG。MCP 只验证通用协议与本地测试服务，未宣称接通任何商业金融数据源。

研究结论的风险主要在来源选择、年份、口径、单位和任务理解。算术、引用与隔离控制不能替代金融正确性评估。技能的小样本验证是上线门槛，不是统计上的普遍收益证明。
