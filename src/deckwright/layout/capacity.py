"""Сколько текста шаблон реально вмещает — для бюджета планировщика.

Планировщик работает до вёрстки и не знает, в какой макет ляжет слайд.
Общий бюджет шаблона («до 6 пунктов по 34 символа») для этого не годится:
на живом плане #12 модель ему подчинилась, а переполнено было 37 слайдов из
90 — рамки конкретных макетов вмещали от одной до четырёх строк.

Здесь бюджет считается тем же предсказателем, по которому вёрстка потом
выбирает макет (`matcher._blocks_fit`): для каждого вида текстового блока —
сколько пунктов заданной длины помещается **читаемым кеглем** хотя бы в один
макет шаблона. Считается для каждого варианта, и берётся минимум: план один
на три варианта, и влезать он обязан во все три.

Расчёт детерминирован и не зовёт модель; планировщику уходят только числа.
"""

from __future__ import annotations

from dataclasses import dataclass

from deckwright.layout.matcher import _blocks_fit, _title_fits, _usable_slots
from deckwright.layout.strategy import Strategy, ladder_for_role, role_typical
from deckwright.layout.text_metrics import metrics_for_spec
from deckwright.schemas import (
    BlockKind,
    ContentBlock,
    SlideIntent,
    SlidePlan,
    SlotRole,
    TemplateSpec,
)

# Виды блоков, длину которых пишет планировщик. Ряд, таблица и картинка
# мерятся площадью, цитата приходит из контент-пакета готовым текстом.
TEXT_KINDS = (BlockKind.BULLETS, BlockKind.STEPS, BlockKind.PARAGRAPH)

# Длины пункта, которые пробуются от бюджета шаблона вниз: если пять пунктов
# полной длины не влезают никуда, три коротких могут влезть.
_LENGTH_STEPS = (1.0, 0.75, 0.5)
# Абзац пробуется от «весь бюджет тела» до одной строки пункта.
_PARAGRAPH_STEPS = (1.0, 0.75, 0.5, 0.33, 0.25, 1 / 6)

# Модельное слово: средняя длина русского слова около семи букв.
_WORD = "абвгдеж"


@dataclass(frozen=True)
class BlockCapacity:
    """Сколько пунктов какой длины помещается в блок этого вида."""

    items: int
    chars: int


def _text(length: int) -> str:
    """Строка из модельных слов заданной длины: перенос считается по словам."""
    words = []
    while len(" ".join(words)) < length:
        words.append(_WORD)
    return " ".join(words)[:length].rstrip()


def _probe(kind: BlockKind, items: int, chars: int, title: str) -> SlidePlan:
    return SlidePlan(
        index=1,
        intent=SlideIntent.SOLUTION,
        takeaway_title=title,
        blocks=[
            ContentBlock(id="probe", kind=kind, items=[_text(chars)] * items),
        ],
    )


def achievable(
    spec: TemplateSpec,
    strategies: list[Strategy],
    item_chars: int,
    title_chars: int,
    max_items: int,
) -> dict[BlockKind, BlockCapacity]:
    """Ёмкость по видам текстовых блоков, общая для всех вариантов.

    `item_chars`, `title_chars` и `max_items` — бюджет шаблона и порог ТЗ:
    ёмкость не бывает больше них, только меньше.
    """
    metrics = metrics_for_spec(spec).metrics
    if metrics is None or not strategies:
        return {}
    ladders = {role: ladder_for_role(spec, role) for role in SlotRole}
    typical = {role: role_typical(spec, role) for role in SlotRole}
    title = _text(max(1, round(title_chars * 0.7)))
    usable = [
        pattern
        for pattern in spec.patterns
        if _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
    ]

    def fits(kind, items, chars, strategy, strict) -> bool:
        slide = _probe(kind, items, chars, title)
        return any(
            _title_fits(pattern, slide, strategy, metrics, ladders[SlotRole.TITLE])
            and _blocks_fit(pattern, spec, slide, strategy, metrics, ladders, strict)
            for pattern in usable
        )

    def measure(kind: BlockKind, strict) -> BlockCapacity:
        single = kind is BlockKind.PARAGRAPH
        ceiling = 1 if single else max_items
        steps = _PARAGRAPH_STEPS if single else _LENGTH_STEPS
        best = BlockCapacity(items=0, chars=0)
        for step in steps:
            chars = max(10, round(item_chars * step * (max_items if single else 1)))
            items = 0
            for count in range(1, ceiling + 1):
                if all(fits(kind, count, chars, st, strict) for st in strategies):
                    items = count
                else:
                    break
            if items * chars > best.items * best.chars:
                best = BlockCapacity(items=items, chars=chars)
            if items >= ceiling:
                break
        return best

    result: dict[BlockKind, BlockCapacity] = {}
    for kind in TEXT_KINDS:
        # Сначала — сколько влезает читаемым кеглем; если так не влезает
        # ничего, — сколько влезает хоть как-то. Ноль остаётся нулём: тогда
        # планировщик получает общий бюджет шаблона, как раньше.
        found = measure(kind, typical)
        result[kind] = found if found.items else measure(kind, None)
    return result
