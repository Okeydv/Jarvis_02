"""Навыки — то, чему Джарвис научился: инструкции и, при необходимости, скрипт на Python.

Формат — открытый стандарт Agent Skills (agentskills.io): папка навыка с файлом SKILL.md
(YAML-заголовок name/description и инструкции) и скриптом script.py. Готовые навыки в этом
формате можно просто положить в папку «Документы\\Jarvis\\Skills».
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .text_utils import normalize, similarity, translit_ru_en

SCRIPT_NAME = "script.py"
APPROVED_NAME = ".approved"  # хеш скрипта, который пользователь видел и одобрил


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    folder: Path
    title: str = ""

    @property
    def script(self) -> Path:
        return self.folder / SCRIPT_NAME

    @property
    def has_script(self) -> bool:
        return self.script.is_file()


def slugify(text: str) -> str:
    """«Сжать картинки» → «szhat-kartinki» (имя по правилам agentskills.io: латиница, цифры, дефисы)."""
    slug = re.sub(r"[^a-z0-9]+", "-", translit_ru_en(normalize(text)).lower()).strip("-")
    return slug[:64] or "skill"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_skill_md(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    meta, body = {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = match.group(2)
    if not isinstance(meta, dict):
        meta = {}
    name = str(meta.get("name") or path.parent.name)
    title_match = re.search(r"^#\s+(.+)$", body, re.M)
    return Skill(name=name, description=str(meta.get("description") or "").strip(), instructions=body.strip(),
                 folder=path.parent, title=title_match.group(1).strip() if title_match else "")


class Skills:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def all(self) -> list[Skill]:
        if not self.folder.is_dir():
            return []
        skills = []
        for skill_md in sorted(self.folder.glob("*/SKILL.md")):
            skill = parse_skill_md(skill_md)
            if skill:
                skills.append(skill)
        return skills

    def get(self, name: str) -> Skill | None:
        key = normalize(name)
        slug = slugify(name)
        best, best_score = None, 0.0
        for skill in self.all():
            for variant in (skill.name, skill.title, skill.folder.name):
                if not variant:
                    continue
                score = 1.0 if variant == slug or normalize(variant) == key else similarity(key, normalize(variant))
                if score > best_score:
                    best, best_score = skill, score
        return best if best_score >= 0.8 else None

    def save(self, name: str, description: str, instructions: str, code: str = "") -> Skill:
        title = " ".join(name.split()).strip()
        slug = slugify(title)
        folder = self.folder / slug
        folder.mkdir(parents=True, exist_ok=True)
        header = yaml.safe_dump({"name": slug, "description": " ".join(description.split())}, allow_unicode=True,
                                sort_keys=False).strip()
        body = instructions.strip() or description.strip()
        (folder / "SKILL.md").write_text(f"---\n{header}\n---\n\n# {title}\n\n{body}\n", encoding="utf-8")
        script, approved = folder / SCRIPT_NAME, folder / APPROVED_NAME
        if code.strip():
            script.write_text(code, encoding="utf-8")
            approved.write_text(_hash(script), encoding="utf-8")  # код пользователь видел при сохранении
        else:
            script.unlink(missing_ok=True)
            approved.unlink(missing_ok=True)
        return parse_skill_md(folder / "SKILL.md")  # type: ignore[return-value]

    @staticmethod
    def approved(skill: Skill) -> bool:
        """Скрипт тот же, что пользователь одобрил (не изменён и не взят со стороны)?"""
        mark = skill.folder / APPROVED_NAME
        try:
            return skill.has_script and mark.read_text(encoding="utf-8").strip() == _hash(skill.script)
        except OSError:
            return False

    @staticmethod
    def approve(skill: Skill) -> None:
        (skill.folder / APPROVED_NAME).write_text(_hash(skill.script), encoding="utf-8")

    def delete(self, name: str) -> str | None:
        skill = self.get(name)
        if skill is None:
            return None
        shutil.rmtree(skill.folder, ignore_errors=True)
        return skill.title or skill.name

    def prompt_block(self) -> str:
        skills = self.all()
        if not skills:
            return ""
        lines = [f"- {s.title or s.name} ({s.name}): {s.description}" for s in skills[:30]]
        return "Твои навыки (применяй инструментом skill с action=use, когда задача подходит):\n" + "\n".join(lines)
