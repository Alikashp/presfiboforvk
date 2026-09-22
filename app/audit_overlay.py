"""Находки аудита рамками поверх картинки слайда.

Критерий A16 требует показать находку **на слайде**, а не списком рядом с ним:
«элемент заходит в поля» без рамки — это загадка, а с рамкой — факт. Номер
рамки совпадает с номером в списке находок, иначе их не сопоставить.

Модуль намеренно не знает про Streamlit: на вход `SlideIR` и находки, на выход
строка HTML. Так его можно проверить тестом, не поднимая интерфейс, и так же
он годится для любого другого способа показа.

Координаты в `SlideIR` — в EMU (914400 на дюйм), картинка — в пикселях.
Пересчёт идёт долями от размера слайда, а не через dpi: доля не зависит от
того, с каким разрешением слайд растеризован, и рамка остаётся на месте при
любом размере картинки.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path

from deckwright.schemas import Issue, Severity

# Цвета рамок по серьёзности. Не из шаблона: это интерфейс сервиса, а не
# слайд, и находка обязана быть видна на любом фоне.
SEVERITY_COLORS = {
    Severity.ERROR: "#e5484d",
    Severity.WARNING: "#ffb224",
    Severity.INFO: "#0090ff",
}


def _percent(value: int, total: int) -> float:
    """Доля в процентах, обрезанная по краям слайда."""
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, value / total * 100))


def _data_uri(image: str | Path) -> str:
    raw = Path(image).read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def render(
    image: str | Path,
    issues: list[Issue],
    slide_width_emu: int,
    slide_height_emu: int,
    selected: set[str] | None = None,
    numbering: dict[str, int] | None = None,
) -> str:
    """HTML: картинка слайда и пронумерованные рамки поверх находок.

    `selected` — ключи отмеченных находок: отмеченная рамка заливается, чтобы
    человек видел, что именно он собирается исправить, глядя на слайд, а не
    сверяясь со списком.

    `numbering` — номера находок по ключу. Список находок нумеруется по всей
    колоде, а рамки рисуются по слайдам; без общей нумерации рамка №1 на
    третьем слайде означала бы другой пункт списка, чем №1 на первом.

    Находка без `bbox` рамки не получает — рисовать её вокруг всего слайда
    значит соврать о том, где проблема. В списке она остаётся.
    """
    selected = selected or set()
    numbering = numbering or {}
    frames: list[str] = []

    for position, issue in enumerate(issues, start=1):
        if issue.bbox is None:
            continue
        number = numbering.get(issue.key, position)
        color = SEVERITY_COLORS.get(issue.severity, SEVERITY_COLORS[Severity.INFO])
        chosen = issue.key in selected
        left = _percent(issue.bbox.x, slide_width_emu)
        top = _percent(issue.bbox.y, slide_height_emu)
        width = _percent(issue.bbox.w, slide_width_emu)
        height = _percent(issue.bbox.h, slide_height_emu)
        # Подпись целиком уходит в title: текст находки бывает длиннее слайда,
        # и вписывать его в рамку значит закрыть собой то, на что показываем.
        hint = html.escape(f"{number}. [{issue.severity.value}] {issue.message}")
        frames.append(
            f'<div class="finding{" chosen" if chosen else ""}" '
            f'style="left:{left:.3f}%;top:{top:.3f}%;'
            f"width:{width:.3f}%;height:{height:.3f}%;"
            f'--frame:{color}" title="{hint}">'
            f'<span class="tag" style="background:{color}">{number}</span>'
            "</div>"
        )

    # Страница самодостаточна: картинка вшита data-URI, стили inline. Внешних
    # запросов нет — компонент Streamlit живёт в изолированном iframe, и
    # ссылка на файл из него не разрешится.
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .slide {{ position: relative; width: 100%; }}
  .slide img {{ display: block; width: 100%; height: auto; }}
  .finding {{
    position: absolute;
    border: 2px solid var(--frame);
    border-radius: 3px;
    box-sizing: border-box;
    cursor: help;
  }}
  .finding.chosen {{ background: color-mix(in srgb, var(--frame) 22%, transparent); }}
  /* Браузер без color-mix просто покажет рамку без заливки — не рассыпается. */
  .tag {{
    position: absolute;
    top: -11px;
    left: -11px;
    min-width: 18px;
    height: 18px;
    padding: 0 4px;
    border-radius: 9px;
    color: #fff;
    font-weight: 600;
    font-size: 11px;
    line-height: 18px;
    text-align: center;
  }}
</style></head>
<body><div class="slide"><img src="{_data_uri(image)}" alt="слайд">
{"".join(frames)}
</div></body></html>"""


def height_for(image_width: int, image_height: int, rendered_width: int = 700) -> int:
    """Высота компонента: Streamlit сам её не считает и обрезает картинку."""
    if image_width <= 0:
        return rendered_width
    return int(rendered_width * image_height / image_width) + 4
