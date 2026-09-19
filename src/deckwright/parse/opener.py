"""Минимальный разбор шаблона. Скелетная версия слоя parse.

Берёт то, что лежит на поверхности и не требует статистики: размер слайда,
палитру и гарнитуры темы, layout'ы с плейсхолдерами. Этого хватает, чтобы
сквозной прогон дошёл до `.pptx`, и недостаточно, чтобы результат выглядел как
шаблон.

Фаза 3 заменит здесь почти всё: палитру и гарнитуры — статистикой
употребления, а не чтением темы (у одного из трёх шаблонов датасета в
`clrScheme` стоковая офисная палитра); layout'ы — библиотекой паттернов,
снятых со слайдов-примеров. Провенанс `THEME` на токенах не украшение: по нему
фаза 3 отличит то, что она уточнила, от того, что осталось с этого прохода.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.presentation import Presentation as PresentationObject

from deckwright.schemas import (
    Box,
    Color,
    ColorToken,
    FontToken,
    LayoutSpec,
    Provenance,
    Slot,
    SlotRole,
    SourceKind,
    TemplateSpec,
    TextStyle,
)

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"

# Тип плейсхолдера OOXML → роль слота. Заголовок и подзаголовок опознаются
# однозначно; всё остальное на этом проходе считается телом, а настоящее
# разделение ролей приходит в фазе 3 вместе с паттернами.
_PH_ROLE: dict[str, SlotRole] = {
    "title": SlotRole.TITLE,
    "ctrTitle": SlotRole.TITLE,
    "subTitle": SlotRole.SUBTITLE,
    "body": SlotRole.BODY,
    "obj": SlotRole.BODY,
    "tbl": SlotRole.TABLE,
    "chart": SlotRole.CHART,
    "pic": SlotRole.IMAGE,
    "ftr": SlotRole.FOOTER,
    "sldNum": SlotRole.SLIDE_NUMBER,
    "dt": SlotRole.FOOTER,
}

_DEFAULT_SCALE_PT = (9.0, 12.0, 14.0, 18.0, 24.0, 32.0, 44.0)


def file_sha256(path: str | Path) -> str:
    """Хэш файла шаблона: ключ кэша и поле манифеста."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _theme_root(prs: PresentationObject) -> etree._Element | None:
    master = prs.slide_masters[0] if len(prs.slide_masters) else None
    if master is None:
        return None
    for rel in master.part.rels.values():
        if rel.reltype.endswith("/theme"):
            return etree.fromstring(rel.target_part.blob)
    return None


def _theme_palette(theme: etree._Element | None) -> list[ColorToken]:
    if theme is None:
        return []
    scheme = theme.find(f".//{{{A_NS}}}clrScheme")
    if scheme is None:
        return []
    tokens: list[ColorToken] = []
    for entry in scheme:
        slot_name = etree.QName(entry).localname
        srgb = entry.find(f"{{{A_NS}}}srgbClr")
        sys_clr = entry.find(f"{{{A_NS}}}sysClr")
        value = (
            srgb.get("val")
            if srgb is not None
            else (sys_clr.get("lastClr") if sys_clr is not None else None)
        )
        if not value:
            continue
        tokens.append(
            ColorToken(
                color=Color(rgb=value, scheme=slot_name),
                usage_count=0,
                provenance=Provenance(
                    kind=SourceKind.THEME,
                    ref=f"clrScheme/{slot_name}",
                    note="объявление темы; фаза 3 уточнит статистикой употребления",
                ),
            )
        )
    return tokens


def _theme_fonts(theme: etree._Element | None) -> list[FontToken]:
    if theme is None:
        return []
    families: list[str] = []
    for role in ("majorFont", "minorFont"):
        latin = theme.find(f".//{{{A_NS}}}{role}/{{{A_NS}}}latin")
        if latin is not None and latin.get("typeface"):
            families.append(latin.get("typeface"))
    seen: list[str] = []
    for family in families:
        if family not in seen:
            seen.append(family)
    return [FontToken(family=family, usage_count=0) for family in seen]


def _theme_colors(theme: etree._Element | None) -> dict[str, str]:
    """{имя слота темы: RRGGBB}. Словарь для резолва `schemeClr`."""
    if theme is None:
        return {}
    scheme = theme.find(f".//{{{A_NS}}}clrScheme")
    if scheme is None:
        return {}
    resolved: dict[str, str] = {}
    for entry in scheme:
        srgb = entry.find(f"{{{A_NS}}}srgbClr")
        sys_clr = entry.find(f"{{{A_NS}}}sysClr")
        value = (
            srgb.get("val")
            if srgb is not None
            else (sys_clr.get("lastClr") if sys_clr is not None else None)
        )
        if value:
            resolved[etree.QName(entry).localname] = value
    return resolved


def _color_map(master) -> dict[str, str]:
    """`p:clrMap` мастера: `bg1`/`tx1` → слот темы.

    Фигуры ссылаются на цвета через `bg1`, `tx1`, `bg2`, `tx2`, а те через
    карту мастера указывают на `lt1`, `dk1` и так далее. Без этого шага
    `schemeClr val="bg1"` не резолвится ни во что.
    """
    clr_map = master._element.find(f"{{{P_NS}}}clrMap")
    if clr_map is None:
        return {}
    return dict(clr_map.attrib)


