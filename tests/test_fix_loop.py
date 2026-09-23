"""Цикл «аудит → исправление → пересборка»: кто принимает решение.

ТЗ требует, чтобы выбирал пользователь. Значит проверять надо не только то,
что исправление применяется, но и то, что **не применяется** без выбора:
режим, который чинит всё подряд, проходит тест «стало лучше» и нарушает A16.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from deckwright.audit import rewrite as rewrite_step
from deckwright.config import load_config
from deckwright.llm.fake import RecordedClient
from deckwright.pipeline import apply_selection, run_variant
from deckwright.schemas import (
    Box,
    CheckKind,
    ContentPack,
    FixKind,
    Issue,
    IssueCategory,
    ProposedFix,
    Severity,
)

CONFIG = "configs/config.yaml"


@pytest.fixture(scope="module")
def pack(content_pack_path):
    return ContentPack.model_validate(json.loads(content_pack_path.read_text("utf-8")))


def _run(template, pack, recorded_dir, output_dir, fix_mode, cfg=None):
    return run_variant(
        template_path=template,
        pack=pack,
        cfg=cfg or load_config(CONFIG),
        client=RecordedClient(recorded_dir),
        variant="balanced",
        output_dir=output_dir,
        fix_mode=fix_mode,
    )


@pytest.fixture(scope="module")
def reviewed(template_paths, pack, recorded_dir, tmp_path_factory):
    """Прогон в режиме review: колода собрана, отчёт есть, правок не было."""
    return _run(
        template_paths[0], pack, recorded_dir, tmp_path_factory.mktemp("review"), "review"
    )


def _stub(result, issues, deck=None):
    """Копия результата прогона с подменённым отчётом.

    Копия, а не правка на месте: `reviewed` — общая фикстура модуля, и тест,
    который её портит, ломает соседние.
    """
    stub = copy.copy(result)
    stub.audit = result.audit.model_copy(deep=True)
    stub.audit.issues = list(issues)
    if deck is not None:
        stub.deck = deck
    return stub


# ── Режим review: решение остаётся человеку ──────────────────────────────────


def test_review_mode_changes_nothing(reviewed):
    """A16: без выбора пользователя прогон колоду не правит.

    Отчёт при этом обязан быть — иначе выбирать не из чего.
    """
    assert reviewed.manifest.fix_mode == "review"
    assert reviewed.manifest.fix_iterations == []
    assert reviewed.audit is not None


def test_every_issue_has_a_selection_key(reviewed):
    """Выбор идёт по ключу находки: без него UI нечего передать обратно."""
    for issue in reviewed.audit.issues:
        assert issue.key.startswith(f"{issue.slide_index}:")
        assert issue.check_id in issue.key


# ── Режим auto: только AUTOMATIC, и не молча ─────────────────────────────────


def test_auto_mode_touches_only_automatic_fixes(
    template_paths, pack, recorded_dir, tmp_path_factory
):
    """Автоматический режим не имеет права применять ASSISTED и контекстные.

    Проверяется по протоколу итераций, а не по итоговому отчёту: находка может
    исчезнуть и сама, а вот запись «применено» — это именно решение прогона.
    """
    result = _run(
        template_paths[0], pack, recorded_dir, tmp_path_factory.mktemp("auto"), "auto"
    )
    applied = {key for record in result.manifest.fix_iterations for key in record.applied}
    by_key = {issue.key: issue for issue in result.audit.issues}
    for key in applied:
        issue = by_key.get(key)
        if issue is None:  # находка исчезла — она и была починена
            continue
        assert issue.fix.kind is FixKind.AUTOMATIC, key


def test_auto_mode_records_what_it_did(template_paths, pack, recorded_dir, tmp_path_factory):
    """Прогон, изменивший колоду, обязан отчитаться в манифесте (A19)."""
    result = _run(
        template_paths[0], pack, recorded_dir, tmp_path_factory.mktemp("auto2"), "auto"
    )
    manifest = result.manifest
    assert manifest.fix_mode == "auto"
    assert len(manifest.fix_iterations) <= manifest_limit()
    for record in manifest.fix_iterations:
        assert record.seconds >= 0
        assert record.issues_before >= record.issues_after or record.rewritten_slides


def manifest_limit() -> int:
    return load_config(CONFIG).run.max_fix_iterations


# ── Явный выбор ──────────────────────────────────────────────────────────────


@pytest.fixture
def damaged(reviewed, pack):
    """Колода с элементом, вынесенным за край, и её настоящий отчёт.

    Находка берётся из аудита, а не пишется руками: иначе тест проверял бы
    свою же выдумку, а не то, что цикл чинит реальные находки.
    """
    from deckwright.audit.report import audit_deck
    from deckwright.schemas import Box

    deck = reviewed.deck.model_copy(deep=True)
    element = deck.slides[0].elements[0]
    element.box = Box(
        x=deck.slide_width_emu + 10_000, y=element.box.y, w=element.box.w, h=element.box.h
    )
    report = audit_deck(
        deck,
        reviewed.spec,
        reviewed.plan,
        pack,
        load_config(CONFIG),
        pptx_path=reviewed.pptx,
        pages=None,
    )
    out_of_bounds = [
        issue for issue in report.issues if issue.check_id == "layout.out_of_bounds"
    ]
    assert out_of_bounds, "проверка выхода за границы не сработала на порче"
    return _stub(reviewed, report.issues, deck=deck), out_of_bounds[0]


def test_selection_applies_only_what_was_chosen(damaged):
    """Выбрана одна находка — применяется ровно она."""
    stub, issue = damaged
    after = apply_selection(stub, {issue.key})
    applied = {key for record in after.manifest.fix_iterations for key in record.applied}
    assert applied and applied <= {issue.key}
    assert after.manifest.fix_mode == "selected"


def test_selected_fix_is_applied_and_deck_rebuilt(damaged):
    """A16: исправление применяется, колода пересобирается, находка уходит."""
    stub, issue = damaged
    after = apply_selection(stub, {issue.key})

    element = after.deck.slides[0].elements[0]
    assert element.box.x + element.box.w <= after.deck.slide_width_emu

    assert after.pptx.exists() and after.pdf.exists() and after.pages
    remaining = [
        found for found in after.audit.issues if found.check_id == "layout.out_of_bounds"
    ]
    assert not remaining, "находка осталась после применения её же исправления"


def test_unchosen_finding_survives(damaged):
    """Не отмеченная находка остаётся нетронутой, даже если чинится сама."""
    stub, issue = damaged
    others = [
        found
        for found in stub.audit.auto_fixable
        if found.key != issue.key and found.slide_index != issue.slide_index
    ]
    after = apply_selection(stub, set())
    applied = [key for record in after.manifest.fix_iterations for key in record.applied]
    assert applied == []
    assert len(after.audit.issues) >= len(others)


def test_contextual_finding_is_never_applied(reviewed):
    """Контекстную находку применять нечем: у неё только показ.

    Даже явный выбор не должен превращаться в правку колоды — ответ модели на
    повторе может отличаться.
    """
    contextual = Issue(
        check_id="content.has_content",
        kind=CheckKind.CONTEXTUAL,
        category=IssueCategory.CONTENT,
        severity=Severity.WARNING,
        slide_index=1,
        confidence=0.7,
        message="на слайде нет содержания",
        fix=ProposedFix(
            kind=FixKind.ASSISTED,
            description="решение за человеком",
            action="review_contextual_finding",
            params={"check_id": "content.has_content", "slide_index": 1},
        ),
    )
    after = apply_selection(_stub(reviewed, [contextual]), {contextual.key})
    applied = [key for record in after.manifest.fix_iterations for key in record.applied]
    assert applied == []


def test_assisted_without_rewrite_flag_stays_a_note(reviewed):
    """Без `run.rewrite_assisted` выбранная ASSISTED остаётся пометкой.

    Это не вторая ветка поведения, а отсутствие шага: прогон не идёт в модель
    и не выдумывает за автора, что сократить.
    """
    overflow = Issue(
        check_id="layout.text_overflow",
        kind=CheckKind.DETERMINISTIC,
        category=IssueCategory.LAYOUT,
        severity=Severity.WARNING,
        slide_index=2,
        element_ids=["e1"],
        bbox=Box(x=0, y=0, w=100, h=100),
        message="e1: текст не помещается в рамку",
        fix=ProposedFix(
            kind=FixKind.ASSISTED,
            description="сократить текст",
            action="shorten_or_split",
            params={"element_id": "e1"},
        ),
    )
    stub = _stub(reviewed, [overflow])
    assert stub.context.cfg.run.rewrite_assisted is False

    after = apply_selection(stub, {overflow.key})
    reasons = [
        reason
        for record in after.manifest.fix_iterations
        for reason in record.skipped.values()
    ]
    assert after.manifest.fix_iterations, "итерация должна быть записана"
    assert any("редактирования" in reason for reason in reasons), reasons


# ── Переписывание моделью: рамки, а не доверие ───────────────────────────────


def test_rewrite_rejects_invented_numbers():
    """Новое число в переписанном тексте означает потерю происхождения факта."""
    from fixtures import deck_plan

    plan = deck_plan()
    slide = plan.slides[1]
    answer = rewrite_step.RewrittenSlide(
        takeaway_title="Выручка выросла на 47 % за квартал"
    )
    problem = rewrite_step._check(slide, answer, budget=None)
    assert problem is not None and "47" in problem


def test_rewrite_rejects_text_over_budget():
    """Текст, снова не влезающий в рамку, закрутил бы цикл на том же месте."""
    from deckwright.plan.budget import LengthBudget
    from fixtures import deck_plan

    plan = deck_plan()
    budget = LengthBudget(
        title_chars=20,
        subtitle_chars=40,
        bullet_chars=60,
        max_bullets=3,
        max_words_per_bullet=10,
        measured_with="тест",
    )
    answer = rewrite_step.RewrittenSlide(
        takeaway_title="Выручка выросла на 34 % за квартал, и это только начало"
    )
    problem = rewrite_step._check(plan.slides[1], answer, budget)
    assert problem is not None and "бюджете" in problem


def test_rewrite_keeps_block_composition():
    """Модель не имеет права заводить блоки: их состав решает вёрстка."""
    from fixtures import deck_plan

    plan = deck_plan()
    answer = rewrite_step.RewrittenSlide(
        takeaway_title="Выручка выросла на 34 %",
        blocks=[rewrite_step.RewrittenBlock(id="b-выдуманный", items=["пункт"])],
    )
    problem = rewrite_step._check(plan.slides[1], answer, budget=None)
    assert problem is not None and "b-выдуманный" in problem


def test_rewrite_applies_accepted_text():
    """Принятая правка меняет план, а не представление вёрстки."""
    from fixtures import deck_plan

    plan = deck_plan()
    issue = Issue(
        check_id="density.bullet_too_long",
        kind=CheckKind.DETERMINISTIC,
        category=IssueCategory.DENSITY,
        severity=Severity.WARNING,
        slide_index=2,
        element_ids=["b1"],
        message="слишком длинный пункт",
        fix=ProposedFix(
            kind=FixKind.ASSISTED,
            description="сократить формулировку",
            action="shorten_paragraph",
            params={"element_id": "b1", "paragraph": 0},
        ),
    )

    class Client:
        mocked = True

        def complete(self, step, prompt, schema, images=None):
            return schema.model_validate({"takeaway_title": "Выручка выросла на 34 %"})

    outcome = rewrite_step.rewrite(plan, [issue], Client())
    assert outcome.rewritten == [2]
    assert outcome.plan.slides[1].takeaway_title == "Выручка выросла на 34 %"
    # Исходный план не тронут: правка возвращается копией.
    assert plan.slides[1].takeaway_title == "Выручка выросла на 34 % за квартал"


def test_second_audit_asks_only_about_changed_slides(damaged):
    """Переспрашиваются только изменённые слайды, а не вся колода.

    Это не экономия на спичках: вопрос по слайду — 15.9 с и 1866 токенов по
    замеру, и полный переспрос после правки трёх слайдов стоил бы как первый
    проход. Проверяется счётчиком вызовов, а не чтением кода.
    """
    stub, issue = damaged

    class CountingVlm:
        mocked = True

        def __init__(self) -> None:
            self.calls = 0
            self.slide_questions = 0

        def complete(self, step, prompt, schema, images=None):
            self.calls += 1
            if step == "audit_slide":
                self.slide_questions += 1
            return schema.model_validate({"answers": []})

    vlm = CountingVlm()
    stub.context.vlm_client = vlm
    # Находки текстового прохода с первого варианта здесь не переезжают:
    # меряем именно картиночный проход.
    stub.context.text_findings = None

    after = apply_selection(stub, {issue.key})

    changed = {
        index
        for record in after.manifest.fix_iterations
        for index in record.rechecked_slides
    }
    assert changed, "цикл не сообщил, какие слайды он изменил"
    assert vlm.slide_questions == len(changed), (
        f"вопросов по слайдам {vlm.slide_questions} при {len(changed)} изменённых "
        f"(в колоде {len(after.deck.slides)}): переспрашивается вся колода"
    )
    assert vlm.slide_questions < len(after.deck.slides)


# ── Переписывание моделью от начала до конца ─────────────────────────────────
#
# Главный сценарий демонстрации: находка «текст не помещается» → выбор
# человека → модель переписывает → колода пересобирается → находка уходит.
# Проверяется на настоящем чужом шаблоне (`data/holdout/`), где вёрстка
# действительно ломается: узкие рамки и длинный русский текст дают четыре
# находки переполнения.

REAL_HOLDOUT = Path(__file__).resolve().parents[1] / "data" / "holdout"


class ScriptedRewriter:
    """Модель, которая честно сокращает: каждый пункт до нескольких слов.

    Настоящая модель на этом месте пишет осмысленнее, но проверяем мы не её
    красноречие, а то, что пайплайн принимает ответ, пересобирает колоду и
    снимает находку.
    """

    mocked = True

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, step, prompt, schema, images=None):
        self.calls += 1
        self.prompts.append(prompt)
        blocks = []
        # Идентификаторы блоков модель берёт из промпта — как настоящая.
        for line in prompt.splitlines():
            line = line.strip()
            if line.startswith("[") and "]" in line:
                block_id = line[1 : line.index("]")]
                blocks.append({"id": block_id, "items": ["Коротко и по делу"]})
        return schema.model_validate(
            {"takeaway_title": "Платформа ускоряет диагностику", "blocks": blocks}
        )


@pytest.fixture
def holdout_deck(pack, tmp_path):
    """Колода на настоящем чужом шаблоне, где текст не влезает в рамки."""
    template = REAL_HOLDOUT / "zelenie_investicii.pptx"
    if not template.exists():
        pytest.skip("настоящего holdout-шаблона нет в data/holdout")
    cfg = load_config(CONFIG)
    cfg.run.rewrite_assisted = True
    result = run_variant(
        template_path=template,
        pack=pack,
        cfg=cfg,
        client=RecordedClient(Path("tests/fixtures/recorded")),
        variant="balanced",
        output_dir=tmp_path / "holdout",
        fix_mode="review",
    )
    overflow = [i for i in result.audit.issues if i.check_id == "layout.text_overflow"]
    if not overflow:
        pytest.skip("на этом шаблоне текст помещается: переписывать нечего")
    return result, overflow


def test_rewrite_removes_the_overflow_it_was_chosen_for(holdout_deck):
    """Находка, выбранная человеком, после переписывания уходит.

    Это и есть A16 в самом дорогом его виде: не сдвинуть рамку, а изменить
    содержание — и только по явному выбору.
    """
    result, overflow = holdout_deck
    target = overflow[0]
    client = ScriptedRewriter()

    after = apply_selection(result, {target.key}, client)

    assert client.calls >= 1, "модель не спрашивали"
    records = after.manifest.fix_iterations
    assert records and records[0].rewritten_slides == [target.slide_index]

    remaining = [
        issue
        for issue in after.audit.issues
        if issue.check_id == "layout.text_overflow"
        and issue.slide_index == target.slide_index
    ]
    assert not remaining, "переполнение осталось после переписывания"


def test_rewritten_text_fits_the_template_budget(holdout_deck):
    """Переписанный текст обязан влезать: иначе цикл вернёт ту же находку."""
    result, overflow = holdout_deck
    target = overflow[0]
    budget = result.prepared.budget

    after = apply_selection(result, {target.key}, ScriptedRewriter())
    slide = next(s for s in after.plan.slides if s.index == target.slide_index)

    assert len(slide.takeaway_title) <= budget.title_chars
    for block in slide.blocks:
        assert len(block.items) <= budget.max_bullets
        for item in block.items:
            assert len(item) <= budget.bullet_chars
            assert len(item.split()) <= budget.max_words_per_bullet


def test_rewrite_that_invents_a_number_is_refused(holdout_deck):
    """Число, которого на слайде не было, означает потерю происхождения факта.

    Такой ответ отклоняется целиком по слайду, находка остаётся, причина
    называется вслух — а не подставляется молча.
    """
    result, overflow = holdout_deck
    target = overflow[0]

    class Liar(ScriptedRewriter):
        def complete(self, step, prompt, schema, images=None):
            self.calls += 1
            # Короткий заголовок: иначе сработает проверка бюджета длины и до
            # проверки чисел дело не дойдёт.
            return schema.model_validate({"takeaway_title": "Рост на 87 %"})

    after = apply_selection(result, {target.key}, Liar())

    records = after.manifest.fix_iterations
    assert records and not records[0].rewritten_slides
    reasons = " ".join(records[0].skipped.values())
    assert "87" in reasons, reasons
    assert [
        issue
        for issue in after.audit.issues
        if issue.check_id == "layout.text_overflow"
        and issue.slide_index == target.slide_index
    ], "находка обязана остаться: правка не применялась"
