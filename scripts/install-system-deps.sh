#!/usr/bin/env sh
# Системные пакеты сервиса — один список на все окружения.
#
# Их три: образ (Dockerfile), раннер CI и машина разработчика. Пока список
# лежал в каждом месте отдельно, они разъезжались молча: без
# libreoffice-impress один libreoffice-core отвечает на .pptx «source file
# could not be loaded», и это выглядит как поломка кода, а не как нехватка
# пакета. Поэтому список здесь, а не в трёх файлах.
#
# Запуск: sh scripts/install-system-deps.sh
set -eu

# Под root (в образе) sudo нет и не нужен; на раннере — наоборот.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

PACKAGES="
libreoffice-impress
poppler-utils
libeot0
fonts-dejavu-core
fonts-liberation
fonts-crosextra-carlito
fonts-crosextra-caladea
fontconfig
"
# libreoffice-impress — конвертация .pptx → .pdf.
# poppler-utils — pdftoppm и pdfinfo: .pdf → PNG для превью и аудита.
# libeot0 — распаковка встроенных в шаблон шрифтов (.fntdata — это EOT с
#   MTX-сжатием); вызывается через ctypes, отдельного CLI в дистрибутиве нет.
# fonts-crosextra-carlito, fonts-crosextra-caladea, fonts-liberation —
#   метрические клоны: те же ширины символов при другом рисунке
#   (carlito ↔ Calibri, caladea ↔ Cambria, liberation ↔ Arial и др.). Без них
#   бюджет длины заголовка считается по чужой гарнитуре, и вёрстка едет.
# fonts-dejavu-core — крайний случай, когда свободного клона нет.

$SUDO apt-get update
# shellcheck disable=SC2086
$SUDO apt-get install -y --no-install-recommends $PACKAGES
