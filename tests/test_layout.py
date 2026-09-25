"""Вёрстка: выбор композиции, закон фиттера и три варианта из одного кода.

Проверяется то, что ломается молча. Слайд с текстом, уехавшим за край, всё
равно соберётся, откроется и пройдёт проверку целостности — увидеть это можно
только измерением.
"""

from __future__ import annotations

import json
import sys
import typing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from make_holdout import build_all

from deckwright.config import load_config
from deckwright.layout.fitter import fit_paragraphs, fit_size, split_blocks
from deckwright.layout.matcher import (
    MIN_CONTRAST,
    _overlaps,
    build_deck_ir,
    needed_profile,
)
from deckwright.layout.strategy import (
    OverflowAction,
    Strategy,
    ladder_for_role,
    role_floor,
    scale_ladder,
)
from deckwright.layout.text_metrics import metrics_for_spec
from deckwright.parse.opener import parse_template
from deckwright.schemas import Box, Color, DeckPlan, SlotRole

CONFIG = "configs/config.yaml"
VARIANTS = ("airy", "balanced", "dense")
REAL_HOLDOUT_DIR = Path(__file__).resolve().parents[1] / "data" / "holdout"


@pytest.fixture(scope="module")
def plan() -> DeckPlan:
    path = Path(__file__).parent / "fixtures" / "recorded" / "plan_deck.json"
    return DeckPlan.model_validate(json.loads(path.read_text("utf-8")))


@pytest.fixture(scope="module")
def specs(tmp_path_factory, template_paths):
    """Все шаблоны, до каких дотянулись: синтетика, датасет и чужой."""
    paths = list(template_paths)
    paths += sorted(build_all(tmp_path_factory.mktemp("layout-holdout")))
    if REAL_HOLDOUT_DIR.is_dir():
        paths += sorted(REAL_HOLDOUT_DIR.glob("*.pptx"))
    return [(path.stem, parse_template(path)) for path in paths]


# ── Готовность фазы ──────────────────────────────────────────────────────────


def test_nothing_lands_outside_the_slide(specs, plan):
    """Критерий готовности фазы: ни один элемент не выходит за слайд.

    Пятая карточка повторителя уезжала на полтора дюйма за правый край:
    `max_count` — оценка вместимости сетки, и она бывает щедрее слайда.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            for slide in deck.slides:
                for element in slide.all_elements():
                    box = element.box
                    assert box.x >= 0 and box.y >= 0, f"{name}/{variant}: {element.id}"
                    assert box.right <= deck.slide_width_emu, (
                        f"{name}/{variant}: {element.id} уехал за правый край на "
                        f"{(box.right - deck.slide_width_emu) / 914400:.2f} дюйма"
                    )
                    assert box.bottom <= deck.slide_height_emu, (
                        f"{name}/{variant}: {element.id} уехал за нижний край на "
                        f"{(box.bottom - deck.slide_height_emu) / 914400:.2f} дюйма"
                    )


def test_every_size_comes_from_the_template_scale(specs, plan):
    """Кегль — только ступень шкалы шаблона.

    Промежуточное значение проверка «кегль не из типографической шкалы»
    найдёт справедливо, и чинить придётся то же самое, но позже и дороже.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        ladder = set(scale_ladder(spec))
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            used = {
                paragraph.style.size_pt
                for slide in deck.slides
                for element in slide.all_elements()
                if element.text is not None
                for paragraph in element.text.paragraphs
            }
            assert used <= ladder, f"{name}/{variant}: не из шкалы {sorted(used - ladder)}"


def test_the_three_variants_are_actually_different(specs, plan):
    """Три варианта — это параметр, а не три ветки кода. Но и не один результат."""
    cfg = load_config(CONFIG)
    differing = 0
    for _, spec in specs:
        shapes = set()
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            sizes = tuple(
                sorted(
                    {
                        paragraph.style.size_pt
                        for slide in deck.slides
                        for element in slide.all_elements()
                        if element.text is not None
                        for paragraph in element.text.paragraphs
                    }
                )
            )
            shapes.add(
                (
                    len(deck.slides),
                    tuple(sorted(s.pattern_id or s.layout_id or "" for s in deck.slides)),
                    sizes,
                )
            )
        if len(shapes) > 1:
            differing += 1
    assert differing, "все варианты дали одинаковую колоду на каждом шаблоне"


