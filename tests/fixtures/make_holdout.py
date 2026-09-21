"""Генератор враждебных holdout-шаблонов.

Синтетику пишет тот же, кто писал парсер, поэтому она ломает только
предусмотренные допущения — это её потолок, и настоящий чужой шаблон она не
заменяет (он лежит в `data/holdout/`). Её ценность в другом: каждый вариант
бьёт прицельно в одно допущение, и если парсер на нём падает, сразу понятно,
какое именно допущение неверно.

Список допущений взят из того, что оказалось правдой на датасете и потому
могло незаметно прирасти к коду:

* слайд шестнадцать к девяти;
* светлый фон;
* имена layout'ов по-русски;
* layout'ы вообще есть;
* цвета и гарнитуры видны на слайдах;
* слайдов-примеров много;
* шрифт шаблона есть в системе, и встроенную копию можно распаковать.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

EMU_PER_INCH = 914_400


@dataclass(frozen=True)
class Variant:
    """Один враждебный вариант и допущение, которое он ломает."""

    name: str
    breaks: str
    width_in: float = 13.333
    height_in: float = 7.5
    background: str = "FFFFFF"
    text_color: str = "1A1A2E"
    accent: str = "E94560"
    font: str = "DejaVu Sans"
    example_slides: int = 3
    # Ничего не красить и не набирать на слайдах: цвет и гарнитура остаются
    # только в теме. Зеркало датасета, где тема, наоборот, лгала.
    blank_slides: bool = False
    # Вшить в шаблон нераспаковываемую копию шрифта. Проверяет две вещи
    # разом: парсер не обязан падать, когда `.fntdata` ему не поддаётся,
    # а сборщик обязан донести эти части до результата — иначе у человека
    # без нужного шрифта колода откроется не тем.
    embed_font: str = ""


VARIANTS: tuple[Variant, ...] = (
    Variant(
        name="four_by_three",
        breaks="слайд не 16:9",
        width_in=10.0,
        height_in=7.5,
        accent="2F6F4E",
    ),
    Variant(
        name="dark",
        breaks="тёмный фон: чёрный текст по умолчанию станет нечитаем",
        background="101014",
        text_color="F2F2F7",
        accent="7C5CFF",
    ),
    Variant(
        name="latin_names",
        breaks="имена layout'ов не по-русски",
        accent="C2410C",
        font="Liberation Sans",
    ),
    Variant(
        name="single_example",
        breaks="один слайд-пример на всю колоду",
        example_slides=1,
        accent="0F766E",
    ),
    Variant(
        name="embedded_font",
        breaks="встроенный шрифт не распаковывается, а системного нет вовсе",
        # Calibri в контейнере нет: измерение уйдёт на метрический клон
        # Carlito. Имя шрифта в результате обязано остаться Calibri.
        font="Calibri",
        accent="1D4ED8",
        embed_font="Calibri",
    ),
    Variant(
        name="theme_only",
        breaks="на слайдах нет ни цвета, ни текста: всё только в теме",
        blank_slides=True,
        example_slides=2,
    ),
)


def _filled(slide, shape_type, x, y, cx, cy, rgb):
    shape = slide.shapes.add_shape(shape_type, x, y, cx, cy)
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(rgb)
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def build(variant: Variant, path: str | Path) -> Path:
    """Собирает один враждебный шаблон."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    prs.slide_width = Emu(int(variant.width_in * EMU_PER_INCH))
    prs.slide_height = Emu(int(variant.height_in * EMU_PER_INCH))
    blank = prs.slide_layouts[6]
    margin = Emu(int(0.6 * EMU_PER_INCH))

    for index in range(variant.example_slides):
        slide = prs.slides.add_slide(blank)
        if variant.blank_slides:
            # Пустой слайд намеренно: пусть парсер добывает, что сможет,
            # из темы и layout'ов.
            continue

        _filled(slide, 1, 0, 0, prs.slide_width, prs.slide_height, variant.background)
        _filled(
            slide, 1, 0, 0, prs.slide_width, Emu(int(0.12 * EMU_PER_INCH)), variant.accent
        )

        heading = slide.shapes.add_textbox(
            margin, margin, prs.slide_width - margin * 2, Emu(int(1.1 * EMU_PER_INCH))
        )
        run = heading.text_frame.paragraphs[0].add_run()
        run.text = f"Пример композиции {index + 1}"
        run.font.size = Pt(32)
        run.font.bold = True
        run.font.name = variant.font
        run.font.color.rgb = RGBColor.from_string(variant.text_color)

        # Ряд одинаковых карточек: проверяет поиск повторителей.
        card_w = (prs.slide_width - margin * 2) // 3 - Emu(int(0.2 * EMU_PER_INCH))
        for column in range(3):
            left = margin + column * (card_w + Emu(int(0.2 * EMU_PER_INCH)))
            top = Emu(int(2.2 * EMU_PER_INCH))
            _filled(slide, 5, left, top, card_w, Emu(int(2.0 * EMU_PER_INCH)), variant.accent)
            label = slide.shapes.add_textbox(
                left + Emu(int(0.15 * EMU_PER_INCH)),
                top + Emu(int(0.2 * EMU_PER_INCH)),
                card_w - Emu(int(0.3 * EMU_PER_INCH)),
                Emu(int(0.6 * EMU_PER_INCH)),
            )
            item = label.text_frame.paragraphs[0].add_run()
            item.text = f"Карточка {column + 1}"
            item.font.size = Pt(16)
            item.font.name = variant.font
            item.font.color.rgb = RGBColor.from_string(variant.background)

    prs.save(str(path))
    if variant.embed_font:
        _embed_font(path, variant.embed_font)
    return path


