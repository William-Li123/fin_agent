"""Explicit scripted demonstration, with real observed evidence and production tools."""
from __future__ import annotations

import json
import time

DEMO_QUESTIONS = ["分析演示公司的营收增长，生成图表和报告", "延续上一轮，重新核验演示公司的营收增长"]
ENGLISH_QUESTION = "Analyze demo revenue growth and generate a chart and report."
DEMO_DOCUMENT = {
    "id": "synthetic-web-company", "question": DEMO_QUESTIONS[0],
    "pre_text": ["SYNTHETIC DEMONSTRATION / 合成演示数据，非真实企业。Amounts in millions of CNY / 单位：人民币百万元。"],
    "post_text": ["Revenue uses the same accounting scope in both years. No future forecast or market data is provided."],
    "table": [["Metric", "2024", "2025"], ["Revenue", "1200", "1440"], ["Operating costs", "720", "864"]],
}


class DemoModel:
    model = "scripted-demo-no-api"

    def __init__(self, delay=0.12):
        self.delay = delay
        self.step = 0

    def complete(self, messages, *, stage):
        # Animation follows real sequential work; no invented model token usage.
        if self.delay:
            time.sleep(self.delay)
        self.step += 1
        results = [json.loads(item["content"].split("\n", 1)[1]) for item in messages
                   if item["content"].startswith("TOOL OBSERVATION ")]
        tools = [item["structured_content"] for item in results if "structured_content" in item]
        if self.step == 1:
            action = {"action": "skill", "name": "financial-ratios"}
        elif self.step == 2:
            action = {"action": "tool", "tool": "finance_read_document", "arguments": {}}
        else:
            evidence = next(item["evidence"] for item in tools if "evidence" in item)
            values = {item["column"]: item for item in evidence if item.get("row") == 1 and item.get("column") in {1, 2}}
            references = [values[1]["evidence_id"], values[2]["evidence_id"]]
            if self.step == 3:
                action = {"action": "tool", "tool": "finance_calculate", "arguments": {
                    "expression": "(current-previous)/previous*100", "variables": {
                        "current": values[2]["text"], "previous": values[1]["text"]}, "evidence_ids": references}}
            elif self.step == 4:
                action = {"action": "tool", "tool": "finance_render_chart", "arguments": {
                    "title": "Synthetic demo revenue / CNY million", "labels": ["2024", "2025"],
                    "values": [values[1]["text"], values[2]["text"]], "evidence_ids": references,
                    "filename": "demo-revenue.svg"}}
            elif self.step == 5:
                calculation = next(item for item in tools if "calculation_id" in item)
                action = {"action": "tool", "tool": "finance_write_report", "arguments": {
                    "title": "合成演示公司的营收分析", "body": (
                        "本报告使用合成演示数据，不代表真实企业或实时市场。\n\n"
                        f"2024年营收为{values[1]['text']}百万元，2025年为{values[2]['text']}百万元。"
                        f"同比增长率为{calculation['value']}%。\n\n"
                        "计算口径：(本期营收 − 上期营收) ÷ 上期营收 × 100。"
                        "仅比较历史两期数据，未提供估值、预测或投资建议。"),
                    "evidence_ids": references, "filename": "demo-report.md"}}
            else:
                calculation = next(item for item in tools if "calculation_id" in item)
                action = {"action": "finish", "final": {"status": "answered", "answer": calculation["value"],
                    "unit": "percent", "explanation": "合成演示数据：营收从1200增至1440百万元，按(1440−1200)/1200×100核验；图表和报告均由真实工具生成。",
                    "sources": references, "calculation_id": calculation["calculation_id"]}}
        return json.dumps(action, ensure_ascii=False)