def test_layout_uses_more_than_one_composition(specs, plan):
    """Колода из десяти одинаковых слайдов формально верна и бесполезна."""
    cfg = load_config(CONFIG)
    rich = [
        (name, spec) for name, spec in specs if len(spec.patterns) >= len(VARIANTS) * 2
    ]
    if not rich:
        pytest.skip("нет шаблона, у которого композиций заметно больше числа вариантов")
    for name, spec in rich:
        deck, _ = build_deck_ir(spec, plan, cfg.variant("balanced"))
        used = {slide.pattern_id for slide in deck.slides if slide.pattern_id}
        assert len(used) > 1, f"{name}: вся колода легла в одну композицию {used}"


# ── Закон фиттера ────────────────────────────────────────────────────────────


def test_fitter_steps_down_the_ladder_and_stops_at_its_bottom():
    """Шаг вниз — только по ступеням, и ниже последней фиттер не идёт."""
    metrics = _metrics()
    ladder = [10.0, 14.0, 18.0, 24.0]
    tight = Box(x=0, y=0, w=914_400, h=300_000)

    result = fit_size("Короткий", metrics, Box(x=0, y=0, w=4_572_000, h=1_828_800),
                      ladder, 24.0)
    assert result.fits
    assert result.size_pt in ladder

    huge = " ".join(["длинноеслово"] * 60)
    result = fit_size(huge, metrics, tight, ladder, 24.0)
    assert not result.fits, "в рамку размером с марку такой текст влезть не может"
    assert result.size_pt == min(ladder), "фиттер обязан остановиться на нижней ступени"
    assert result.overflow_emu > 0, "переполнение обязано быть измерено, а не угадано"


def test_fitter_never_rises_above_the_size_the_template_uses():
    """Выше стартового кегля подниматься нельзя: это уже не типографика шаблона."""
    metrics = _metrics()
    ladder = [10.0, 14.0, 18.0, 24.0, 40.0]
    roomy = Box(x=0, y=0, w=9_144_000, h=4_572_000)
    result = fit_size("Два слова", metrics, roomy, ladder, 14.0)
    assert result.size_pt <= 14.0


def test_paragraphs_of_one_list_share_a_single_size():
    """Разный кегль у соседних пунктов списка — авария, а не вёрстка."""
    metrics = _metrics()
    ladder = [8.0, 12.0, 16.0, 20.0]
    box = Box(x=0, y=0, w=4_572_000, h=1_371_600)
    lines = ["Первый пункт", "Второй пункт подлиннее", "Третий"]
    result = fit_paragraphs(lines, metrics, box, ladder, 20.0)
    assert result.size_pt in ladder
    # То же содержание одной строкой не должно требовать более крупного кегля.
    assert result.size_pt <= fit_size(lines[0], metrics, box, ladder, 20.0).size_pt


def test_overflow_at_the_bottom_step_becomes_a_finding(specs, plan):
    """Четвёртый шаг закона: молча обрезанный текст хуже честно помеченного."""
    cfg = load_config(CONFIG)
    found = []
    for _, spec in specs:
        for variant in VARIANTS:
            _, issues = build_deck_ir(spec, plan, cfg.variant(variant))
            found.extend(issues)
    if not found:
        pytest.skip("ни на одном шаблоне текст не переполнился — проверять нечего")
    for issue in found:
        if issue.check_id != "layout.text_overflow":
            continue
        assert issue.fix.action, "находка без предложенного исправления бесполезна"
        assert issue.bbox is not None, "UI рисует рамку вокруг находки — bbox обязателен"


def test_splitting_never_tears_a_block_in_half():
    """Блок — смысловая единица: половина списка на слайде хуже, чем два слайда."""
    blocks = ["a", "b", "c", "d", "e"]
    parts = split_blocks(blocks)
    assert sum(len(part) for part in parts) == len(blocks)
    assert [item for part in parts for item in part] == blocks
    # Делить нечего, когда блок один.
    assert split_blocks(["a"]) == [["a"]]


# ── Стратегия ────────────────────────────────────────────────────────────────


