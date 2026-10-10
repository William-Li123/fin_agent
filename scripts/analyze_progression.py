"""Offline statistics and figures from frozen historical predictions; no model calls."""
from __future__ import annotations
import argparse
from collections import defaultdict
from decimal import Decimal
import hashlib
import math
from pathlib import Path
import random

from finagent.evaluation.dataset import source_group
from finagent.io import read_json, save_json


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wilson(correct, total):
    z = 1.959963984540054
    proportion, denominator = correct / total, 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [center - half, center + half]


def paired(before, after, ids):
    gain = sum(not before[i] and after[i] for i in ids)
    loss = sum(before[i] and not after[i] for i in ids)
    disagreements = gain + loss
    p = min(1, 2 * sum(math.comb(disagreements, j) for j in range(min(gain, loss) + 1)) / 2 ** disagreements) if disagreements else 1
    grouped = defaultdict(list)
    for i in ids:
        grouped[source_group(i)].append(int(after[i]) - int(before[i]))
    groups, rng = list(grouped.values()), random.Random(20261010)
    samples = []
    for _ in range(20000):
        drawn = rng.choices(groups, k=len(groups))
        samples.append(sum(sum(g) for g in drawn) / sum(len(g) for g in drawn))
    samples.sort()
    return {"gains": gain, "losses": loss,
            "both_correct": sum(before[i] and after[i] for i in ids),
            "both_wrong": sum(not before[i] and not after[i] for i in ids),
            "difference": (gain - loss) / len(ids),
            "case_independent_exact_mcnemar_p_two_sided": p,
            "company_year_cluster_bootstrap_95_percentile_interval": [samples[500], samples[19499]],
            "bootstrap_groups": len(groups), "bootstrap_repetitions": 20000, "bootstrap_seed": 20261010,
            "caution": "Descriptive exploratory statistics for a reused cohort. Case independence is violated by repeated source groups; neither test proves architecture causality."}


def analyze(old_root: Path, new_report: Path, output: Path):
    old_root, new_report, output = (Path(p).resolve() for p in (old_root, new_report, output))
    baseline_path = old_root / "output/finance/paired_baseline_20261010/offline_fairness_audit.json"
    first_path = old_root / "output/finance/final_holdout_20261010/results.json"
    campaign_path = first_path.with_name("campaign.json")
    experiment_path = baseline_path.with_name("experiment.json")
    manifest_path = old_root / "data/finance/final_20261010/host/manifest.json"
    manifest, baseline, first, second = [read_json(p) for p in (manifest_path, baseline_path, first_path, new_report)]
    campaign = read_json(campaign_path)
    ids = [c["case_id"] for c in manifest["cases"]]
    if len(ids) != 50 or len(set(ids)) != 50 or not second["all_finished"]:
        raise ValueError("Expected the complete frozen fifty-case cohort")
    def audit_scores(name):
        return {r["case_id"]: r["correct"] for r in baseline[name]["results"]}
    scalar = audit_scores("baseline_raw_answer")
    executed = audit_scores("baseline_shared_decoder_program_only")
    v1 = {r["case_id"]: r["answer_correct"] for r in first["metrics"]["test"]["rows"]}
    v2 = {r["case_id"]: r["passed"] for r in second["results"]}
    stages = {"baseline_raw_scalar": scalar, "baseline_posthoc_executed_program": executed,
              "financial_v1_scalar": v1, "financial_v2_scalar": v2}
    if any(set(s) != set(ids) for s in stages.values()):
        raise ValueError("Stages do not cover exactly the same questions")
    if [a["case_id"] for a in campaign["attempts"]] != ids:
        raise ValueError("Historical usage ledger does not match the frozen cohort")
    ledger = [entry for a in campaign["attempts"] for entry in a["usage"]["ledger"]]
    if any(entry["status"] != "reconciled" for entry in ledger):
        raise ValueError("Historical usage has unreconciled requests")
    v1_input = sum(entry["usage"]["input_tokens"] for entry in ledger)
    v1_output = sum(entry["usage"]["output_tokens"] for entry in ledger)
    v2_input, v2_output = second["usage"]["prompt_tokens"], second["usage"]["completion_tokens"]
    def uncached_cost(input_tokens, output_tokens):
        return (Decimal(input_tokens) * 2 + Decimal(output_tokens) * 8) / 1_000_000
    cost_v1, cost_v2 = uncached_cost(v1_input, v1_output), uncached_cost(v2_input, v2_output)
    counts = {name: {"correct": sum(s.values()), "total": 50, "accuracy": sum(s.values()) / 50,
                     "case_independent_wilson_95_interval": wilson(sum(s.values()), 50)} for name, s in stages.items()}
    result = {"kind": "offline_exploratory_progression_analysis", "new_model_calls": 0,
              "source_hashes": {str(p): sha(p) for p in (manifest_path, baseline_path, first_path, campaign_path, experiment_path, new_report)},
              "normalized_all_input_cache_miss_cost": {
                  "rates": "CNY 2/million input and 8/million output",
                  "v1_cny": str(cost_v1), "v2_cny": str(cost_v2),
                  "relative_change": str(cost_v2 / cost_v1 - 1),
                  "v1_input_tokens": v1_input, "v1_output_tokens": v1_output,
                  "v2_input_tokens": v2_input, "v2_output_tokens": v2_output,
                  "v1_model_calls": len(ledger)},
              "metrics": counts,
              "paired": {"baseline_raw_to_v1": paired(scalar, v1, ids),
                         "baseline_posthoc_program_to_v1": paired(executed, v1, ids),
                         "v1_to_v2": paired(v1, v2, ids)},
              "case_scores": [{"case_id": i, "source_group": source_group(i), **{n: s[i] for n, s in stages.items()}} for i in ids],
              "settings": {"baseline": read_json(experiment_path), "v1": first["frozen_settings"], "v2": second["freeze"]["config"]},
              "cautions": ["Original unmodified my_agent has no comparable financial score",
                           "Raw scalar and executed-program metrics are different; baseline has two explicit views",
                           "Posthoc baseline execution is not an additional paid model experiment",
                           "All confidence intervals describe the historical sample, not unseen production finance tasks"]}
    # Do not copy historical experiment's redundant fifty full predictions into the summary.
    result["settings"]["baseline"] = {k: v for k, v in result["settings"]["baseline"].items() if k not in {"runs", "summaries", "notice"}}
    save_json(output / "progression-statistics.json", result)
    return result


