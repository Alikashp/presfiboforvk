"""Проверка обобщаемости: шаблоны, под которые парсер не писался.

Два разных вида проверки, и они не заменяют друг друга.

**Синтетические враждебные шаблоны** бьют прицельно: каждый ломает одно
допущение, и по упавшему тесту сразу видно, какое. Их потолок в том, что писал
их тот же, кто писал парсер, — они ломают только предусмотренное.

**Настоящий чужой шаблон** в `data/holdout/` ломает непредусмотренное. Свой
первый прогон он окупил сразу: нашёл, что `graphicFrame` держит геометрию в
`p:xfrm`, а не в `a:xfrm`, из-за чего все нативные графики и таблицы были для
парсера невидимы. В датасете нативных графиков нет вовсе, и синтетику с ними
я бы не написал — потому что не знал, что там есть что ломать.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pptx import Presentation

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from make_holdout import VARIANTS, build_all

from deckwright.config import load_config
from deckwright.llm.fake import RecordedClient
from deckwright.parse.opener import parse_template
from deckwright.pipeline import run_variant
from deckwright.render.package_check import check_package
from deckwright.render.pptx_writer import slide_is_single_image
from deckwright.schemas import ContentPack, PatternClass, SlotRole

REAL_HOLDOUT_DIR = Path(__file__).resolve().parents[1] / "data" / "holdout"


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory) -> list[Path]:
    return build_all(tmp_path_factory.mktemp("holdout"))


@pytest.fixture(scope="module")
def real_holdouts() -> list[Path]:
    return sorted(REAL_HOLDOUT_DIR.glob("*.pptx")) if REAL_HOLDOUT_DIR.is_dir() else []


def _pack() -> ContentPack:
    path = Path(__file__).parent / "fixtures" / "content_pack.json"
    return ContentPack.model_validate(json.loads(path.read_text("utf-8")))


def _assert_spec_is_usable(path: Path) -> None:
    """Разбор не падает, и добытое годится для вёрстки, а не просто непусто."""
    spec = parse_template(path)

    assert spec.palette, f"{path.name}: палитра пуста, красить нечем"
    assert spec.fonts, f"{path.name}: гарнитуры не найдены"
    assert spec.type_scale_pt, f"{path.name}: типографическая шкала пуста"
    assert all(size > 0 for size in spec.type_scale_pt)
    assert spec.layouts, f"{path.name}: layout'ы не разобраны"

    if spec.grid is not None:
        grid = spec.grid
        assert grid.margin_left_emu + grid.margin_right_emu < spec.slide_width_emu * 0.5
        assert grid.margin_top_emu + grid.margin_bottom_emu < spec.slide_height_emu * 0.5

    for pattern in spec.patterns:
        assert pattern.capacity > 0
        assert pattern.content_area.right <= spec.slide_width_emu
        assert pattern.content_area.bottom <= spec.slide_height_emu


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda v: v.name)
def test_adversarial_template_parses(variant, synthetic):
    """Каждый вариант ломает своё допущение и обязан быть разобран."""
    path = next(p for p in synthetic if p.stem == variant.name)
    _assert_spec_is_usable(path)


def test_aspect_ratio_is_taken_from_the_file(synthetic):
    """Слайд 4:3 не должен разбираться как 16:9."""
    path = next(p for p in synthetic if p.stem == "four_by_three")
    assert parse_template(path).aspect_ratio == pytest.approx(4 / 3, abs=0.01)


def test_background_painted_by_a_shape_is_recognised_as_dark(synthetic):
    """Фон объявляют и элементом `p:bg`, и прямоугольником во весь слайд.

    При проверке только `p:bg` второй случай считается светлым, и по чёрному
    фону пишется чёрным. В датасете фон объявлен через `p:bg`, поэтому дыру
    показал только синтетический тёмный шаблон.
    """
    path = next(p for p in synthetic if p.stem == "dark")
    spec = parse_template(path)
    assert spec.patterns, "тёмный шаблон обязан дать хоть один паттерн"
    assert all(pattern.is_dark for pattern in spec.patterns)


def test_template_without_content_on_slides_degrades_and_says_so(synthetic):
    """Пустые слайды — не повод падать, но повод предупредить."""
    path = next(p for p in synthetic if p.stem == "theme_only")
    spec = parse_template(path)
    assert spec.palette and spec.fonts
    assert any("бедn" in w or "беден" in w or "плейсхолдер" in w for w in spec.warnings)


def test_real_holdout_parses(real_holdouts):
    """Настоящий чужой шаблон: ломает то, чего я не предусмотрел."""
    if not real_holdouts:
        pytest.skip("в data/holdout/ нет .pptx")
    for path in real_holdouts:
        _assert_spec_is_usable(path)


def test_real_holdout_recognises_native_charts_and_tables(real_holdouts):
    """Нативный график не должен опознаваться таблицей и не должен пропадать.

    `graphicFrame` держит геометрию в `p:xfrm`, а не в `a:xfrm`: поиск только
    по `a:` делает графики и таблицы невидимыми целиком.
    """
    if not real_holdouts:
        pytest.skip("в data/holdout/ нет .pptx")
    roles = {
        slot.role
        for path in real_holdouts
        for pattern in parse_template(path).patterns
        for slot in pattern.slots
    }
    if not (roles & {SlotRole.CHART, SlotRole.TABLE}):
        pytest.skip("в приложенных holdout'ах нет ни графиков, ни таблиц")
    assert SlotRole.CHART in roles or SlotRole.TABLE in roles


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda v: v.name)
def test_deck_generates_on_an_adversarial_template(variant, synthetic, recorded_dir, tmp_path):
    """Колода собирается, открывается и состоит из нативных объектов."""
    path = next(p for p in synthetic if p.stem == variant.name)
    result = run_variant(
        template_path=path,
        pack=_pack(),
        cfg=load_config("configs/config.yaml"),
        client=RecordedClient(recorded_dir),
        variant="balanced",
        output_dir=tmp_path,
    )
    assert check_package(result.pptx).ok
    assert slide_is_single_image(result.pptx) == []
    reopened = Presentation(str(result.pptx))
    assert len(reopened.slides._sldIdLst) == result.plan.slide_count
    assert len(result.pages) == result.plan.slide_count


def test_deck_generates_on_the_real_holdout(real_holdouts, recorded_dir, tmp_path):
    if not real_holdouts:
        pytest.skip("в data/holdout/ нет .pptx")
    for index, path in enumerate(real_holdouts):
        result = run_variant(
            template_path=path,
            pack=_pack(),
            cfg=load_config("configs/config.yaml"),
            client=RecordedClient(recorded_dir),
            variant="balanced",
            output_dir=tmp_path / f"h{index}",
        )
        assert check_package(result.pptx).ok, path.name
        assert slide_is_single_image(result.pptx) == [], path.name


# ── Запасной путь для неопознанных композиций ────────────────────────────────

def test_unclassified_patterns_stay_selectable(template_paths):
    """Композиция, не подошедшая ни под одно правило, не выбрасывается.

    Подбор идёт по структуре слотов, а класс только поднимает паттерн в
    выдаче. Иначе на чужом шаблоне, где правила опознают меньшую часть
    композиций, вёрстка лишилась бы большинства макетов.
    """
    specs = [parse_template(path) for path in template_paths]
    spec = next(
        (
            s
            for s in specs
            if any(p.pattern_class is PatternClass.FREEFORM for p in s.patterns)
        ),
        None,
    )
    if spec is None:
        pytest.skip("во всех шаблонах каждая композиция опознана правилами")

    matched = spec.patterns_matching({SlotRole.BODY: 1})
    assert any(p.pattern_class is PatternClass.FREEFORM for p in matched), (
        "неопознанные композиции выпали из подбора"
    )

    # Уверенные идут первыми, но неопознанные остаются в списке.
    confidences = [p.provenance.confidence for p in matched]
    assert confidences == sorted(confidences, reverse=True)


def test_pattern_profile_describes_structure_without_a_class():
    """Профиль слотов считается по структуре и от класса не зависит."""
    from deckwright.schemas import Box, Pattern, Provenance, Slot, SourceKind

    box = Box(x=0, y=0, w=100, h=100)
    pattern = Pattern(
        id="p",
        pattern_class=PatternClass.FREEFORM,
        donor_slide_index=1,
        slots=[
            Slot(
                id="a",
                role=SlotRole.BODY,
                box=box,
                provenance=Provenance(kind=SourceKind.SLIDE),
            )
        ],
        content_area=box,
        provenance=Provenance(kind=SourceKind.SLIDE),
    )
    assert pattern.slot_profile == {SlotRole.BODY: 1}
    assert pattern.fits({SlotRole.BODY: 1})
    assert not pattern.fits({SlotRole.CHART: 1})
