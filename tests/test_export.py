"""Экспорт: три формата, и каждый несёт то же содержание.

Проверяется не «файл создался», а то, что в нём. PDF из нужного числа страниц,
HTML с настоящим текстом в DOM и без единого внешнего запроса — иначе колода,
открытая без сети, окажется другой.
"""

from __future__ import annotations

import json
import re

import pytest

from deckwright.config import load_config
from deckwright.llm.fake import RecordedClient
from deckwright.pipeline import run_variant
from deckwright.render.html import export_html
from deckwright.schemas import ContentPack

CONFIG = "configs/config.yaml"


@pytest.fixture(scope="module")
def pack(content_pack_path):
    return ContentPack.model_validate(json.loads(content_pack_path.read_text("utf-8")))


@pytest.fixture(scope="module")
def exported(template_paths, pack, recorded_dir, tmp_path_factory):
    cfg = load_config(CONFIG)
    root = tmp_path_factory.mktemp("export")
    return [
        (
            path.stem,
            run_variant(
                template_path=path,
                pack=pack,
                cfg=cfg,
                client=RecordedClient(recorded_dir),
                variant="balanced",
                output_dir=root / path.stem,
            ),
        )
        for path in template_paths
    ]


def test_all_three_formats_are_produced(exported):
    """ТЗ просит .pptx, .pdf и .html — и все три обязаны существовать."""
    for name, result in exported:
        assert result.pptx.exists(), f"{name}: нет .pptx"
        assert result.pdf.exists(), f"{name}: нет .pdf"
        assert result.html.exists(), f"{name}: нет .html"
        assert result.pages, f"{name}: нет картинок слайдов"


def test_pdf_has_a_page_per_slide(exported):
    """Расхождение значит, что конвертация потеряла или удвоила слайд.

    Считается по картинкам: `pdftoppm` делает ровно одну на страницу, и это
    честнее, чем верить заголовку PDF.
    """
    for name, result in exported:
        assert len(result.pages) == len(result.deck.slides), (
            f"{name}: страниц {len(result.pages)}, слайдов {len(result.deck.slides)}"
        )


def test_html_carries_real_text_not_a_picture_of_it(exported):
    """Соблазн был конвертировать PDF в SVG — замер показал, чего это стоит.

    На одном слайде `vk_workspace` выходило 855 КБ, ноль элементов `<text>` и
    338 ссылок на глифы: весь текст переведён в кривые. Ни выделить, ни найти
    поиском. Здесь текст обязан быть текстом.
    """
    for name, result in exported:
        page = result.html.read_text("utf-8")
        titles = [slide.takeaway_title for slide in result.plan.slides]
        landed = [title for title in titles if title and title in page]
        assert landed, f"{name}: ни один заголовок плана не доехал до HTML"
        assert page.count("<section") == len(result.deck.slides), (
            f"{name}: секций в HTML не столько, сколько слайдов"
        )


def test_html_is_self_contained(exported):
    """Ни одного внешнего запроса: без сети страница обязана выглядеть так же.

    Ссылка на шрифт или картинку в интернете означает, что колода у проверяющего
    и колода у нас — разные документы.
    """
    for name, result in exported:
        page = result.html.read_text("utf-8")
        external = re.findall(r'(?:src|href)\s*=\s*"(?!data:|#)([^"]+)"', page)
        assert not external, f"{name}: HTML тянет внешние ресурсы: {external[:3]}"


def test_html_keeps_the_template_font_names(exported):
    """Имя гарнитуры в HTML то же, что в шаблоне и в `.pptx`.

    Подменять его нельзя нигде: человек, открывший страницу, должен видеть
    шрифт шаблона, а аудит — не находить чужой.
    """
    for name, result in exported:
        page = result.html.read_text("utf-8")
        families = {token.family for token in result.spec.fonts}
        if not families:
            continue
        assert any(f"'{family}'" in page for family in families), (
            f"{name}: в HTML нет ни одной гарнитуры шаблона ({sorted(families)})"
        )


def test_html_embeds_each_picture_once(exported):
    """Декор шаблона лежит на мастере и повторяется на каждом слайде.

    Вкладывать его по разу на слайд — тот же файл двенадцать раз: страница
    `vk_workspace` выходила 31 МБ вместо восьми.
    """
    for name, result in exported:
        page = result.html.read_text("utf-8")
        blobs = re.findall(r"base64,([A-Za-z0-9+/=]{200,})", page)
        assert len(blobs) == len(set(blobs)), f"{name}: одни и те же байты в HTML дважды"


