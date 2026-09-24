"""Экспорт `.pptx` → `.pdf` через LibreOffice headless.

LibreOffice запускается отдельным процессом с собственным профилем: без
`-env:UserInstallation` параллельные запуски дерутся за общий профиль в
домашнем каталоге и подвисают.

Важное ограничение, которое нельзя забыть: LibreOffice **молчит** о битых
ссылках внутри пакета. Файл с висящим `r:embed` конвертируется без единой
жалобы, картинка просто не рисуется, а PowerPoint на том же файле требует
восстановления. Поэтому целостность проверяется отдельно, в
`package_check.check_package`, а не выводится из факта успешной конвертации.

Второе: шрифты, извлечённые из шаблона, LibreOffice сам не видит — он ищет
их через fontconfig в системных каталогах. Без подсказки `vk_tech` рисовался
DejaVu Sans вместо Play: шире на глаз, и текст, который фиттер честно
уложил по метрикам Play, на картинке рвал слова посередине. Каталоги шрифтов
передаются через собственный `fonts.conf` в профиле запуска; системный
конфиг подключается им же, так что остальные шрифты никуда не деваются.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from xml.sax.saxutils import escape


class ConversionError(RuntimeError):
    """LibreOffice не смог сконвертировать файл."""


def pptx_to_pdf(
    pptx_path: str | Path,
    output_dir: str | Path,
    soffice_binary: str = "soffice",
    timeout_seconds: int = 180,
    font_dirs: Iterable[str | Path] = (),
) -> Path:
    pptx_path = Path(pptx_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if shutil.which(soffice_binary) is None:
        raise ConversionError(
            f"{soffice_binary} не найден. Нужен пакет libreoffice-impress: "
            "без него .pptx не открывается вовсе (см. README, раздел «Сетап»)."
        )

    with tempfile.TemporaryDirectory(prefix="deckwright-lo-") as profile:
        command = [
            soffice_binary,
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(pptx_path),
        ]
        env = _with_fonts(profile, font_dirs)
        try:
            result = subprocess.run(  # аргументы фиксированы, не из пользовательского ввода
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(
                f"LibreOffice не уложился в {timeout_seconds} с на {pptx_path.name}"
            ) from exc

    pdf_path = output_dir / f"{pptx_path.stem}.pdf"
    if not pdf_path.exists():
        raise ConversionError(
            f"PDF не создан для {pptx_path.name}. "
            f"Вывод LibreOffice: {(result.stdout or result.stderr or '').strip()[:400]}"
        )
    return pdf_path


def _with_fonts(profile: str, font_dirs: Iterable[str | Path]) -> dict[str, str] | None:
    """Окружение, в котором fontconfig видит ещё и шрифты шаблона."""
    dirs = [Path(d).resolve() for d in font_dirs if Path(d).is_dir()]
    if not dirs:
        return None
    conf = Path(profile) / "fonts.conf"
    entries = "".join(f"<dir>{escape(str(d))}</dir>" for d in dirs)
    conf.write_text(
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
        '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
        f"{entries}<cachedir>{escape(str(Path(profile) / 'fc-cache'))}</cachedir>"
        "</fontconfig>",
        encoding="utf-8",
    )
    return {**os.environ, "FONTCONFIG_FILE": str(conf)}
