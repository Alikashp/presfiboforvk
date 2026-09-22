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
        print(
            f"[{variant}] {result.pptx.name}: {len(result.deck.slides)} слайдов, "
            f"{len(result.pages)} страниц PDF, {result.html.name}"
        )
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


def _cmd_audit_probe(args: argparse.Namespace) -> int:
    """Живой прогон контекстного аудита: замер вместо оценки.

    Колода собирается по записанным ответам — платить за планирование, чтобы
    проверить аудит, незачем. Живой остаётся только та часть, которую и надо
    измерить: вопросы по картинке слайда.
    """
    import json as _json
    import time as _time

    from deckwright.audit import probe as audit_probe
    from deckwright.audit.spoil import spoil
    from deckwright.llm.client import LiveClient, probe_endpoint
    from deckwright.pipeline import run_variant
    from deckwright.render.pdf import pptx_to_pdf
    from deckwright.render.png import pdf_to_png
    from deckwright.render.pptx_writer import render_deck

    cfg = load_config(args.config)
    model = cfg.vlm if cfg.vlm.configured else cfg.llm
    if not model.configured:
        print(
            "Модель со зрением не настроена: заполните VLM_BASE_URL, VLM_API_KEY "
            "и VLM_MODEL (или LLM_*) в .env — см. .env.example.",
            file=sys.stderr,
        )
        return 1

    print(f"endpoint : {model.base_url}")
    print(f"модель   : {model.model}")
    reachable, detail, available = probe_endpoint(model)
    print(f"ключ     : {detail}")
    if available:
        print(f"моделей  : {len(available)}")
        print(
            f"модель   : {model.model} — "
            + ("доступна" if model.model in available else "В СПИСКЕ НЕТ")
        )
    if not reachable:
        print("\nEndpoint не принял ключ: см. подсказки в `deckwright probe`.", file=sys.stderr)
        return 1

    pack = ContentPack.model_validate(
        _json.loads(Path(args.content).read_text(encoding="utf-8"))
    )
    output = Path(args.output)
    print("\nсобираю колоду по записанным ответам…")
    built = run_variant(
        template_path=args.template,
        pack=pack,
        cfg=cfg,
        client=RecordedClient(args.recorded),
        variant=args.variant,
        output_dir=output / "clean",
    )
    print(f"  слайдов {len(built.deck.slides)}, картинок {len(built.pages)}")

    check_ids = cfg.audit.checks_by_mode("image")
    client = LiveClient(model)

    started = _time.monotonic()
    clean = audit_probe.run(
        client,
        built.deck,
        built.plan,
        built.pages,
        check_ids,
        limit=args.slides,
        measure_cost=True,
    )

    # Шесть вопросов из одиннадцати задаются по тексту колоды, а не по
    # картинке. Не спросить их значит померить меньше половины аудита.
    text_ids = cfg.audit.checks_by_mode("text")
    if text_ids:
        clean.deck_pass = audit_probe.run_text_pass(client, built.plan, text_ids)

    spoiled_result = None
    damage = []
    if args.spoil:
        print("\nсобираю заведомо испорченную колоду…")
        broken, damage = spoil(built.deck)
        broken_pptx = render_deck(
            broken, built.spec, args.template, output / "spoiled" / "spoiled.pptx"
        )
        broken_pdf = pptx_to_pdf(
            broken_pptx,
            output / "spoiled",
            soffice_binary=cfg.render.soffice_binary,
            timeout_seconds=cfg.render.soffice_timeout_seconds,
        )
        broken_pages = pdf_to_png(
            broken_pdf, output / "spoiled" / "png", dpi=cfg.render.png_dpi
        )
        print(f"  порч внесено: {len(damage)}")
        spoiled_result = audit_probe.run(
            client,
            broken,
            built.plan,
            broken_pages,
            check_ids,
            limit=max(item.slide_index for item in damage) if damage else args.slides,
            measure_cost=False,
        )

    print()
    print(
        audit_probe.format_report(
            clean,
            spoiled_result,
            damage,
            check_ids,
            cfg.audit.contextual_dpi,
            text_ids,
        )
    )
    print()
    print(f"весь прогон          : {_time.monotonic() - started:.1f} с")
    print(f"вызовов              : {client.calls}")
    print(f"повторов             : {client.retries}")
    print(f"блоков <think>       : {client.thinking_blocks}")
    print(f"ответов 429          : {client.rate_limit_hits}")
    print(f"отброшено полей      : {sorted(client.dropped_params) or 'нет'}")
    cost = model.cost_usd(client.prompt_tokens, client.completion_tokens)
    print(f"стоимость            : ${cost:.6f}")

    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(
            _json.dumps(
                {
                    "dpi": cfg.audit.contextual_dpi,
                    "image_token_cost": clean.image_token_cost,
                    "slides": [vars(s) for s in clean.slides],
                    "spoiled": [vars(s) for s in (spoiled_result.slides if spoiled_result else [])],
                    "damage": [vars(d) for d in damage],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"замеры сохранены     : {args.save}")

    if any(slide.error for slide in clean.slides):
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

    audit = sub.add_parser(
        "audit-probe",
        help="Живой прогон контекстного аудита: токены, время, формат, чувствительность.",
    )
    audit.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    audit.add_argument("--template", required=True, help="Шаблон .pptx.")
    audit.add_argument("--content", required=True, help="Контент-пакет в JSON.")
    audit.add_argument(
        "--recorded",
        default="tests/fixtures/recorded",
        help="Записанные ответы для планирования: платить за него незачем.",
    )
    audit.add_argument("--variant", default="balanced", help="Вариант вёрстки.")
    audit.add_argument("--output", default="outputs/audit-probe", help="Куда класть колоды.")
    audit.add_argument(
        "--slides",
        type=int,
        default=0,
        help="Сколько слайдов спросить. 0 — всю колоду.",
    )
    audit.add_argument(
        "--spoil",
        action="store_true",
        help=(
            "Прогнать ещё и по заведомо испорченной колоде. Аудит, который "
            "всегда доволен, проходит все тесты и бесполезен."
        ),
    )
    audit.add_argument("--save", default=None, help="Куда сохранить замеры в JSON.")
    audit.set_defaults(func=_cmd_audit_probe)


    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
