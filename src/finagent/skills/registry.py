"""Versioned, progressively disclosed procedural skills.

Only metadata is returned by ``catalog``. Bodies are read when ``load`` is
called. Learning candidates have a separate directory and cannot be discovered
through this registry until LearningService validates and promotes them.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterator
from uuid import uuid4


class SkillError(ValueError):
    """An invalid skill, version, or registry transition."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(value: str, label: str = "skill name") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", value):
        raise SkillError(f"Invalid {label}: use lowercase letters, digits, and hyphens")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def mutation_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 5
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SkillError(f"Skill store is busy; inspect stale lock if no writer is active: {path}")
            time.sleep(0.025)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def render_skill(metadata: dict[str, Any], body: str) -> str:
    # Quoted scalars and JSON arrays are valid YAML; no executable YAML tags.
    fields = ["---"]
    for key in ("name", "description", "triggers", "version"):
        fields.append(f"{key}: {json.dumps(metadata[key], ensure_ascii=False)}")
    return "\n".join(fields) + "\n---\n\n" + body.strip() + "\n"


def parse_skill(text: str) -> dict[str, Any]:
    if len(text) > 24_000:
        raise SkillError("Skill exceeds the maximum size")
    match = re.fullmatch(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, flags=re.DOTALL)
    if not match:
        raise SkillError("SKILL.md requires YAML front matter")
    metadata: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition(":")
        if not separator or key not in {"name", "description", "triggers", "version"} or key in metadata:
            raise SkillError("Unexpected or duplicate skill metadata field")
        try:
            metadata[key] = json.loads(raw.strip())
        except json.JSONDecodeError:
            # A small, explicit YAML scalar subset keeps this loader dependency-free.
            if raw.strip().startswith(("[", "{", "!", "&", "*", "|", ">", "\"", "'")):
                raise SkillError("Use JSON-quoted metadata strings and a JSON array for triggers")
            metadata[key] = raw.strip()
    if set(metadata) != {"name", "description", "triggers", "version"}:
        raise SkillError("Skill metadata needs name, description, triggers, and version")
    identifier(metadata["name"])
    if not isinstance(metadata["description"], str) or not 8 <= len(metadata["description"]) <= 600:
        raise SkillError("Skill description must contain 8–600 characters")
    if not isinstance(metadata["version"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,79}", metadata["version"]):
        raise SkillError("Invalid skill version")
    triggers = metadata["triggers"]
    if not isinstance(triggers, list) or not 1 <= len(triggers) <= 12 or any(not isinstance(x, str) or not 2 <= len(x) <= 80 for x in triggers):
        raise SkillError("Skill triggers must be a short nonempty list")
    body = match.group(2).strip()
    if not 40 <= len(body) <= 16_000:
        raise SkillError("Skill body must contain 40–16000 characters")
    result = {**metadata, "body": body}
    result["sha256"] = digest(result)
    return result


class SkillRegistry:
    def __init__(self, builtin_dir: Path | None = None, *, state_dir: Path):
        self.builtin_dir = Path(builtin_dir or Path(__file__).parent / "builtin").resolve()
        self.root = Path(state_dir).resolve() / "skills"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "registry.json"
        self.lock_path = self.root / ".registry.lock"

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": 1, "active": {}, "disabled": [], "history": {}, "events": []}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("schema_version") != 1:
            raise SkillError("Unsupported skill registry schema")
        return state

    def _builtins(self) -> dict[str, dict[str, Any]]:
        result = {}
        if not self.builtin_dir.exists():
            return result
        for path in sorted(self.builtin_dir.glob("*/SKILL.md")):
            if not path.resolve().is_relative_to(self.builtin_dir):
                raise SkillError("Builtin skill resolves outside its directory")
            skill = parse_skill(path.read_text(encoding="utf-8"))
            if skill["name"] in result:
                raise SkillError("Duplicate builtin skill name")
            result[skill["name"]] = {**skill, "version_id": "builtin", "origin": "builtin"}
        return result

    def _version(self, name: str, version_id: str) -> dict[str, Any]:
        identifier(name)
        identifier(version_id, "version id")
        folder = self.root / "versions" / name / version_id
        if not folder.resolve().is_relative_to(self.root.resolve()):
            raise SkillError("Skill version resolves outside its directory")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        skill = parse_skill((folder / "SKILL.md").read_text(encoding="utf-8"))
        if skill["sha256"] != manifest["sha256"] or skill["name"] != name:
            raise SkillError("Skill version integrity check failed")
        return {**skill, "version_id": version_id, "origin": "learned", "validation_report_id": manifest["validation_report_id"]}

    def catalog(self) -> list[dict[str, Any]]:
        state = self._state()
        entries = self._builtins()
        for name, version in state["active"].items():
            entries[name] = self._version(name, version)
        keys = ("name", "description", "triggers", "version", "version_id", "origin", "sha256")
        return [{key: entries[name][key] for key in keys} for name in sorted(entries) if name not in state["disabled"]]

    def load(self, name: str) -> dict[str, Any]:
        identifier(name)
        state = self._state()
        if name in state["disabled"]:
            raise SkillError(f"Skill is disabled: {name}")
        if name in state["active"]:
            return self._version(name, state["active"][name])
        builtins = self._builtins()
        if name not in builtins:
            raise SkillError(f"Unknown active skill: {name}")
        return builtins[name]

    def fingerprint(self) -> str:
        return digest(self.catalog())

    def disable(self, name: str) -> dict[str, Any]:
        identifier(name)
        with mutation_lock(self.lock_path):
            state = self._state()
            if name not in state["active"] and name not in self._builtins():
                raise SkillError(f"Unknown skill: {name}")
            state["disabled"] = sorted(set(state["disabled"]) | {name})
            state["events"].append({"event": "disable", "name": name, "at": now()})
            atomic_json(self.state_path, state)
        return {"name": name, "disabled": True}

    def enable(self, name: str) -> dict[str, Any]:
        identifier(name)
        with mutation_lock(self.lock_path):
            state = self._state()
            if name not in state["active"] and name not in self._builtins():
                raise SkillError(f"Unknown skill: {name}")
            state["disabled"] = [value for value in state["disabled"] if value != name]
            state["events"].append({"event": "enable", "name": name, "at": now()})
            atomic_json(self.state_path, state)
        return self.load(name)

    def versions(self, name: str) -> list[dict[str, Any]]:
        identifier(name)
        state = self._state()
        return [{"version_id": item, "active": state["active"].get(name) == item} for item in state["history"].get(name, [])]

    def rollback(self, name: str, version_id: str | None = None) -> dict[str, Any]:
        identifier(name)
        with mutation_lock(self.lock_path):
            state = self._state()
            history = state["history"].get(name, [])
            current = state["active"].get(name)
            if version_id is None:
                if current not in history or history.index(current) == 0:
                    raise SkillError("No previous validated version; disable the skill instead")
                version_id = history[history.index(current) - 1]
            identifier(version_id, "version id")
            if version_id not in history:
                raise SkillError("Rollback requires a previously promoted version")
            self._version(name, version_id)
            state["active"][name] = version_id
            state["events"].append({"event": "rollback", "name": name, "version_id": version_id, "at": now()})
            atomic_json(self.state_path, state)
        return {"name": name, "version_id": version_id, "disabled": name in state["disabled"]}

    def _install_validated(self, candidate: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        """Internal promotion boundary; LearningService verifies saved audit hashes."""
        if not report.get("approved") or report.get("candidate_sha256") != candidate.get("sha256"):
            raise SkillError("Promotion requires an approved report for this exact candidate")
        skill = parse_skill(render_skill(candidate, candidate["body"]))
        if skill["sha256"] != candidate["sha256"]:
            raise SkillError("Candidate content changed after validation")
        name = identifier(skill["name"])
        if name in self._builtins():
            raise SkillError("Learned skills cannot shadow builtins")
        version_id = "v-" + skill["sha256"][:20]
        with mutation_lock(self.lock_path):
            if self.fingerprint() != report.get("registry_fingerprint"):
                raise SkillError("Active skills changed; revalidate before promotion")
            state = self._state()
            folder = self.root / "versions" / name / version_id
            if not folder.resolve().is_relative_to(self.root.resolve()):
                raise SkillError("Skill version resolves outside its directory")
            folder.mkdir(parents=True, exist_ok=True)
            skill_path = folder / "SKILL.md"
            rendered = render_skill(skill, skill["body"])
            if skill_path.exists() and skill_path.read_text(encoding="utf-8") != rendered:
                raise SkillError("Existing skill version is immutable")
            skill_path.write_text(rendered, encoding="utf-8", newline="\n")
            atomic_json(folder / "manifest.json", {"sha256": skill["sha256"], "validation_report_id": report["id"], "report_sha256": report["report_sha256"], "promoted_at": now()})
            history = state["history"].setdefault(name, [])
            if version_id not in history:
                history.append(version_id)
            state["active"][name] = version_id
            state["disabled"] = [value for value in state["disabled"] if value != name]
            state["events"].append({"event": "promote", "name": name, "version_id": version_id, "report_id": report["id"], "at": now()})
            atomic_json(self.state_path, state)
        return self.load(name)
