"""Абсолютная геометрия фигур, включая вложенные в группы.

Координаты фигуры внутри `grpSp` заданы не в системе слайда, а в собственной
системе координат группы. Группа объявляет два прямоугольника: `off`/`ext` —
куда она встаёт на слайде, и `chOff`/`chExt` — в каких координатах заданы её
дети. Между ними масштаб, и без его применения дети «висят» там, где их
записал редактор, а не там, где они нарисованы.

Цена ошибки конкретная: детерминированные проверки «элемент вышел за границы»
и «два блока наложились» считают по этим координатам. На карточной сетке
`vk_tech` (слайд 21) четыре группы стоят на 0.56″, 2.91″, 5.26″ и 7.63″, а
внутри у всех четырёх дети записаны от 0.56″ — то есть без пересчёта три
карточки из четырёх окажутся в одном месте, и аудит найдёт наложения, которых
на слайде нет.

Поворот учитывается расширением прямоугольника до объемлющего: повёрнутая
фигура занимает больше места, чем её собственные ширина и высота. Отражения
на габариты не влияют.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from lxml import etree

from deckwright.schemas import Box

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Угол в OOXML задан в шестидесятитысячных долях градуса.
ANGLE_UNITS_PER_DEGREE = 60_000


@dataclass(frozen=True)
class Transform:
    """Преобразование из системы координат группы в систему её родителя."""

    offset_x: int
    offset_y: int
    scale_x: float
    scale_y: float
    child_x: int
    child_y: int

    def apply(self, x: int, y: int) -> tuple[int, int]:
        return (
            round(self.offset_x + (x - self.child_x) * self.scale_x),
            round(self.offset_y + (y - self.child_y) * self.scale_y),
        )

    def scale(self, w: int, h: int) -> tuple[int, int]:
        return max(1, round(w * self.scale_x)), max(1, round(h * self.scale_y))

    def compose(self, inner: Transform) -> Transform:
        """Преобразование вложенной группы в системе самого верхнего родителя."""
        offset_x, offset_y = self.apply(inner.offset_x, inner.offset_y)
        return Transform(
            offset_x=offset_x,
            offset_y=offset_y,
            scale_x=self.scale_x * inner.scale_x,
            scale_y=self.scale_y * inner.scale_y,
            child_x=inner.child_x,
            child_y=inner.child_y,
        )


# Тождественное преобразование: система координат самого слайда.
IDENTITY = Transform(offset_x=0, offset_y=0, scale_x=1.0, scale_y=1.0, child_x=0, child_y=0)


def _int(element: etree._Element | None, name: str, default: int = 0) -> int:
    if element is None:
        return default
    raw = element.get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def group_transform(group_element: etree._Element) -> Transform:
    """Читает `a:xfrm` группы. Без него дети остаются в своих координатах."""
    xfrm = group_element.find(f".//{{{A_NS}}}xfrm")
    if xfrm is None:
        return IDENTITY

    off = xfrm.find(f"{{{A_NS}}}off")
    ext = xfrm.find(f"{{{A_NS}}}ext")
    child_off = xfrm.find(f"{{{A_NS}}}chOff")
    child_ext = xfrm.find(f"{{{A_NS}}}chExt")

    ext_cx, ext_cy = _int(ext, "cx", 1), _int(ext, "cy", 1)
    child_cx, child_cy = _int(child_ext, "cx", 0), _int(child_ext, "cy", 0)

    return Transform(
        offset_x=_int(off, "x"),
        offset_y=_int(off, "y"),
        # Нулевой chExt означает, что масштаба нет — а не деление на ноль.
        scale_x=(ext_cx / child_cx) if child_cx else 1.0,
        scale_y=(ext_cy / child_cy) if child_cy else 1.0,
        child_x=_int(child_off, "x"),
        child_y=_int(child_off, "y"),
    )


def _rotated_extent(w: int, h: int, degrees: float) -> tuple[int, int]:
    """Габариты объемлющего прямоугольника для повёрнутой фигуры."""
    radians = math.radians(degrees)
    cos, sin = abs(math.cos(radians)), abs(math.sin(radians))
    return max(1, round(w * cos + h * sin)), max(1, round(w * sin + h * cos))


def shape_box(element: etree._Element, transform: Transform = IDENTITY) -> Box | None:
    """Абсолютный прямоугольник фигуры. None — если геометрия не задана.

    Фигура без `a:off`/`a:ext` наследует положение от плейсхолдера layout'а;
    разрешать это наследование здесь нельзя, поэтому возвращается None, и
    вызывающий решает, откуда взять геометрию.
    """
    xfrm = element.find(f".//{{{A_NS}}}xfrm")
    if xfrm is None:
        return None
    off, ext = xfrm.find(f"{{{A_NS}}}off"), xfrm.find(f"{{{A_NS}}}ext")
    if off is None or ext is None:
        return None

    w, h = _int(ext, "cx"), _int(ext, "cy")
    if w <= 0 or h <= 0:
        return None

    x, y = transform.apply(_int(off, "x"), _int(off, "y"))
    w, h = transform.scale(w, h)

    rotation = _int(xfrm, "rot") / ANGLE_UNITS_PER_DEGREE
    if rotation % 180:
        # Поворот идёт вокруг центра, поэтому центр сохраняется, а габариты
        # растут: иначе повёрнутый блок «не выходит» за границу, которую он
        # на самом деле пересекает.
        center_x, center_y = x + w // 2, y + h // 2
        w, h = _rotated_extent(w, h, rotation)
        x, y = center_x - w // 2, center_y - h // 2

    return Box(x=x, y=y, w=w, h=h)


def iter_shapes(
    container: etree._Element, transform: Transform = IDENTITY
) -> list[tuple[etree._Element, Box | None, Transform]]:
    """Все фигуры дерева с уже применёнными преобразованиями групп.

    Группы раскрываются: наружу выходят их листья, каждый со своим абсолютным
    прямоугольником. Сама группа тоже возвращается — она нужна поиску
    повторяющихся компонентов, где важна структура, а не только листья.
    """
    found: list[tuple[etree._Element, Box | None, Transform]] = []
    for child in container:
        tag = etree.QName(child).localname
        if tag == "grpSp":
            found.append((child, shape_box(child, transform), transform))
            found.extend(iter_shapes(child, transform.compose(group_transform(child))))
        elif tag in ("sp", "pic", "graphicFrame", "cxnSp"):
            found.append((child, shape_box(child, transform), transform))
    return found
