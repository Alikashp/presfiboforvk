"""Живой ключ не должен тратиться на каждый коммит.

Проверка инварианта, а не поведения кода: обычный CI обязан оставаться
бесплатным и офлайновым, а всё, что ходит в платную модель, — запускаться
только руками. Разъехаться это может незаметно, одной строкой в YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))

# Workflow'ы, которым разрешено обращаться к секретам. Всё остальное обязано
# работать без ключей — на записанных ответах.
PAID = {"llm-probe.yml"}


def _triggers(path: Path) -> list[str]:
    # В YAML голое `on` читается как булево True, а не как строка.
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list((data.get("on") or data.get(True) or {}).keys())


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_only_paid_workflows_touch_secrets(path: Path):
    uses_secrets = "secrets." in path.read_text(encoding="utf-8")
    if path.name in PAID:
        assert uses_secrets, f"{path.name} числится платным, но секретов не использует"
    else:
        assert not uses_secrets, (
            f"{path.name} обращается к секретам, хотя запускается автоматически"
        )


@pytest.mark.parametrize("path", [p for p in WORKFLOWS if p.name in PAID], ids=lambda p: p.name)
def test_paid_workflows_run_only_by_hand(path: Path):
    assert _triggers(path) == ["workflow_dispatch"], (
        f"{path.name} запускается не только вручную: ключ будет тратиться сам по себе"
    )


# ── Диагностический вызов ────────────────────────────────────────────────────
#
# Проба ходит в платную модель и запускается руками, поэтому её путь не
# закрыт обычным CI — и один раз это стоило живого вызова впустую. Фаза 5
# добавила планировщику третье возвращаемое значение, вызов в `cli.py`
# остался двухместным, и проба упала с `too many values to unpack` уже
# **после** ответа модели: деньги списаны, план потерян.
#
# Тест идёт на записанных ответах: сеть и ключ не нужны, а несостыковка
# контрактов между CLI и планировщиком ловится на том же коммите.


def test_probe_runs_end_to_end_on_recorded_answers(monkeypatch, tmp_path, recorded_dir):
    import sys as _sys

    import deckwright.llm.client as client_module
    from deckwright.cli import main
    from deckwright.llm.fake import RecordedClient

    _sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
    from make_template import build_template

    template = build_template(tmp_path / "probe.pptx")

    made: list[RecordedClient] = []

    class _Recorded(RecordedClient):
        def __init__(self, _cfg):
            super().__init__(recorded_dir)
            made.append(self)

    monkeypatch.setattr(client_module, "LiveClient", _Recorded)
    monkeypatch.setattr(
        client_module, "probe_endpoint", lambda _cfg: (True, "принят", ["fake-model"])
    )
    monkeypatch.setenv("LLM_API_KEY", "probe-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "fake-model")

    saved = tmp_path / "plan.json"
    code = main(
        [
            "probe",
            "--content",
            str(Path(__file__).parent / "fixtures" / "content_pack.json"),
            "--template",
            str(template),
            "--repeats",
            "3",
            "--save",
            str(saved),
        ]
    )
    assert code == 0, "проба завершилась ошибкой на записанных ответах"
    assert saved.exists(), "план не сохранён"
    # Разброс времени виден только на нескольких прогонах: один замер
    # проверяет, что вызов проходит, а не сколько он занимает.
    assert made and made[0].calls == 3, (
        f"--repeats 3 обязан дать три вызова планировщика, а дал "
        f"{made[0].calls if made else 0}"
    )


# ── Все окружения ставят один и тот же список ────────────────────────────────
#
# `deckwright doctor` идёт и в сборке образа, и в CI, и проверяет одни и те же
# системные пакеты. Пока список лежал в каждом файле отдельно, они
# разъезжались молча — ровно это и случилось дважды: сначала в CI забыли
# метрические клоны шрифтов, потом их забыли в пробе, и живой замер шёл на
# подставленной гарнитуре с другими ширинами.
#
# Сравнивать два списка мало: консументов больше двух, и третий (машина
# разработчика) списка не имеет вовсе. Поэтому список один —
# `scripts/install-system-deps.sh`, — а тест следит, чтобы мимо него никто не
# ставил пакеты сам.

INSTALLER = "scripts/install-system-deps.sh"
CONSUMERS = (
    "Dockerfile",
    ".github/workflows/ci.yml",
    ".github/workflows/llm-probe.yml",
)


def _apt_packages(text: str) -> set[str]:
    """Пакеты из всех `apt-get install` в файле.

    Строки склеиваются по переносу `\\`, потому что список пакетов обычно
    разложен по строкам.
    """
    joined: list[str] = []
    buffer = ""
    for line in text.splitlines():
        buffer += line.rstrip()
        if buffer.endswith("\\"):
            buffer = buffer[:-1] + " "
            continue
        joined.append(buffer)
        buffer = ""
    if buffer:
        joined.append(buffer)

    packages: set[str] = set()
    for line in joined:
        if "apt-get install" not in line:
            continue
        tail = line.split("apt-get install", 1)[1]
        # Всё после `&&` — уже следующая команда, пакетов там нет.
        tail = tail.split("&&", 1)[0]
        for word in tail.split():
            if word.startswith("-") or word.startswith("$"):
                continue
            packages.add(word)
    return packages


def test_every_environment_installs_from_the_same_list():
    """Ни образ, ни CI, ни проба не ставят пакеты в обход общего списка."""
    root = Path(__file__).resolve().parents[1]
    assert (root / INSTALLER).exists(), "общий список системных пакетов пропал"

    for name in CONSUMERS:
        text = (root / name).read_text("utf-8")
        assert INSTALLER in text, f"{name} не ставит пакеты общим скриптом"
        inline = _apt_packages(text)
        assert not inline, (
            f"{name} ставит пакеты мимо общего списка: {sorted(inline)}. "
            "Списки, лежащие в двух местах, расходятся молча"
        )


def test_the_shared_list_covers_what_doctor_demands():
    """Пакеты, без которых прогон падает, обязаны быть в списке.

    Без `libreoffice-impress` один `libreoffice-core` отвечает на .pptx
    «source file could not be loaded», и это выглядит как поломка кода.
    Без `poppler-utils` не из чего делать картинки слайдов для аудита.
    """
    root = Path(__file__).resolve().parents[1]
    installer = (root / INSTALLER).read_text("utf-8")
    for package in (
        "libreoffice-impress",
        "poppler-utils",
        "libeot0",
        "fonts-crosextra-carlito",
    ):
        assert package in installer, f"{package} пропал из общего списка"


def test_audit_probe_always_runs_against_a_spoiled_deck():
    """Главный вопрос прогона — различает ли аудит плохое и хорошее.

    Без `--spoil` прогон меряет токены и время, но не отвечает на него:
    аудит, который всегда доволен, укладывается в любой бюджет и бесполезен.
    """
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "llm-probe.yml"
    ).read_text("utf-8")
    assert "audit-probe" in workflow, "режим живого аудита не заведён в workflow"
    audit_step = workflow[workflow.index("deckwright audit-probe") :]
    assert "--spoil" in audit_step[:600], "живой аудит запускается без испорченной колоды"
