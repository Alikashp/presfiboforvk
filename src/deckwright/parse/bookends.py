"""Обложка и финал шаблона — слайды, которые колода берёт целиком.

Первый и последний слайды колоды — это титул и финал самого шаблона, с его
фоном, картинкой и типографикой; меняется только текст. Собранный из
контентной композиции титул выглядит внутренним слайдом: на `vk_education`
белый лист вместо синей обложки, на `vk_workspace` заголовок налезал на
подзаголовок.

Служебный слайд опознаётся по структуре, не по именам и не по тексту:
объявлен плейсхолдер заголовка, текста и фигур мало, данных нет. Много
текста — слайд-инструкция дизайнера («как пользоваться шаблоном»), а не
обложка.

* Обложка — первый слайд файла, если он такой. Иначе — слайд с объявленным
  заголовком титула (`ctrTitle`) или на layout'е типа `title`. Иначе её нет,
  и титул собирается из обычной композиции.
* Финал — последний такой слайд файла, кроме обложки, с заголовком в теле
  слайда, а не в шапке. Стоит он не всегда в конце: на `vk_tech` «Спасибо
  за внимание» идут сразу за обложками.
"""

from __future__ import annotations

from lxml import etree

from deckwright.schemas import (
    Box,
    Pattern,
    PatternClass,
    Provenance,
    Slot,
    SlotRole,
    SourceKind,
    TextStyle,
)

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Больше текста — это уже содержание или инструкция, а не обложка.
MAX_TEXTS = 5
MAX_CHARS = 200
MAX_SHAPES = 8

# Насколько может вырасти рамка текста под заголовком: втрое, но не больше
# четверти слайда и не ниже нижних десяти процентов (там логотип и колонтитул).
GROWTH = 3
GROWTH_SHARE = 0.25
BOTTOM_SHARE = 0.9

# Центр заголовка ниже этой доли высоты — заголовок в теле слайда, не в шапке.
TITLE_BODY_SHARE = 0.25

_TITLE_TYPES = {"title", "ctrTitle"}
# Плейсхолдеры, которые шаблон держит для себя.
_OWN_TYPES = {"dt", "ftr", "sldNum"}


def _ph_type(element: etree._Element) -> str | None:
    ph = element.find(f"./{{{P_NS}}}nvSpPr/{{{P_NS}}}nvPr/{{{P_NS}}}ph")
    if ph is None:
        return None
    return ph.get("type", "body")


