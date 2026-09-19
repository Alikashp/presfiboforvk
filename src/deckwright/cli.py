"""Точка входа командной строки."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deckwright.config import load_config
from deckwright.environment import run_checks
from deckwright.llm.base import StructuredClient
from deckwright.llm.fake import RecordedClient
from deckwright.pipeline import run_variant
from deckwright.schemas import ContentPack

DEFAULT_CONFIG = Path("configs/config.yaml")


def _cmd_doctor(args: argparse.Namespace) -> int:
    soffice = "soffice"
    if args.config and Path(args.config).exists():
        try:
            cfg = load_config(args.config)
        except Exception as exc:  # конфиг мог быть отредактирован руками
            print(f"✗ конфигурация {args.config}: {exc}", file=sys.stderr)
            return 1
        soffice = cfg.render.soffice_binary
        print(f"✓ конфигурация {args.config}: вариантов {len(cfg.variants)}")

    checks = run_checks(soffice)
    for check in checks:
        mark = "✓" if check.ok else "✗"
        stream = sys.stdout if check.ok else sys.stderr
        print(f"{mark} {check.name}: {check.detail}", file=stream)

    failed = [c.name for c in checks if not c.ok]
    if failed:
        print(f"\nНе хватает: {', '.join(failed)}. См. README, раздел «Сетап».", file=sys.stderr)
        return 1
    return 0


def _make_client(cfg, recorded_dir: str | None) -> StructuredClient:
    """Записанные ответы, если каталог задан или модель не настроена.

    Прогон без ключа обязан работать: сквозной тест идёт в CI, где живого
    endpoint'а нет, и падать там из-за отсутствия секрета бессмысленно.
    """
    if recorded_dir:
        return RecordedClient(recorded_dir)
    if not cfg.llm.configured:
        raise SystemExit(
            "Модель не настроена: заполните LLM_BASE_URL, LLM_API_KEY и LLM_MODEL "
            "в .env, либо укажите --recorded с каталогом записанных ответов."
        )
    from deckwright.llm.client import LiveClient

    return LiveClient(cfg.llm)


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    pack = ContentPack.model_validate(
        json.loads(Path(args.content).read_text(encoding="utf-8"))
    )
    client = _make_client(cfg, args.recorded)

    variants = [v.name for v in cfg.variants] if args.variant is None else [args.variant]
    output_root = Path(args.output or cfg.run.output_dir)

    for variant in variants:
        result = run_variant(
            template_path=args.template,
            pack=pack,
            cfg=cfg,
            client=client,
            variant=variant,
            output_dir=output_root / variant,
        )
        manifest = result.manifest
        stages = ", ".join(f"{t.stage} {t.seconds}с" for t in manifest.timings)
        print(f"[{variant}] {result.pptx.name}: {len(result.deck.slides)} слайдов")
        print(f"[{variant}] {stages}")
        print(f"[{variant}] всего {manifest.total_seconds}с из {cfg.run.time_budget_seconds}с")
        for warning in manifest.warnings:
            print(f"[{variant}] ⚠ {warning}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deckwright",
        description="Собирает презентацию по правилам произвольного .pptx-шаблона.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Проверить системные зависимости и конфигурацию.")
    doctor.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Путь к config.yaml (по умолчанию {DEFAULT_CONFIG}); "
        "пропускается, если файла нет.",
    )
    doctor.set_defaults(func=_cmd_doctor)

    run = sub.add_parser("run", help="Собрать презентацию по шаблону и контент-пакету.")
    run.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    run.add_argument("--template", required=True, help="Шаблон .pptx.")
    run.add_argument("--content", required=True, help="Контент-пакет в JSON.")
    run.add_argument("--variant", default=None, help="Один вариант вместо всех из конфига.")
    run.add_argument("--output", default=None, help="Каталог артефактов.")
    run.add_argument(
        "--recorded",
        default=None,
        help="Каталог записанных ответов модели: прогон без сети и без ключа.",
    )
    run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