def test_role_floor_comes_from_the_template_not_from_the_deck_wide_scale(specs):
    """Предел «мельче нельзя» — свой у каждой роли.

    Шкала целиком для этого не годится: она вбирает декор. У `vk_tech` в ней
    есть ступень 4.14 pt, и фиттер, честно спустившийся до неё, выдал бы
    нечитаемый слайд, формально соблюдя закон.
    """
    for name, spec in specs:
        floor = role_floor(spec, SlotRole.BODY)
        if floor <= 0:
            continue
        ladder = ladder_for_role(spec, SlotRole.BODY)
        assert min(ladder) >= floor, f"{name}: лестница уходит ниже предела роли"
        assert floor in set(scale_ladder(spec)), (
            f"{name}: предел роли должен быть ступенью шкалы шаблона"
        )


def test_data_mode_decides_what_a_series_becomes():
    """Один и тот же ряд у плотного варианта — таблица, у воздушного — число."""
    cfg = load_config(CONFIG)
    dense = Strategy.from_config(cfg.variant("dense"))
    airy = Strategy.from_config(cfg.variant("airy"))
    assert dense.data_viz_mode != airy.data_viz_mode
    assert dense.on_overflow in tuple(OverflowAction)


def test_signature_counts_the_title_and_every_block(plan):
    """Сигнатура слайда строится из плана, а не из текста."""
    cfg = load_config(CONFIG)
    strategy = Strategy.from_config(cfg.variant("balanced"))
    slide = plan.slides[0]
    profile = needed_profile(slide, strategy)
    assert profile[SlotRole.TITLE] == 1
    assert sum(profile.values()) == 1 + len(slide.blocks)


def _metrics():
    """Метрики какого-нибудь настоящего шрифта: тесты меряют, а не гадают."""

    class _Spec:
        fonts: typing.ClassVar[list] = []

    source = metrics_for_spec(_Spec())
    if source.metrics is None:
        pytest.skip("в системе нет ни одного шрифта: мерить нечем")
    return source.metrics


def test_no_two_texts_land_on_top_of_each_other(specs, plan):
    """Композиция снимается со слайда-примера, и её слоты там перекрываются.

    Положить в такие два разных текста — выдать кашу: на `zelenie_investicii`
    выходило до шести наложений на колоду, на `theme_only` — шесть.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            for slide in deck.slides:
                texts = [e for e in slide.all_elements() if e.text is not None]
                for first in range(len(texts)):
                    for second in range(first + 1, len(texts)):
                        a, b = texts[first], texts[second]
                        assert not _overlaps(a.box, b.box), (
                            f"{name}/{variant}: слайд {slide.index}, "
                            f"{a.id} налез на {b.id}"
                        )


def test_text_is_readable_on_the_background_it_lands_on(specs, plan):
    """Цвет шаблона проверяется контрастом, а не принимается на веру.

    Композиция приезжает со светлого слайда-примера на тёмный фон layout'а, и
    чёрный текст шаблона оказывается чёрным по чёрному. Это законно разные
    слайды, и поймать расхождение можно только измерением.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            for slide in deck.slides:
                for element in slide.all_elements():
                    # Фон, на котором текст лежит: подложка донора, если она
                    # есть (карточка, тёмная панель), иначе фон слайда.
                    backdrop = element.backdrop or slide.background
                    if element.text is None or backdrop is None:
                        continue
                    # Бывает подложка, на которой порога не даёт никакой
                    # цвет: на фиолетовой карточке синтетического тёмного
                    # шаблона у белого 4.35, у чёрного 4.4. Тогда требуется
                    # лучший достижимый контраст, а не невозможный.
                    reachable = min(
                        MIN_CONTRAST,
                        max(
                            Color(rgb=rgb).contrast_ratio(backdrop)
                            for rgb in ("FFFFFF", "111111")
                        ),
                    )
                    for paragraph in element.text.paragraphs:
                        ratio = paragraph.style.color.contrast_ratio(backdrop)
                        assert ratio >= reachable - 0.01, (
                            f"{name}/{variant}: слайд {slide.index}, {element.id} — "
                            f"контраст {ratio:.2f} при достижимом {reachable:.2f}"
                        )


