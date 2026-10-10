from pathlib import Path

import pytest

from finagent.skills import SkillError, SkillRegistry
from finagent.skills.registry import parse_skill


def test_catalog_discloses_metadata_and_explicit_load_discloses_body(tmp_path: Path):
    registry = SkillRegistry(state_dir=tmp_path)
    catalog = registry.catalog()
    assert len(catalog) == 6
    assert all("body" not in entry for entry in catalog)
    assert all("sha256" in entry and entry["triggers"] for entry in catalog)
    loaded = registry.load("financial-ratios")
    assert "denominator" in loaded["body"]
    assert loaded["sha256"] == next(entry["sha256"] for entry in catalog if entry["name"] == "financial-ratios")


@pytest.mark.parametrize("name", ["../financial-ratios", "../../outside", "C:/secrets", "financial-ratios/SKILL.md", ""])
def test_skill_paths_are_not_user_controlled(tmp_path: Path, name: str):
    registry = SkillRegistry(state_dir=tmp_path)
    with pytest.raises(SkillError):
        registry.load(name)
    with pytest.raises(SkillError):
        registry.disable(name)


def test_disable_enable_persists_and_changes_fingerprint(tmp_path: Path):
    registry = SkillRegistry(state_dir=tmp_path)
    original = registry.fingerprint()
    registry.disable("financial-ratios")
    restarted = SkillRegistry(state_dir=tmp_path)
    assert restarted.fingerprint() != original
    assert "financial-ratios" not in {entry["name"] for entry in restarted.catalog()}
    with pytest.raises(SkillError, match="disabled"):
        restarted.load("financial-ratios")
    restarted.enable("financial-ratios")
    assert restarted.fingerprint() == original


def test_loader_rejects_executable_or_duplicate_frontmatter():
    with pytest.raises(SkillError):
        parse_skill('---\nname: !!python/object/apply:os.system\ndescription: "unsafe"\ntriggers: ["run"]\nversion: "one"\n---\n' + "body " * 20)
    with pytest.raises(SkillError, match="duplicate"):
        parse_skill('---\nname: "safe"\nname: "other"\n---\n' + "body " * 20)
