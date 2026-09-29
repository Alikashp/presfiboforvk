"""Пять живых прогонов питча × `vk_tech` (llm-probe run 38) — на каждом.

Модель каждый раз строит другой план: 11–14 слайдов, разные блоки, разные
ряды. Приёмка сайта 28.09 — по всем пяти, а не по одному удачному:

* ни одного слайда из одного заголовка;
* ни одного текста мельче 9 pt;
* соседние слайды — в разных композициях;
* число показателя крупнее своей подписи;
* на слайде о команде — люди из входа;
* варианты различаются больше чем на треть слайдов содержания.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from deckwright.audit.deterministic.content import title_only
from deckwright.config import load_config
from deckwright.layout.matcher import LEGIBLE_FLOOR_PT
from deckwright.llm.fake import RecordedClient
from deckwright.pipeline import lay_out_variant
from deckwright.schemas import ContentPack, SlotRole

ROOT = Path(__file__).resolve().parents[1]
LIVE = Path(__file__).parent / "fixtures" / "pitch_live"
VK_TECH = ROOT / "data" / "templates" / "vk_tech.pptx"
CONFIG = ROOT / "configs" / "config.yaml"
VARIANTS = ("dense", "balanced", "airy")
RUNS = sorted(path.name for path in LIVE.iterdir()) if LIVE.exists() else []


@pytest.fixture(scope="module", params=RUNS)
def run(request, tmp_path_factory):
    if not VK_TECH.exists():
        pytest.skip("нет шаблона vk_tech")
    source = LIVE / request.param
    pack = ContentPack.model_validate(json.loads((source / "pack.json").read_text("utf-8")))
    laid, prepared = {}, None
    out = tmp_path_factory.mktemp(request.param)
    for variant in VARIANTS:
        laid[variant] = lay_out_variant(
            VK_TECH, pack, load_config(CONFIG), RecordedClient(source / "recorded"), variant,
            out / variant, fix_mode="off", prepared=prepared,
        )
        prepared = laid[variant].prepared
    return request.param, pack, laid


def _texts(slide):
    return [e for e in slide.all_elements() if e.text is not None]


def test_no_slide_is_only_a_title(run):
    name, _, laid = run
    for variant, result in laid.items():
        assert title_only(result.deck, result.spec) == [], (name, variant)


def test_no_text_smaller_than_legible(run):
    name, _, laid = run
    for variant, result in laid.items():
        small = [
            (slide.index, paragraph.text[:30], paragraph.style.size_pt)
            for slide in result.deck.slides
            for element in _texts(slide)
            for paragraph in element.text.paragraphs
            if paragraph.text.strip() and paragraph.style.size_pt < LEGIBLE_FLOOR_PT
        ]
        assert not small, (name, variant, small)


def test_neighbours_are_in_different_compositions(run):
    name, _, laid = run
    for variant, result in laid.items():
        slides, bookends = result.deck.slides, result.spec.bookend_ids
        pairs = [
            (a.index, a.pattern_id)
            for a, b in pairwise(slides)
            if a.pattern_id == b.pattern_id and a.pattern_id not in bookends
        ]
        assert not pairs, (name, variant, pairs)


def test_kpi_numbers_are_bigger_than_captions(run):
    """Показатель, разделённый на число и подпись, — число крупнее."""
    name, _, laid = run
    for variant, result in laid.items():
        for slide in result.deck.slides:
            texts = {e.id: e for e in _texts(slide)}
            for element in texts.values():
                if element.role is not SlotRole.KPI_VALUE and not element.id.endswith("_0"):
                    continue
                caption = texts.get(element.id[:-2] + "_1") if element.id.endswith("_0") else None
                if caption is None or not any(c.isdigit() for c in element.text.paragraphs[0].text):
                    continue
                number = element.text.paragraphs[0].style.size_pt
                below = caption.text.paragraphs[0].style.size_pt
                label = element.text.paragraphs[0].text
                assert number >= below, (name, variant, slide.index, label)


def test_team_slide_names_the_people(run):
    """Люди из входа — на слайдах, а не «опытная команда»."""
    name, pack, laid = run
    people = [fact.text for fact in pack.facts if "Шпагина" in fact.text]
    if not people:
        pytest.skip("в пакете этого прогона нет людей")
    for variant, result in laid.items():
        text = " ".join(
            p.text for slide in result.deck.slides for e in _texts(slide) for p in e.text.paragraphs
        )
        assert "Шпагина" in text and "Исаев" in text, (name, variant)


def test_variants_differ_on_most_content_slides(run):
    name, _, laid = run
    decks = [laid[v].deck for v in VARIANTS]
    bookends = laid["dense"].spec.bookend_ids
    count = min(len(deck.slides) for deck in decks)
    content = [i for i in range(count) if decks[0].slides[i].pattern_id not in bookends]
    same = [i for i in content if len({deck.slides[i].pattern_id for deck in decks}) == 1]
    assert len(same) * 3 <= len(content), (name, same)
