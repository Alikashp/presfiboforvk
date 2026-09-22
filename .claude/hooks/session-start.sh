#!/bin/bash
# Подготовка сессии Claude Code на вебе: то же окружение, что в образе и в CI.
#
# Нужен потому, что контейнер веб-сессии — третье окружение сервиса, и списка
# пакетов у него нет. Без libreoffice-impress один libreoffice-core отвечает
# на .pptx «source file could not be loaded», и семь тестов падают так, будто
# сломан код. Список берётся общий — scripts/install-system-deps.sh.
set -euo pipefail

# На машине разработчика окружение своё, трогать его нечего.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

sh scripts/install-system-deps.sh

# PyYAML в этом образе стоит пакетом дистрибутива и pip'ом не удаляется:
# без --ignore-installed установка падает на попытке его снести.
pip install -e ".[dev]" --ignore-installed PyYAML

# Шаблоны датасета в репозиторий не коммитятся (58 МБ чужих материалов), и без
# них три четверти тестов молча пропускаются: «зелёный» прогон тогда ничего не
# значит. CI собирает синтетические на месте — здесь то же самое.
python tests/fixtures/make_template.py .cache/ci-templates/synthetic.pptx
python tests/fixtures/make_holdout.py .cache/ci-templates/holdout
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export DECKWRIGHT_TEMPLATES=$(pwd)/.cache/ci-templates" >> "$CLAUDE_ENV_FILE"
fi

# Последнее слово — за собственной проверкой окружения сервиса.
deckwright doctor