def test_html_without_a_deck_file_still_renders_text(deck_and_spec, tmp_path):
    """Страница обязана собираться и без `.pptx`: картинок не будет, текст будет.

    Это тот случай, когда экспорт зовут по одному `SlideIR` — например, чтобы
    посмотреть вёрстку до сборки колоды.
    """
    deck, spec = deck_and_spec
    path = export_html(deck, tmp_path / "bare.html", spec=spec, pptx_path=None)
    page = path.read_text("utf-8")
    assert page.count("<section") == len(deck.slides)
    assert "<img" not in page and "background-image" not in page


@pytest.fixture(scope="module")
def deck_and_spec(template_paths, pack, recorded_dir):
    from deckwright.layout.matcher import build_deck_ir
    from deckwright.parse.opener import parse_template
    from deckwright.plan.planner import build_plan

    cfg = load_config(CONFIG)
    spec = parse_template(template_paths[0])
    plan, _, _ = build_plan(pack, RecordedClient(recorded_dir), cfg.deck.min_slides)
    deck, _ = build_deck_ir(spec, plan, cfg.variant("balanced"), pack=pack)
    return deck, spec


# ── Частичная растеризация ───────────────────────────────────────────────────
#
# Итерация цикла исправления трогает два-три слайда, а растеризация всей
# колоды стоит 18 с из 20. Перерисовывать все страницы ради трёх незачем — но
# переиспользование прошлых картинок обязано быть честным: страница с номером
# 4 после правки может оказаться другим слайдом.


def test_only_requested_pages_are_rerendered(exported, tmp_path):
    """Перерисовывается только названная страница, список остаётся полным."""
    from deckwright.render.png import pdf_to_png

    result = exported[0][1]
    directory = tmp_path / "png"
    first = pdf_to_png(result.pdf, directory, dpi=48)
    assert len(first) == len(result.deck.slides)

    marks = {page: page.stat().st_mtime_ns for page in first}
    again = pdf_to_png(result.pdf, directory, dpi=48, only_pages={2})

    assert len(again) == len(first), "список страниц обязан остаться полным"
    changed = [page for page in again if page.stat().st_mtime_ns != marks[page]]
    assert [page.name for page in changed] == [first[1].name]


def test_empty_selection_rerenders_nothing(exported, tmp_path):
    """Правка могла не тронуть ни одной страницы — это законный случай."""
    from deckwright.render.png import pdf_to_png

    result = exported[0][1]
    directory = tmp_path / "png"
    first = pdf_to_png(result.pdf, directory, dpi=48)
    marks = {page: page.stat().st_mtime_ns for page in first}

    again = pdf_to_png(result.pdf, directory, dpi=48, only_pages=set())
    assert len(again) == len(first)
    assert all(page.stat().st_mtime_ns == marks[page] for page in again)


def test_shorter_deck_leaves_no_stale_pages(exported, tmp_path):
    """Колода стала короче — хвост прошлой сборки в отчёт попасть не должен.

    Иначе аудит получил бы картинку слайда, которого в `.pdf` уже нет, а
    интерфейс показал бы его пользователю.
    """
    from deckwright.render.png import pdf_to_png

    result = exported[0][1]
    directory = tmp_path / "png"
    pages = pdf_to_png(result.pdf, directory, dpi=48)
    stale = directory / f"{result.pdf.stem}-{len(pages) + 1:02d}.png"
    stale.write_bytes(pages[0].read_bytes())

    # Число картинок разошлось с числом страниц: частичная растеризация
    # запрещена, идёт полная, и лишний файл убирается.
    again = pdf_to_png(result.pdf, directory, dpi=48, only_pages={1})
    assert not stale.exists(), "осталась страница от прошлой, более длинной сборки"
    assert len(again) == len(pages)


def test_pdf_is_drawn_in_the_templates_own_font(exported):
    """Картинка рисуется тем шрифтом, которым фиттер мерил текст.

    Без этого `vk_tech` рисовался DejaVu Sans вместо Play: шире, и текст,
    честно уложенный по метрикам Play, на картинке рвал слова посередине.
    """
    import shutil
    import subprocess

    if shutil.which("pdffonts") is None:
        pytest.skip("pdffonts не установлен")
    checked = 0
    for name, result in exported:
        embedded = {
            token.family.replace(" ", "")
            for token in result.spec.fonts
            if token.embedded and token.file_path
        }
        if not embedded:
            continue
        listing = subprocess.run(
            ["pdffonts", str(result.pdf)], capture_output=True, text=True, check=True
        ).stdout.replace(" ", "")
        assert any(family in listing for family in embedded), f"{name}: {listing[:300]}"
        checked += 1
    if not checked:
        pytest.skip("ни в одном шаблоне нет встроенных шрифтов")
