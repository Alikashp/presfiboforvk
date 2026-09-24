"""`TemplateSpec` — что мы узнали о шаблоне. Выход слоя parse.

Устройство схемы следует из разбора датасета (`research/research.md` §2):

* тема шаблона не обязана отражать дизайн-систему — у одного из трёх шаблонов
  в `clrScheme` лежит стоковая офисная палитра. Поэтому палитра и гарнитуры
  хранят не «что написано в теме», а **что реально употребляется**, со счётчиком
  и происхождением;
* у большинства layout'ов нет контентных плейсхолдеров — сплошь один заголовок
  и декор. Поэтому композиции живут не в `layouts`, а в `patterns`, добытых из
  слайдов-примеров, и это основной путь подбора макета, а не запасной.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deckwright.schemas.common import Box, Color, Provenance, TextStyle


class SlotRole(StrEnum):
    """Роль содержательного слота. Определяется структурой, не текстом-заглушкой."""

    TITLE = "title"
    SUBTITLE = "subtitle"
    BODY = "body"
    BULLETS = "bullets"
    CAPTION = "caption"
    KPI_VALUE = "kpi_value"
    KPI_LABEL = "kpi_label"
    QUOTE = "quote"
    IMAGE = "image"
    ICON = "icon"
    CHART = "chart"
    TABLE = "table"
    LOGO = "logo"
    FOOTER = "footer"
    SLIDE_NUMBER = "slide_number"
    DECOR = "decor"
    UNKNOWN = "unknown"


class PatternClass(StrEnum):
    """Семантика композиции. Совпадает со словарём `pattern_preference` вариантов."""

    TITLE = "title"
    SECTION = "section"
    AGENDA = "agenda"
    STATEMENT = "statement"
    TWO_COLUMN = "two_column"
    GRID = "grid"
    KPI_ROW = "kpi_row"
    CHART = "chart"
    TABLE = "table"
    TIMELINE = "timeline"
    SCHEMA = "schema"
    QUOTE = "quote"
    HERO = "hero"
    TEAM = "team"
    CLOSING = "closing"
    FREEFORM = "freeform"


class Slot(BaseModel):
    """Одно место под содержание внутри layout'а или паттерна."""

    model_config = ConfigDict(extra="forbid")

    id: str
    role: SlotRole
    box: Box
    style: TextStyle | None = None
    # Текст-заглушка из шаблона («Заголовок», «Текст», «XXX%»). Слабая подсказка
    # о роли: помогает, но решение принимается по структуре — шаблон может
    # прийти и с настоящим содержанием вместо рыбы.
    placeholder_text: str = ""
    # Индекс плейсхолдера OOXML, если слот им является. None — слот добыт из
    # свободной фигуры на слайде-примере.
    ph_idx: int | None = None
    optional: bool = False
    # Цвет залитой фигуры донора, на которой лежит слот: карточка, панель,
    # сам залитый прямоугольник с текстом. Контраст текста проверяется по
    # нему, а не по фону слайда: на `vk_education` светлый слайд с чёрной
    # панелью давал тёмный текст по чёрному.
    backdrop: Color | None = None
    provenance: Provenance


class Repeater(BaseModel):
    """Повторяющийся компонент: карточки в ряд, шаги, колонки.

    Найден кластеризацией одинаковых по структуре соседних групп. Хранит
    шаблон одного элемента и сетку, по которой они расставлены, поэтому
    число элементов можно менять под контент.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    item_slots: list[Slot] = Field(min_length=1)
    item_box: Box
    axis: str = Field(default="horizontal", pattern=r"^(horizontal|vertical|grid)$")
    observed_count: int = Field(gt=0)
    min_count: int = Field(default=1, gt=0)
    max_count: int = Field(gt=0)
    pitch_emu: int = Field(gt=0)
    gutter_emu: int = Field(ge=0)
    provenance: Provenance

    @model_validator(mode="after")
    def _counts_are_sane(self) -> Repeater:
        if self.min_count > self.max_count:
            raise ValueError("min_count не может быть больше max_count")
        if not self.min_count <= self.observed_count <= self.max_count:
            raise ValueError(
                f"observed_count={self.observed_count} вне границ "
                f"{self.min_count}..{self.max_count}"
            )
        return self


class LayoutSpec(BaseModel):
    """Layout шаблона: фон, декор и плейсхолдеры."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    master_id: str
    slots: list[Slot] = Field(default_factory=list)
    decor_boxes: list[Box] = Field(default_factory=list)
    background: Color | None = None
    # Тёмный фон меняет выбор цвета текста, поэтому он часть контракта, а не
    # вывод, который каждый слой делает заново.
    is_dark: bool = False