def figures(result, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    available = {f.name for f in font_manager.fontManager.ttflist}
    for family in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC"):
        if family in available:
            plt.rcParams["font.family"] = family
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 4.9), gridspec_kw={"width_ratios": [1.4, 1]}, layout="constrained")
    stages = ["baseline_raw_scalar", "financial_v1_scalar", "financial_v2_scalar"]
    values = [100 * result["metrics"][s]["accuracy"] for s in stages]
    bars = axes[0].bar(["单次模型基线\n原始数值离线复评", "第一版金融 Agent\n已验收最终数值", "第二版金融 Agent\n已验收最终数值"], values,
                       color=["#A8B1BC", "#507DB4", "#31846D"], width=0.6)
    for bar, s, value in zip(bars, stages, values):
        axes[0].text(bar.get_x() + bar.get_width() / 2, value + 2,
                     f"{result['metrics'][s]['correct']}/50 · {value:.0f}%", ha="center", fontweight="bold")
    axes[0].set_ylim(0, 101)
    axes[0].set_ylabel("原始 exe_ans 尺度的最终数值匹配率（%）")
    axes[0].set_title("完整系统得分进展 · 同一批 50 题", pad=15)
    axes[0].spines[["top", "right"]].set_visible(False)
    axes[0].text(0.02, 0.97, "基线生成程序事后执行：29/50（58%）\n该指标单列，不混作最终数值分数", transform=axes[0].transAxes,
                 va="top", fontsize=9, color="#475569")
    p = result["paired"]["v1_to_v2"]
    cells = [[p["both_correct"], p["losses"]], [p["gains"], p["both_wrong"]]]
    axes[1].imshow(cells, cmap="Blues", vmin=0, vmax=34)
    texts = [["两版都对", "由对变错"], ["由错变对", "两版都错"]]
    for row in range(2):
        for col in range(2):
            axes[1].text(col, row, f"{texts[row][col]}\n{cells[row][col]}", ha="center", va="center",
                         color="white" if cells[row][col] > 17 else "#172B4D", fontsize=13, fontweight="bold")
    axes[1].set_xticks([0, 1], ["第二版正确", "第二版错误"])
    axes[1].set_yticks([0, 1], ["第一版正确", "第一版错误"])
    axes[1].set_title("逐题配对：8 改善、1 退步", pad=15)
    fig.suptitle("金融 Agent 的工程进步与评测边界", fontweight="bold", fontsize=15)
    fig.savefig(output / "progression-results.png", dpi=180)
    svg_path = output / "progression-results.svg"
    fig.savefig(svg_path)
    svg_path.write_text("\n".join(line.rstrip() for line in svg_path.read_text(encoding="utf-8").splitlines()) + "\n", encoding="utf-8")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-root", type=Path, required=True)
    parser.add_argument("--new-report", type=Path, default=Path("runs/same50_20261010/report.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/research"))
    parser.add_argument("--figures", action="store_true")
    args = parser.parse_args()
    result = analyze(args.old_root, args.new_report, args.output)
    if args.figures:
        figures(result, args.output)
    print({"metrics": result["metrics"], "paired": result["paired"]["v1_to_v2"], "new_model_calls": 0})


if __name__ == "__main__":
    main()
