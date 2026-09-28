"""Форма данных слайда — по фактам, а не по тому, как блок назвала модель.

Живые планы питча Fibonacci клали TAM/SAM/SOM, метрики и юнит-экономику в
один блок kpi с заголовком «Объём рынка»: в место крупного числа садился
заголовок, цифры шли мелким текстом. Здесь — правила из приёмки:
2–4 числа → показатели, доли в сумме 100 → кольцо, 5+ однородных → столбцы.
"""

from __future__ import annotations

import json
from pathlib import Path

from deckwright.plan.shaping import figure_text, shape_data
from deckwright.schemas import BlockKind, ContentPack, DeckPlan, SeriesShape

PITCH = Path(__file__).parent / "fixtures" / "fibonacci_pitch"


def _pack() -> ContentPack:
    return ContentPack.model_validate(json.loads((PITCH / "pack.json").read_text("utf-8")))


def _plan(*slides: dict) -> DeckPlan:
    body = [
        {"index": 1, "intent": "title", "takeaway_title": "Fibonacci AI"},
        *({"index": number, **slide} for number, slide in enumerate(slides, start=2)),
    ]
    return DeckPlan.model_validate({"title": "Питч", "purpose": "product", "slides": body})


def test_market_block_becomes_three_kpis_with_heading_in_subtitle():
    """«Объём рынка» и три строки TAM/SAM/SOM — три показателя, заголовок — подзаголовок."""
    plan = _plan(
        {
            "intent": "evidence",
            "takeaway_title": "Рынок EdTech — 160 млрд ₽",
            "blocks": [
                {"id": "b1", "kind": "kpi", "heading": "Объём рынка",
                 "items": ["TAM: мировой рынок", "SAM: Россия", "SOM: достижимо"],
                 "fact_ids": ["f10", "f11", "f12"]},
            ],
        }
    )
    shaped, _ = shape_data(plan, _pack())
    slide = shaped.slides[1]
    assert [block.kind for block in slide.blocks] == [BlockKind.KPI] * 3
    assert [block.heading for block in slide.blocks] == [
        "160 млрд ₽", "630 млн ₽", "30 млн ₽",
    ]
    assert [block.items for block in slide.blocks] == [
        ["TAM: мировой рынок"], ["SAM: Россия"], ["SOM: достижимо"],
    ]
    assert slide.subtitle == "Объём рынка"
    assert {figure.text for figure in slide.figures} >= {b.heading for b in slide.blocks}


def test_parts_of_a_whole_become_a_donut_series():
    """30 % и 70 % — доли целого: блок ряда, ряд пакета с теми же долями."""
    plan = _plan(
        {
            "intent": "ask",
            "takeaway_title": "4 млн ₽ на разработку и маркетинг",
            "blocks": [
                {"id": "b1", "kind": "bullets", "items": ["разработка", "маркетинг"],
                 "fact_ids": ["f18", "f19"]},
            ],
        }
    )
    shaped, pack = shape_data(plan, _pack())
    block = shaped.slides[1].blocks[0]
    assert block.kind is BlockKind.SERIES
    series = next(item for item in pack.series if item.id == block.series_ids[0])
    assert sorted(series.values) == [30.0, 70.0]


def test_parts_without_a_series_get_one_from_facts():
    pack = _pack()
    pack = pack.model_copy(update={"series": []})
    plan = _plan(
        {
            "intent": "ask",
            "takeaway_title": "Средства — на разработку и маркетинг",
            "blocks": [
                {"id": "b1", "kind": "paragraph", "items": ["разработка", "маркетинг"],
                 "fact_ids": ["f18", "f19"]},
            ],
        }
    )
    shaped, pack = shape_data(plan, pack)
    block = shaped.slides[1].blocks[0]
    series = next(item for item in pack.series if item.id == block.series_ids[0])
    assert series.shape is SeriesShape.PARTS
    assert series.categories == ["разработка", "маркетинг"]
    assert series.unit == "%"


def test_five_homogeneous_numbers_become_bars():
    pack = _pack()
    facts = [
        {"id": f"c{n}", "text": f"Город {n}: {v} заказов", "source_doc_id": "d1",
         "value": v, "unit": "заказов"}
        for n, v in enumerate((120, 95, 80, 64, 40), start=1)
    ]
    pack = pack.model_copy(
        update={"facts": [*pack.facts, *(type(pack.facts[0]).model_validate(f) for f in facts)]}
    )
    plan = _plan(
        {
            "intent": "evidence",
            "takeaway_title": "Москва — треть заказов",
            "blocks": [{"id": "b1", "kind": "bullets", "fact_ids": [f["id"] for f in facts],
                        "items": ["Москва", "СПб", "Казань", "Уфа", "Пермь"]}],
        }
    )
    shaped, pack = shape_data(plan, pack)
    block = shaped.slides[1].blocks[0]
    assert block.kind is BlockKind.SERIES
    series = next(item for item in pack.series if item.id == block.series_ids[0])
    assert series.shape is SeriesShape.COMPARE and len(series.points) == 5


def test_text_blocks_and_single_numbers_stay_as_they_are():
    """Факт без числа, одно число, цитата — форма не меняется."""
    plan = _plan(
        {
            "intent": "context",
            "takeaway_title": "Команда из 7 человек",
            "blocks": [{"id": "b1", "kind": "bullets", "items": ["CEO", "CTO"],
                        "fact_ids": ["f4", "f13"]}],
        },
        {
            "intent": "evidence",
            "takeaway_title": "Конверсия 2 %",
            "blocks": [{"id": "b2", "kind": "kpi", "heading": "2 %",
                        "items": ["в платящих"], "fact_ids": ["f2"]}],
        },
    )
    shaped, pack = shape_data(plan, _pack())
    assert shaped.slides[1].blocks[0].kind is BlockKind.BULLETS
    assert shaped.slides[2].blocks[0].heading == "2 %"
    assert pack.series == _pack().series


def test_figure_text_follows_the_deck_language():
    fact = _pack().facts[0]  # 10 000 MAU
    assert figure_text(fact, "ru") == "10 000 MAU"
    percent = _pack().facts[1]
    assert figure_text(percent, "ru") == "2%"


def test_whole_and_its_parts_become_a_kpi_and_a_donut():
    """«4 млн ₽ — 30 % разработка, 70 % маркетинг»: сумма числом, доли кольцом."""
    plan = _plan(
        {
            "intent": "ask",
            "takeaway_title": "Запрашиваем 4 млн ₽",
            "blocks": [{"id": "b1", "kind": "bullets",
                        "items": ["раунд Pre-seed", "разработка", "маркетинг"],
                        "fact_ids": ["f17", "f18", "f19"]}],
        }
    )
    shaped, pack = shape_data(plan, _pack())
    blocks = shaped.slides[1].blocks
    assert [block.kind for block in blocks] == [BlockKind.KPI, BlockKind.SERIES]
    assert blocks[0].heading == "4 млн ₽" and blocks[0].items == ["раунд Pre-seed"]
    series = next(item for item in pack.series if item.id == blocks[1].series_ids[0])
    assert sorted(series.values) == [30.0, 70.0]


def test_long_unit_stays_in_the_caption():
    """«100 000 пользователей» — число крупно, «пользователей» — в подписи."""
    fact = next(fact for fact in _pack().facts if fact.id == "f7")
    assert figure_text(fact, "ru") == "100 000"
