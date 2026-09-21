"""Точка входа командной строки."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median

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
            f"  1. Сейчас запрос уходит на {cfg.llm.base_url}.\n"
            "     У SiliconFlow два независимых сервиса, .com и .cn, с разными\n"
            "     аккаунтами: ключ одного на другом всегда отвечает отказом.\n"
            "     Попробуйте тот домен, на котором заведён аккаунт.\n"
            "  2. В значении секрета не должно быть пробелов, переносов строки\n"
            "     и кавычек — только сам ключ.\n"
            "  3. Ключ должен быть активен, а на счету должен быть баланс.",
            file=sys.stderr,
        )
        return 1
    print()

    # Шаблон нужен, чтобы промпт ушёл в модель таким же, каким уходит в
    # настоящем прогоне: с ограничениями длины, посчитанными по его рамкам.
    # Без шаблона проба проверяет не тот промпт, который потом работает.
    spec = None
    if args.template:
        from deckwright.parse.opener import parse_template

        spec = parse_template(
            args.template,
            cache_dir=cfg.template.cache_dir,
            font_dir=cfg.fonts.extract_dir,
        )

    # Один замер ничего не говорит: скорость у провайдера плавает, и разница
    # между 34 и 317 секундами на двух прогонах может оказаться и разницей
    # нагрузки, и разницей в нашем запросе. Гоняем планировщик несколько раз
    # и смотрим на разброс, а не на одно число.
    #
    # Счётчики клиента накапливаются, поэтому по каждому прогону берётся
    # приращение, а не текущее значение.
    def _counters() -> dict[str, int]:
        return {
            "prompt": client.prompt_tokens,
            "completion": client.completion_tokens,
            "retries": client.retries,
            "thinking": client.thinking_blocks,
            "rate_limit": client.rate_limit_hits,
        }

    runs: list[dict[str, float]] = []
    failed = False
    plan = None
    budget = None

    for attempt in range(1, args.repeats + 1):
        before = _counters()
        started = time.monotonic()
        try:
            plan, _, budget = build_plan(
                pack,
                client,
                slide_count,
                spec=spec,
                max_bullets=cfg.audit.max_bullets_per_slide,
                max_words_per_bullet=cfg.audit.max_words_per_bullet,
                substitution_slack=cfg.fonts.substitution_slack,
            )
        except Exception as exc:  # диагностика обязана досказать, что произошло
            failed = True
            print(f"ОШИБКА на прогоне {attempt}: {exc}", file=sys.stderr)
            # Дальше не идём: если запрос ломается, остальные прогоны
            # потратят деньги на тот же отказ.
            break
        elapsed = time.monotonic() - started
        after = _counters()
        runs.append(
            {
                "n": attempt,
                "seconds": elapsed,
                **{key: after[key] - before[key] for key in before},
            }
        )

    if budget is not None:
        print(
            f"бюджет длины    : заголовок {budget.title_chars}, "
            f"пункт {budget.bullet_chars} симв; мерили: {budget.measured_with}"
        )

    if runs:
        print(f"\nпрогонов        : {len(runs)} из {args.repeats}")
        print(
            "  №   время, с   выход, ток   ток/с   повторов   <think>   429"
        )
        for run in runs:
            speed = run["completion"] / run["seconds"] if run["seconds"] else 0.0
            print(
                f"  {int(run['n']):<3} {run['seconds']:>8.1f}   {int(run['completion']):>10}"
                f"   {speed:>5.1f}   {int(run['retries']):>8}   {int(run['thinking']):>7}"
                f"   {int(run['rate_limit']):>3}"
            )

        times = sorted(run["seconds"] for run in runs)
        speeds = sorted(
            run["completion"] / run["seconds"] if run["seconds"] else 0.0 for run in runs
        )
        print(
            f"\nвремя, с        : мин {times[0]:.1f}  медиана {median(times):.1f}  "
            f"макс {times[-1]:.1f}"
        )
        print(
            f"скорость, ток/с : мин {speeds[0]:.1f}  медиана {median(speeds):.1f}  "
            f"макс {speeds[-1]:.1f}"
        )
        # Разброс важнее среднего: если максимум вдесятеро больше минимума,
        # планировать бюджет по медиане нельзя. На долях секунды отношение
        # считать бессмысленно — там шумит сам замер.
        if times[0] >= 1.0:
            print(f"разброс         : макс/мин = {times[-1] / times[0]:.1f}×")

    print(f"\nвызовов         : {client.calls}")
    print(f"повторов        : {client.retries}  (ответ не прошёл валидацию по схеме)")
    print(f"блоков <think>  : {client.thinking_blocks}")
    print(f"отброшено полей : {sorted(client.dropped_params) or 'нет'}")
    print(
        f"токенов         : вход {client.prompt_tokens}, "
        f"выход {client.completion_tokens}, всего "
        f"{client.prompt_tokens + client.completion_tokens}"
    )

    print(f"ответов 429     : {client.rate_limit_hits}")

    # Цены берутся из конфига; аргументы командной строки их перекрывают, если
    # у провайдера другой тариф.
    price_in = args.price_in if args.price_in is not None else cfg.llm.price_per_1m_input
    price_out = args.price_out if args.price_out is not None else cfg.llm.price_per_1m_output
    if price_in or price_out:
        total = (
            client.prompt_tokens * price_in + client.completion_tokens * price_out
        ) / 1_000_000
        per_call = total / client.calls if client.calls else 0.0
        print(
            f"стоимость       : ${per_call:.6f} за вызов, ${total:.6f} за пробу "
            f"(${price_in}/${price_out} за 1M)"
        )
        print(
            f"  три шаблона   : ${per_call * 3:.4f}  (планирование по разу на шаблон)"
        )
    else:
        print("стоимость       : цены не заданы ни в конфиге, ни аргументами")

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
        "--repeats",
        type=int,
        default=3,
        help=(
            "Сколько раз подряд прогнать планировщик. Один замер ничего не "
            "говорит: скорость у провайдера плавает."
        ),
    )
    probe.add_argument(
        "--template",
        default=None,
        help="Шаблон, по которому считать ограничения длины для промпта.",
    )
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
