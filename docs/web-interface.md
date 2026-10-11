# 金融研究界面

界面围绕一轮真实研究展开：选择资料、提问、查看执行过程、阅读来源与计算结果、下载报告和图表，然后在同一会话追问。页面使用 React、TypeScript 和 Vite，Python Web 层使用 FastAPI，调用现有 `ResearchAgent`。

## 启动

在仓库根目录安装 Web 和文档扩展，构建前端：

```powershell
python -m pip install -e ".[web,documents]"
cd frontend
npm ci
npm run build
cd ..
finagent --state .finagent web --port 8000
```

访问 `http://127.0.0.1:8000`。服务默认只绑定本机地址。`python -m finagent.web --state .finagent --port 8000` 是同一服务的独立入口。

源码目录默认提供 `frontend/dist` 的构建内容。使用安装后的 Python wheel 时，前端需独立构建，并显式传入 `finagent --state PATH web --frontend FRONTEND_DIST_PATH`；wheel 不包含 Node 依赖或构建产物。

开发前端时保留 Python 服务，在另一终端运行：

```powershell
cd frontend
npm run dev
```

Vite 在 `127.0.0.1:5173` 提供页面，把 `/api` 请求转发给 `127.0.0.1:8000`。调整后端端口时，应同步修改开发代理配置。

## 分析与费用

新会话不附带任何资料。上传并选择真实文件后，填写问题并开始分析；首页快捷问题同样使用选中的上传资料。生产入口不生成演示文档，不提供演示模式。已有本地状态会移除演示目录条目和当前资料选择，历史任务、来源证据及产物仍可查看。

分析从启动服务的进程环境读取 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`。状态页仅显示是否配置密钥，不发模型请求来验证连通性。前端和静态构建中不包含 API Key；环境变量也不会通过接口返回。

Windows 可使用 `./scripts/start-local-web.ps1 -PythonExe python -Port 8000` 复用本地启动配置。脚本优先读取当前进程环境；缺少密钥时，读取当前 Windows 用户保存在 `%LOCALAPPDATA%/FinAgent/deepseek-key.dpapi` 的 DPAPI 加密缓存，仅向服务进程传递解密后的环境变量，退出后恢复原环境。缓存位于仓库之外，脚本不写入或打印密钥，不初始化预算，也不自动调用模型验证。需要其他目录时传入 `-State` 与 `-Frontend`。

真实请求必须使用 `--state` 下已有的 `budget.sqlite3`。服务不提供预算初始化、提额、清零或解除冻结接口。已有研究账本应继续使用同一路径；若是全新安装，请先按主 README 的预算初始化流程操作。预算是项目按配置费率计算的估算值，并非供应商实扣账单。

点击停止会阻止后续模型步骤。若已有请求正在发送或等待返回，服务会等待该请求完成计费结算，然后停止继续执行；不能用中断绕过账本保留和结算。

## 状态与资料

Web 会话、上传资料和运行目录放在 `--state` 下的 Web 状态目录，均受 `.gitignore` 排除。页面刷新后会从服务端恢复会话；浏览器只保存界面偏好和最近选中的会话 ID。

上传支持 JSON、CSV、TXT、Markdown、PDF 和 XLSX。解析复用现有 ingestion：JSON 必须符合无标签的五字段文档契约，PDF 仅提取可读取文字，XLSX 只读取已缓存的值。上传不会自动触发模型调用。模型只访问本轮选中的文档作用域。

资料侧栏展示来源预览；执行记录展示已发生的模型步骤、Skill 加载、工具调用与错误；报告侧栏提供本轮实际生成的文件。报告内容作为文本或图片展示，不执行上传材料中的 HTML 或脚本。

Skills 页面展示现有技能正文及隔离中的学习候选。候选生成、跨来源验证和晋升仍遵循现有学习流程；页面不会把候选自动激活为正式技能。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/status` | 模型配置、Docker、共享预算 |
| `GET/POST /api/sessions` | 列出或创建研究会话 |
| `GET/PATCH /api/sessions/{id}` | 会话历史、标题和选择的资料 |
| `GET/POST /api/documents` | 列出或上传研究资料 |
| `GET /api/documents/{id}` | 有界资料预览 |
| `POST /api/sessions/{id}/runs` | 对选定资料开始真实研究 |
| `GET /api/runs/{id}` | 状态、执行事件、最终结果和产物 |
| `POST /api/runs/{id}/cancel` | 阻止后续执行 |
| `GET /api/runs/{id}/artifacts/{artifact_id}` | 下载已登记的产物 |
| `GET /api/skills` 与 `GET /api/skills/{name}` | 技能元数据与正文 |
| `GET /api/learning` | 失败经验、候选及验证记录 |

写请求携带 `X-Finagent-Client: web`，服务检查本机 Host 与 Origin。产物按注册 ID 提供，接口不接受任意宿主文件路径。本版本是本机单用户研究工作台，未设计公网多用户登录、租户隔离或集群任务调度。

## 离线验收

```powershell
python -m pip install -e ".[web,documents,dev]"
python -m pytest -q
cd frontend
npm ci
npm run build
npx playwright install chromium
npm run test:e2e
```

本机已有 Chromium 时，可用环境变量 `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` 指定可执行文件。浏览器测试自动启动 8011 端口的服务，使用 `.finagent/web-e2e-real` 隔离状态和空 API Key；不会改动研究预算，也不能发起付费请求。

2026-10-10 移除默认演示资料后的本机验收：Python **274 项通过、2 项跳过**，其中 13 项覆盖 Web，包括真实默认入口、旧资料迁移、实际工具链、取消计费语义、产物边界及公开来源 URL 保留。浏览器 **4 项通过**，覆盖无预选资料、真实 Apple CSV 上传预览、移除资料、会话恢复、未配置密钥时的提交检查、Skills、浅深色与移动端。TypeScript 与生产构建通过。测试中的脚本模型仅通过显式 Python 测试钩子注入，不属于生产页面能力。GitHub 工作流同步加入构建和浏览器验收；推送后的 CI 结果需独立确认。

以下截图来自本机浏览器测试，展示真实 CSV 的上传预览：

![研究结果与执行过程](screenshots/fin-agent-workspace.png)

## 验证范围

前端新增会话、资料、运行与展示层，复用原有模型、金融工具和技能机制。此前报告中的 84% 是冻结版本在固定 50 道 FinQA 题上的历史评测结果；新增 Web 文件会改变完整源码指纹，不能把历史分数视为本次 Web 版本重新跑测所得。

安装、构建、上传和预览不会调用模型。离线测试以注入模型验证实际工具链和 Web 行为，不代表新的付费模型质量验收。真实模式模型质量继续以已公开的研究验收记录为依据。
