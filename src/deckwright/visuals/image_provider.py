"""Слот под text-to-image. Интерфейс без реализации — и это намеренно.

ТЗ разрешает генерацию изображений моделью до 20B, но не требует её, а
картинка в презентации стоит дороже, чем кажется: время генерации, место в
пакете, риск выдать изображение не по теме и необходимость проверять его
содержание аудитом. Поэтому здесь объявлен контракт и заглушка, которая
честно говорит «нечем», а решение подключать провайдера остаётся за конфигом.

Композиции шаблона это не обедняет: картинки, пиктограммы и декор приезжают
клонированием донорского слайда вместе с композицией. Генерировать нужно
только то, чего в шаблоне нет вовсе.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


class ImageUnavailable(RuntimeError):
    """Картинку по этому запросу получить нечем."""


@runtime_checkable
class ImageProvider(Protocol):
    """Что обязан уметь поставщик изображений.

    `generate` возвращает путь к файлу. Путь, а не байты: изображение
    попадает в пакет через `python-pptx`, которому нужен файл, и хранить
    мегабайты в памяти ради этого незачем.
    """

    def generate(self, prompt: str, width: int, height: int, out_dir: Path) -> Path:
        ...


class NullImageProvider:
    """Поставщик, которого нет.

    Отказ явный и с причиной: молча вернуть пустую картинку значит поставить
    на слайд белый прямоугольник и оставить человека гадать, что случилось.
    """

    def __init__(self, reason: str = "провайдер изображений не настроен") -> None:
        self._reason = reason

    def generate(self, prompt: str, width: int, height: int, out_dir: Path) -> Path:
        raise ImageUnavailable(f"{self._reason}: запрос {prompt!r} не выполнен")


def provider_from_config(cfg) -> ImageProvider:
    """Поставщик по конфигу. Пока всегда пустой — реализации ещё нет."""
    enabled = getattr(getattr(cfg, "image_provider", None), "enabled", False)
    if enabled:
        return NullImageProvider(
            "провайдер изображений включён в конфиге, но реализации ещё нет"
        )
    return NullImageProvider()
