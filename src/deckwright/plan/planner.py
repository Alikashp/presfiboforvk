"""Построение `DeckPlan`. Скелетная версия слоя plan.

План описывает, что сказать и в каком порядке, и не знает про шаблон ничего:
ни координат, ни кеглей, ни имён layout'ов. Поэтому один и тот же план
раскладывается тремя вариантами вёрстки и ложится на любой шаблон.

Промпт живёт отдельным версионируемым файлом в `prompts/` — в коде его нет.

Контент-пакет приходит готовым: разбор входа — слой `content`.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from deckwright.config import agent_prompt
from deckwright.llm.base import StructuredClient
from deckwright.plan.budget import LengthBudget, compute_budget
from deckwright.schemas import (
    BlockKind,
    ContentBlock,
    ContentPack,
    DeckPlan,
    PromptVersion,
    SlideIntent,
    TemplateSpec,
)

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
    """Факты с их числовыми значениями.

    Значение показывается отдельно, чтобы модель могла сослаться на него в
    формуле выведенного числа: без этого она не знает, что `f1` — это 42.
    """
    if not pack.facts:
        return "(фактов не предоставлено)"
    lines = []
    for fact in pack.facts:
        numeric = ""
        if fact.value is not None:
            unit = f" {fact.unit}" if fact.unit else ""
            numeric = f"  (значение: {fact.value}{unit})"
        lines.append(f"- [{fact.id}] {fact.text}{numeric}")
    return "\n".join(lines)


def _format_series(pack: ContentPack) -> str:
    if not pack.series:
        return "(числовых рядов не предоставлено)"
    lines = []
    for series in pack.series:
        points = ", ".join(f"{p.label}={p.value}" for p in series.points)
        unit = f" ({series.unit})" if series.unit else ""
        lines.append(f"- [{series.id}] {series.name}{unit}: {points}")
    return "\n".join(lines)


def _format_quotes(pack: ContentPack) -> str:
    if not pack.quotes:
        return "(цитат не предоставлено)"
    lines = []
    for quote in pack.quotes:
        who = ", ".join(part for part in (quote.author, quote.role) if part)
        lines.append(f"- [{quote.id}] «{quote.text}»" + (f" — {who}" if who else ""))
    return "\n".join(lines)


def slide_count_text(slide_count: int | None, min_slides: int, max_slides: int) -> str:
    """Сколько слайдов просить: заданное число или диапазон ТЗ на выбор модели.

    Число, заданное пользователем, — закон. Не задано — модель выбирает сама
    в границах ТЗ (10–15) по объёму материала: брифу в две фразы хватит
    нижней границы, отчёту на десять страниц нужна верхняя.
    """
    if slide_count:
        return str(slide_count)
    return (
        f"от {min_slides} до {max_slides} (число выбери сам: чем меньше материала, "
        f"тем ближе к {min_slides}; первый и последний слайды входят в счёт)"
    )


def build_plan(
    pack: ContentPack,
    client: StructuredClient,
    slide_count: int | str,
    spec: TemplateSpec | None = None,
    max_bullets: int = 6,
    max_words_per_bullet: int = 15,
    substitution_slack: float = 0.8,
    prompts_dir: str | Path | None = None,
    block_limits: tuple[tuple[str, int, int], ...] = (),
    min_slides: int = 0,
) -> tuple[DeckPlan, Prompt, LengthBudget | None]:
    """План, использованный промпт и бюджеты длины.

    Бюджеты считаются по шаблону, если он передан: у каждого шаблона своя
    заголовочная рамка и свой кегль, и «слишком длинно» у них разное. Без
    шаблона модель работает по одним порогам плотности из ТЗ — план тогда
    может не влезть, и разбираться с этим придётся фиттеру.
    """
    prompt = load_prompt(agent_prompt("plan_deck"), prompts_dir)
    budget = (
        compute_budget(
            spec, max_bullets, max_words_per_bullet, substitution_slack=substitution_slack
        )
        if spec is not None
        else None
    )
    if budget is not None and block_limits:
        budget = replace(budget, block_limits=block_limits)
    limits = (
        budget.as_prompt_lines()
        if budget is not None
        else (
            f"- пунктов на слайде: не больше {max_bullets}\n"
            f"- пункт списка: не длиннее {max_words_per_bullet} слов"
        )
    )

    brief = pack.brief
    text = prompt.render(
        topic=brief.topic,
        purpose=brief.purpose.value,
        audience=brief.audience or "не указана",
        goal=brief.goal or "не указана",
        language=brief.language,
        extra_instructions=brief.extra_instructions or "нет",
        request=brief.request or "(не передан: материалы — документы)",
        quotes=_format_quotes(pack),
        facts=_format_facts(pack),
        series=_format_series(pack),
        slide_count=slide_count,
        length_limits=limits,
    )
    plan = client.complete(step=prompt.step, prompt=text, schema=_bounded(min_slides))
    plan = DeckPlan.model_validate(plan.model_dump())
    plan = drop_unfounded(resolve_facts(resolve_quotes(plan, pack), pack), pack)
    return fill_or_drop(plan, pack), prompt, budget


def _bounded(min_slides: int) -> type[DeckPlan]:
    """Схема ответа, где слишком короткий план — невалидный ответ.

    Живой прогон `sales × vk_tech` (run 29): модель на просьбу «от 10 до 15»
    вернула обложку и повестку — два слайда, 168 токенов, — и колода вышла из
    двух слайдов. Такой ответ уходит на повтор, как любой невалидный. Порог —
    сама нижняя граница (A6: 10–15 слайдов): порог в половину её пропустил
    план из пяти слайдов на `observability × vk_tech` (run 33).
    """
    floor = min_slides or 0
    if not floor:
        return DeckPlan

    class BoundedDeckPlan(DeckPlan):
        @model_validator(mode="after")
        def _long_enough(self) -> BoundedDeckPlan:
            if len(self.slides) < floor:
                raise ValueError(
                    f"в плане {len(self.slides)} слайдов, а нужно не меньше {floor}: "
                    "план оборван — верни его целиком"
                )
            return self

    return BoundedDeckPlan


def resolve_quotes(plan: DeckPlan, pack: ContentPack) -> DeckPlan:
    """Блок цитаты получает её текст и автора из контент-пакета.

    Модель ссылается на цитату идентификатором и текст не переписывает — так
    цитата остаётся дословной. Но вёрстка кладёт на слайд строки блока, а у
    блока с одним `quote_id` их нет: слайд с цитатой выходил пустым (живой
    прогон на docx-отчёте, все три варианта).
    """
    quotes = {quote.id: quote for quote in pack.quotes}
    plan = plan.model_copy(deep=True)
    for slide in plan.slides:
        for block in slide.blocks:
            quote = quotes.get(block.quote_id or "")
            if quote is None or block.items:
                continue
            who = ", ".join(part for part in (quote.author, quote.role) if part)
            block.items = [f"«{quote.text}»"] + ([f"— {who}"] if who else [])
    return plan


# Блоки, чей текст — строки пунктов. У графика, таблицы, KPI и картинки
# содержимое другое, и текст фактов в них подставлять нельзя.
_TEXT_BLOCKS = (BlockKind.PARAGRAPH, BlockKind.BULLETS, BlockKind.STEPS)


def resolve_facts(plan: DeckPlan, pack: ContentPack) -> DeckPlan:
    """Блок, где у модели есть только ссылки на факты, получает текст фактов.

    Живой прогон pdf × vk_tech: у слайда-решения один абзац с `fact_ids` и
    без строк — слайд выходил пустым во всех трёх вариантах. Текст факта
    взят из входа и уже сверен, так что подставлять его безопасно.

    Показатель с одними `fact_ids` (питч Fibonacci: «LTV в 4 раза выше CAC»,
    «Конверсия 2 %» — сайт собрал их из одних заголовков) получает число
    факта и подпись, а если фактов несколько — становится списком их текстов.
    Ряд, которого нет в пакете (модель сослалась на факты как на ряды или
    на выброшенный сверкой ряд), — тоже список фактов; сослаться не на что —
    блок уходит, а не печатает на слайде идентификатор «s_market».
    """
    facts = {fact.id: fact for fact in pack.facts}
    known_series = {item.id for item in pack.series}
    plan = plan.model_copy(deep=True)
    for slide in plan.slides:
        kept = []
        for block in slide.blocks:
            if block.kind is BlockKind.SERIES and not any(
                sid in known_series for sid in block.series_ids
            ):
                ids = [*block.fact_ids, *(sid for sid in block.series_ids if sid in facts)]
                block = block.model_copy(
                    update={"kind": BlockKind.BULLETS, "series_ids": [], "fact_ids": ids}
                )
            cited = [facts[fact_id] for fact_id in block.fact_ids if fact_id in facts]
            if block.kind in _TEXT_BLOCKS and not block.items:
                block.items = [fact.text for fact in cited]
            elif block.kind is BlockKind.KPI and not block.items and not block.heading:
                if len(cited) == 1 and cited[0].value is not None:
                    fact = cited[0]
                    block.heading = f"{fact.value:g} {fact.unit}".strip()
                    block.items = [fact.text]
                else:
                    block = block.model_copy(
                        update={"kind": BlockKind.BULLETS, "items": [f.text for f in cited]}
                    )
            if block.items or block.heading or block.series_ids or block.table or (
                block.kind is BlockKind.IMAGE
            ):
                kept.append(block)
        slide.blocks = kept
    return plan


# Слайды, которые законно состоят из одного заголовка.
_BARE_INTENTS = frozenset({SlideIntent.TITLE, SlideIntent.SECTION, SlideIntent.CLOSING})


def fill_or_drop(plan: DeckPlan, pack: ContentPack) -> DeckPlan:
    """Слайд содержания без блоков не уходит в колоду: заполнить или убрать.

    После `resolve_facts` у слайда могут не остаться блоки — модель
    сослалась на ряд, которого нет, и ни на один факт. Такой слайд — один
    заголовок. Числа, вынесенные на слайд (`figures`), ссылаются на факты:
    их тексты и становятся списком. Сослаться не на что — слайд уходит, а
    номера остальных сдвигаются.
    """
    facts = {fact.id: fact for fact in pack.facts}
    plan = plan.model_copy(deep=True)
    kept = []
    for slide in plan.slides:
        if not slide.blocks and slide.intent not in _BARE_INTENTS:
            cited = list(
                dict.fromkeys(
                    fact_id
                    for figure in slide.figures
                    for fact_id in figure.fact_ids
                    if fact_id in facts
                )
            )
            if not cited:
                continue
            slide.blocks = [
                ContentBlock(
                    id=f"s{slide.index}_filled",
                    kind=BlockKind.BULLETS,
                    items=[facts[fact_id].text for fact_id in cited],
                    fact_ids=cited,
                )
            ]
        kept.append(slide)
    # Два финала подряд — «Инвестируйте…» с контактами и пустое «Спасибо за
    # внимание!» (питч Fibonacci, run 38): пустой уходит. Колода и так бывает
    # на пределе ТЗ, а деление слайдов добавляет своё.
    closings = [slide for slide in kept if slide.intent is SlideIntent.CLOSING]
    if len(closings) > 1:
        bare = [slide for slide in closings if not slide.blocks]
        drop = bare[: len(closings) - 1] if len(bare) < len(closings) else bare[:-1]
        kept = [slide for slide in kept if all(slide is not other for other in drop)]
    for number, slide in enumerate(kept, start=1):
        slide.index = number
    plan.slides = kept
    return plan


# Целые до этого числа в тексте — счёт («три этапа»), а не факт; как в
# сверке входа (`content.ingest.SMALL_COUNT`).
_SMALL_COUNT = 10


def drop_unfounded(plan: DeckPlan, pack: ContentPack) -> DeckPlan:
    """Пункт с числом или адресом, которых во входе нет, в колоду не идёт.

    Живой план питча Fibonacci (run 38, прогон 1): на финале — «email@
    example.com», «+7 (999) 000-00-00», «Сайт: fibonacci-ai.com». Контакты
    в PDF есть, но в пакет не попали, и модель подставила заглушки. Аудит
    находил число (`content.undeclared_number`), а слайд уходил с ним. Здесь
    — до вёрстки: сверка с пакетом, по тем же правилам, что у аудита.
    """
    from deckwright.content.grounding import normalize, source_numbers, ungrounded

    sources = [pack.brief.topic, pack.brief.request, pack.brief.goal, pack.brief.audience]
    for fact in pack.facts:
        sources.append(fact.text)
        if fact.value is not None:
            sources.append(f"{fact.value:g}")
    for series in pack.series:
        sources.append(series.name)
        sources += [f"{point.label} {point.value:g}" for point in series.points]
    sources += [quote.text for quote in pack.quotes]
    corpus = normalize("\n".join(sources))
    grounded = source_numbers("\n".join(sources))
    # Телефон — не величина: сверка чисел прощает 999 при 1000 во входе.
    # Номер сверяется целиком, последовательностью цифр.
    digit_runs = ["".join(char for char in text if char.isdigit()) for text in sources if text]
    facts = {fact.id for fact in pack.facts}

    plan = plan.model_copy(deep=True)
    for slide in plan.slides:
        allowed = set(grounded)
        for figure in slide.figures:
            if any(fact_id in facts for fact_id in figure.fact_ids):
                allowed |= source_numbers(figure.text)
        for block in slide.blocks:
            if block.kind not in _TEXT_BLOCKS:
                continue
            block.items = [
                item
                for item in block.items
                if not ungrounded(item, allowed, _SMALL_COUNT)
                and not _unknown_number(item, digit_runs)
                and not any(
                    _address(token) and normalize(token) not in corpus for token in item.split()
                )
            ]
        slide.blocks = [
            block for block in slide.blocks if block.items or block.kind not in _TEXT_BLOCKS
        ]
    return plan


# Столько цифр в пункте — это номер (телефон, счёт), а не величина.
_PHONE_DIGITS = 10


def _unknown_number(item: str, digit_runs: list[str]) -> bool:
    """Номер из 10+ цифр, которого нет ни в одном источнике целиком."""
    digits = "".join(char for char in item if char.isdigit())
    return len(digits) >= _PHONE_DIGITS and not any(digits in run for run in digit_runs)


def _address(token: str) -> bool:
    """Почта, ник, ссылка или домен: «@», «://» или латинское «имя.зона»."""
    token = token.strip(".,;:()«»\"'")
    if "@" in token or "://" in token:
        return True
    name, dot, zone = token.rpartition(".")
    return bool(dot) and zone.isascii() and zone.isalpha() and len(zone) >= 2 and any(
        char.isalpha() and char.isascii() for char in name
    )