def test_deck_is_set_in_the_templates_own_colours(template_paths):
    """Цвет текста берётся из палитры шаблона, а не придумывается.

    Раньше, когда цвет донора на нашем фоне не читался (или шаблон не сказал
    цвета вовсе), подставлялся `#111111` — цвет, которого в шаблоне нет. Он
    давал 24 находки `template.color_not_in_palette` из 28 на holdout и 30 из
    30 на синтетическом шаблоне, хотя в палитре обоих лежит `#000000` с ролью
    `text`. Аудит был прав, а виновата была вёрстка.
    """
    import json
    from pathlib import Path

    from deckwright.config import load_config
    from deckwright.llm.fake import RecordedClient
    from deckwright.pipeline import run_variant
    from deckwright.schemas import ContentPack

    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "configs" / "config.yaml")
    pack = ContentPack.model_validate(
        json.loads((root / "tests" / "fixtures" / "content_pack.json").read_text("utf-8"))
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = run_variant(
            template_path=template_paths[0],
            pack=pack,
            cfg=cfg,
            client=RecordedClient(root / "tests" / "fixtures" / "recorded"),
            variant="balanced",
            output_dir=tmp,
            fix_mode="review",
        )

    palette = {token.color.rgb for token in result.spec.palette}
    invented = {
        paragraph.style.color.rgb
        for slide in result.deck.slides
        for element in slide.all_elements()
        if element.text
        for paragraph in element.text.paragraphs
        if paragraph.style.color.rgb not in palette
    }
    assert not invented, (
        f"колода набрана цветами, которых в шаблоне нет: {sorted(invented)}; "
        f"палитра шаблона: {sorted(palette)}"
    )


# ── Ёмкость композиции (шаг 1: бюджет длины по макету) ──────────────────────


@pytest.fixture(scope="module")
def live_plan() -> DeckPlan:
    """Живой план из LLM probe #12 на `vk_tech`: то, что увидит жюри."""
    path = Path(__file__).parent / "fixtures" / "recorded_live" / "plan_deck.json"
    return DeckPlan.model_validate(json.loads(path.read_text("utf-8")))


def _overflowing(issues) -> set[int]:
    return {issue.slide_index for issue in issues if issue.check_id == "layout.text_overflow"}


def test_composition_is_chosen_where_the_text_fits(specs, live_plan):
    """Если в шаблоне есть композиция, куда слайд влезает, он туда и ложится.

    До этого выбор мерил только заголовок: на живом плане #12 повестка из
    пяти пунктов шла в рамку на две строки, хотя рядом были рамки на пять —
    37 переполненных слайдов из 90 на трёх шаблонах датасета.
    """
    from deckwright.layout.matcher import _blocks_fit, _title_fits, _usable_slots

    cfg = load_config(CONFIG)
    for name, spec in specs:
        metrics = metrics_for_spec(spec).metrics
        ladders = {role: ladder_for_role(spec, role) for role in SlotRole}
        for variant in VARIANTS:
            preset = cfg.variant(variant)
            strategy = Strategy.from_config(preset)
            deck, issues = build_deck_ir(spec, live_plan, preset)
            if len(deck.slides) != len(live_plan.slides):
                continue  # деление сдвинуло номера — сверять не с чем
            over = _overflowing(issues)
            for slide in live_plan.slides:
                could_fit = any(
                    _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
                    and _title_fits(pattern, slide, strategy, metrics, ladders[SlotRole.TITLE])
                    and _blocks_fit(pattern, spec, slide, strategy, metrics, ladders)
                    for pattern in spec.patterns
                )
                if could_fit:
                    assert slide.index not in over, (
                        f"{name}/{variant}: слайд {slide.index} переполнен, "
                        "хотя в шаблоне есть композиция, куда он влезает"
                    )


def test_chart_never_lands_in_a_caption_sized_frame(specs, live_plan):
    """График в рамке подписи «влезает» по тексту и не виден на слайде."""
    from deckwright.schemas import ElementKind

    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, live_plan, cfg.variant(variant))
            for slide in deck.slides:
                for element in slide.all_elements():
                    if element.kind is ElementKind.CHART:
                        assert element.box.h >= deck.slide_height_emu * 0.25, (
                            f"{name}/{variant}: график на слайде {slide.index} "
                            f"высотой {element.box.h / 914400:.2f} дюйма"
                        )


