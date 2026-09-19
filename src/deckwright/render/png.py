"""Экспорт `.pdf` → PNG через poppler.

PNG нужны дважды: как превью в интерфейсе и как вход контекстных проверок
аудита, которые смотрят на слайд глазами модели.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class RasterizeError(RuntimeError):
    """Не удалось получить картинки слайдов."""


def pdf_to_png(
    pdf_path: str | Path,
    output_dir: str | Path,
    dpi: int = 96,
    timeout_seconds: int = 180,
) -> list[Path]:
    """Возвращает пути к картинкам слайдов по порядку."""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if shutil.which("pdftoppm") is None:
        raise RasterizeError(
            "pdftoppm не найден. Нужен пакет poppler-utils (см. README)."
        )

    prefix = output_dir / pdf_path.stem
    command = ["pdftoppm", "-png", "-r", str(dpi), str(pdf_path), str(prefix)]
    try:
        result = subprocess.run(  # аргументы фиксированы, не из пользовательского ввода
            command, capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RasterizeError(f"pdftoppm не уложился в {timeout_seconds} с") from exc

    pages = sorted(output_dir.glob(f"{pdf_path.stem}-*.png"))
    if not pages:
        raise RasterizeError(
            f"картинки не созданы для {pdf_path.name}: "
            f"{(result.stdout or result.stderr or '').strip()[:400]}"
        )
    return pages
