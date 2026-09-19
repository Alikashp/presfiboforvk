"""Сборка синтетического `.pptx`-шаблона для тестов.

Шаблоны датасета в репозиторий не кладутся: 58 МБ чужих материалов. Но
сквозной тест обязан идти в CI на каждом коммите, значит нужен шаблон, который
собирается на месте.

Заодно это проверка обобщаемости: синтетический шаблон ничем не похож на
датасет — другая палитра, другие имена layout'ов, другой фон. Если пайплайн
работает и на нём, значит он не опирается на особенности трёх присланных
файлов. Фаза 4 развернёт этот генератор в набор враждебных holdout'ов,
каждый из которых ломает своё допущение.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

EMU_PER_INCH = 914_400


def build_template(
    path: str | Path,
    *,
    width_inches: float = 13.333,
    height_inches: float = 7.5,
    background: str = "FFFFFF",
    text_color: str = "1A1A2E",
    accent: str = "E94560",
    font: str = "DejaVu Sans",
) -> Path:
    """Собирает шаблон с фоном, заголовком и контентным блоком на каждом макете.

    Параметры вынесены наружу, чтобы фаза 4 могла из этого же генератора
    получить светлый и тёмный варианты, 4:3 и 16:9, другие гарнитуры.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    prs.slide_width = Emu(int(width_inches * EMU_PER_INCH))
    prs.slide_height = Emu(int(height_inches * EMU_PER_INCH))

    blank = prs.slide_layouts[6]
    margin = Emu(int(0.6 * EMU_PER_INCH))

    # Слайды-примеры: по ним фаза 3 будет добывать паттерны, а сейчас они
    # просто показывают, что шаблон живой и с оформлением.
    for title, body_lines in (
        ("Заголовок примера", []),
        ("Заголовок с содержанием", ["Первый пункт", "Второй пункт", "Третий пункт"]),
    ):
        slide = prs.slides.add_slide(blank)

        backdrop = slide.shapes.add_shape(1, 0, 0, prs.slide_width, prs.slide_height)
        backdrop.fill.solid()
        backdrop.fill.fore_color.rgb = RGBColor.from_string(background)
        backdrop.line.fill.background()
        backdrop.shadow.inherit = False

        stripe = slide.shapes.add_shape(
            1, 0, 0, prs.slide_width, Emu(int(0.12 * EMU_PER_INCH))
        )
        stripe.fill.solid()
        stripe.fill.fore_color.rgb = RGBColor.from_string(accent)
        stripe.line.fill.background()
        stripe.shadow.inherit = False

        heading = slide.shapes.add_textbox(
            margin, margin, prs.slide_width - margin * 2, Emu(int(1.1 * EMU_PER_INCH))
        )
        run = heading.text_frame.paragraphs[0].add_run()
        run.text = title
        run.font.size = Pt(32)
        run.font.bold = True
        run.font.name = font
        run.font.color.rgb = RGBColor.from_string(text_color)

        if body_lines:
            body = slide.shapes.add_textbox(
                margin,
                Emu(int(2.0 * EMU_PER_INCH)),
                prs.slide_width - margin * 2,
                Emu(int(3.5 * EMU_PER_INCH)),
            )
            frame = body.text_frame
            frame.word_wrap = True
            for index, line in enumerate(body_lines):
                paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                item = paragraph.add_run()
                item.text = line
                item.font.size = Pt(16)
                item.font.name = font
                item.font.color.rgb = RGBColor.from_string(text_color)

    prs.save(str(path))
    return path


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "holdout/synthetic.pptx")
    print(build_template(target))