def _text(element: etree._Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{{{A_NS}}}t")).strip()


def is_bookend(slide) -> bool:
    """Похож ли слайд на обложку или финал: заголовок, мало текста, нет данных."""
    tree = slide.shapes._spTree
    if not any(_ph_type(sp) in _TITLE_TYPES for sp in tree.iter(f"{{{P_NS}}}sp")):
        return False
    if any(True for _ in tree.iter(f"{{{P_NS}}}graphicFrame")):
        return False
    texts = [text for sp in tree.iter(f"{{{P_NS}}}sp") if (text := _text(sp))]
    return (
        len(texts) <= MAX_TEXTS
        and sum(len(text) for text in texts) <= MAX_CHARS
        and len(slide.shapes) <= MAX_SHAPES
    )


def _declares_cover(slide) -> bool:
    tree = slide.shapes._spTree
    if any(_ph_type(sp) == "ctrTitle" for sp in tree.iter(f"{{{P_NS}}}sp")):
        return True
    return slide.slide_layout._element.get("type") == "title"


def _title_in_body(slide, slide_h: int) -> bool:
    """Стоит ли заголовок в теле слайда, а не в его шапке.

    У содержательного слайда заголовок наверху, у финала — посередине или
    ниже: на `vk_tech` разреженный слайд с картинкой (43) иначе выбирался
    финалом вместо «Спасибо за внимание».
    """
    for shape in slide.placeholders:
        if _ph_type(shape._element) in _TITLE_TYPES and shape.height:
            center = (shape.top or 0) + shape.height / 2
            return center >= slide_h * TITLE_BODY_SHARE
    return False


def _grown(slots: list[Slot], slide_h: int) -> list[Slot]:
    """Рамки текста под заголовком — с запасом вниз.

    Подзаголовок обложки рассчитан на одну строку шаблона («Разработчик
    корпоративного ПО»): на `vk_tech` 0.31 дюйма. Две строки нашего
    подзаголовка ужимались в нём до 8 pt. Рамка растёт вниз до ближайшего
    места под ней, но не больше чем втрое и не больше четверти слайда.
    """
    grown = []
    for slot in slots:
        if slot.role is SlotRole.TITLE:
            grown.append(slot)
            continue
        below = [
            other.box.y
            for other in slots
            if other is not slot
            and other.box.y >= slot.box.bottom
            and min(other.box.right, slot.box.right) > max(other.box.x, slot.box.x)
        ]
        limit = min([*below, int(slide_h * BOTTOM_SHARE)])
        height = min(
            slot.box.h * GROWTH, slot.box.h + int(slide_h * GROWTH_SHARE), limit - slot.box.y
        )
        if height > slot.box.h:
            slot = slot.model_copy(update={"box": slot.box.model_copy(update={"h": height})})
        grown.append(slot)
    return grown


def find_bookends(slides: list, slide_h: int) -> tuple[int | None, int | None]:
    """Номера (с единицы) обложки и финала среди слайдов шаблона."""
    candidates = [index for index, slide in enumerate(slides, start=1) if is_bookend(slide)]
    cover: int | None = None
    if candidates and candidates[0] == 1:
        cover = 1
    else:
        cover = next(
            (index for index in candidates if _declares_cover(slides[index - 1])), None
        )
    closing = next(
        (
            index
            for index in reversed(candidates)
            if index != cover and _title_in_body(slides[index - 1], slide_h)
        ),
        None,
    )
    return cover, closing


def bookend_pattern(
    slide,
    index: int,
    pattern_class: PatternClass,
    layout_id: str | None,
    layout_slots: list[Slot],
    default_style: TextStyle,
    slide_w: int,
    slide_h: int,
    is_dark: bool,
) -> Pattern | None:
    """Композиция служебного слайда: его плейсхолдеры и надписи — места под текст.

    Рамка плейсхолдера без своей берётся из layout'а (python-pptx наследует
    её сам), стиль — из плейсхолдера layout'а с тем же номером: кегль титула
    задаёт он.
    """
    by_idx = {slot.ph_idx: slot for slot in layout_slots if slot.ph_idx is not None}
    slots: list[Slot] = []
    for shape in slide.shapes:
        element = shape._element
        kind = _ph_type(element) if etree.QName(element).localname == "sp" else None
        is_text = etree.QName(element).localname == "sp" and element.find(
            f"./{{{P_NS}}}txBody"
        ) is not None
        if kind in _OWN_TYPES or not is_text:
            continue
        if kind is None and not _text(element):
            continue
        if not (shape.width and shape.height):
            continue
        box = Box(x=shape.left or 0, y=shape.top or 0, w=shape.width, h=shape.height)
        ph_idx = shape.placeholder_format.idx if shape.is_placeholder else None
        inherited = by_idx.get(ph_idx)
        role = SlotRole.TITLE if kind in _TITLE_TYPES else SlotRole.BODY
        slots.append(
            Slot(
                id=f"b{index}_{len(slots)}",
                role=role,
                box=box,
                style=inherited.style if inherited is not None else None,
                placeholder_text=_text(element)[:80],
                ph_idx=ph_idx,
                provenance=Provenance(kind=SourceKind.SLIDE, ref=f"slide{index}"),
            )
        )
    titles = [slot for slot in slots if slot.role is SlotRole.TITLE]
    if not titles:
        return None
    # Заголовок один — самый крупный; остальные «заголовки» станут текстом.
    main = max(titles, key=lambda slot: slot.box.area)
    slots = [
        slot if slot is main or slot.role is not SlotRole.TITLE
        else slot.model_copy(update={"role": SlotRole.BODY})
        for slot in slots
    ]
    # Порядок мест: заголовок, потом то, что под ним, потом остальное. На
    # финале `vk_workspace` над заголовком стоит квадрат «QR-code», и текст
    # по порядку чтения уходил в него.
    slots.sort(
        key=lambda slot: (
            slot is not main,
            slot.box.y < main.box.y,
            slot.box.y,
            slot.box.x,
        )
    )
    slots = _grown(slots, slide_h)
    if main.style is None:
        slots = [
            slot.model_copy(update={"style": default_style}) if slot.style is None else slot
            for slot in slots
        ]
    return Pattern(
        id=f"{pattern_class.value}{index}",
        pattern_class=pattern_class,
        layout_id=layout_id,
        donor_slide_index=index,
        slots=slots,
        content_area=Box(x=0, y=0, w=slide_w, h=slide_h),
        is_dark=is_dark,
        provenance=Provenance(
            kind=SourceKind.DERIVED,
            ref=f"slide{index}",
            note="служебный слайд шаблона: объявлен заголовок, мало текста, нет данных",
        ),
    )

