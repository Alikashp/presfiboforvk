"""Верность шаблону: гарнитуры, кегли, цвета, контраст, layout, колонтитулы.

Это те проверки, ради которых весь проект и затевался. Колода может быть
безупречна геометрически и всё равно не быть презентацией **этого** шаблона —
если набрана чужой гарнитурой, кеглем не из шкалы или цветом не из палитры.
"""

from __future__ import annotations

from deckwright.audit.registry import check
from deckwright.schemas import (
    DeckIR,
    FixKind,
    Issue,
    ProposedFix,
    SlideIR,
    SlotRole,
    TemplateSpec,
)

# Порог Приложения 1. Тем же числом меряет вёрстка, когда выбирает цвет текста.
MIN_CONTRAST = 4.5

# Кегль считается тем же, если отличается меньше чем на четверть пункта:
# шкала добывается статистикой и хранит дробные значения вроде 13.22.
SIZE_TOLERANCE_PT = 0.25

# Насколько цвет может отличаться от палитрового по каждому каналу.
COLOR_TOLERANCE = 12

# Насколько повторяющийся элемент может сдвинуться со своего места.
RECURRING_TOLERANCE_EMU = 45_720  # 0.05 дюйма


def _issue(check_id: str, slide_index: int, message: str, **kwargs) -> Issue:
    spec = check(check_id)
    fix = kwargs.pop("fix", None)
    return Issue(
        check_id=check_id,
        kind=spec.kind,
        category=spec.category,
        severity=spec.severity,
        slide_index=slide_index,
        message=message,
        fix=fix or ProposedFix(kind=FixKind.NONE),
        **kwargs,
    )


def _rgb(value: str) -> tuple[int, int, int]:
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _close_to_palette(rgb: str, palette: list[str]) -> bool:
    if not palette:
        return True
    target = _rgb(rgb)
    return any(
        all(abs(a - b) <= COLOR_TOLERANCE for a, b in zip(target, _rgb(other), strict=True))
        for other in palette
    )


def fonts_and_sizes(slide: SlideIR, spec: TemplateSpec) -> list[Issue]:
    """Гарнитура — только из шаблона, кегль — только из его шкалы.

    Шкала берётся расширенная: к статистической шкале добавляются кегли,
    которыми шаблон набирает свои слоты. Иначе заголовок, набранный
    объявленным в мастере 44 pt, оказался бы «не из шкалы» — при том что это
    и есть кегль шаблона.
    """
    from deckwright.layout.strategy import scale_ladder

    families = {token.family.strip().lower() for token in spec.fonts}
    ladder = scale_ladder(spec)

    found: list[Issue] = []
    for element in slide.all_elements():
        if element.text is None:
            continue
        for index, paragraph in enumerate(element.text.paragraphs):
            style = paragraph.style
            if families and style.font_family.strip().lower() not in families:
                found.append(
                    _issue(
                        "template.font_not_in_template",
                        slide.index,
                        f"{element.id}: гарнитура {style.font_family!r}, "
                        f"а шаблон использует {sorted(families)}",
                        element_ids=[element.id],
                        bbox=element.box,
                    )
                )
            if ladder and not any(
                abs(style.size_pt - step) <= SIZE_TOLERANCE_PT for step in ladder
            ):
                found.append(
                    _issue(
                        "template.size_not_in_scale",
                        slide.index,
                        f"{element.id}, абзац {index + 1}: кегль {style.size_pt:g} pt "
                        "не из шкалы шаблона",
                        element_ids=[element.id],
                        bbox=element.box,
                    )
                )
    return found


def colors(slide: SlideIR, spec: TemplateSpec) -> list[Issue]:
    """Цвет текста — из палитры шаблона, а не из головы."""
    palette = [token.color.rgb for token in spec.palette]
    if not palette:
        return []

    found: list[Issue] = []
    for element in slide.all_elements():
        if element.text is None:
            continue
        for paragraph in element.text.paragraphs:
            rgb = paragraph.style.color.rgb
            if _close_to_palette(rgb, palette):
                continue
            found.append(
                _issue(
                    "template.color_not_in_palette",
                    slide.index,
                    f"{element.id}: цвет #{rgb} не из палитры шаблона",
                    element_ids=[element.id],
                    bbox=element.box,
                )
            )
    return found


