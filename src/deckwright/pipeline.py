"""Сквозной прогон: шаблон и контент → `.pptx`, `.pdf`, PNG.

Здесь слои соединяются и больше ничего не делается. Логики вёрстки, разбора и
экспорта в этом модуле нет — только порядок вызовов, замер времени по этапам и
сборка манифеста.

Скелетная версия. Фаза 10 добавит параллельную сборку слайдов, кэш разобранного
шаблона по хэшу и цикл исправления находок аудита.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from deckwright.config import Config
from deckwright.layout.matcher import build_deck_ir
from deckwright.layout.text_metrics import metrics_for_spec
from deckwright.llm.base import StructuredClient
from deckwright.parse.opener import parse_template
from deckwright.plan.planner import build_plan
from deckwright.render.package_check import check_package
from deckwright.render.pdf import pptx_to_pdf
from deckwright.render.png import pdf_to_png
from deckwright.render.pptx_writer import render_deck, slide_is_single_image
from deckwright.schemas import (
    ContentPack,
    DeckIR,
    DeckPlan,
    FontSubstitution,
    ModelUsage,
    RunManifest,
    StageTiming,
    TemplateSpec,
)


class PipelineResult:
    """Что получилось за один прогон одного варианта."""

    def __init__(
        self,
        spec: TemplateSpec,
        plan: DeckPlan,
        deck: DeckIR,
        layout_issues: list,
        pptx: Path,
        pdf: Path,
        pages: list[Path],
        manifest: RunManifest,
    ) -> None:
        self.spec = spec
        self.plan = plan
        self.deck = deck
        self.layout_issues = layout_issues
        self.pptx = pptx
        self.pdf = pdf
        self.pages = pages
        self.manifest = manifest


@contextmanager
def _timed(manifest: RunManifest, stage: str):
    started = time.monotonic()
    try:
        yield
    finally:
        manifest.timings.append(
            StageTiming(stage=stage, seconds=round(time.monotonic() - started, 3))
        )


def run_variant(
    template_path: str | Path,
    pack: ContentPack,
    cfg: Config,
    client: StructuredClient,
    variant: str,
    output_dir: str | Path,
    run_id: str | None = None,
) -> PipelineResult:
    """Прогоняет один вариант вёрстки от шаблона до картинок слайдов."""
    template_path = Path(template_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_id = run_id or datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")
    manifest = RunManifest(
        run_id=run_id,
        started_at=datetime.now(UTC),
        template_sha256="0" * 64,
        template_name=template_path.name,
        seed=cfg.run.seed,
        variants=[variant],
    )

    with _timed(manifest, "parse"):
        # Кэш по хэшу файла: три варианта вёрстки разбирают один и тот же
        # шаблон, а разбор колоды на полсотни слайдов занимает секунды.
        spec = parse_template(
            template_path,
            cache_dir=cfg.template.cache_dir,
            font_dir=cfg.fonts.extract_dir,
        )
    manifest.template_sha256 = spec.template_sha256
    manifest.warnings.extend(spec.warnings)
    # Подстановка шрифта расходится с задумкой дизайнера и ломает измерение
    # текста, поэтому она обязана быть видна в паспорте прогона.
    # Чем мерили текст и можно ли этому верить — в паспорт прогона.
    source = metrics_for_spec(spec)
    if source.substituted:
        manifest.font_substitutions.append(
            FontSubstitution(
                requested=source.requested,
                used=source.used,
                reason=(
                    "метрически совместимый клон: ширины совпадают, вёрстка не сдвигается"
                    if source.metric_compatible
                    else "ширины не совпадают с оригиналом, бюджет длины ужат"
                ),
            )
        )
    manifest.font_substitutions.extend(
        FontSubstitution(
            requested=token.family,
            used="системный подбор",
            reason="шрифт не встроен в шаблон",
        )
        for token in spec.fonts
        if token.usage_count > 0 and not token.embedded and token.family != source.requested
    )

    slide_count = cfg.deck.slide_count or cfg.deck.min_slides
    with _timed(manifest, "plan"):
        plan, prompt, budget = build_plan(
            pack,
            client,
            slide_count,
            spec=spec,
            max_bullets=cfg.audit.max_bullets_per_slide,
            max_words_per_bullet=cfg.audit.max_words_per_bullet,
            substitution_slack=cfg.fonts.substitution_slack,
        )
    if budget is not None:
        manifest.warnings.append(
            f"бюджет длины ({budget.measured_with}): заголовок {budget.title_chars} симв, "
            f"пункт {budget.bullet_chars} симв, до {budget.max_bullets} пунктов"
        )
    manifest.prompts.append(prompt.as_manifest_entry())
    manifest.models.append(
        ModelUsage(
            role="llm",
            model="recorded" if client.mocked else cfg.llm.model,
            base_url="" if client.mocked else cfg.llm.base_url,
            calls=getattr(client, "calls", 0),
            prompt_tokens=getattr(client, "prompt_tokens", 0),
            completion_tokens=getattr(client, "completion_tokens", 0),
            retries=getattr(client, "retries", 0),
            thinking_blocks=getattr(client, "thinking_blocks", 0),
            rate_limit_hits=getattr(client, "rate_limit_hits", 0),
            cost_usd=cfg.llm.cost_usd(
                getattr(client, "prompt_tokens", 0), getattr(client, "completion_tokens", 0)
            ),
            dropped_params=sorted(getattr(client, "dropped_params", ())),
            mocked=client.mocked,
        )
    )

    with _timed(manifest, "layout"):
        # Вариант передаётся пресетом, а не именем: плотность, предпочтение
        # композиций и поведение при переполнении — это он и есть.
        try:
            preset = cfg.variant(variant)
        except KeyError:
            preset = variant
        deck, layout_issues = build_deck_ir(spec, plan, preset)
    # Находки вёрстки о самой себе едут дальше вместе с колодой: текст, не
    # влезший на минимальной ступени шкалы, обязан быть виден, а не обрезан
    # молча.
    manifest.warnings.extend(issue.message for issue in layout_issues)

    stem = f"{template_path.stem}_{variant}"
    with _timed(manifest, "render_pptx"):
        pptx_path = render_deck(deck, spec, template_path, output_dir / f"{stem}.pptx")

    # Целостность пакета проверяется здесь, а не в тестах: LibreOffice о битых
    # ссылках молчит, и без этой проверки поломка доедет до PowerPoint.
    with _timed(manifest, "verify_package"):
        check_package(pptx_path).raise_if_broken(pptx_path)
        single_image = slide_is_single_image(pptx_path)
        if single_image:
            manifest.warnings.append(
                f"слайды {single_image} состоят из одной картинки — ТЗ такое не засчитывает"
            )

    with _timed(manifest, "render_pdf"):
        pdf_path = pptx_to_pdf(
            pptx_path,
            output_dir,
            soffice_binary=cfg.render.soffice_binary,
            timeout_seconds=cfg.render.soffice_timeout_seconds,
        )

    with _timed(manifest, "render_png"):
        pages = pdf_to_png(pdf_path, output_dir / "png", dpi=cfg.render.png_dpi)

    manifest.finished_at = datetime.now(UTC)
    manifest.artifacts = {
        "pptx": str(pptx_path),
        "pdf": str(pdf_path),
        "png_dir": str(output_dir / "png"),
    }
    if not manifest.within_budget(cfg.run.time_budget_seconds):
        manifest.warnings.append(
            f"прогон занял {manifest.total_seconds} с при бюджете "
            f"{cfg.run.time_budget_seconds} с"
        )

    (output_dir / f"{stem}.manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return PipelineResult(
        spec, plan, deck, layout_issues, pptx_path, pdf_path, pages, manifest
    )