# Тип содержимого для `.fntdata` — тот же, что пишет сам PowerPoint.
FNTDATA_CONTENT_TYPE = "application/x-fontdata"
FNTDATA_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
)


def _embed_font(path: Path, typeface: str) -> None:
    """Дописывает в готовый `.pptx` встроенную копию шрифта.

    Байты внутри — не настоящий EOT, и распаковать их нельзя. Так и надо:
    проверяется не распаковка, а то, что части шрифта доезжают до
    результирующей колоды в целости и объявлены в `presentation.xml`.
    """
    source = zipfile.ZipFile(path)
    items = [(item, source.read(item.filename)) for item in source.infolist()]
    source.close()

    part = "ppt/fonts/font1.fntdata"
    rel_id = "rIdEmbeddedFont1"
    patched: list[tuple[str, bytes]] = []
    for item, data in items:
        text = None
        if item.filename == "[Content_Types].xml":
            text = data.decode("utf-8").replace(
                "</Types>",
                f'<Override PartName="/{part}" '
                f'ContentType="{FNTDATA_CONTENT_TYPE}"/></Types>',
            )
        elif item.filename == "ppt/_rels/presentation.xml.rels":
            text = data.decode("utf-8").replace(
                "</Relationships>",
                f'<Relationship Id="{rel_id}" Type="{FNTDATA_REL_TYPE}" '
                'Target="fonts/font1.fntdata"/></Relationships>',
            )
        elif item.filename == "ppt/presentation.xml":
            # Схема требует `embeddedFontLst` после `notesSz`.
            text = data.decode("utf-8")
            anchor = text.index("/>", text.index("<p:notesSz")) + 2
            text = (
                text[:anchor]
                + "<p:embeddedFontLst><p:embeddedFont>"
                + f'<p:font typeface="{typeface}" pitchFamily="34" charset="0"/>'
                + f'<p:regular r:id="{rel_id}"/>'
                + "</p:embeddedFont></p:embeddedFontLst>"
                + text[anchor:]
            )
        patched.append((item.filename, text.encode("utf-8") if text else data))

    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in patched:
            out.writestr(name, data)
        out.writestr(part, b"deckwright-embedded-font-fixture")
    shutil.move(str(tmp), str(path))


def build_all(target_dir: str | Path) -> list[Path]:
    """Собирает весь набор. Возвращает пути в порядке `VARIANTS`."""
    target_dir = Path(target_dir)
    return [build(variant, target_dir / f"{variant.name}.pptx") for variant in VARIANTS]


if __name__ == "__main__":
    import sys

    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "holdout")
    for path, variant in zip(build_all(directory), VARIANTS, strict=True):
        print(f"{path}  ломает: {variant.breaks}")
