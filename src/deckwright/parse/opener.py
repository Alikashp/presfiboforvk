"""Разбор шаблона в `TemplateSpec`. Вход слоя parse, один на весь пайплайн.

Модуль только собирает результат из частей и кэширует его. Вся добыча знаний
живёт в соседях: `tokens` — палитра, гарнитуры, шкала и поля;
`patterns` — композиции со слайдов-примеров; `recurring` — логотип и
колонтитул; `fonts` — встроенные шрифты; `semantics` — класс композиции.

Кэш по хэшу файла нужен бюджету времени: разбор колоды на полсотни слайдов с
двумя сотнями картинок занимает секунды, а три варианта вёрстки разбирают один
и тот же шаблон трижды.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.presentation import Presentation as PresentationObject

from deckwright.parse import tokens as tokens_mod
from deckwright.parse.fonts import extract_embedded_fonts
from deckwright.parse.patterns import mine_slide
from deckwright.parse.recurring import find_recurring
from deckwright.parse.semantics import classified
from deckwright.schemas import (
    Box,
    Color,
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

# Тип плейсхолдера OOXML → роль слота.
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

# Кегль по умолчанию, когда шаблон не сказал ничего: нужен только чтобы
# собрать валидный стиль, реальные значения приходят из шкалы.
FALLBACK_SIZE_PT = 18.0


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _theme_root(prs: PresentationObject) -> etree._Element | None:
    if not len(prs.slide_masters):
        return None
    for rel in prs.slide_masters[0].part.rels.values():
        if rel.reltype.endswith("/theme"):
            return etree.fromstring(rel.target_part.blob)
    return None


def _theme_families(theme: etree._Element | None) -> list[str]:
    if theme is None:
        return []
    families: list[str] = []
    for role in ("majorFont", "minorFont"):
        latin = theme.find(f".//{{{A_NS}}}{role}/{{{A_NS}}}latin")
        typeface = latin.get("typeface") if latin is not None else None
        if typeface and typeface not in families:
            families.append(typeface)
    return families


def _shape_tree(element: etree._Element) -> etree._Element | None:
    return element.find(f".//{{{P_NS}}}spTree")


def _placeholder_role(element: etree._Element) -> SlotRole:
    ph = element.findall(f".//{{{P_NS}}}ph")
    if not ph:
        return SlotRole.UNKNOWN
    return _PH_ROLE.get(ph[0].get("type", "body"), SlotRole.BODY)


def _text_color(
    element: etree._Element, theme: dict[str, str], clr_map: dict[str, str]
) -> Color | None:
    """Цвет, которым сам шаблон пишет текст в этом плейсхолдере.

    Надёжнее вывода по яркости фона: шаблон уже принял решение с учётом
    градиентов и фоновых картинок, о которых парсер ничего не знает.
    """
    for node in element.iter():
        if etree.QName(node).localname in ("defRPr", "rPr", "endParaRPr"):
            color = tokens_mod.resolve_color(node, theme, clr_map)
            if color is not None:
                return color
    return None


def _layout_slots(layout, style: TextStyle, theme, clr_map, fallback: Color) -> list[Slot]:
    slots: list[Slot] = []
    for shape in layout.placeholders:
        if None in (shape.left, shape.top, shape.width, shape.height):
            continue
        if shape.width <= 0 or shape.height <= 0:
            continue
        fmt = shape.placeholder_format
        color = _text_color(shape._element, theme, clr_map) or fallback
        slots.append(
            Slot(
                id=f"ph{fmt.idx}",
                role=_placeholder_role(shape._element),
                box=Box(x=shape.left, y=shape.top, w=shape.width, h=shape.height),
                style=style.model_copy(update={"color": color}),
                placeholder_text=shape.text_frame.text if shape.has_text_frame else "",
                ph_idx=fmt.idx,
                provenance=Provenance(kind=SourceKind.LAYOUT, ref=layout.name),
            )
        )
    return slots


def _parse(path: Path, font_dir: Path | None) -> TemplateSpec:
    prs = Presentation(str(path))
    slide_w, slide_h = prs.slide_width, prs.slide_height
    theme_root = _theme_root(prs)
    theme = tokens_mod.theme_colors(theme_root)
    warnings: list[str] = []

    # ── Статистика по слайдам, layout'ам и мастерам ──────────────────────────
    usage = tokens_mod.Usage()
    for master in prs.slide_masters:
        clr_map = tokens_mod.color_map(master._element)
        trees = [_shape_tree(master._element)]
        trees += [_shape_tree(layout._element) for layout in master.slide_layouts]
        tokens_mod.collect(
            [t for t in trees if t is not None],
            theme,
            clr_map,
            slide_w,
            slide_h,
            usage,
            on_slide=False,
        )
    primary_map = (
        tokens_mod.color_map(prs.slide_masters[0]._element) if len(prs.slide_masters) else {}
    )
    tokens_mod.collect(
        [slide.shapes._spTree for slide in prs.slides],
        theme,
        primary_map,
        slide_w,
        slide_h,
        usage,
    )

    palette = tokens_mod.build_palette(usage, theme)
    fonts = tokens_mod.build_fonts(usage, _theme_families(theme_root))
    type_scale = tokens_mod.build_type_scale(usage)
    grid = tokens_mod.build_grid(usage)

    # ── Встроенные шрифты ────────────────────────────────────────────────────
    if font_dir is not None:
        extracted, font_warnings = extract_embedded_fonts(path, font_dir / path.stem)
        warnings.extend(font_warnings)
        by_family = {font.family for font in extracted}
        fonts = [
            token.model_copy(
                update={
                    "embedded": True,
                    "file_path": str(
                        next(f.path for f in extracted if f.family == token.family)
                    ),
                }
            )
            if token.family in by_family
            else token
            for token in fonts
        ]
        missing = [t.family for t in fonts if not t.embedded and t.usage_count > 0]
        if missing:
            warnings.append(
                "шрифты не встроены в шаблон и будут подставлены системой, "
                f"метрики могут разойтись: {', '.join(missing[:5])}"
            )

    base_family = fonts[0].family if fonts else "Arial"
    base_size = type_scale[len(type_scale) // 2] if type_scale else FALLBACK_SIZE_PT
    base_color = Color(rgb=palette[0].color.rgb) if palette else Color(rgb="000000")
    base_style = TextStyle(font_family=base_family, size_pt=base_size, color=base_color)

    # ── Layout'ы ─────────────────────────────────────────────────────────────
    layouts: list[LayoutSpec] = []
    masters: list[str] = []
    layout_ids: dict[int, str] = {}
    for m_index, master in enumerate(prs.slide_masters):
        master_id = f"master{m_index + 1}"
        masters.append(master_id)
        clr_map = tokens_mod.color_map(master._element)
        master_bg = tokens_mod.resolve_color(
            master._element.find(f".//{{{P_NS}}}bg"), theme, clr_map
        ) if master._element.find(f".//{{{P_NS}}}bg") is not None else None

        for l_index, layout in enumerate(master.slide_layouts):
            bg_node = layout._element.find(f".//{{{P_NS}}}bg")
            background = (
                tokens_mod.resolve_color(bg_node, theme, clr_map)
                if bg_node is not None
                else None
            ) or master_bg
            dark = background is not None and background.luminance < 0.5
            fallback = Color(rgb="FFFFFF") if dark else Color(rgb="111111")
            layout_id = f"{master_id}/layout{l_index + 1}"
            layout_ids[id(layout._element)] = layout_id
            layouts.append(
                LayoutSpec(
                    id=layout_id,
                    name=layout.name,
                    master_id=master_id,
                    slots=_layout_slots(layout, base_style, theme, clr_map, fallback),
                    background=background,
                    is_dark=dark,
                )
            )

    if not any(
        slot.role not in (SlotRole.TITLE, SlotRole.FOOTER, SlotRole.SLIDE_NUMBER)
        for layout in layouts
        for slot in layout.slots
    ):
        warnings.append(
            "ни в одном layout'е нет контентных плейсхолдеров: "
            "вёрстка опирается на паттерны со слайдов-примеров"
        )

    # ── Паттерны со слайдов-примеров ─────────────────────────────────────────
    patterns = []
    for index, slide in enumerate(prs.slides, start=1):
        pattern = mine_slide(
            slide.shapes._spTree,
            index,
            slide_w,
            slide_h,
            layout_ids.get(id(slide.slide_layout._element)),
            base_style,
        )
        if pattern is not None:
            patterns.append(classified(pattern, slide_w, slide_h))

    slide_count = len(prs.slides._sldIdLst)
    if slide_count and len(patterns) / slide_count < 0.5:
        warnings.append(
            f"композиции сняты лишь с {len(patterns)} слайдов из {slide_count}: "
            "шаблон беден примерами, вёрстка будет опираться на поля и сетку"
        )

    # ── Повторяющиеся элементы ───────────────────────────────────────────────
    per_slide: list[list[etree._Element]] = []
    for slide in prs.slides:
        trees = [slide.shapes._spTree]
        for source in (slide.slide_layout, slide.slide_layout.slide_master):
            tree = _shape_tree(source._element)
            if tree is not None:
                trees.append(tree)
        per_slide.append(trees)
    recurring = find_recurring(per_slide, slide_w, slide_h)

    return TemplateSpec(
        template_sha256=file_sha256(path),
        source_name=path.name,
        slide_width_emu=slide_w,
        slide_height_emu=slide_h,
        palette=palette,
        fonts=fonts,
        type_scale_pt=type_scale,
        grid=grid,
        masters=masters,
        layouts=layouts,
        patterns=patterns,
        recurring=recurring,
        warnings=warnings,
    )


def parse_template(
    path: str | Path,
    cache_dir: str | Path | None = None,
    font_dir: str | Path | None = None,
) -> TemplateSpec:
    """Разбирает `.pptx`. Повторный разбор того же файла берётся из кэша."""
    path = Path(path)
    cache_path: Path | None = None

    if cache_dir is not None:
        cache_path = Path(cache_dir) / f"{file_sha256(path)}.json"
        if cache_path.exists():
            try:
                return TemplateSpec.model_validate_json(cache_path.read_text("utf-8"))
            except (ValueError, json.JSONDecodeError):
                # Кэш от прежней версии схемы: разбираем заново и перезаписываем.
                cache_path.unlink(missing_ok=True)

    spec = _parse(path, Path(font_dir) if font_dir else None)

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(spec.model_dump_json(), encoding="utf-8")
    return spec
