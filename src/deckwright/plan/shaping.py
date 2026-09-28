"""Форма данных слайда — по самим фактам, а не по тому, как их назвала модель.

Живые планы питча Fibonacci на сайте (сентябрь 2026): TAM/SAM/SOM, метрики
продукта, юнит-экономика и «30 % разработка / 70 % маркетинг» пришли блоком
kpi с заголовком «Объём рынка» и тремя строками цифр или списком. Число
показателя в плане — `heading`, и в место крупного числа садился заголовок
блока, а цифры шли мелким текстом ниже. Ни одного KPI и ни одного графика
на колоду, где цифр больше, чем слов.

Модель каждый раз строит другой план, поэтому решение принимается кодом по
фактам, на которые блок ссылается:

* доли одного целого — проценты с суммой 100 — кольцом (ряд `parts`);
* 2–4 числа — показателями: число крупно, подпись отдельно;
* 5 и больше однородных чисел одной единицы — столбцами (ряд `compare`).

Числа берутся из фактов пакета, уже сверенных с входом; ряд, собранный из
фактов, добавляется в пакет с их документом.
"""

from __future__ import annotations

from deckwright.schemas import (
    BlockKind,
    ContentBlock,
    ContentPack,
    DeckPlan,
    Fact,
    Figure,
    NumericPoint,
    Series,
    SeriesShape,
    SlideIntent,
)
from deckwright.visuals.charts import format_value

# Блоки, чьё содержание решает модель словами: их форму можно выбрать по
# фактам. У графика, таблицы, цитаты и картинки форма уже задана.
_SHAPEABLE = frozenset({BlockKind.PARAGRAPH, BlockKind.BULLETS, BlockKind.KPI, BlockKind.STEPS})
_BARE = frozenset({SlideIntent.TITLE, SlideIntent.SECTION, SlideIntent.CLOSING})
# Показателей в ряду: одно число — это не ряд, пять — уже не показатели.
KPI_MIN, KPI_MAX = 2, 4
# С какого числа однородных величин они — столбцы, а не показатели.
BARS_MIN = 5
# Допуск суммы долей: 33 + 33 + 33 — тоже целое.
_WHOLE_TOLERANCE = 1.5
# Единица числа показателя — не длиннее: «млрд ₽», «MAU», «%».
_UNIT_MAX = 6


def shape_data(plan: DeckPlan, pack: ContentPack) -> tuple[DeckPlan, ContentPack]:
    """План, где числовые блоки получили свою форму, и пакет с новыми рядами."""
    facts = {fact.id: fact for fact in pack.facts}
    language = pack.brief.language
    plan = plan.model_copy(deep=True)
    series = list(pack.series)
    for slide in plan.slides:
        if slide.intent in _BARE:
            continue
        shapeable = [block for block in slide.blocks if block.kind in _SHAPEABLE]
        blocks: list[ContentBlock] = []
        for block in slide.blocks:
            if block.kind not in _SHAPEABLE:
                blocks.append(block)
                continue
            cited = _cited(block, slide, facts, alone=len(shapeable) == 1)
            captions = _captions(block, cited)
            shaped = None
            parts = [n for n, fact in enumerate(cited) if fact.unit.strip() == "%"]
            if _is_whole(cited):
                ref = _series_for(cited, captions, SeriesShape.PARTS, block, series, slide.index)
                shaped = [_series_block(block, ref)]
            elif (
                len(parts) >= 2
                and len(parts) < len(cited) <= KPI_MAX + len(parts)
                and _is_whole([cited[n] for n in parts])
            ):
                # «4 млн ₽ — 30 % разработка, 70 % маркетинг»: целое — числом,
                # доли — кольцом.
                rest = [n for n in range(len(cited)) if n not in parts]
                ref = _series_for(
                    [cited[n] for n in parts], [captions[n] for n in parts],
                    SeriesShape.PARTS, block, series, slide.index,
                )
                shaped = [
                    *_kpis(block, [cited[n] for n in rest], [captions[n] for n in rest], language),
                    _series_block(block, ref),
                ]
            elif KPI_MIN <= len(cited) <= KPI_MAX:
                shaped = _kpis(block, cited, captions, language)
            elif len(cited) >= BARS_MIN and len({fact.unit for fact in cited}) == 1:
                ref = _series_for(cited, captions, SeriesShape.COMPARE, block, series, slide.index)
                shaped = [_series_block(block, ref)]
            if shaped is None:
                blocks.append(block)
                continue
            # Заголовок блока («Объём рынка») — в подзаголовок, а не в место числа.
            heading = block.heading.strip()
            if heading and not any(char.isdigit() for char in heading) and not slide.subtitle:
                slide.subtitle = heading
            blocks.extend(shaped)
            _declare(slide, shaped)
        slide.blocks = blocks
    return plan, pack.model_copy(update={"series": series})


