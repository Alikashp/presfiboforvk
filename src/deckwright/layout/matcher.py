"""Раскладка плана по шаблону. Скелетная версия слоя layout.

Выбирает layout под намерение слайда и раскладывает блоки плана по его
плейсхолдерам, а чего не хватило — ставит в свободную область внутри полей.

Скелетная версия намеренно проста: первый подходящий layout по числу
контентных слотов. Фаза 6 заменит это матчером по блочной сигнатуре с учётом
`LayoutStrategy`, фиттером текста по метрикам шрифта и клонированием паттернов
вместо плейсхолдеров.

Что уже здесь по-настоящему: цвет текста выбирается по яркости фона, а не
берётся чёрным. На тёмном шаблоне чёрный текст по чёрному фону — не
теоретический риск, а то, что получилось при первом же прогоне на
`vk_workspace`.
"""

from __future__ import annotations

from deckwright.schemas import (
    Box,
    Color,
    DeckIR,
    Element,
    ElementKind,
    LayoutSpec,
    Paragraph,
    Provenance,
    SlideIntent,
    SlideIR,
    SlidePlan,
    SlotRole,
    SourceKind,
    TemplateSpec,
    TextContent,
    TextStyle,
)

# Намерение слайда → роли, которые ему нужны от layout'а. На этом проходе
# различаются только «нужен ли контентный слот помимо заголовка».
_BARE_INTENTS = frozenset({SlideIntent.TITLE, SlideIntent.SECTION, SlideIntent.CLOSING})

_CONTENT_ROLES = frozenset(
    {SlotRole.BODY, SlotRole.BULLETS, SlotRole.CHART, SlotRole.TABLE, SlotRole.IMAGE}
)

# Доля кегля шкалы: заголовок берёт верх шкалы, тело — середину.
_TITLE_SCALE_INDEX = -2
_BODY_SCALE_INDEX = 1


class LayoutError(RuntimeError):
    """Шаблон не даёт ни одного места, куда положить содержание."""


def _content_slots(layout: LayoutSpec) -> list:
    return [s for s in layout.slots if s.role in _CONTENT_ROLES]


def _title_slot(layout: LayoutSpec):
    for slot in layout.slots:
        if slot.role is SlotRole.TITLE:
            return slot
    return None


def pick_layout(spec: TemplateSpec, plan_slide: SlidePlan) -> LayoutSpec:
    """Первый layout, чья структура не противоречит намерению слайда.

    Титул, разделитель и финал довольствуются одним заголовком; остальным
    нужен хотя бы один контентный слот, а если таких layout'ов в шаблоне нет —
    берём любой с заголовком и ставим содержание в свободную область.
    """
    if not spec.layouts:
        raise LayoutError(f"в шаблоне {spec.source_name!r} нет ни одного layout'а")

    with_title = [layout for layout in spec.layouts if _title_slot(layout) is not None]
    candidates = with_title or spec.layouts

    if plan_slide.intent in _BARE_INTENTS:
        return candidates[0]

    with_content = [layout for layout in candidates if _content_slots(layout)]
    return (with_content or candidates)[0]


def _scale(spec: TemplateSpec, index: int) -> float:
    scale = spec.type_scale_pt or [18.0]
    return scale[max(-len(scale), min(index, len(scale) - 1))]


def _text_color(layout: LayoutSpec, role: SlotRole) -> Color:
    """Цвет текста для роли — взятый из самого шаблона.

    Порядок предпочтений: цвет, которым шаблон пишет текст этой роли в этом
    layout'е; затем цвет любого его текстового слота; и только если шаблон
    не сказал ничего — выбор по яркости фона.

    Так правильнее, чем всегда считать по фону: шаблон уже решил, каким цветом
    здесь писать, и его решение учитывает градиенты, фоновые картинки и декор,
    о которых мы не знаем ничего. Наивный вариант «чёрный текст по умолчанию»
    на первом же прогоне дал чёрное по чёрному на тёмном шаблоне.
    """
    for slot in layout.slots:
        if slot.role is role and slot.style is not None:
            return slot.style.color
    for slot in layout.slots:
        if slot.style is not None:
            return slot.style.color
    background = layout.background
    if background is not None and background.luminance < 0.5:
        return Color(rgb="FFFFFF")
    return Color(rgb="111111")