class Pattern(BaseModel):
    """Композиция, снятая со слайда-примера. Основная единица вёрстки.

    ``donor_slide_index`` — то, откуда рендерер клонирует поддерево фигур.
    Без него пришлось бы рисовать композицию заново и терять оформление,
    ради которого шаблон и берут.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    pattern_class: PatternClass
    layout_id: str | None = None
    donor_slide_index: int
    slots: list[Slot] = Field(default_factory=list)
    repeaters: list[Repeater] = Field(default_factory=list)
    content_area: Box
    is_dark: bool = False
    # Как определён класс: правилами по структуре или моделью по рендеру.
    provenance: Provenance

    @model_validator(mode="after")
    def _has_something_to_fill(self) -> Pattern:
        if not self.slots and not self.repeaters:
            raise ValueError(
                f"паттерн {self.id!r} без слотов и повторителей нечем заполнять"
            )
        return self

    @property
    def capacity(self) -> int:
        """Сколько содержательных единиц вмещает при наблюдённой раскладке."""
        fixed = sum(1 for s in self.slots if s.role is not SlotRole.DECOR)
        repeated = sum(r.observed_count * len(r.item_slots) for r in self.repeaters)
        return fixed + repeated

    @property
    def capacity_range(self) -> tuple[int, int]:
        """Сколько единиц вмещает при наименьшей и наибольшей раскладке.

        Повторитель раздвигается: сетка из четырёх карточек принимает и три,
        и пять. Вёрстке нужны границы, а не единственное наблюдённое число.
        """
        fixed = sum(1 for s in self.slots if s.role is not SlotRole.DECOR)
        low = fixed + sum(r.min_count * len(r.item_slots) for r in self.repeaters)
        high = fixed + sum(r.max_count * len(r.item_slots) for r in self.repeaters)
        return low, high

    @property
    def slot_profile(self) -> dict[SlotRole, int]:
        """Сколько мест какой роли даёт паттерн, с учётом повторителей.

        Это **структурный** портрет композиции, не зависящий от того, удалось
        ли её классифицировать. По нему вёрстка подбирает паттерн под слайд и
        тогда, когда `pattern_class` равен `FREEFORM`: неопознанная композиция
        остаётся пригодной, если у неё есть нужные места.
        """
        profile: dict[SlotRole, int] = {}
        for slot in self.slots:
            if slot.role is SlotRole.DECOR:
                continue
            profile[slot.role] = profile.get(slot.role, 0) + 1
        for repeater in self.repeaters:
            for slot in repeater.item_slots:
                if slot.role is SlotRole.DECOR:
                    continue
                profile[slot.role] = profile.get(slot.role, 0) + repeater.max_count
        return profile

    def fits(self, needed: dict[SlotRole, int]) -> bool:
        """Хватает ли паттерну мест под запрошенные роли.

        Проверка идёт по структуре и ничего не знает о классе. Это и есть
        запасной путь для композиций, которые правила не опознали.
        """
        profile = self.slot_profile
        return all(profile.get(role, 0) >= count for role, count in needed.items())


class ColorToken(BaseModel):
    """Цвет палитры с весом: чем больше площади он красит, тем он главнее."""

    model_config = ConfigDict(extra="forbid")

    color: Color
    usage_count: int = Field(ge=0)
    painted_area_emu: int = Field(default=0, ge=0)
    roles: list[str] = Field(default_factory=list)
    provenance: Provenance


class FontToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str
    usage_count: int = Field(ge=0)
    # Извлечён ли настоящий шрифт шаблона. False означает подстановку, и это
    # обязано быть видно в манифесте прогона и в отчёте аудита.
    embedded: bool = False
    substituted_with: str | None = None
    file_path: str | None = None

    @model_validator(mode="after")
    def _substitution_is_declared(self) -> FontToken:
        if self.embedded and self.substituted_with is not None:
            raise ValueError(
                f"{self.family}: шрифт извлечён из шаблона, подстановка не нужна"
            )
        return self


class Grid(BaseModel):
    """Поля и направляющие, выведенные кластеризацией рёбер контентных фигур."""

    model_config = ConfigDict(extra="forbid")

    margin_left_emu: int = Field(ge=0)
    margin_right_emu: int = Field(ge=0)
    margin_top_emu: int = Field(ge=0)
    margin_bottom_emu: int = Field(ge=0)
    columns: list[int] = Field(default_factory=list)
    rows: list[int] = Field(default_factory=list)
    gutter_emu: int = Field(default=0, ge=0)
    provenance: Provenance


class RecurringElement(BaseModel):
    """Логотип, колонтитул, номер слайда — то, что стоит на своём месте всегда.

    Найдено кластеризацией позиций по колоде. ``frequency`` — на какой доле
    слайдов встретилось; по нему аудит понимает, считать ли сдвиг ошибкой.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    role: SlotRole
    box: Box
    frequency: float = Field(gt=0.0, le=1.0)
    image_part: str | None = None
    text: str | None = None
    provenance: Provenance


# Порог читаемости из Приложения 1: им же меряет аудит.
MIN_TEXT_CONTRAST = 4.5