def figure_text(fact: Fact, language: str) -> str:
    """Число показателя: значение факта в записи колоды и его единица.

    Единица длиннее `_UNIT_MAX` символов («пользователей») остаётся в
    подписи: «100 000 пользователей» крупным кеглем не входило ни в одно
    место числа `vk_tech`, и слайд уходил в композицию с мелкими бирками.
    """
    number = format_value(fact.value, [fact.value], language)
    unit = fact.unit.strip()
    if not unit or len(unit) > _UNIT_MAX:
        return number
    glue = "" if unit[0] in "%/" else " "
    return f"{number}{glue}{unit}"


def _kpis(
    block: ContentBlock, cited: list[Fact], captions: list[str], language: str
) -> list[ContentBlock]:
    """Показатель на число: число крупно, подпись отдельно, ссылка на факт."""
    return [
        ContentBlock(
            id=f"{block.id}_k{number}",
            kind=BlockKind.KPI,
            heading=figure_text(fact, language),
            items=[caption],
            fact_ids=[fact.id],
        )
        for number, (fact, caption) in enumerate(zip(cited, captions, strict=True))
    ]


def _cited(block: ContentBlock, slide, facts: dict[str, Fact], alone: bool) -> list[Fact]:
    """Числовые факты блока — по его ссылкам, а у единственного блока — и по числам слайда.

    Если хоть один факт без числа, блок — не данные, а текст с цифрой.
    """
    ids = list(block.fact_ids)
    if not ids and alone:
        ids = [fact_id for figure in slide.figures for fact_id in figure.fact_ids]
    cited = [facts[fact_id] for fact_id in dict.fromkeys(ids) if fact_id in facts]
    if not cited or any(fact.value is None for fact in cited):
        return []
    return cited


def _captions(block: ContentBlock, cited: list[Fact]) -> list[str]:
    """Подписи: строки блока, если их столько же, сколько чисел, иначе тексты фактов."""
    if len(block.items) == len(cited):
        return [item.strip() for item in block.items]
    return [fact.text for fact in cited]


def _is_whole(cited: list[Fact]) -> bool:
    """Проценты одного целого: два и больше, в сумме сто."""
    return (
        len(cited) >= 2
        and all(fact.unit.strip() == "%" for fact in cited)
        and abs(sum(fact.value for fact in cited) - 100) <= _WHOLE_TOLERANCE
    )


def _series_for(
    cited: list[Fact],
    captions: list[str],
    shape: SeriesShape,
    block: ContentBlock,
    series: list[Series],
    slide_index: int,
) -> str:
    """Ряд пакета с теми же значениями — или новый, собранный из фактов."""
    values = sorted(fact.value for fact in cited)
    for item in series:
        if sorted(item.values) == values:
            return item.id
    new = Series(
        id=f"sf{slide_index}_{block.id}",
        name=block.heading.strip() or captions[0],
        unit=cited[0].unit.strip(),
        points=[
            NumericPoint(label=caption, value=fact.value)
            for fact, caption in zip(cited, captions, strict=True)
        ],
        source_doc_id=cited[0].source_doc_id,
        locator=cited[0].locator,
        shape=shape,
    )
    series.append(new)
    return new.id


def _series_block(block: ContentBlock, series_id: str) -> ContentBlock:
    return ContentBlock(
        id=block.id, kind=BlockKind.SERIES, series_ids=[series_id], fact_ids=block.fact_ids
    )


def _declare(slide, shaped: list[ContentBlock]) -> None:
    """Числа показателей объявлены в `figures`: аудит сверяет каждое число слайда."""
    declared = {figure.text for figure in slide.figures}
    for block in shaped:
        if block.kind is not BlockKind.KPI or block.heading in declared:
            continue
        slide.figures.append(Figure(text=block.heading, fact_ids=list(block.fact_ids)))
        declared.add(block.heading)
