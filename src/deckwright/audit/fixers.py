"""Детерминированные исправления: то, что можно починить, не спрашивая.

A17 требует чинить детерминированные проблемы детерминированно там, где это
возможно. «Где возможно» — существенная оговорка: сдвинуть элемент внутрь
слайда можно без потерь, а сократить текст — нельзя, потому что решать, что
выкинуть, значит решать за автора.

Поэтому фиксеры делятся надвое и это видно в типе исправления:

* `AUTOMATIC` — правка предсказуема и обратима, применяется сама;
* `ASSISTED` — правка требует решения человека, интерфейс её только предлагает.

Правки применяются к `SlideIR`, а не к собранному `.pptx`: колода после
исправления пересобирается из представления, и чинить оба места значило бы
держать два источника правды.
"""

from __future__ import annotations

from dataclasses import dataclass

from deckwright.schemas import Box, DeckIR, FixKind, Issue


@dataclass(frozen=True)
class FixOutcome:
    """Что удалось применить, а что осталось человеку."""

    applied: list[str]
    skipped: dict[str, str]

    @property
    def changed_slides(self) -> set[int]:
        return {int(item.split(":")[0]) for item in self.applied}


def _element(deck: DeckIR, slide_index: int, element_id: str):
    for slide in deck.slides:
        if slide.index != slide_index:
            continue
        for element in slide.all_elements():
            if element.id == element_id:
                return slide, element
    return None, None


def _move_inside(box: Box, width: int, height: int) -> Box:
    return _fit_into(box, 0, 0, width, height)


def _fit_into(box: Box, left: int, top: int, right: int, bottom: int) -> Box:
    """Вписывает рамку в прямоугольник: сдвигом, а если она шире — ужатием.

    Одного сдвига мало. На `vk_workspace` график донора идёт на всю ширину
    слайда, а заголовок шире области полей на 0.03 дюйма: сдвинутый к левому
    полю, такой элемент вылезает за правое, находка возвращается, и правка
    «применена» дважды впустую — по 7 с на итерацию.
    """
    w = min(box.w, right - left)
    h = min(box.h, bottom - top)
    x = max(left, min(box.x, right - w))
    y = max(top, min(box.y, bottom - h))
    return Box(x=x, y=y, w=w, h=h)


def apply(deck: DeckIR, issues: list[Issue], spec=None) -> FixOutcome:
    """Применяет все автоматические исправления. Возвращает, что сделано.

    Изменённые слайды нужны следующей итерации аудита: переспрашивать модель
    про слайды, которых правка не коснулась, значит платить за колоду дважды.
    """
    applied: list[str] = []
    skipped: dict[str, str] = {}

    for issue in issues:
        if issue.fix.kind is not FixKind.AUTOMATIC:
            skipped[issue.check_id] = (
                "требует решения человека"
                if issue.fix.kind is FixKind.ASSISTED
                else "автоматического исправления нет"
            )
            continue

        element_id = str(issue.fix.params.get("element_id", ""))
        slide, element = _element(deck, issue.slide_index, element_id)
        if element is None:
            skipped[issue.check_id] = f"элемент {element_id!r} не найден"
            continue

        action = issue.fix.action
        if action == "move_inside_slide":
            element.box = _move_inside(
                element.box, deck.slide_width_emu, deck.slide_height_emu
            )
        elif action == "move_inside_margins":
            grid = getattr(spec, "grid", None)
            if grid is None:
                skipped[issue.check_id] = "полей у шаблона нет: двигать не к чему"
                continue
            element.box = _fit_into(
                element.box,
                grid.margin_left_emu,
                grid.margin_top_emu,
                deck.slide_width_emu - grid.margin_right_emu,
                deck.slide_height_emu - grid.margin_bottom_emu,
            )
        elif action == "snap_to_grid":
            element.box = Box(
                x=int(issue.fix.params["x"]),
                y=element.box.y,
                w=element.box.w,
                h=element.box.h,
            )
        elif action == "restore_recurring_position":
            element.box = Box(
                x=int(issue.fix.params["x"]),
                y=int(issue.fix.params["y"]),
                w=element.box.w,
                h=element.box.h,
            )
        elif action == "restore_aspect":
            image = element.image
            if image is None or not image.native_w or not image.native_h:
                skipped[issue.check_id] = "исходные пропорции картинки неизвестны"
                continue
            ratio = image.native_w / image.native_h
            element.box = Box(
                x=element.box.x,
                y=element.box.y,
                w=element.box.w,
                h=max(1, round(element.box.w / ratio)),
            )
        else:
            skipped[issue.check_id] = f"операция {action!r} не реализована"
            continue

        applied.append(f"{slide.index}:{issue.check_id}:{element_id}")

    return FixOutcome(applied=applied, skipped=skipped)