def readable_text_color(
    palette: list[ColorToken], background: Color | None, is_dark: bool = False
) -> Color | None:
    """Самый читаемый на этом фоне цвет текста **из палитры шаблона**.

    Живёт в контракте, а не в слое: спрашивают его и разбор (когда шаблон не
    сказал, каким цветом писать), и вёрстка (когда цвет донора на нашем фоне
    не читается). Два ответа на один вопрос разошлись бы, и колода набралась
    бы двумя разными цветами.

    Зачем вообще: раньше в обоих местах подставлялся `#111111` — цвет,
    которого в шаблоне нет. Он давал 24 находки из 28 на holdout и 30 из 30
    на синтетическом шаблоне, хотя в палитре обоих лежит `#000000` с ролью
    `text` и контрастом 21 к белому.

    Предпочтение — цветам, которыми шаблон пишет текст: заливка бывает
    контрастной и совершенно негодной для набора. Фон бывает неизвестен —
    композиция снята с донора, а фон слайду назначит layout; тогда судим по
    яркости шаблона, ею фон и окажется.
    """
    if background is None:
        background = Color(rgb="000000") if is_dark else Color(rgb="FFFFFF")
    readable = [
        token
        for token in palette
        if token.color.contrast_ratio(background) >= MIN_TEXT_CONTRAST
    ]
    if not readable:
        return None
    texty = [token for token in readable if "text" in token.roles]
    return max(
        texty or readable,
        key=lambda token: token.color.contrast_ratio(background),
    ).color


class TemplateSpec(BaseModel):
    """Полный разбор шаблона. Выход слоя parse, вход слоёв layout и audit."""

    model_config = ConfigDict(extra="forbid")

    # Хэш файла: ключ кэша и поле манифеста, по которому прогон воспроизводим.
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_name: str

    slide_width_emu: int = Field(gt=0)
    slide_height_emu: int = Field(gt=0)

    palette: list[ColorToken] = Field(default_factory=list)
    fonts: list[FontToken] = Field(default_factory=list)
    # Типографическая шкала шаблона. Уменьшать кегль при переполнении можно
    # только по ней — произвольное значение нарушило бы правила шаблона.
    type_scale_pt: list[float] = Field(default_factory=list)
    grid: Grid | None = None

    masters: list[str] = Field(default_factory=list)
    layouts: list[LayoutSpec] = Field(default_factory=list)
    patterns: list[Pattern] = Field(default_factory=list)
    recurring: list[RecurringElement] = Field(default_factory=list)

    # Что не удалось разобрать. Пустой список — не признак успеха, а признак
    # того, что парсер ничего не заметил; список читает аудит и манифест.
    warnings: list[str] = Field(default_factory=list)

    @property
    def aspect_ratio(self) -> float:
        return self.slide_width_emu / self.slide_height_emu

    def layout(self, layout_id: str) -> LayoutSpec:
        for layout in self.layouts:
            if layout.id == layout_id:
                return layout
        raise KeyError(f"layout {layout_id!r} не найден в шаблоне {self.source_name!r}")

    def patterns_of(self, pattern_class: PatternClass) -> list[Pattern]:
        return [p for p in self.patterns if p.pattern_class is pattern_class]

    def patterns_matching(
        self,
        needed: dict[SlotRole, int],
        preferred: PatternClass | None = None,
    ) -> list[Pattern]:
        """Паттерны, куда помещается запрошенный набор мест.

        Сначала по структуре, и только потом по классу. Опознанный класс
        поднимает паттерн в выдаче, но неопознанный не исключает его: класс —
        подсказка, а не пропуск.

        Иначе шаблон, композиции которого не подошли ни под одно правило, терял
        бы большинство своих макетов — а именно так выглядит произвольный чужой
        шаблон, ради которого всё и делается. На `vk_education` правила не
        опознают около двенадцати процентов композиций; выбрасывать их значит
        добровольно обеднить вёрстку на восьмую часть.
        """
        candidates = [pattern for pattern in self.patterns if pattern.fits(needed)]

        def rank(pattern: Pattern) -> tuple[int, float, int]:
            class_match = 0 if (preferred and pattern.pattern_class is preferred) else 1
            # Композиция, в классе которой парсер уверен, предпочтительнее
            # неопознанной — но обе остаются в выдаче.
            return (class_match, -pattern.provenance.confidence, pattern.capacity)

        return sorted(candidates, key=rank)

    @model_validator(mode="after")
    def _patterns_point_at_real_layouts(self) -> TemplateSpec:
        known = {layout.id for layout in self.layouts}
        for pattern in self.patterns:
            if pattern.layout_id is not None and pattern.layout_id not in known:
                raise ValueError(
                    f"паттерн {pattern.id!r} ссылается на неизвестный layout "
                    f"{pattern.layout_id!r}"
                )
        return self

    @model_validator(mode="after")
    def _type_scale_is_sorted_and_unique(self) -> TemplateSpec:
        if self.type_scale_pt != sorted(set(self.type_scale_pt)):
            raise ValueError("type_scale_pt должна быть отсортирована и без повторов")
        return self
