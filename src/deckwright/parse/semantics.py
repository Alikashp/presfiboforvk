"""Класс композиции — правилами по структуре паттерна.

ТЗ разрешает привлекать модель к определению семантики layout'ов, но ставит
правила первыми, и на то есть причины помимо цены. Правило детерминированно:
один и тот же шаблон разбирается одинаково при каждом прогоне, и разбор
кэшируется по хэшу файла. Правило объяснимо: в `TemplateSpec` видно, почему
паттерн признан сеткой, а не цитатой. Модель нужна там, где структура
неоднозначна, и её решение помечается пониженной уверенностью — чтобы вёрстка
знала, чему доверять меньше.

Классифицируется структура, а не текст. Имена фигур в этих колодах
бессмысленны (`Google Shape;904`), а текст-заглушка может отсутствовать вовсе:
шаблоном пользователя вполне может оказаться его старая презентация с
настоящим содержанием.
"""

from __future__ import annotations

from deckwright.schemas import Pattern, PatternClass, Provenance, SlotRole, SourceKind

# Картинка крупнее этой доли слайда делает композицию «обложкой».
HERO_IMAGE_SHARE = 0.35

# Две колонки — ровно два одинаковых блока, каждый примерно в половину
# доступной ширины.
TWO_COLUMN_MIN_WIDTH_SHARE = 0.3

# Сетка — от трёх повторяющихся элементов.
GRID_MIN_ITEMS = 3

# Один крупный текстовый блок и ничего больше — утверждение.
STATEMENT_MAX_SLOTS = 2


def _roles(pattern: Pattern) -> set[SlotRole]:
    roles = {slot.role for slot in pattern.slots}
    for repeater in pattern.repeaters:
        roles.update(slot.role for slot in repeater.item_slots)
    return roles


def _body_slots(pattern: Pattern) -> int:
    return sum(
        1
        for slot in pattern.slots
        if slot.role not in (SlotRole.TITLE, SlotRole.SUBTITLE, SlotRole.DECOR)
    )


def classify(pattern: Pattern, slide_w: int, slide_h: int) -> PatternClass:
    """Класс композиции по её структуре."""
    roles = _roles(pattern)
    slide_area = slide_w * slide_h

    if SlotRole.TABLE in roles:
        return PatternClass.TABLE
    if SlotRole.CHART in roles:
        return PatternClass.CHART

    biggest_image = max(
        (slot.box.area for slot in pattern.slots if slot.role is SlotRole.IMAGE),
        default=0,
    )
    if biggest_image >= slide_area * HERO_IMAGE_SHARE:
        return PatternClass.HERO

    for repeater in pattern.repeaters:
        if repeater.observed_count >= GRID_MIN_ITEMS:
            # Ряд числовых показателей — не сетка карточек, а строка метрик.
            item_roles = {slot.role for slot in repeater.item_slots}
            if SlotRole.KPI_VALUE in item_roles:
                return PatternClass.KPI_ROW
            return PatternClass.GRID
        paired = repeater.observed_count == 2 and repeater.axis == "horizontal"
        if paired and repeater.item_box.w >= slide_w * TWO_COLUMN_MIN_WIDTH_SHARE:
            return PatternClass.TWO_COLUMN

    if SlotRole.KPI_VALUE in roles:
        return PatternClass.KPI_ROW
    if SlotRole.QUOTE in roles:
        return PatternClass.QUOTE

    body = _body_slots(pattern)
    if SlotRole.TITLE in roles and body == 0:
        # Только заголовок. Подзаголовок отличает титул от разделителя:
        # у титула есть вторая строка (автор, дата, назначение), у
        # разделителя — одно название раздела.
        return PatternClass.TITLE if SlotRole.SUBTITLE in roles else PatternClass.SECTION
    if body and body <= STATEMENT_MAX_SLOTS and not pattern.repeaters:
        return PatternClass.STATEMENT

    return PatternClass.FREEFORM


def classified(pattern: Pattern, slide_w: int, slide_h: int) -> Pattern:
    """Паттерн с проставленным классом и записанным источником решения."""
    pattern_class = classify(pattern, slide_w, slide_h)
    note = (
        "класс определён правилами по структуре"
        if pattern_class is not PatternClass.FREEFORM
        else "структура не подошла ни под одно правило; нужен разбор моделью"
    )
    return pattern.model_copy(
        update={
            "pattern_class": pattern_class,
            "provenance": Provenance(
                kind=SourceKind.DERIVED,
                ref=pattern.provenance.ref,
                # Неопознанная композиция — не то же самое, что опознанная:
                # вёрстка должна предпочитать паттерны, в классе которых
                # парсер уверен.
                confidence=1.0 if pattern_class is not PatternClass.FREEFORM else 0.3,
                note=note,
            ),
        }
    )