def test_variants_avoid_each_others_compositions(specs, live_plan):
    """A12: общий реестр не даёт вариантам съехаться на одни композиции.

    Сравнение — с независимой сборкой тех же вариантов: с реестром
    совпадений на тех же местах не больше, чем без него.
    """
    cfg = load_config(CONFIG)

    def same_places(spec, siblings) -> int:
        decks = [
            build_deck_ir(spec, live_plan, cfg.variant(v), siblings=siblings)[0]
            for v in VARIANTS
        ]
        ids = [[s.pattern_id for s in deck.slides] for deck in decks]
        return sum(
            1
            for a in range(len(ids))
            for b in range(a + 1, len(ids))
            for x, y in zip(ids[a], ids[b], strict=False)
            if x == y
        )

    for name, spec in specs:
        assert same_places(spec, {}) <= same_places(spec, None), name


def test_rebuild_keeps_its_own_compositions(specs, live_plan):
    """Пересборка в цикле исправления не избегает собственного выбора."""
    cfg = load_config(CONFIG)
    for name, spec in specs:
        siblings: dict = {}
        first = [
            build_deck_ir(spec, live_plan, cfg.variant(v), siblings=siblings)[0]
            for v in VARIANTS
        ]
        again, _ = build_deck_ir(spec, live_plan, cfg.variant(VARIANTS[-1]), siblings=siblings)
        assert [s.pattern_id for s in again.slides] == [
            s.pattern_id for s in first[-1].slides
        ], name


def test_word_wider_than_the_line_does_not_fit():
    """Рендер режет такое слово посередине — «обнаруже / ния»."""
    metrics = _metrics()
    word = "обнаружения"
    size = 24.0
    width = metrics.width_emu(word, size)
    box = Box(x=0, y=0, w=int(width * 0.8), h=914400 * 5)
    assert not fit_size(word, metrics, box, [size], size).fits
    assert not fit_paragraphs([word], metrics, box, [size], size).fits


def test_lists_are_read_at_the_body_size_of_the_template(specs):
    """У списков нет своих слотов — читаться они обязаны как тело текста."""
    from deckwright.layout.strategy import role_typical

    for name, spec in specs:
        if role_typical(spec, SlotRole.BODY):
            assert role_typical(spec, SlotRole.BULLETS) > 0, name


# ── Шаг 2: ёмкость для планировщика ─────────────────────────────────────────


def test_declared_capacity_really_fits_in_every_variant(specs):
    """Каждая точка кривой ёмкости обязана укладываться во всех трёх вариантах.

    И кривая монотонна: больше пунктов — пункт не длиннее.
    """
    from deckwright.layout.capacity import _probe, _text, achievable
    from deckwright.layout.matcher import _blocks_fit, _title_fits, _usable_slots

    cfg = load_config(CONFIG)
    strategies = [Strategy.from_config(cfg.variant(v)) for v in VARIANTS]
    for name, spec in specs:
        metrics = metrics_for_spec(spec).metrics
        if metrics is None:
            continue
        ladders = {role: ladder_for_role(spec, role) for role in SlotRole}
        capacity = achievable(spec, strategies, item_chars=34, title_chars=34, max_items=6)
        for kind, curve in capacity.items():
            assert all(point.items <= 6 for point in curve), f"{name}/{kind}: больше порога ТЗ"
            lengths = [point.chars for point in curve]
            assert lengths == sorted(lengths, reverse=True), f"{name}/{kind}: {curve}"
            for point in curve:
                slide = _probe(kind, point.items, point.chars, _text(24))
                for strategy in strategies:
                    assert any(
                        _usable_slots(p, spec.slide_width_emu, spec.slide_height_emu)
                        and _title_fits(p, slide, strategy, metrics, ladders[SlotRole.TITLE])
                        and _blocks_fit(p, spec, slide, strategy, metrics, ladders)
                        for p in spec.patterns
                    ), f"{name}/{strategy.name}: {kind} {point.items}×{point.chars} не влезает"


