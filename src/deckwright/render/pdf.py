"""Экспорт `.pptx` → `.pdf` через LibreOffice headless.

LibreOffice запускается отдельным процессом с собственным профилем: без
`-env:UserInstallation` параллельные запуски дерутся за общий профиль в
домашнем каталоге и подвисают.

Важное ограничение, которое нельзя забыть: LibreOffice **молчит** о битых
ссылках внутри пакета. Файл с висящим `r:embed` конвертируется без единой
жалобы, картинка просто не рисуется, а PowerPoint на том же файле требует
восстановления. Поэтому целостность проверяется отдельно, в
`package_check.check_package`, а не выводится из факта успешной конвертации.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class ConversionError(RuntimeError):
    """LibreOffice не смог сконвертировать файл."""


def pptx_to_pdf(
    pptx_path: str | Path,
    output_dir: str | Path,
    soffice_binary: str = "soffice",
    timeout_seconds: int = 180,
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
        try:
            result = subprocess.run(  # аргументы фиксированы, не из пользовательского ввода
                command, capture_output=True, text=True, timeout=timeout_seconds, check=False
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
