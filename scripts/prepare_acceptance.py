"""One-time reproducible acceptance selection, excluding documents used in old campaigns."""
from pathlib import Path
import argparse
from finagent.evaluation.dataset import prepare, source_group
from finagent.io import read_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-project", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/acceptance"))
    args = parser.parse_args()
    excluded = set()
    for path in (args.previous_project / "data" / "finance").rglob("manifest.json"):
        if path.parent.name != "host":
            continue
        for case in read_json(path).get("cases", []):
            excluded.add(source_group(case["case_id"]))
    raw = args.previous_project / "data/finance/raw/0f16e2867befa6840783e58be38c9efb9229d742"
    manifest = prepare(args.output, dev_count=12, test_count=12, seed=202610101,
                       raw_cache=raw, exclude_groups=excluded)
    print(f"Prepared {manifest}; excluded {len(excluded)} previously used company/year document groups")


if __name__ == "__main__":
    main()