def contrast(slide: SlideIR, min_ratio: float = MIN_CONTRAST) -> list[Issue]:
    """Контраст ниже порога — это нечитаемый слайд, а не стилистический выбор.

    Порог приходит из конфига (`audit.contrast_min_ratio`); умолчание — 4.5:1
    по WCAG AA для основного текста.
    """
    found: list[Issue] = []
    for element in slide.all_elements():
        # Мерится по фону, на котором текст лежит: подложка элемента, если
        # она есть, иначе фон слайда.
        backdrop = element.backdrop or slide.background
        if element.text is None or backdrop is None:
            continue
        for paragraph in element.text.paragraphs:
            ratio = paragraph.style.color.contrast_ratio(backdrop)
            if ratio >= min_ratio:
                continue
            found.append(
                _issue(
                    "template.low_contrast",
                    slide.index,
                    f"{element.id}: контраст {ratio:.2f} при пороге {min_ratio}",
                    element_ids=[element.id],
                    bbox=element.box,
                )
            )
    return found


def layout_reference(slide: SlideIR, spec: TemplateSpec) -> list[Issue]:
    """Слайд обязан ссылаться на существующий layout шаблона."""
    if slide.layout_id is None:
        return []
    known = {layout.id for layout in spec.layouts}
    if slide.layout_id in known:
        return []
    return [
        _issue(
            "template.unknown_layout",
            slide.index,
            f"слайд ссылается на layout {slide.layout_id!r}, которого нет в шаблоне",
        )
    ]


def recurring_elements(slide: SlideIR, spec: TemplateSpec) -> list[Issue]:
    """Логотип и колонтитул обязаны стоять там же, где в шаблоне.

    Сдвинутый логотип виден на просмотре колоды сразу: он «прыгает» от слайда
    к слайду. Проверяются только элементы, стоящие в шаблоне почти всегда —
    редкие не обязаны повторяться.
    """
    found: list[Issue] = []
    expected = {
        item.role: item
        for item in spec.recurring
        if item.frequency >= 0.5 and item.role in (SlotRole.LOGO, SlotRole.FOOTER)
    }
    if not expected:
        return []

    for element in slide.all_elements():
        anchor = expected.get(element.role)
        if anchor is None:
            continue
        drift = max(abs(element.box.x - anchor.box.x), abs(element.box.y - anchor.box.y))
        if drift <= RECURRING_TOLERANCE_EMU:
            continue
        found.append(
            _issue(
                "template.recurring_element_moved",
                slide.index,
                f"{element.id}: {element.role.value} сдвинут на "
                f"{drift / 914400:.2f} дюйма от своего места в шаблоне",
                element_ids=[element.id],
                bbox=element.box,
                fix=ProposedFix(
                    kind=FixKind.AUTOMATIC,
                    description="вернуть элемент на место шаблона",
                    action="restore_recurring_position",
                    params={
                        "element_id": element.id,
                        "x": anchor.box.x,
                        "y": anchor.box.y,
                    },
                ),
            )
        )
    return found


def run(
    deck: DeckIR, spec: TemplateSpec, min_contrast: float = MIN_CONTRAST
) -> list[Issue]:
    found: list[Issue] = []
    for slide in deck.slides:
        found.extend(fonts_and_sizes(slide, spec))
        found.extend(colors(slide, spec))
        found.extend(contrast(slide, min_contrast))
        found.extend(layout_reference(slide, spec))
        found.extend(recurring_elements(slide, spec))
    return found


__all__ = [
    "colors",
    "contrast",
    "fonts_and_sizes",
    "layout_reference",
    "recurring_elements",
    "run",
]
