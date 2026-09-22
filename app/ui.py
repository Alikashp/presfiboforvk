"""Веб-интерфейс: шаблон и контент на входе, три варианта и аудит на выходе.

Сценарий один и короткий: загрузить шаблон, дать бриф, запустить, посмотреть
три варианта, отметить находки аудита прямо на слайде, применить, скачать.

Два решения, которые видно в коде.

**Прогон идёт в фоне.** Пять минут в обработчике кнопки означали бы
заблокированную страницу и потерю результата при обновлении. Прогон живёт в
потоке (`app/runs.py`), его `run_id` — в параметрах адреса, и обновление
страницы возвращает к тому же прогону.

**Находки показываются на слайде, а не только списком.** «Элемент заходит в
поля» без рамки — загадка; номер рамки совпадает с номером в списке
(`app/audit_overlay.py`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # запуск через `streamlit run app/ui.py`
    sys.path.insert(0, str(ROOT))

from app import audit_overlay, runs  # noqa: E402

from deckwright.config import load_config  # noqa: E402
from deckwright.llm.fake import RecordedClient  # noqa: E402
from deckwright.schemas import ContentPack, FixKind, Severity  # noqa: E402

CONFIG = ROOT / "configs" / "config.yaml"
SAMPLE_PACK = ROOT / "tests" / "fixtures" / "content_pack.json"
RECORDED = ROOT / "tests" / "fixtures" / "recorded"
UPLOADS = ROOT / "outputs" / "uploads"

SEVERITY_MARK = {Severity.ERROR: "●", Severity.WARNING: "●", Severity.INFO: "●"}
FIX_EXPLANATION = {
    FixKind.AUTOMATIC: "применится само",
    FixKind.ASSISTED: "требует решения человека",
    FixKind.MANUAL: "чинится руками",
    FixKind.NONE: "информационная",
}


def _save_upload(uploaded, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / uploaded.name
    path.write_bytes(uploaded.getbuffer())
    return path


def _make_client(cfg, recorded: bool):
    """Записанные ответы или живая модель.

    Демонстрация без ключа обязана работать: иначе интерфейс нечем показать.
    """
    if recorded:
        return RecordedClient(RECORDED), None
    from deckwright.llm.client import LiveClient

    vlm = LiveClient(cfg.vlm) if cfg.vlm.configured else None
    return LiveClient(cfg.llm), vlm


def _sidebar(cfg):
    """Вход прогона. Возвращает параметры запуска или None."""
    st.sidebar.header("Что собираем")

    template = st.sidebar.file_uploader("Шаблон презентации", type=["pptx", "potx"])
    pack_file = st.sidebar.file_uploader("Контент-пакет (JSON)", type=["json"])
    use_sample = st.sidebar.checkbox(
        "Взять демонстрационный контент-пакет", value=pack_file is None
    )

    names = [variant.name for variant in cfg.variants]
    chosen = st.sidebar.multiselect("Варианты вёрстки", names, default=names)

    fix_mode = st.sidebar.radio(
        "Что делать с находками аудита",
        options=("review", "auto", "off"),
        index=0,
        format_func=lambda mode: {
            "review": "показать и дать выбрать (по ТЗ)",
            "auto": "применить автоматические самому",
            "off": "не трогать",
        }[mode],
        help=(
            "Находки, требующие решения человека, и ответы модели не "
            "применяются ни в одном режиме — только по вашему выбору ниже."
        ),
    )

    recorded = st.sidebar.checkbox(
        "Записанные ответы модели",
        value=not cfg.llm.configured,
        help="Прогон без ключа и без сети: план берётся из записи.",
    )
    if not recorded and not cfg.llm.configured:
        st.sidebar.warning("Модель не настроена: заполните .env или включите записанные ответы.")

    ready = template is not None and (pack_file is not None or use_sample) and chosen
    if not st.sidebar.button("Собрать", type="primary", disabled=not ready):
        return None

    raw = json.loads(
        pack_file.getvalue().decode("utf-8") if pack_file else SAMPLE_PACK.read_text("utf-8")
    )
    return {
        "template": _save_upload(template, UPLOADS),
        "pack": ContentPack.model_validate(raw),
        "variants": chosen,
        "fix_mode": fix_mode,
        "recorded": recorded,
    }


@st.fragment(run_every=2)
def _live_progress(state: runs.RunState, cfg) -> None:
    """Прогресс обновляется сам, не трогая остальную страницу.

    Раньше вся страница перезапускалась каждые две секунды, пока шёл прогон.
    Это не только мигание: перезапуск приходится ровно на то время, когда
    человек отмечает находки уже готового варианта, и отметка терялась.
    Фрагмент перерисовывает только себя.
    """
    _progress(state, cfg)
    if state.finished:
        # Прогон закончился — перерисовать страницу целиком, чтобы появились
        # вкладки с результатами, и больше не опрашивать.
        st.rerun(scope="app")


def _progress(state: runs.RunState, cfg) -> None:
    total = len(state.variants)
    st.progress(
        min(1.0, state.done_count / total if total else 1.0),
        text=f"готово вариантов {state.done_count} из {total}, {state.elapsed} с",
    )
    columns = st.columns(total or 1)
    for column, variant_state in zip(columns, state.variants.values(), strict=False):
        column.metric(variant_state.name, variant_state.stage)
    if state.elapsed > cfg.run.time_budget_seconds:
        st.warning(
            f"прогон идёт {state.elapsed} с при бюджете {cfg.run.time_budget_seconds} с"
        )


def _pick_key(state: runs.RunState, variant: str, issue_key: str, number: int) -> str:
    """Ключ виджета-галочки. Один на два места: отметить и снять."""
    return f"pick-{state.run_id}-{variant}-{issue_key}-{number}"


def _findings_panel(state: runs.RunState, variant_state: runs.VariantState, client) -> None:
    """Список находок с выбором. Номера совпадают с рамками на слайде."""
    report = variant_state.result.report
    if not report.issues:
        st.success("Находок нет.")
    else:
        st.caption(
            f"находок {len(report.issues)}, из них ошибок {report.error_count}; "
            f"применяется само — {len(report.auto_fixable)}"
        )

    for number, issue in enumerate(report.issues, start=1):
        columns = st.columns([1, 14])
        # `value=` здесь не передаётся намеренно. Вместе с `key=` он означает
        # «сбросить виджет к этому значению на каждом перезапуске скрипта», а
        # перезапуск случается на каждое нажатие — галочка снималась сама, и
        # выбрать находку было невозможно. Состояние держит session_state по
        # ключу, а `selected` собирается из него.
        chosen = columns[0].checkbox(
            f"{number}",
            key=_pick_key(state, variant_state.name, issue.key, number),
            label_visibility="collapsed",
        )
        if chosen:
            variant_state.selected.add(issue.key)
        else:
            variant_state.selected.discard(issue.key)
        columns[1].markdown(
            f"**{number}. {issue.check_id}** · слайд {issue.slide_index} · "
            f"{issue.severity.value} · {FIX_EXPLANATION[issue.fix.kind]}  \n"
            f"{issue.message}"
        )

    if report.skipped_checks:
        with st.expander(f"Не выполнено проверок: {len(report.skipped_checks)}"):
            for check_id, reason in sorted(report.skipped_checks.items()):
                st.write(f"`{check_id}` — {reason}")

    disabled = variant_state.applying or not variant_state.selected
    if st.button(
        f"Применить отмеченное ({len(variant_state.selected)})",
        key=f"apply-{state.run_id}-{variant_state.name}",
        disabled=disabled,
    ):
        # Галочки здесь не снимаются: Streamlit запрещает менять состояние
        # виджета после того, как виджет создан, и попытка обрывала весь
        # обработчик — применение не начиналось вовсе. Этого и не нужно:
        # исправленная находка исчезает из отчёта вместе со своей галочкой,
        # а оставшаяся отмеченной означает, что она осталась.
        runs.apply(state, variant_state.name, client)
        st.rerun()

    if variant_state.applying:
        _await_fix(variant_state)
    else:
        _last_fix_outcome(variant_state)


def _last_fix_outcome(variant_state: runs.VariantState) -> None:
    """Что сделала последняя попытка исправления и чего не сделала.

    Молчание здесь читается как «кнопка не работает»: у находки может не быть
    механического исправления вовсе, и тогда правильный ответ — сказать это,
    а не ничего не изменить.
    """
    records = variant_state.result.manifest.fix_iterations
    if not records:
        return
    last = records[-1]
    if last.applied:
        st.success(f"Применено исправлений: {len(last.applied)}, колода пересобрана.")
    if last.skipped:
        with st.expander(f"Не применено: {len(last.skipped)} — почему"):
            for what, reason in last.skipped.items():
                st.write(f"`{what}` — {reason}")


@st.fragment(run_every=2)
def _await_fix(variant_state: runs.VariantState) -> None:
    """Ждёт конца пересборки и обновляет страницу сам.

    Без этого страница застывала на «применяю» навсегда: исправление идёт в
    потоке, а Streamlit перерисовывает страницу только в ответ на действие
    человека — и результат появлялся лишь после случайного нажатия.
    """
    if variant_state.applying:
        st.info("Применяю исправления и пересобираю колоду…")
    else:
        st.rerun(scope="app")


def _downloads(variant_state: runs.VariantState) -> None:
    result = variant_state.result
    columns = st.columns(3)
    for column, label, path in (
        (columns[0], "Скачать .pptx", result.pptx),
        (columns[1], "Скачать .pdf", result.pdf),
        (columns[2], "Скачать .html", result.html),
    ):
        if Path(path).exists():
            column.download_button(
                label,
                data=Path(path).read_bytes(),
                file_name=Path(path).name,
                key=f"dl-{variant_state.name}-{Path(path).suffix}",
            )


def _variant_tab(state: runs.RunState, variant_state: runs.VariantState, client) -> None:
    result = variant_state.result
    if result is None:
        st.info(f"{variant_state.name}: {variant_state.stage}")
        return
    if variant_state.error:
        st.error(variant_state.error)

    manifest = result.manifest
    columns = st.columns(4)
    columns[0].metric("слайдов", len(result.deck.slides))
    columns[1].metric("находок", len(result.report.issues))
    columns[2].metric("ошибок", result.report.error_count)
    columns[3].metric("время, с", manifest.total_seconds)

    _downloads(variant_state)

    slides, findings = st.columns([3, 2])
    with slides:
        numbering = {
            issue.key: number for number, issue in enumerate(result.report.issues, start=1)
        }
        for index, page in enumerate(result.pages, start=1):
            on_slide = [issue for issue in result.report.issues if issue.slide_index == index]
            st.caption(f"Слайд {index} · находок {len(on_slide)}")
            # `st.iframe`, а не снятый с поддержки `components.v1.html`.
            # HTML здесь наш собственный: картинка слайда и рамки по находкам,
            # ничего пользовательского внутрь не попадает.
            st.iframe(
                audit_overlay.render(
                    page,
                    on_slide,
                    result.deck.slide_width_emu,
                    result.deck.slide_height_emu,
                    selected=variant_state.selected,
                    numbering=numbering,
                ),
                height=audit_overlay.height_for(16, 9, 640),
            )
    with findings:
        _findings_panel(state, variant_state, client)

    with st.expander("Паспорт прогона"):
        st.caption(
            "Версии промптов, модель, параметры, хэш шаблона и seed — то, чем "
            "результат воспроизводится."
        )
        st.json(json.loads(manifest.model_dump_json()), expanded=False)


def main() -> None:
    st.set_page_config(page_title="deckwright", layout="wide")
    st.title("deckwright")
    st.caption(
        "Читает шаблон как набор правил и собирает по нему новые слайды: "
        "три варианта вёрстки, аудит поверх слайда, экспорт в три формата."
    )

    cfg = load_config(CONFIG)
    request = _sidebar(cfg)

    if request is not None:
        client, vlm = _make_client(cfg, request["recorded"])
        state = runs.start(
            template_path=request["template"],
            pack=request["pack"],
            cfg=cfg,
            client=client,
            variants=request["variants"],
            output_root=cfg.run.output_dir,
            vlm_client=vlm,
            fix_mode=request["fix_mode"],
        )
        # run_id в адресе: обновление страницы возвращает к тому же прогону,
        # а не начинает новый.
        st.query_params["run"] = state.run_id
        st.rerun()

    run_id = st.query_params.get("run")
    state = runs.get(run_id) if run_id else runs.latest()
    if state is None:
        st.info("Загрузите шаблон и контент-пакет слева, затем нажмите «Собрать».")
        return

    st.subheader(f"Прогон {state.run_id} · {state.template_name}")
    if state.finished:
        _progress(state, cfg)
    else:
        _live_progress(state, cfg)
    if state.error:
        st.error(state.error)

    ready = state.ready()
    if ready:
        client = RecordedClient(RECORDED) if not cfg.llm.configured else None
        tabs = st.tabs([variant_state.name for variant_state in state.variants.values()])
        for tab, variant_state in zip(tabs, state.variants.values(), strict=False):
            with tab:
                _variant_tab(state, variant_state, client)




main()
