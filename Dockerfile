FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/app

# Системные пакеты одним списком на все окружения: образ, CI и машину
# разработчика. Что именно и зачем — в самом скрипте; держать список в трёх
# местах значит однажды их разъехать.
COPY scripts/install-system-deps.sh /tmp/install-system-deps.sh
RUN sh /tmp/install-system-deps.sh \
    && rm -rf /var/lib/apt/lists/* /tmp/install-system-deps.sh

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
# Веб-интерфейс — фаза 11; пока точка входа — проверка окружения.
# Тогда заменяется на: streamlit run app/ui.py --server.address=0.0.0.0
CMD ["deckwright", "doctor"]
