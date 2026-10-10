# 来源与借鉴

本仓库由当前项目工程整理而来。基础原型为用户指定的 `William-Li123/my_agent`，先前实验基线提交为 `77216163b3dc8c29c74d8478e537f8261b236335`。保留并重组了本地金融扩展中的财务工具、终端、RPC、会话记忆与相关测试；模型循环、CLI、候选技能学习和统一评估层在这个独立包中实现。

GitHub 仓库于 2026-10-10 从 `my_agent` 改名为 `fin_agent`，主分支迁移为第二版金融实现。原始通用原型保留在 [归档分支](https://github.com/William-Li123/fin_agent/tree/archive/my-agent-before-fin-agent-20261010)，旧提交与新实现的历史均保留；此前的本地原型及金融扩展工作树未被覆盖。

以下成熟项目用于架构研究，未将其整个实现或预置技能包复制进本仓库：

- [Dexter](https://github.com/virattt/dexter/tree/534f6be455523c5926eba72bdaf4f914555be5c9)：技能元数据发现、按需加载、上下文与工具组织。
- [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot/tree/master/finrobot_desktop)：金融方法与确定性计算分层。
- [RD-Agent](https://github.com/microsoft/RD-Agent)：用运行结果而非仅凭文本反思判断改进。
- [TradingAgents](https://github.com/TauricResearch/TradingAgents)：结果反馈和历史经验时间边界的参考。

金融测试数据来自 [FinQA](https://github.com/czyssrs/FinQA)，固定提交 `0f16e2867befa6840783e58be38c9efb9229d742`。下载器记录原始 URL 与 SHA256；原始数据及标签不随源码提交。应遵守上游数据及公司年报的适用条款。本项目没有复制 FinQA 官方程序评估器，使用自行实现并明确命名的数值答案评估。

模型配置核验日期：2026-10-10。[DeepSeek 官方中文定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) 的 Flash 高峰费率为每百万 token：缓存命中输入 ¥0.04、未命中输入 ¥2、输出 ¥8；程序按这些配置费率保守核算。费率会变化，不应把这里的历史配置理解为永久价格。