def test_planner_is_told_the_capacity_not_the_generic_threshold():
    """В промпт уходит кривая ёмкости, а не общий порог «до 6»."""
    from deckwright.plan.budget import LengthBudget

    budget = LengthBudget(
        title_chars=34,
        subtitle_chars=27,
        bullet_chars=34,
        max_bullets=6,
        max_words_per_bullet=15,
        measured_with="тест",
        block_limits=(
            ("bullets", 2, 110),
            ("bullets", 3, 74),
            ("bullets", 5, 50),
            ("bullets", 6, 26),
            ("paragraph", 1, 132),
        ),
    )
    lines = budget.as_prompt_lines()
    assert (
        "1–2 пункта до 110 символов; 3 пункта до 74 символов; "
        "4–5 пунктов до 50 символов; 6 пунктов до 26 символов"
    ) in lines
    assert "абзац (paragraph): не длиннее 132" in lines
    assert "не больше 6" not in lines


def test_list_is_spread_over_the_cards_one_item_each():
    """Список из трёх пунктов на композиции с четырьмя карточками.

    Главный дефект листов #12: весь список в первой карточке при трёх пустых
    рядом, 14 слайдов из 90 на `vk_tech`. Каждой мысли — своя карточка;
    лишняя карточка остаётся свободной, и рендер её уберёт.
    """
    from deckwright.layout.matcher import _seat, _usable_slots
    from deckwright.schemas import (
        BlockKind,
        ContentBlock,
        Pattern,
        PatternClass,
        Provenance,
        Repeater,
        Slot,
        SourceKind,
    )

    inch = 914400
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    body = Slot(id="b", role=SlotRole.BODY, box=Box(x=inch, y=inch, w=2 * inch, h=2 * inch),
                provenance=here)
    cards = Repeater(
        id="rep",
        item_slots=[body],
        item_box=body.box,
        observed_count=4,
        max_count=4,
        pitch_emu=2 * inch + inch // 4,
        gutter_emu=inch // 4,
        member_offsets=[(n * (2 * inch + inch // 4), 0) for n in range(4)],
        provenance=here,
    )
    title = Slot(id="t", role=SlotRole.TITLE, box=Box(x=inch, y=0, w=8 * inch, h=inch // 2),
                 provenance=here)
    pattern = Pattern(
        id="p", pattern_class=PatternClass.GRID, donor_slide_index=1, slots=[title],
        repeaters=[cards], content_area=Box(x=0, y=0, w=10 * inch, h=5 * inch),
        provenance=here,
    )
    block = ContentBlock(id="b1", kind=BlockKind.BULLETS, items=["один", "два", "три"])
    free = _usable_slots(pattern, 10 * inch, int(5.625 * inch))

    seats = _seat(pattern, block, SlotRole.BULLETS, free, [title.box])

    assert seats is not None and len(seats) == 3
    assert [lines for _, lines in seats] == [["один"], ["два"], ["три"]]
    assert [slot.box.x for slot, _ in seats] == [inch + n * cards.pitch_emu for n in range(3)]
    # Четвёртая карточка не достаётся следующему блоку: иначе он сел бы в
    # неё при пустых второй и третьей.
    assert not any(slot.id.startswith("rep_") for slot in free)

    many = ContentBlock(id="b2", kind=BlockKind.STEPS, items=[str(n) for n in range(6)])
    seats = _seat(pattern, many, SlotRole.BULLETS,
                  _usable_slots(pattern, 10 * inch, int(5.625 * inch)), [title.box])
    assert seats is not None
    assert [len(lines) for _, lines in seats] == [2, 2, 1, 1]


def test_title_and_closing_use_the_templates_own_slides(specs, plan):
    """Первый и последний слайды колоды — обложка и финал шаблона."""
    from deckwright.schemas import SlideIntent

    cfg = load_config(CONFIG)
    for name, spec in specs:
        if spec.cover_pattern_id is None:
            continue
        deck, _ = build_deck_ir(spec, plan, cfg.variant("balanced"))
        for slide, plan_slide in zip(deck.slides, plan.slides, strict=False):
            if plan_slide.intent is SlideIntent.TITLE:
                assert slide.pattern_id == spec.cover_pattern_id, name
            if plan_slide.intent is SlideIntent.CLOSING:
                wanted = spec.closing_pattern_id or spec.cover_pattern_id
                assert slide.pattern_id == wanted, name
