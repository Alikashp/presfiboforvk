"""Построение `DeckPlan`. Скелетная версия слоя plan.

План описывает, что сказать и в каком порядке, и не знает про шаблон ничего:
ни координат, ни кеглей, ни имён layout'ов. Поэтому один и тот же план
раскладывается тремя вариантами вёрстки и ложится на любой шаблон.

Промпт живёт отдельным версионируемым файлом в `prompts/` — в коде его нет.

Фаза 5 добавит сюда разбор контент-пакета из файлов и привязку каждого числа к
источнику. Сейчас `ContentPack` приходит готовым.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from deckwright.llm.base import StructuredClient
from deckwright.schemas import ContentPack, DeckPlan, PromptVersion

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


class Prompt(BaseModel):
    """Промпт, загруженный из файла, вместе со своей версией."""

    name: str
    version: str
    step: str
    template: str
    description: str = ""
    sha256: str = Field(default="")

    def render(self, **values: object) -> str:
        return self.template.format(**values)

    def as_manifest_entry(self) -> PromptVersion:
        return PromptVersion(name=self.name, version=self.version, sha256=self.sha256)


def load_prompt(name: str, prompts_dir: str | Path | None = None) -> Prompt:
    """Читает промпт из `prompts/<name>.yaml` и считает его хэш.

    Хэш идёт в манифест: он гарантирует, что прогон воспроизводится именно тем
    текстом промпта, а не его более поздней правкой.
    """
    directory = Path(prompts_dir) if prompts_dir else PROMPTS_DIR
    path = directory / f"{name}.yaml"
    raw = path.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8"))
    return Prompt(**data, sha256=hashlib.sha256(raw).hexdigest())


def _format_facts(pack: ContentPack) -> str:
    if not pack.facts:
        return "(фактов не предоставлено)"
    return "\n".join(f"- [{fact.id}] {fact.text}" for fact in pack.facts)


def _format_series(pack: ContentPack) -> str:
    if not pack.series:
        return "(числовых рядов не предоставлено)"
    lines = []
    for series in pack.series:
        points = ", ".join(f"{p.label}={p.value}" for p in series.points)
        unit = f" ({series.unit})" if series.unit else ""
        lines.append(f"- [{series.id}] {series.name}{unit}: {points}")
    return "\n".join(lines)


def build_plan(
    pack: ContentPack,
    client: StructuredClient,
    slide_count: int,
    prompts_dir: str | Path | None = None,
) -> tuple[DeckPlan, Prompt]:
    """Возвращает план и использованный промпт — последний нужен манифесту."""
    prompt = load_prompt("plan_deck.v1", prompts_dir)
    brief = pack.brief
    text = prompt.render(
        topic=brief.topic,
        purpose=brief.purpose.value,
        audience=brief.audience or "не указана",
        goal=brief.goal or "не указана",
        language=brief.language,
        extra_instructions=brief.extra_instructions or "нет",
        facts=_format_facts(pack),
        series=_format_series(pack),
        slide_count=slide_count,
    )
    plan = client.complete(step=prompt.step, prompt=text, schema=DeckPlan)
    return plan, prompt
