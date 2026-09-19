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


def _cmd_probe(args: argparse.Namespace) -> int:
    """Один живой вызов модели с полной диагностикой.

    Нужна, чтобы убедиться, что endpoint настроен и отвечает так, как ждёт
    пайплайн, до того как это выяснится посреди генерации колоды. Показывает
    время, токены, повторы из-за невалидного ответа и блоки рассуждений —
    то, по чему видно, годится ли модель и провайдер для бюджета в пять минут.
    """
    import time

    cfg = load_config(args.config)
    if not cfg.llm.configured:
        print(
            "Модель не настроена. Нужны LLM_BASE_URL, LLM_API_KEY и LLM_MODEL "
            "(см. .env.example).",
            file=sys.stderr,
        )
        return 1

    from deckwright.llm.client import LiveClient, probe_endpoint
    from deckwright.plan.planner import build_plan

    pack = ContentPack.model_validate(
        json.loads(Path(args.content).read_text(encoding="utf-8"))
    )
    client = LiveClient(cfg.llm)
    slide_count = cfg.deck.slide_count or cfg.deck.min_slides

    print(f"endpoint : {cfg.llm.base_url}")
    print(f"модель   : {cfg.llm.model}")
    params = cfg.llm.step("plan_deck")
    print(
        f"параметры: temperature={params.temperature} max_tokens={params.max_tokens} "
        f"enable_thinking={params.enable_thinking} "
        f"reasoning_effort={params.reasoning_effort or 'не задан'}"
    )
    print(f"слайдов  : {slide_count}\n")

    # Сначала проверяем ключ и наличие модели: 401 или неизвестное имя модели
    # видно сразу и с подсказкой, а не как провал посреди генерации.
    reachable, detail, available = probe_endpoint(cfg.llm)
    print(f"ключ     : {detail}")
    if available:
        print(f"моделей  : {len(available)}")
        if cfg.llm.model in available:
            print(f"модель   : {cfg.llm.model} — доступна")
        else:
            needle = cfg.llm.model.split("/")[-1].lower()[:6]
            close = [m for m in available if needle in m.lower()][:8]
            print(f"модель   : {cfg.llm.model} — В СПИСКЕ НЕТ")
            if close:
                print("           похожие: " + ", ".join(close))
    if not reachable:
        print(
            "\nEndpoint не принял ключ. Что проверить:\n"
            "  1. У SiliconFlow два независимых сервиса: api.siliconflow.cn и\n"
            "     api.siliconflow.com. Ключ одного на другом не работает.\n"
            "  2. В значении секрета не должно быть пробелов, переносов строки\n"
            "     и кавычек — только сам ключ.\n"
            "  3. Ключ должен быть активен, а на счету должен быть баланс.",
            file=sys.stderr,
        )
        return 1
    print()

    started = time.monotonic()
    failed = False
    try:
        plan, _ = build_plan(pack, client, slide_count)
    except Exception as exc:  # диагностика обязана досказать, что произошло
        failed = True
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        plan = None
    elapsed = round(time.monotonic() - started, 2)

    print(f"время           : {elapsed} с")
    print(f"вызовов         : {client.calls}")
    print(f"повторов        : {client.retries}  (ответ не прошёл валидацию по схеме)")
    print(f"блоков <think>  : {client.thinking_blocks}")
    print(f"отброшено полей : {sorted(client.dropped_params) or 'нет'}")
    print(
        f"токенов         : вход {client.prompt_tokens}, "
        f"выход {client.completion_tokens}, всего "
        f"{client.prompt_tokens + client.completion_tokens}"
    )

    if args.price_in and args.price_out:
        cost = (
            client.prompt_tokens * args.price_in + client.completion_tokens * args.price_out
        ) / 1_000_000
        print(f"стоимость       : ${cost:.6f} за прогон (по заданным ценам за 1M токенов)")
        print(f"  девять колод  : ${cost * 9:.4f}")
    else:
        print(
            "стоимость       : цены не заданы. Передайте --price-in и --price-out "
            "(за 1M токенов) со страницы тарифов провайдера."
        )

    if plan is not None:
        print(f"\nплан: {plan.slide_count} слайдов")
        for slide in plan.slides:
            print(f"  {slide.index}. [{slide.intent.value}] {slide.takeaway_title}")
        if args.save:
            Path(args.save).parent.mkdir(parents=True, exist_ok=True)
            Path(args.save).write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            print(f"\nплан сохранён: {args.save}")

    if client.last_raw:
        head = client.last_raw[:400].replace("\n", " ")
        print(f"\nначало сырого ответа:\n  {head}")

    return 1 if failed else 0


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

    probe = sub.add_parser(
        "probe", help="Один живой вызов модели с диагностикой: время, токены, повторы."
    )
    probe.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    probe.add_argument("--content", required=True, help="Контент-пакет в JSON.")
    probe.add_argument("--save", default=None, help="Куда сохранить полученный план.")
    probe.add_argument(
        "--price-in", type=float, default=None, help="Цена за 1M входных токенов."
    )
    probe.add_argument(
        "--price-out", type=float, default=None, help="Цена за 1M выходных токенов."
    )
    probe.set_defaults(func=_cmd_probe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
