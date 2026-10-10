# 技能与失败学习

本项目的 skill 是可版本化的金融研究操作方法。它告诉 Agent 何时检查口径、如何选取证据、怎样计算与复核；公司数据和某道题的答案不应成为 skill。代码入口是 `src/finagent/skills/registry.py` 与 `src/finagent/learning/service.py`。

## 内置技能与按需加载

| 技能名 | 适用工作 |
| --- | --- |
| `source-alignment` | 核对主体、期间、单位、会计定义和来源冲突 |
| `financial-ratios` | 比率、增长率、占比、平均值及分母检查 |
| `statement-reconciliation` | 资产负债表、现金流和利润表的勾稽复核 |
| `comparative-analysis` | 公司、分部或期间的可比分析与表格 |
| `valuation-dcf` | 在输入充分且假设明确时进行 DCF 情景计算 |
| `terminal-analysis` | Python、SQL、批量计算、文件和后台任务的可复现操作 |

每个技能保存在 `src/finagent/skills/builtin/<name>/SKILL.md`。文件使用受限 YAML 前置元数据：`name`、`description`、`triggers`、`version`，随后是 Markdown 操作正文。字符串采用 JSON 引号形式，`triggers` 使用 JSON 数组；加载器不执行 YAML 标签。

`SkillRegistry.catalog()` 只返回名称、描述、触发词、版本与哈希。运行时先把这些元数据放入上下文，模型选择 `{"action":"skill","name":"financial-ratios"}` 后，`load()` 才载入正文。默认单轮最多加载三个技能，重复加载不会重复增加正文。候选技能不出现在正式目录中。

```powershell
finagent --state .finagent skills list
finagent --state .finagent skills show financial-ratios
```

技能不能注册新工具、更改系统规则、增加预算或获得额外文件权限。即使模型读取了技能，最终答案仍需通过运行时的证据与计算记录校验。

## 从实际失败到候选

流程是：开发失败记录 → 模型提炼通用操作方法 → 隔离候选 → 独立案例双臂验证 → 晋升或拒绝。

`record_failure()` 保存问题、有限长度的执行轨迹、错误诊断、数据划分和来源组。它清理常见密钥格式、凭据字段，并移除显式 `gold_answer`、`expected_answer` 等标签字段。调用方仍有责任提供不含答案的诊断，例如“分母期间错误”，而非“正确答案为某值”。

`propose()` 通过统一的付费模型客户端请求一个 JSON 技能草案。候选必须使用 `learned-` 名称前缀，给出触发条件、证据检查、操作步骤、复核方法及停止条件。保守过滤拒绝数字字面量、源案例标识、部分凭据与明显覆盖系统规则的文本。通过格式检查后，候选保存在状态目录的 `learning/candidates/`，仍然处于隔离状态。

提案明确要求 `triggers` 为一至八个短字符串，每个二至六十字符。JSON、字段结构或内容检查失败时，默认允许一次同样计费的修复调用，因此一次 `propose()` 最多产生两次请求；Python 接口可用 `max_repairs=0` 关闭修复。网络、预算及模型客户端异常不会触发修复重试。每次调用前写入 `learning/proposals/` 收据，随后保存脱敏且有长度上限的响应、校验失败原因、调用 stage 和是否最终失败；候选记录关联全部 `proposal_attempt_ids`。`LearningService.proposal_attempts()` 可读取这些收据，避免付费失败后只剩一条错误消息。

开发评估产生的运行目录可手动记录失败，再生成候选：

```powershell
finagent --state .finagent learn record --run artifacts/dev/runs/case_000 --error "The selected denominator belongs to the wrong reporting period"
finagent --state .finagent learn propose exp-REPLACE_WITH_RETURNED_ID
finagent --state .finagent learn list
```

以上 ID 是占位符，应替换为命令输出的实际 ID。`learn propose` 会调用模型并计费。也可以在 `ask` 命令后添加 `--learn-on-failure`；发现工具或最终执行失败后，Agent 会尝试记录经验并生成候选，生成失败也会写入运行结果。这个选项不会自动批准候选。

`test` 与 `holdout` 不能产生学习经验或用于候选晋升。交互问答的自动学习按开发数据处理；若要复现标准学习流程，优先使用明确划分的开发集运行记录。

## 独立验证与晋升

`LearningService.validate(candidate_id, cases, evaluate)` 对每个案例分别运行 baseline 和临时注入候选的 Agent。候选只允许在开发验证模式中注入。两个分支使用同一输入，真实评估器提供模型、配置、数据选择与正式技能目录的指纹；每次运行使用独立会话，避免两臂共享题目记忆。

晋升门槛是：

1. 至少三个不同案例，且来自三个不同 `source_group`；排除最初失败的案例及其来源组。
2. 案例必须是 `train`、`dev` 或 `validation` 数据。项目 CLI 从清单的 `dev` 集选取。
3. 至少一题从 baseline 失败变为候选成功，且没有成功变失败的退步。
4. 没有评估器异常，两臂评估上下文一致，验证期间正式技能目录不变。
5. 晋升时再次校验保存的候选、报告哈希和当前正式技能目录。目录变化后需要重新验证。

```powershell
finagent --state .finagent learn validate candidate-REPLACE_WITH_RETURNED_ID --manifest data/finqa/host/manifest.json --output artifacts/skill-validation --count 3
finagent --state .finagent learn promote candidate-REPLACE_WITH_RETURNED_ID --report report-REPLACE_WITH_RETURNED_ID
```

`learn validate` 会运行每题两次并计费，报告写入所给输出目录的 `validation.json`。`learn promote` 不调用模型；拒绝报告不能晋升。无改进、出现退步、评估异常或目录变化都会留下明确的拒绝原因。不要为了通过门槛反复挑选只对候选有利的案例。

这里的三题门槛是最低工程验收条件，不能证明稳定的统计收益。需要更强结论时，应预先固定更大的开发验证集，并在技能冻结后使用未参与调优的测试集；保留全部失败案例和分母。

## 版本、禁用和回滚

通过验证的内容写入 `skills/versions/<name>/<version-id>/`，包含 `SKILL.md` 与验证清单；`skills/registry.json` 保存激活指针、版本历史及操作事件。版本内容不可覆盖，加载时核对内容哈希。

```powershell
finagent --state .finagent skills versions learned-denominator-check
finagent --state .finagent skills disable learned-denominator-check
finagent --state .finagent skills enable learned-denominator-check
finagent --state .finagent skills rollback learned-denominator-check
```

示例名称同样需要替换成真实技能名。`rollback` 默认恢复前一个已晋升版本，也可用 `--version` 指定历史版本。首次晋升没有前一版本时，应禁用技能。回滚不会把未验证候选变成正式版本。`learn list` 展示 `quarantined`、`validated`、`rejected`、`active` 或 `promoted_inactive` 状态。

## 能力边界与验证位置

过滤规则不能证明文本完全没有提示注入或隐含答案泄漏；内容哈希用于追踪和检测修改，不是抵御拥有宿主机写权限者的数字签名。真正的执行边界仍然是受限工具、Docker、证据作用域、预算与宿主机验证器。

技能质量测试位于 `tests/test_skills.py` 和 `tests/test_learning.py`，覆盖路径逃逸、隔离、来源独立性、测试集拒绝、无收益/退步拒绝、评估异常、内容修改检测、目录冻结及版本回滚。离线测试证明这些控制流程的行为；真实 API 效果应查看具体实验报告，不能由单元测试推断。