def _content_area(spec: TemplateSpec, layout: LayoutSpec) -> Box:
    """Свободная область: поля шаблона минус то, что занимает заголовок."""
    grid = spec.grid
    left = grid.margin_left_emu if grid else spec.slide_width_emu // 20
    right = grid.margin_right_emu if grid else spec.slide_width_emu // 20
    top = grid.margin_top_emu if grid else spec.slide_height_emu // 12
    bottom = grid.margin_bottom_emu if grid else spec.slide_height_emu // 12

    title = _title_slot(layout)
    if title is not None:
        top = max(top, title.box.bottom + spec.slide_height_emu // 40)

    width = spec.slide_width_emu - left - right
    height = spec.slide_height_emu - top - bottom
    if width <= 0 or height <= 0:
        raise LayoutError(
            f"поля шаблона {spec.source_name!r} не оставляют места под содержание"
        )
    return Box(x=left, y=top, w=width, h=height)


def _block_lines(plan_slide: SlidePlan) -> list[str]:
    """Блоки плана, приведённые к строкам. Скелет верстает всё текстом."""
    lines: list[str] = []
    for block in plan_slide.blocks:
        if block.heading:
            lines.append(block.heading)
        lines.extend(block.items)
        if block.series_ids and not block.items:
            lines.append(f"Данные: {', '.join(block.series_ids)}")
    return lines


def build_slide_ir(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    layout: LayoutSpec,
    font_family: str,
) -> SlideIR:
    background = layout.background
    provenance = Provenance(kind=SourceKind.LAYOUT, ref=layout.id)

    elements: list[Element] = []

    title_slot = _title_slot(layout)
    title_box = title_slot.box if title_slot else _content_area(spec, layout)
    elements.append(
        Element(
            id=f"s{plan_slide.index}_title",
            kind=ElementKind.TEXT,
            role=SlotRole.TITLE,
            box=title_box,
            provenance=provenance,
            text=TextContent(
                paragraphs=[
                    Paragraph(
                        text=plan_slide.takeaway_title,
                        style=TextStyle(
                            font_family=font_family,
                            size_pt=_scale(spec, _TITLE_SCALE_INDEX),
                            bold=True,
                            color=_text_color(layout, SlotRole.TITLE),
                        ),
                    )
                ]
            ),
        )
    )

    lines = _block_lines(plan_slide)
    if lines:
        slots = _content_slots(layout)
        box = slots[0].box if slots else _content_area(spec, layout)
        body_style = TextStyle(
            font_family=font_family,
            size_pt=_scale(spec, _BODY_SCALE_INDEX),
            color=_text_color(layout, SlotRole.BODY),
        )
        elements.append(
            Element(
                id=f"s{plan_slide.index}_body",
                kind=ElementKind.TEXT,
                role=SlotRole.BODY,
                box=box,
                provenance=provenance,
                text=TextContent(
                    paragraphs=[
                        Paragraph(text=line, style=body_style, bullet=True) for line in lines
                    ]
                ),
            )
        )

    return SlideIR(
        index=plan_slide.index,
        layout_id=layout.id,
        elements=elements,
        background=background,
        is_dark=layout.is_dark,
        speaker_notes=plan_slide.speaker_notes,
    )


def build_deck_ir(spec: TemplateSpec, plan, variant: str) -> DeckIR:
    font_family = spec.fonts[0].family if spec.fonts else "Arial"
    slides = [
        build_slide_ir(spec, plan_slide, pick_layout(spec, plan_slide), font_family)
        for plan_slide in plan.slides
    ]
    return DeckIR(
        variant=variant,
        template_sha256=spec.template_sha256,
        slide_width_emu=spec.slide_width_emu,
        slide_height_emu=spec.slide_height_emu,
        slides=slides,
    )
