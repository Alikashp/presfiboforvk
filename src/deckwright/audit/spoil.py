"""Заведомо испорченный слайд — единственный способ проверить, что аудит видит.

Аудит, который всегда отвечает «да», проходит все тесты и бесполезен. Поймать
это можно только одним способом: показать модели слайд, про который точно
известно, что с ним не так, и посмотреть, назовёт ли она проблему.

Порчи выбраны под конкретные вопросы Приложения 1, а не «чтобы выглядело
плохо»: каждая обязана дать ответ «нет» на свой вопрос и не мешать остальным.
"""

from __future__ import annotations

from dataclasses import dataclass

from deckwright.schemas import Color, DeckIR, SlideIR


@dataclass(frozen=True)
class Spoilage:
    """Одна порча: что сделано и какой вопрос обязан это заметить."""

    slide_index: int
    kind: str
    expect_check: str
    description: str


def _first_text(slide: SlideIR):
    for element in slide.all_elements():
        if element.text is not None and element.text.paragraphs:
            return element
    return None


def spoil(deck: DeckIR, prompt_text: str = "Образец заголовка") -> tuple[DeckIR, list[Spoilage]]:
    """Возвращает копию колоды с известными дефектами и их список.

    Колода копируется: портить ту, что уходит пользователю, нельзя.
    """
    spoiled = deck.model_copy(deep=True)
    damage: list[Spoilage] = []

    for position, slide in enumerate(spoiled.slides):
        element = _first_text(slide)
        if element is None:
            continue

        # 1. Подсказка шаблона вместо содержания.
        if position == 0:
            paragraph = element.text.paragraphs[0]
            element.text.paragraphs[0] = paragraph.model_copy(update={"text": prompt_text})
            damage.append(
                Spoilage(
                    slide.index,
                    "подсказка шаблона вместо заголовка",
                    "content.no_prompt_leftovers",
                    f"заголовок заменён на {prompt_text!r}",
                )
            )

        # 2. Весь текст цветом фона: формально есть, читать нечем.
        #    Красится каждый элемент, а не первый: слайд с невидимым
        #    заголовком и видимым телом содержание всё-таки несёт, и вопрос
        #    «есть ли на слайде содержание» ответил бы «да» справедливо.
        elif position == 1 and slide.background is not None:
            for target in slide.all_elements():
                if target.text is None:
                    continue
                for index, paragraph in enumerate(target.text.paragraphs):
                    target.text.paragraphs[index] = paragraph.model_copy(
                        update={
                            "style": paragraph.style.model_copy(
                                update={"color": Color(rgb=slide.background.rgb)}
                            )
                        }
                    )
            damage.append(
                Spoilage(
                    slide.index,
                    "весь текст цветом фона",
                    "content.has_content",
                    "каждый текстовый элемент слайда перекрашен в цвет фона",
                )
            )

        # 3. Заголовок из другой вселенной: содержание слайда ему не отвечает.
        elif position == 2:
            element.text.paragraphs = [
                element.text.paragraphs[0].model_copy(
                    update={
                        "text": (
                            "Рецепт борща: свёкла, капуста, картофель, томатная паста, "
                            "варить сорок минут"
                        )
                    }
                )
            ]
            damage.append(
                Spoilage(
                    slide.index,
                    "заголовок не про содержание слайда",
                    "content.body_matches_title",
                    "заголовок заменён на рецепт борща при нетронутом теле",
                )
            )

        # 4. Текст, который заведомо не влезет.
        elif position == 3:
            long_line = " ".join(["избыточноедлинноеслово"] * 60)
            element.text.paragraphs = [
                element.text.paragraphs[0].model_copy(update={"text": long_line})
            ]
            element.text.truncated = True
            damage.append(
                Spoilage(
                    slide.index,
                    "текст не помещается в рамку",
                    "content.has_content",
                    "в рамку положено шестьдесят длинных слов",
                )
            )
            break

    return spoiled, damage
