"""Pinned FinQA sampling; model inputs and host-only labels occupy separate trees."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import random
import urllib.request
from finagent.io import read_json, save_json

REVISION = "0f16e2867befa6840783e58be38c9efb9229d742"
RAW_HASHES = {"dev": "a847fb7e0d61a3125a1e2909852df6b89f1ee64d2c5ff1bf689e332214deee51",
              "test": "831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc"}


def source_group(case_id: str) -> str:
    return "/".join(case_id.split("/")[:2])


def prepare(root: Path, *, dev_count: int = 12, test_count: int = 12, seed: int = 20261010,
            raw_cache: Path | None = None, exclude_groups: set[str] | None = None) -> Path:
    root = Path(root).resolve()
    manifest_path = root / "host" / "manifest.json"
    excluded = sorted(exclude_groups or [])
    spec = {"revision": REVISION, "dev_count": dev_count, "test_count": test_count, "seed": seed,
            "excluded_source_groups": excluded}
    if min(dev_count, test_count) < 1:
        raise ValueError("Both splits need at least one case")
    if manifest_path.exists():
        existing = read_json(manifest_path)
        if existing["selection"] != spec:
            raise ValueError("Dataset is already frozen with a different selection")
        load_manifest(manifest_path)
        return manifest_path
    cases, sources = [], {}
    groups_seen = set(excluded)
    for split, count in (("dev", dev_count), ("test", test_count)):
        url = f"https://raw.githubusercontent.com/czyssrs/FinQA/{REVISION}/dataset/{split}.json"
        cached = Path(raw_cache) / f"{split}.json" if raw_cache else root / "host" / "raw" / f"{split}.json"
        if cached.exists():
            raw = cached.read_bytes()
        else:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "finance-research-agent/0.1"}), timeout=60) as response:
                raw = response.read()
        if hashlib.sha256(raw).hexdigest() != RAW_HASHES[split]:
            raise ValueError("Pinned upstream dataset hash mismatch")
        pool = sorted(json.loads(raw), key=lambda r: r["id"])
        random.Random(f"{seed}:{split}").shuffle(pool)
        selected = []
        for record in pool:
            group = source_group(record["id"])
            if group not in groups_seen:
                selected.append(record)
                groups_seen.add(group)
            if len(selected) == count:
                break
        if len(selected) != count:
            raise ValueError("Too few distinct company/year documents after exclusions")
        sources[split] = {"url": url, "sha256": RAW_HASHES[split], "total": len(pool)}
        gold = {}
        for record in selected:
            identity = record["id"]
            safe_id = hashlib.sha256(identity.encode()).hexdigest()[:20]
            doc = {"id": identity, "question": record["qa"]["question"], "pre_text": record["pre_text"],
                   "post_text": record["post_text"], "table": record["table"]}
            path = save_json(root / "inputs" / split / safe_id / "input.json", doc)
            cases.append({"case_id": identity, "source_group": source_group(identity), "split": split,
                "input_path": path.relative_to(root).as_posix(), "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            gold[identity] = {"answer": record["qa"]["exe_ans"], "program": record["qa"]["program"]}
        save_json(root / "host" / "gold" / f"{split}.json", gold)
    return save_json(manifest_path, {"dataset": "FinQA", "selection": spec, "sources": sources, "cases": cases,
        "gold_sha256": {s: hashlib.sha256((root / "host" / "gold" / f"{s}.json").read_bytes()).hexdigest() for s in ("dev", "test")}})


def load_manifest(path: Path) -> dict:
    path = Path(path).resolve()
    manifest = read_json(path)
    if manifest.get("dataset") != "FinQA" or manifest["selection"]["revision"] != REVISION:
        raise ValueError("Unsupported evaluation manifest")
    root = path.parent.parent
    ids = [c["case_id"] for c in manifest["cases"]]
    groups = [c["source_group"] for c in manifest["cases"]]
    if len(ids) != len(set(ids)) or len(groups) != len(set(groups)):
        raise ValueError("Evaluation documents must have unique IDs and source groups")
    for case in manifest["cases"]:
        candidate = (root / case["input_path"]).resolve()
        if not candidate.is_relative_to(root / "inputs") or hashlib.sha256(candidate.read_bytes()).hexdigest() != case["input_sha256"]:
            raise ValueError("Input scope or integrity mismatch")
        doc = read_json(candidate)
        if set(doc) != {"id", "question", "pre_text", "post_text", "table"} or doc["id"] != case["case_id"]:
            raise ValueError("Input contains forbidden fields or a mismatched ID")
    manifest["_root"] = str(root)
    return manifest


def host_gold(manifest: dict, split: str) -> dict:
    if split not in {"dev", "test"}:
        raise ValueError("Invalid split")
    path = Path(manifest["_root"]) / "host" / "gold" / f"{split}.json"
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["gold_sha256"][split]:
        raise ValueError("Gold integrity mismatch")
    gold = read_json(path)
    if set(gold) != {c["case_id"] for c in manifest["cases"] if c["split"] == split}:
        raise ValueError("Gold IDs mismatch")
    return gold

