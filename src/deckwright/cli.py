"""Точка входа командной строки."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from deckwright.config import load_config
from deckwright.environment import run_checks

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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
