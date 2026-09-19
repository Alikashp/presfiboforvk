"""Проверка системных зависимостей, без которых пайплайн не работает.

Три внешние зависимости не ставятся через pip и молча ломают разные участки
пайплайна, если их нет:

* LibreOffice Impress — без него ``soffice`` на ``.pptx`` отвечает
  ``source file could not be loaded``, и экспорт в PDF отваливается;
* poppler (``pdftoppm``) — без него нет PNG, а значит нет превью и нет
  контекстного аудита по картинке слайда;
* libeot — без него не распаковываются встроенные в шаблон шрифты, и рендер,
  PDF и измерение текста расходятся с тем, что задумал дизайнер.

Проверяются они здесь, одним вызовом, и на этапе сборки образа — чтобы
отсутствие вскрывалось до первого прогона, а не посреди него.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
from dataclasses import dataclass

# Имя библиотеки и функции libeot. Отдельного CLI в дистрибутивах нет,
# обращаемся к разделяемой библиотеке напрямую.
LIBEOT_SONAME = "libeot.so.0"
LIBEOT_SYMBOL = "EOT2ttf_buffer"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _version_line(binary: str, args: list[str]) -> str:
    try:
        proc = subprocess.run(  # список аргументов фиксирован, не из пользовательского ввода
            [binary, *args], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"не запускается: {exc}"
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    return out[0] if out else "версия не определена"


def check_soffice(binary: str = "soffice") -> Check:
    path = shutil.which(binary)
    if path is None:
        return Check("LibreOffice", False, f"{binary} не найден в PATH")
    return Check("LibreOffice", True, f"{path}: {_version_line(binary, ['--version'])}")


def check_pdftoppm() -> Check:
    path = shutil.which("pdftoppm")
    if path is None:
        return Check("poppler", False, "pdftoppm не найден в PATH")
    return Check("poppler", True, f"{path}: {_version_line('pdftoppm', ['-v'])}")


def check_libeot() -> Check:
    try:
        lib = ctypes.CDLL(LIBEOT_SONAME)
    except OSError as exc:
        return Check("libeot", False, f"{LIBEOT_SONAME} не загружается: {exc}")
    if not hasattr(lib, LIBEOT_SYMBOL):
        return Check("libeot", False, f"{LIBEOT_SONAME} без символа {LIBEOT_SYMBOL}")
    return Check("libeot", True, f"{LIBEOT_SONAME}, {LIBEOT_SYMBOL} на месте")


def check_fallback_fonts() -> Check:
    """Есть ли хоть один шрифт для подстановки, когда шрифт шаблона недоступен."""
    try:
        from fontTools.ttLib import TTFont  # noqa: F401
    except ImportError as exc:  # pragma: no cover - fontTools в зависимостях
        return Check("шрифты", False, f"fontTools недоступен: {exc}")

    from pathlib import Path

    roots = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
    found = [p for root in roots if root.is_dir() for p in root.rglob("*.ttf")]
    if not found:
        return Check("шрифты", False, "ни одного .ttf в /usr/share/fonts")
    return Check("шрифты", True, f"{len(found)} .ttf, например {found[0].name}")


def run_checks(soffice_binary: str = "soffice") -> list[Check]:
    return [
        check_soffice(soffice_binary),
        check_pdftoppm(),
        check_libeot(),
        check_fallback_fonts(),
    ]
