"""Подгонка текста под рамку. Порядок действий — закон, а не предпочтение.

1. **Измерить** по метрикам шрифта шаблона. На autofit PowerPoint не
   полагаться: он подбирает кегль сам, из любых значений, и у разных людей
   результат разный — а проверка «кегль не из шкалы» увидит именно его.
2. **Не влезло — спуститься на ступень вниз по шкале шаблона**, и только по
   ней. Промежуточные значения запрещены: проверка найдёт их справедливо, и
   исправлять придётся то же самое, но позже и дороже.
3. **Дошли до минимальной ступени** — дальше кегль не уменьшается. Вместо
   этого сокращаем текст или разбиваем слайд; что именно, решает стратегия
   варианта.
4. **Ни то ни другое не вышло** — оставить как есть и завести находку аудита.
   Молча обрезанный текст хуже честно помеченного.

Бюджеты длины из фазы 5 эту работу уменьшают, но не отменяют: модель следует
ограничению приблизительно, а переполнение бывает и от одного длинного слова,
которое не переносится вовсе.
"""

from __future__ import annotations

from dataclasses import dataclass

from deckwright.layout.text_metrics import (
    DEFAULT_LINE_HEIGHT,
    FRAME_INSET_X_EMU,
    FRAME_INSET_Y_EMU,
    FontMetrics,
    measure_height_emu,
    wrap,
)
from deckwright.schemas import Box


@dataclass(frozen=True)
class FitResult:
    """Чем кончился подбор кегля."""

    size_pt: float
    fits: bool
    # Сколько ступеней шкалы пришлось пройти вниз. Ноль — влезло сразу.
    steps_down: int
    # На сколько высота текста превышает рамку. Ноль, когда влезло.
    overflow_emu: int
    lines: int

    @property
    def at_minimum(self) -> bool:
        """Подбор упёрся в минимальную ступень и не помог."""
        return not self.fits


def _height_emu(
    text: str, metrics: FontMetrics, size_pt: float, box: Box, line_height: float
) -> tuple[int, int]:
    usable_width = max(1, box.w - FRAME_INSET_X_EMU)
    lines = wrap(text, metrics, size_pt, usable_width)
    height = measure_height_emu(text, metrics, size_pt, box.w, line_height)
    return height, len(lines)


def fit_size(
    text: str,
    metrics: FontMetrics,
    box: Box,
    ladder: list[float],
    start_pt: float,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> FitResult:
    """Наибольший кегль **из шкалы**, на котором текст влезает в рамку.

    Начинает со стартового и спускается по ступеням. Выше стартового не
    поднимается: стартовый выбран стратегией от того, которым шаблон набирает
    этот слот, и увеличивать его — значит верстать не тем кеглем, что шаблон.

    Если не влезло нигде, возвращает минимальную ступень с `fits=False`:
    решение, что делать дальше, принимает вызывающий, а не фиттер.
    """
    usable_height = max(1, box.h - FRAME_INSET_Y_EMU)
    steps = [size for size in ladder if size <= start_pt] or [min(ladder)]

    last_height, last_lines = 0, 0
    for steps_down, size in enumerate(reversed(steps)):
        height, lines = _height_emu(text, metrics, size, box, line_height)
        if height <= usable_height:
            return FitResult(
                size_pt=size,
                fits=True,
                steps_down=steps_down,
                overflow_emu=0,
                lines=lines,
            )
        last_height, last_lines = height, lines

    smallest = steps[0]
    return FitResult(
        size_pt=smallest,
        fits=False,
        steps_down=len(steps) - 1,
        overflow_emu=last_height - usable_height,
        lines=last_lines,
    )


def fit_paragraphs(
    lines: list[str],
    metrics: FontMetrics,
    box: Box,
    ladder: list[float],
    start_pt: float,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> FitResult:
    """То же для списка абзацев: все они живут в одной рамке и одном кегле.

    Разный кегль у соседних пунктов одного списка — не вёрстка, а авария,
    поэтому ступень ищется общая: та, на которой помещается вся сумма.
    """
    usable_height = max(1, box.h - FRAME_INSET_Y_EMU)
    steps = [size for size in ladder if size <= start_pt] or [min(ladder)]

    last_height, last_lines = 0, 0
    for steps_down, size in enumerate(reversed(steps)):
        total_height = 0
        total_lines = 0
        for line in lines:
            height, count = _height_emu(line, metrics, size, box, line_height)
            total_height += height
            total_lines += count
        if total_height <= usable_height:
            return FitResult(
                size_pt=size,
                fits=True,
                steps_down=steps_down,
                overflow_emu=0,
                lines=total_lines,
            )
        last_height, last_lines = total_height, total_lines

    smallest = steps[0]
    return FitResult(
        size_pt=smallest,
        fits=False,
        steps_down=len(steps) - 1,
        overflow_emu=last_height - usable_height,
        lines=last_lines,
    )


def split_blocks(blocks: list, parts: int = 2) -> list[list]:
    """Делит блоки слайда на несколько слайдов, не разрывая блок.

    Блок — смысловая единица: разрезать список пунктов посередине значит
    оставить на первом слайде половину мысли. Если блок один, делить нечего,
    и вызывающий обязан обойтись находкой аудита.
    """
    if parts < 2 or len(blocks) < parts:
        return [blocks]
    size = -(-len(blocks) // parts)  # округление вверх
    chunks = [blocks[i : i + size] for i in range(0, len(blocks), size)]
    return [chunk for chunk in chunks if chunk]
