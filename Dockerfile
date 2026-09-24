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
# Демонстрационный контент-пакет и записанные ответы модели: интерфейс
# предлагает их галочками, и без них в образе галочка роняла прогон.
COPY tests/fixtures/content_pack.json ./tests/fixtures/content_pack.json
COPY tests/fixtures/recorded/ ./tests/fixtures/recorded/

RUN mkdir -p /app/outputs /app/.cache/templates /app/.cache/fonts

# Проверка окружения на этапе сборки: если LibreOffice, poppler, libeot или
# шрифтов нет, образ не соберётся — вместо того чтобы падать на первом прогоне.
RUN deckwright doctor

EXPOSE 8501
# Порт — из $PORT, если хостинг его задаёт (Railway задаёт), иначе 8501.
# Статистика использования Streamlit выключена: это исходящий трафик, по
# которому хостинг считает сервис активным и не даёт ему уснуть.
CMD ["sh", "-c", "exec streamlit run app/ui.py --server.address=0.0.0.0 --server.port=${PORT:-8501} --server.headless=true --browser.gatherUsageStats=false"]
