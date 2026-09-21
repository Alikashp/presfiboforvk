FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/app

# libreoffice-impress — конвертация .pptx → .pdf. Один libreoffice-core на
# .pptx отвечает "source file could not be loaded", Impress обязателен.
# poppler-utils — pdftoppm, .pdf → PNG для превью и контекстного аудита.
# libeot0 — распаковка встроенных в шаблон шрифтов: .fntdata это EOT с
# MTX-сжатием, срезом заголовка не достаётся. Вызывается через ctypes
# (EOT2ttf_buffer), отдельного CLI в дистрибутиве нет.
# fonts-* — подстановка, когда шрифт шаблона распаковать не удалось. Три
# из них метрически совпадают с проприетарными оригиналами, то есть дают те
# же ширины символов при другом рисунке: carlito ↔ Calibri, caladea ↔
# Cambria, liberation ↔ Arial / Times New Roman / Courier New. Без них
# Calibri подменялся DejaVu, ширины расходились, и бюджет длины заголовка
# считался по чужой гарнитуре. dejavu остаётся крайним случаем — для
# шрифтов, у которых свободного клона нет.
# Подставляются они только для измерения текста и рендера внутри
# контейнера; в сам .pptx всегда пишется имя шрифта из шаблона.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        poppler-utils \
        libeot0 \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e "."

COPY configs/ ./configs/
COPY prompts/ ./prompts/
COPY agents/ ./agents/
COPY app/ ./app/

RUN mkdir -p /app/outputs /app/.cache/templates /app/.cache/fonts

# Проверка окружения на этапе сборки: если LibreOffice, poppler, libeot или
# шрифтов нет, образ не соберётся — вместо того чтобы падать на первом прогоне.
RUN deckwright doctor

EXPOSE 8501
# Фаза 0: веб-интерфейса ещё нет, точка входа — проверка окружения.
# В фазе 10 заменяется на: streamlit run app/ui.py --server.address=0.0.0.0
CMD ["deckwright", "doctor"]
