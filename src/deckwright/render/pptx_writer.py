"""Сборка `.pptx` из `SlideIR` на шаблоне-доноре. Скелетная версия слоя render.

Колода строится не с нуля, а поверх самого шаблона: открываем его, снимаем
слайды-примеры, добавляем новые на его же layout'ах. Всё оформление — мастер,
тема, декор layout'ов, встроенные шрифты, колонтитулы — остаётся на месте,
потому что это те же самые части пакета.

Текст кладётся в плейсхолдеры там, где они есть: тогда он наследует кегль,
гарнитуру и цвет от layout'а, и выглядит так, как задумал дизайнер. Где
плейсхолдера нет, ставится текстовый фрейм со стилем из токенов — включая цвет,
подобранный под яркость фона.

Все объекты нативные. Растеризация слайда запрещена ТЗ, и её здесь нет.

Фаза 7 добавит клонирование паттернов (`clone.py` уже готов), нативные графики
и таблицы, схемы из фигур и коннекторов.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from deckwright.render.clone import purge_slides
from deckwright.schemas import (
    Align,
    DeckIR,
    Element,
    ElementKind,
    SlotRole,
    TemplateSpec,
    TextStyle,
)

_ALIGN = {
    Align.LEFT: PP_ALIGN.LEFT,
    Align.CENTER: PP_ALIGN.CENTER,
    Align.RIGHT: PP_ALIGN.RIGHT,
    Align.JUSTIFY: PP_ALIGN.JUSTIFY,
}


class RenderError(RuntimeError):
    """Колоду нельзя собрать по этому `SlideIR`."""


def _layout_index(spec: TemplateSpec, layout_id: str | None) -> tuple[int, int]:
    """(индекс мастера, индекс layout'а) по идентификатору из `TemplateSpec`.

    Идентификатор собран парсером как `masterN/layoutM`, поэтому разбор здесь —
    обратная операция к тому, что делает `parse.opener`, а не догадка.
    """
    if not layout_id:
        return 0, 0
    try:
        master_part, layout_part = layout_id.split("/", 1)
        return int(master_part.removeprefix("master")) - 1, int(
            layout_part.removeprefix("layout")
        ) - 1
    except (ValueError, IndexError) as exc:
        raise RenderError(f"не разобрать идентификатор layout'а {layout_id!r}") from exc


def _apply_style(run, style: TextStyle) -> None:
    run.font.name = style.font_family
    run.font.size = Pt(style.size_pt)
    run.font.bold = style.bold
    run.font.italic = style.italic
    run.font.color.rgb = RGBColor.from_string(style.color.rgb)


def _fill_text_frame(text_frame, element: Element, *, styled: bool) -> None:
    """Заполняет текстовый фрейм абзацами элемента.

    `styled=False` для плейсхолдеров: там кегль, гарнитура и цвет приходят с
    layout'а, и перебивать их своими значениями означало бы выкинуть ровно ту
    типографику шаблона, ради которой шаблон и взяли.
    """
    if element.text is None:
        raise RenderError(f"элемент {element.id!r} без текста попал в текстовый фрейм")

    text_frame.clear()
    for index, paragraph in enumerate(element.text.paragraphs):
        target = text_frame.paragraphs[0] if index == 0 else text_frame.add_paragraph()
        target.alignment = _ALIGN.get(paragraph.style.align)
        target.level = paragraph.level
        run = target.add_run()
        run.text = paragraph.text
        if styled:
            _apply_style(run, paragraph.style)


def _placeholder_by_role(slide, role: SlotRole):
    """Плейсхолдер слайда под нужную роль, если такой есть."""
    wanted = {"title", "ctrTitle"} if role is SlotRole.TITLE else {"subTitle"}
    if role not in (SlotRole.TITLE, SlotRole.SUBTITLE):
        return None
    for shape in slide.placeholders:
        ph = shape._element.xpath(".//*[local-name()='ph']")
        if ph and ph[0].get("type", "body") in wanted:
            return shape
    return None


def _render_element(slide, element: Element) -> None:
    if element.kind is not ElementKind.TEXT:
        # Скелет верстает только текст; графики, таблицы и картинки — фаза 7.
        return

    placeholder = _placeholder_by_role(slide, element.role)
    if placeholder is not None:
        _fill_text_frame(placeholder.text_frame, element, styled=False)
        return

    box = element.box
    textbox = slide.shapes.add_textbox(Emu(box.x), Emu(box.y), Emu(box.w), Emu(box.h))
    textbox.text_frame.word_wrap = True
    _fill_text_frame(textbox.text_frame, element, styled=True)


def _drop_empty_placeholders(slide) -> None:
    """Убирает плейсхолдеры, в которые ничего не положили.

    Оставленный пустым плейсхолдер показывает в PowerPoint подсказку вида
    «Текст заголовка», а в PDF даёт пустую рамку. Проверка аудита «остался
    текст-заглушка» на таком слайде сработает справедливо — проще не создавать
    повод.
    """
    for shape in list(slide.placeholders):
        if shape.has_text_frame and not shape.text_frame.text.strip():
            shape._element.getparent().remove(shape._element)


def render_deck(
    deck: DeckIR,
    spec: TemplateSpec,
    template_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Собирает `.pptx` поверх шаблона и сохраняет его."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation(str(template_path))
    if prs.slide_width != deck.slide_width_emu or prs.slide_height != deck.slide_height_emu:
        raise RenderError(
            "размер слайда в IR не совпадает с шаблоном: "
            f"{deck.slide_width_emu}×{deck.slide_height_emu} против "
            f"{prs.slide_width}×{prs.slide_height}"
        )

    purge_slides(prs)

    for slide_ir in deck.slides:
        master_index, layout_index = _layout_index(spec, slide_ir.layout_id)
        try:
            layout = prs.slide_masters[master_index].slide_layouts[layout_index]
        except IndexError as exc:
            raise RenderError(
                f"слайд {slide_ir.index}: в шаблоне нет layout'а {slide_ir.layout_id!r}"
            ) from exc

        slide = prs.slides.add_slide(layout)
        for element in slide_ir.elements:
            _render_element(slide, element)
        _drop_empty_placeholders(slide)
        if slide_ir.speaker_notes:
            slide.notes_slide.notes_text_frame.text = slide_ir.speaker_notes

    prs.save(str(output_path))
    return output_path


def count_native_shapes(path: str | Path) -> list[int]:
    """Сколько нативных фигур на каждом слайде готового файла.

    Нужно проверке «слайд не является одной картинкой»: слайд, состоящий из
    единственного изображения, ТЗ не засчитывает.
    """
    prs = Presentation(str(path))
    return [len(slide.shapes) for slide in prs.slides]


def slide_is_single_image(path: str | Path) -> list[int]:
    """Номера слайдов, которые состоят из одной картинки и ничего больше."""
    prs = Presentation(str(path))
    offenders: list[int] = []
    for index, slide in enumerate(prs.slides, start=1):
        shapes = list(slide.shapes)
        if len(shapes) == 1 and shapes[0].shape_type == 13:
            offenders.append(index)
    return offenders
