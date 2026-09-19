"""Минимальные валидные объекты контрактов для тестов.

Значения не взяты из конкретного шаблона: это синтетические 16:9 на 12192000 ×
6858000 EMU. Привязка фикстур к реальному шаблону датасета была бы тем самым
оверфитом, который запрещён.
"""

from __future__ import annotations

from datetime import UTC, datetime

from deckwright.schemas import (
    BlockKind,
    Box,
    Brief,
    Color,
    ColorToken,
    ContentBlock,
    ContentPack,
    DeckIR,
    DeckPlan,
    DeckPurpose,
    Element,
    ElementKind,
    Fact,
    FontToken,
    Grid,
    LayoutSpec,
    NumericPoint,
    Paragraph,
    Pattern,
    PatternClass,
    Provenance,
    RunManifest,
    Series,
    SlideIntent,
    SlideIR,
    SlidePlan,
    Slot,
    SlotRole,
    SourceDoc,
    SourceKind,
    TemplateSpec,
    TextContent,
    TextStyle,
)

SHA = "0" * 64
SLIDE_W = 12_192_000
SLIDE_H = 6_858_000

ACCENT = Color(rgb="0077FF", scheme="accent1")
ON_LIGHT = Color(rgb="111111")

FROM_SLIDE = Provenance(kind=SourceKind.SLIDE, ref="slide3")
FROM_THEME = Provenance(kind=SourceKind.THEME, ref="theme1")
DERIVED = Provenance(kind=SourceKind.DERIVED, ref="edge-clustering")


def style(size_pt: float = 18.0) -> TextStyle:
    return TextStyle(font_family="Play", size_pt=size_pt, color=ON_LIGHT)


def slot(slot_id: str, role: SlotRole, box: Box) -> Slot:
    return Slot(id=slot_id, role=role, box=box, style=style(), provenance=FROM_SLIDE)


def template_spec() -> TemplateSpec:
    layout = LayoutSpec(
        id="layout1",
        name="Заголовок",
        master_id="master1",
        slots=[slot("ph0", SlotRole.TITLE, Box(x=457200, y=457200, w=11277600, h=1000000))],
    )
    pattern = Pattern(
        id="pat1",
        pattern_class=PatternClass.TWO_COLUMN,
        layout_id="layout1",
        donor_slide_index=3,
        slots=[
            slot("s_title", SlotRole.TITLE, Box(x=457200, y=457200, w=11277600, h=900000)),
            slot("s_left", SlotRole.BODY, Box(x=457200, y=1600200, w=5400000, h=4000000)),
            slot("s_right", SlotRole.BODY, Box(x=6334800, y=1600200, w=5400000, h=4000000)),
        ],
        content_area=Box(x=457200, y=1600200, w=11277600, h=4000000),
        provenance=FROM_SLIDE,
    )
    return TemplateSpec(
        template_sha256=SHA,
        source_name="synthetic.pptx",
        slide_width_emu=SLIDE_W,
        slide_height_emu=SLIDE_H,
        palette=[ColorToken(color=ACCENT, usage_count=42, provenance=FROM_THEME)],
        fonts=[FontToken(family="Play", usage_count=120, embedded=True)],
        type_scale_pt=[9.0, 12.0, 14.0, 18.0, 32.0],
        grid=Grid(
            margin_left_emu=457200,
            margin_right_emu=457200,
            margin_top_emu=457200,
            margin_bottom_emu=457200,
            provenance=DERIVED,
        ),
        masters=["master1"],
        layouts=[layout],
        patterns=[pattern],
    )


def content_pack() -> ContentPack:
    doc = SourceDoc(id="d1", name="brief.md", kind="md", char_count=1200)
    return ContentPack(
        brief=Brief(topic="Платформа наблюдаемости", purpose=DeckPurpose.PRODUCT),
        documents=[doc],
        facts=[Fact(id="f1", text="Выручка выросла на 34 %", source_doc_id="d1")],
        series=[
            Series(
                id="ser1",
                name="Выручка",
                unit="млн ₽",
                points=[
                    NumericPoint(label="Q1", value=10.2),
                    NumericPoint(label="Q2", value=13.7),
                ],
                source_doc_id="d1",
            )
        ],
    )


def deck_plan() -> DeckPlan:
    return DeckPlan(
        title="Платформа наблюдаемости",
        purpose=DeckPurpose.PRODUCT,
        slides=[
            SlidePlan(index=1, intent=SlideIntent.TITLE, takeaway_title="Платформа наблюдаемости"),
            SlidePlan(
                index=2,
                intent=SlideIntent.EVIDENCE,
                takeaway_title="Выручка выросла на 34 % за квартал",
                blocks=[
                    ContentBlock(id="b1", kind=BlockKind.SERIES, series_ids=["ser1"]),
                ],
            ),
        ],
    )


def deck_ir() -> DeckIR:
    title = Element(
        id="e1",
        kind=ElementKind.TEXT,
        role=SlotRole.TITLE,
        box=Box(x=457200, y=457200, w=11277600, h=900000),
        provenance=FROM_SLIDE,
        text=TextContent(paragraphs=[Paragraph(text="Заголовок-вывод", style=style(32))]),
    )
    return DeckIR(
        variant="balanced",
        template_sha256=SHA,
        slide_width_emu=SLIDE_W,
        slide_height_emu=SLIDE_H,
        slides=[SlideIR(index=1, layout_id="layout1", pattern_id="pat1", elements=[title])],
    )


def run_manifest() -> RunManifest:
    return RunManifest(
        run_id="run-0001",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        template_sha256=SHA,
        template_name="synthetic.pptx",
        variants=["dense", "balanced", "airy"],
    )
