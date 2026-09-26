"""Нативные графики в токенах шаблона.

Растеризация запрещена ТЗ, и обойти это картинкой нельзя: график обязан
остаться объектом `c:chart`, чтобы человек мог открыть его данные и
отредактировать. Поэтому здесь нет ни одной строки, рисующей пиксели.

Цвета берутся из палитры шаблона, а не из умолчаний Office. Умолчания синие и
одинаковые у всех, и график в них сразу виден как чужеродный: остальная колода
в цветах шаблона, а столбики — нет.
"""

from __future__ import annotations

from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.util import Emu, Pt

from deckwright.schemas import Box, ChartContent, ChartKind, ChartSeries, Color

# Нашему виду графика — тип в терминах OOXML. Набор намеренно узкий: каждый
# лишний тип это ещё одна раскладка, которую нужно проверять на четырёх
# шаблонах.
_CHART_TYPE = {
    ChartKind.BAR: XL_CHART_TYPE.BAR_CLUSTERED,
    ChartKind.COLUMN: XL_CHART_TYPE.COLUMN_CLUSTERED,
    ChartKind.LINE: XL_CHART_TYPE.LINE_MARKERS,
    ChartKind.PIE: XL_CHART_TYPE.PIE,
    ChartKind.DOUGHNUT: XL_CHART_TYPE.DOUGHNUT,
    ChartKind.SCATTER: XL_CHART_TYPE.XY_SCATTER_LINES,
    ChartKind.AREA: XL_CHART_TYPE.AREA,
}


def series_from_pack(
    series_ids: list[str], pack, palette: list[Color]
) -> tuple[list[str], list[ChartSeries]]:
    """Категории и ряды для графика, собранные из контент-пакета.

    Ряды берутся по идентификаторам из плана: числа в презентацию попадают
    только из пакета, и придумать их здесь неоткуда.

    Категории берутся у первого ряда и служат общими: график с рядами разной
    длины нарисовать нельзя, и молча дорисовывать недостающие точки нечем.
    Ряд, не совпавший по категориям, отбрасывается — врать о данных хуже, чем
    показать меньше данных.
    """
    known = {item.id: item for item in getattr(pack, "series", [])}
    chosen = [known[sid] for sid in series_ids if sid in known]
    if not chosen:
        return [], []

    categories = chosen[0].categories
    out: list[ChartSeries] = []
    for index, item in enumerate(chosen):
        if item.categories != categories:
            continue
        color = palette[index % len(palette)] if palette else Color(rgb="4472C4")
        out.append(ChartSeries(name=item.name, values=item.values, color=color))
    return categories, out


def series_unit(series_ids: list[str], pack) -> str:
    """Единица первого ряда: подписи значений без неё — числа без смысла."""
    known = {item.id: item for item in getattr(pack, "series", [])}
    first = next((known[sid] for sid in series_ids if sid in known), None)
    return first.unit if first is not None else ""


def add_chart(slide, box: Box, content: ChartContent):
    """Кладёт нативный график в рамку и красит его в цвета шаблона."""
    data = CategoryChartData()
    data.categories = content.categories
    for series in content.series:
        data.add_series(series.name, series.values)

    frame = slide.shapes.add_chart(
        _CHART_TYPE[content.chart_kind],
        Emu(box.x),
        Emu(box.y),
        Emu(box.w),
        Emu(box.h),
        data,
    )
    chart = frame.chart
    chart.has_legend = content.has_legend and len(content.series) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False

    _paint(chart, content)
    return frame


def _paint(chart, content: ChartContent) -> None:
    """Красит ряды в палитру шаблона и набирает подписи его гарнитурой.

    Круговая диаграмма красится по точкам, а не по рядам: у неё ряд один, а
    цвет нужен каждому сектору.
    """
    style = content.label_style
    if style is not None:
        chart.font.size = Pt(style.size_pt)
        chart.font.name = style.font_family
        chart.font.color.rgb = RGBColor.from_string(style.color.rgb)

    single_series = content.chart_kind in (ChartKind.PIE, ChartKind.DOUGHNUT)
    if content.highlight and content.muted_color is not None and not single_series:
        _emphasize(chart, content)
        return
    for index, plot_series in enumerate(chart.plots[0].series):
        if single_series:
            for point_index, point in enumerate(plot_series.points):
                color = content.series[point_index % len(content.series)].color
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = RGBColor.from_string(color.rgb)
            continue
        color = content.series[index % len(content.series)].color
        plot_series.format.fill.solid()
        plot_series.format.fill.fore_color.rgb = RGBColor.from_string(color.rgb)


def _emphasize(chart, content: ChartContent) -> None:
    """Главные точки — цветом бренда, остальные — приглушённым; значения — на точках.

    График несёт вывод слайда, а не просто числа: взгляд должен упасть на то,
    о чём заголовок. Ось значений и сетка при подписанных точках — шум, их нет;
    заголовок диаграммы тоже: вывод уже стоит в заголовке слайда.
    """
    accent = RGBColor.from_string(content.series[0].color.rgb)
    muted = RGBColor.from_string(content.muted_color.rgb)
    plot = chart.plots[0]
    plot_series = plot.series[0]
    for index, point in enumerate(plot_series.points):
        point.format.fill.solid()
        point.format.fill.fore_color.rgb = accent if index in content.highlight else muted
    chart.has_title = False
    if content.show_values:
        plot.has_data_labels = True
        labels = plot.data_labels
        labels.number_format = f'0" {content.unit}"' if content.unit else "0"
        labels.number_format_is_linked = False
        labels.position = XL_LABEL_POSITION.OUTSIDE_END
        if content.label_style is not None:
            labels.font.size = Pt(content.label_style.size_pt)
            labels.font.bold = True
        value_axis = chart.value_axis
        value_axis.visible = False
        value_axis.has_major_gridlines = False