def _resolve_fill(
    element: etree._Element | None,
    theme: dict[str, str],
    clr_map: dict[str, str],
) -> Color | None:
    """Первый сплошной цвет внутри элемента, приведённый к RGB."""
    if element is None:
        return None
    fill = element.find(f".//{{{A_NS}}}solidFill")
    if fill is None:
        return None
    srgb = fill.find(f"{{{A_NS}}}srgbClr")
    if srgb is not None and srgb.get("val"):
        return Color(rgb=srgb.get("val"))
    scheme = fill.find(f"{{{A_NS}}}schemeClr")
    if scheme is not None and scheme.get("val"):
        name = scheme.get("val")
        slot = clr_map.get(name, name)
        value = theme.get(slot)
        if value:
            return Color(rgb=value, scheme=slot)
    return None


def _layout_background(
    layout, theme: dict[str, str], clr_map: dict[str, str]
) -> Color | None:
    """Цвет фона layout'а, если он задан сплошной заливкой.

    Фон-картинка или градиент цвета не дают — тогда None, и вёрстка возьмёт
    цвет текста из самого шаблона, а не из яркости фона.
    """
    return _resolve_fill(layout._element.find(f".//{{{P_NS}}}bg"), theme, clr_map)


def _placeholder_text_color(
    element: etree._Element, theme: dict[str, str], clr_map: dict[str, str]
) -> Color | None:
    """Цвет, которым сам шаблон пишет текст в этом плейсхолдере.

    Это надёжнее, чем выводить цвет из яркости фона: шаблон уже решил, каким
    цветом здесь писать, и решение учитывает градиенты, картинки и декор,
    про которые мы ничего не знаем. На `vk_workspace` и `vk_tech` это `lt1`,
    на `vk_education` — `dk1`.
    """
    for node in element.iter():
        if node.tag in (f"{{{A_NS}}}defRPr", f"{{{A_NS}}}rPr", f"{{{A_NS}}}endParaRPr"):
            color = _resolve_fill(node, theme, clr_map)
            if color is not None:
                return color
    return None


def _placeholder_role(element: etree._Element) -> SlotRole:
    ph = element.xpath(".//*[local-name()='ph']")
    if not ph:
        return SlotRole.UNKNOWN
    return _PH_ROLE.get(ph[0].get("type", "body"), SlotRole.BODY)


def _layout_slots(
    layout,
    default_font: str,
    theme: dict[str, str],
    clr_map: dict[str, str],
    fallback_text: Color,
) -> list[Slot]:
    slots: list[Slot] = []
    for shape in layout.placeholders:
        if None in (shape.left, shape.top, shape.width, shape.height):
            continue
        if shape.width <= 0 or shape.height <= 0:
            continue
        fmt = shape.placeholder_format
        slots.append(
            Slot(
                id=f"ph{fmt.idx}",
                role=_placeholder_role(shape._element),
                box=Box(x=shape.left, y=shape.top, w=shape.width, h=shape.height),
                style=TextStyle(
                    font_family=default_font,
                    size_pt=18.0,
                    color=_placeholder_text_color(shape._element, theme, clr_map)
                    or fallback_text,
                ),
                placeholder_text=shape.text_frame.text if shape.has_text_frame else "",
                ph_idx=fmt.idx,
                provenance=Provenance(kind=SourceKind.LAYOUT, ref=layout.name),
            )
        )
    return slots


def parse_template(path: str | Path) -> TemplateSpec:
    """Открывает `.pptx` и снимает с него то, что доступно без статистики."""
    path = Path(path)
    prs = Presentation(str(path))
    theme = _theme_root(prs)

    fonts = _theme_fonts(theme)
    default_font = fonts[0].family if fonts else "Arial"

    theme_colors = _theme_colors(theme)

    layouts: list[LayoutSpec] = []
    masters: list[str] = []
    for m_index, master in enumerate(prs.slide_masters):
        master_id = f"master{m_index + 1}"
        masters.append(master_id)
        clr_map = _color_map(master)
        master_bg = _resolve_fill(
            master._element.find(f".//{{{P_NS}}}bg"), theme_colors, clr_map
        )
        for l_index, layout in enumerate(master.slide_layouts):
            background = _layout_background(layout, theme_colors, clr_map) or master_bg
            # Цвет текста для слотов, у которых шаблон его не задал: по яркости
            # фона, если фон известен, иначе тёмный.
            fallback_text = (
                Color(rgb="FFFFFF")
                if background is not None and background.luminance < 0.5
                else Color(rgb="111111")
            )
            layouts.append(
                LayoutSpec(
                    id=f"{master_id}/layout{l_index + 1}",
                    name=layout.name,
                    master_id=master_id,
                    slots=_layout_slots(
                        layout, default_font, theme_colors, clr_map, fallback_text
                    ),
                    background=background,
                    is_dark=background is not None and background.luminance < 0.5,
                )
            )

    warnings: list[str] = []
    if not any(
        slot.role not in (SlotRole.TITLE, SlotRole.FOOTER, SlotRole.SLIDE_NUMBER)
        for layout in layouts
        for slot in layout.slots
    ):
        # Не поломка, а известное свойство датасета: у большинства layout'ов
        # нет ни одного контентного плейсхолдера. Вёрстка на них опереться
        # не сможет, композиции придётся брать со слайдов-примеров (фаза 3).
        warnings.append(
            "ни в одном layout'е нет контентных плейсхолдеров: "
            "композиции нужно добывать из слайдов-примеров"
        )

    return TemplateSpec(
        template_sha256=file_sha256(path),
        source_name=path.name,
        slide_width_emu=prs.slide_width,
        slide_height_emu=prs.slide_height,
        palette=_theme_palette(theme),
        fonts=fonts,
        type_scale_pt=list(_DEFAULT_SCALE_PT),
        masters=masters,
        layouts=layouts,
        warnings=warnings,
    )
