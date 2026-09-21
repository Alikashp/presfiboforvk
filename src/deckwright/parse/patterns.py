"""Добыча композиционных паттернов из слайдов-примеров.

Центральный модуль парсера. Причина, по которой он существует, — устройство
реальных шаблонов: у большинства layout'ов датасета нет ни одного контентного
плейсхолдера. У всех пятнадцати layout'ов `vk_workspace` ровно один
плейсхолдер заголовка и три-девять декоративных фигур; в `vk_tech` тридцать
шесть слайдов из пятидесяти четырёх стоят на layout'е «Свободный дизайн» с
одним заголовком. Опереться на плейсхолдеры при вёрстке попросту не на что.

Зато сами колоды — каталоги готовых композиций с рыбным текстом. Слайд-пример
несёт и геометрию, и стили, и подсказку о роли каждого места («Заголовок»,
«Текст», «XXX%», «Вставить фото»). Отсюда и берутся паттерны.

Отдельная ценность — повторяющиеся компоненты. На двадцать первом слайде
`vk_tech` четыре одинаковые по структуре группы стоят с шагом 2.35 дюйма при
ширине 1.77 — это карточная сетка, у которой число карточек можно менять под
содержание. Такие группы опознаются по совпадению структурной подписи и
равномерности шага, а не по именам: колоды экспортированы через Google Slides,
и все фигуры там называются `Google Shape;NNN`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise

from lxml import etree

from deckwright.parse.geometry import iter_shapes
from deckwright.schemas import (
    Box,
    Pattern,
    PatternClass,
    Provenance,
    Repeater,
    Slot,
    SlotRole,
    SourceKind,
    TextStyle,
)

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Шаги повторителя, отличающиеся меньше чем на эту долю, считаются равными.
PITCH_TOLERANCE = 0.08

# Меньше этого числа одинаковых элементов — совпадение, а не сетка.
MIN_REPEAT = 2

# Насколько можно раздвинуть наблюдённое число элементов. Шаблон показал сетку
# из четырёх карточек; три и пять в ту же полосу укладываются, десять — нет.
COUNT_SLACK = 2

# Тип содержимого `graphicFrame` объявлен в `graphicData/@uri`. В датасете
# нативных графиков не было вовсе, и рамка считалась таблицей всегда; на чужом
# шаблоне с четырьмя графиками это подменяло график таблицей.
GRAPHIC_ROLES = {
    "chart": SlotRole.CHART,
    "table": SlotRole.TABLE,
}

# Фигура мельче этой доли слайда по обеим сторонам — маркер или точка, а не
# содержательное место.
MIN_SLOT_AREA_SHARE = 0.0004

# Шаг меньше половины размера элемента означает, что элементы лежат друг на
# друге, а не стоят в ряд. Два блока, смещённые на волосок (тень, подложка),
# сеткой не являются, но формально дают равномерный шаг.
MIN_PITCH_SHARE = 0.5


@dataclass(frozen=True)
class _Shape:
    element: etree._Element
    box: Box
    tag: str
    text: str
    size_pt: float


def _text_of(element: etree._Element) -> str:
    return " ".join(
        (node.text or "") for node in element.findall(f".//{{{A_NS}}}t")
    ).strip()


def _max_size(element: etree._Element) -> float:
    sizes = [
        int(node.get("sz")) / 100
        for node in element.iter()
        if etree.QName(node).localname in ("rPr", "defRPr") and node.get("sz")
    ]
    return max(sizes) if sizes else 0.0


def _signature(element: etree._Element, box: Box) -> tuple:
    """Структурная подпись фигуры — по чему опознаётся повтор.

    В подпись входят состав дочерних тегов и габариты, огрублённые до сотой
    дюйма. Ни имена, ни координаты: имена в этих колодах бессмысленны, а
    координаты у элементов сетки как раз и различаются.
    """
    tags = defaultdict(int)
    for node in element.iter():
        name = etree.QName(node).localname
        if name in ("sp", "pic", "graphicFrame", "cxnSp", "grpSp"):
            tags[name] += 1
    return (
        tuple(sorted(tags.items())),
        round(box.w / 9144),
        round(box.h / 9144),
        len(element.findall(f".//{{{A_NS}}}t")),
    )


def _role_from_geometry(shape: _Shape, shapes: list[_Shape], slide_h: int) -> SlotRole:
    """Роль слота по структуре, а не по тексту-заглушке.

    Главный признак — ранг кегля среди текстовых фигур слайда: самый крупный
    текст в верхней части и есть заголовок. Текст-заглушка («Заголовок»,
    «Текст») — только подсказка, и подсказка ненадёжная: шаблоном может
    оказаться старая презентация с настоящим содержанием, где никаких рыбных
    слов нет.
    """
    if shape.tag == "pic":
        return SlotRole.IMAGE
    if shape.tag == "graphicFrame":
        data = shape.element.find(f".//{{{A_NS}}}graphicData")
        uri = (data.get("uri", "") if data is not None else "").rsplit("/", 1)[-1]
        # Неизвестный вид рамки (SmartArt, встроенный объект) честнее пометить
        # неопознанным, чем назвать таблицей и потом строить не то.
        return GRAPHIC_ROLES.get(uri, SlotRole.UNKNOWN)
    if not shape.text:
        return SlotRole.DECOR

    sized = sorted({s.size_pt for s in shapes if s.size_pt > 0}, reverse=True)
    rank = sized.index(shape.size_pt) if shape.size_pt in sized else len(sized)

    in_upper_half = shape.box.y < slide_h * 0.45
    if rank == 0 and in_upper_half:
        return SlotRole.TITLE
    if rank == 1 and in_upper_half:
        return SlotRole.SUBTITLE

    # Очень крупный и очень короткий текст — числовой показатель, а не абзац:
    # «91 %», «XXX%», «42». Опознаётся по форме, а не по содержанию.
    compact = len(shape.text) <= 12
    if compact and rank <= 1:
        return SlotRole.KPI_VALUE
    if compact and shape.size_pt and shape.size_pt <= (sized[-1] if sized else 0) * 1.2:
        return SlotRole.KPI_LABEL
    return SlotRole.BODY


def _collect(container: etree._Element, slide_w: int, slide_h: int) -> list[_Shape]:
    shapes: list[_Shape] = []
    min_area = slide_w * slide_h * MIN_SLOT_AREA_SHARE
    for element, box, _ in iter_shapes(container):
        if box is None or box.area < min_area:
            continue
        shapes.append(
            _Shape(
                element=element,
                box=box,
                tag=etree.QName(element).localname,
                text=_text_of(element),
                size_pt=_max_size(element),
            )
        )
    return shapes


def _cluster(values: list[int], tolerance: int) -> list[list[int]]:
    """Близкие значения в одну группу. Нужно поиску сеток по двум осям."""
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return clusters


def _grid_pitch(shapes: list[_Shape], slide_w: int) -> tuple[str, int] | None:
    """Шаг сетки по двум осям: элементы стоят рядами и колонками.

    Карточки часто разложены в две строки по три, а не в одну полосу. Такая
    раскладка не проходит ни проверку на горизонтальный шаг, ни на
    вертикальный — по каждой оси позиции повторяются.
    """
    tolerance = max(1, slide_w // 100)
    columns = _cluster([s.box.x for s in shapes], tolerance)
    rows = _cluster([s.box.y for s in shapes], tolerance)
    if len(columns) < MIN_REPEAT or len(rows) < 2:
        return None
    if len(columns) * len(rows) != len(shapes):
        return None
    centers = [round(sum(c) / len(c)) for c in columns]
    pitch = _even_pitch(centers)
    return ("grid", pitch) if pitch else None


def _even_pitch(positions: list[int]) -> int | None:
    """Шаг, если элементы расставлены равномерно. Иначе None.

    Равномерность — признак сетки. Четыре случайно похожих блока в разных
    местах слайда сеткой не являются, и раздвигать их числом элементов нельзя.
    """
    if len(positions) < MIN_REPEAT:
        return None
    steps = [b - a for a, b in pairwise(positions)]
    if not steps or min(steps) <= 0:
        return None
    average = sum(steps) / len(steps)
    if any(abs(step - average) > average * PITCH_TOLERANCE for step in steps):
        return None
    return round(average)


def _find_repeaters(
    shapes: list[_Shape], slide_index: int, slide_w: int, slide_h: int
) -> tuple[list[Repeater], set[int]]:
    """Группы одинаковой структуры, расставленные равномерно.

    Возвращает повторители и идентификаторы использованных фигур, чтобы те не
    попали ещё и в обычные слоты.
    """
    by_signature: dict[tuple, list[_Shape]] = defaultdict(list)
    for shape in shapes:
        if shape.tag in ("grpSp", "sp") and shape.box.area > 0:
            by_signature[_signature(shape.element, shape.box)].append(shape)

    repeaters: list[Repeater] = []
    consumed: set[int] = set()

    # Сначала самые крупные композиции: внешняя группа должна забрать свои
    # фигуры раньше, чем её же дети образуют собственный «повтор».
    candidates = sorted(
        by_signature.values(), key=lambda members: -members[0].box.area
    )
    for index, members in enumerate(candidates):
        if len(members) < MIN_REPEAT:
            continue
        if any(id(shape.element) in consumed for shape in members):
            continue
        horizontal = sorted(members, key=lambda s: s.box.x)
        vertical = sorted(members, key=lambda s: s.box.y)

        pitch = _even_pitch([s.box.x for s in horizontal])
        axis, ordered = "horizontal", horizontal
        if pitch is None:
            pitch = _even_pitch([s.box.y for s in vertical])
            axis, ordered = "vertical", vertical
        if pitch is None:
            found = _grid_pitch(members, slide_w)
            if found is None:
                continue
            axis, pitch, ordered = found[0], found[1], horizontal

        first = ordered[0]
        item_slots = _slots_of(first, shapes, slide_h, prefix=f"r{index}")
        if not item_slots:
            continue

        span = slide_h if axis == "vertical" else slide_w
        item_size = first.box.h if axis == "vertical" else first.box.w
        if pitch < item_size * MIN_PITCH_SHARE:
            continue
        gutter = max(0, pitch - item_size)
        # Сколько элементов физически помещается в полосу с тем же шагом.
        fits = max(len(ordered), int(span // pitch)) if pitch else len(ordered)

        repeaters.append(
            Repeater(
                id=f"s{slide_index}_rep{index}",
                item_slots=item_slots,
                item_box=first.box,
                axis=axis,
                observed_count=len(ordered),
                min_count=max(1, len(ordered) - COUNT_SLACK),
                max_count=min(fits, len(ordered) + COUNT_SLACK),
                pitch_emu=pitch,
                gutter_emu=gutter,
                provenance=Provenance(
                    kind=SourceKind.SLIDE,
                    ref=f"slide{slide_index}",
                    note=f"{len(ordered)} одинаковых элементов с равным шагом",
                ),
            )
        )
        for shape in members:
            consumed.add(id(shape.element))
            consumed.update(
                id(element) for element, _, _ in iter_shapes(shape.element)
            )

    return _merge_repeaters(repeaters), consumed


def _merge_repeaters(repeaters: list[Repeater]) -> list[Repeater]:
    """Сливает повторители, описывающие один и тот же компонент.

    Карточка состоит из нескольких фигур — подложки, заголовка, подписи, — и
    каждая из них образует собственный ряд с тем же шагом. Это одна сетка,
    а не три: слоты у них складываются, а геометрия общая.
    """
    merged: list[Repeater] = []
    for repeater in sorted(repeaters, key=lambda r: -r.item_box.area):
        twin = next(
            (
                existing
                for existing in merged
                if existing.axis == repeater.axis
                and existing.observed_count == repeater.observed_count
                and abs(existing.pitch_emu - repeater.pitch_emu)
                <= existing.pitch_emu * PITCH_TOLERANCE
            ),
            None,
        )
        if twin is None:
            merged.append(repeater)
            continue
        slots = list(twin.item_slots)
        known = {slot.box for slot in slots}
        slots.extend(slot for slot in repeater.item_slots if slot.box not in known)
        merged[merged.index(twin)] = twin.model_copy(update={"item_slots": slots})
    return merged


def _slots_of(
    parent: _Shape, all_shapes: list[_Shape], slide_h: int, prefix: str
) -> list[Slot]:
    """Слоты внутри одного элемента повторителя."""
    inner = [
        _Shape(
            element=element,
            box=box,
            tag=etree.QName(element).localname,
            text=_text_of(element),
            size_pt=_max_size(element),
        )
        for element, box, _ in iter_shapes(parent.element)
        if box is not None
    ]
    # Повторяется не только группа. Ряд одинаковых текстовых блоков — та же
    # сетка, просто её элемент неделим: тогда слотом становится он сам.
    if not inner:
        inner = [parent]
    slots: list[Slot] = []
    for position, shape in enumerate(inner):
        role = _role_from_geometry(shape, inner or all_shapes, slide_h)
        if role is SlotRole.DECOR:
            continue
        slots.append(
            Slot(
                id=f"{prefix}_{position}",
                role=role,
                box=shape.box,
                placeholder_text=shape.text[:80],
                provenance=Provenance(kind=SourceKind.SLIDE, ref="элемент повторителя"),
            )
        )
    return slots


def mine_slide(
    container: etree._Element,
    slide_index: int,
    slide_w: int,
    slide_h: int,
    layout_id: str | None,
    default_style: TextStyle,
    is_dark: bool = False,
) -> Pattern | None:
    """Разбирает слайд-пример в композиционный паттерн."""
    shapes = _collect(container, slide_w, slide_h)
    if not shapes:
        return None

    repeaters, consumed = _find_repeaters(shapes, slide_index, slide_w, slide_h)

    slots: list[Slot] = []
    for position, shape in enumerate(shapes):
        if id(shape.element) in consumed or shape.tag == "grpSp":
            continue
        role = _role_from_geometry(shape, shapes, slide_h)
        if role is SlotRole.DECOR:
            continue
        slots.append(
            Slot(
                id=f"s{slide_index}_{position}",
                role=role,
                box=shape.box,
                style=default_style,
                placeholder_text=shape.text[:80],
                provenance=Provenance(kind=SourceKind.SLIDE, ref=f"slide{slide_index}"),
            )
        )

    if not slots and not repeaters:
        return None

    boxes = [slot.box for slot in slots] + [r.item_box for r in repeaters]
    # Область обрезается по слайду: дизайнеры выпускают фигуры за обрез, и
    # объемлющий прямоугольник тогда выходит за холст. Класть туда содержание
    # нельзя — часть его окажется за краем, а аудит справедливо это найдёт.
    left = max(0, min(b.x for b in boxes))
    top = max(0, min(b.y for b in boxes))
    right = min(slide_w, max(b.right for b in boxes))
    bottom = min(slide_h, max(b.bottom for b in boxes))
    content_area = Box(x=left, y=top, w=max(1, right - left), h=max(1, bottom - top))

    return Pattern(
        id=f"pattern{slide_index}",
        pattern_class=PatternClass.FREEFORM,
        layout_id=layout_id,
        donor_slide_index=slide_index,
        slots=slots,
        repeaters=repeaters,
        content_area=content_area,
        is_dark=is_dark,
        provenance=Provenance(
            kind=SourceKind.SLIDE,
            ref=f"slide{slide_index}",
            note="композиция снята со слайда-примера",
        ),
    )
