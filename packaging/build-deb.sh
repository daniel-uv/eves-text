#!/usr/bin/env bash
# Сборка .deb без debhelper: вручную собираем дерево файлов и упаковываем dpkg-deb.
#
# Использование:
#   ./packaging/build-deb.sh
#   MAINTAINER="Имя <mail@example.org>" ./packaging/build-deb.sh
#
# Результат: dist/eves-text_<версия>_all.deb
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PKG="eves-text"
VERSION="$(python3 -c 'import pathlib,re; print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("eves_text/__init__.py").read_text(encoding="utf-8")).group(1))')"
DEB_VERSION="${VERSION}-1"
ARCH="all"
MAINTAINER="${MAINTAINER:-Jean <jean@localhost>}"

STAGE="$ROOT/build/${PKG}_${DEB_VERSION}_${ARCH}"
DIST="$ROOT/dist"
PREFIX="$STAGE/usr"

echo "==> $PKG $DEB_VERSION ($ARCH)"

rm -rf "$STAGE"
mkdir -p \
    "$PREFIX/lib/eves-text" \
    "$PREFIX/bin" \
    "$PREFIX/share/applications" \
    "$PREFIX/share/icons/hicolor/scalable/apps" \
    "$PREFIX/share/doc/$PKG"

# --- код приложения
cp -a eves_text "$PREFIX/lib/eves-text/"
find "$PREFIX/lib/eves-text" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$PREFIX/lib/eves-text" -name '*.pyc' -delete

# Точка входа: скрипт лежит рядом с пакетом, поэтому import eves_text работает.
cat > "$PREFIX/lib/eves-text/eves-text" <<'PY'
#!/usr/bin/python3
"""Запуск установленного редактора."""
import sys

from eves_text.application import main

if __name__ == "__main__":
    sys.exit(main())
PY
chmod 755 "$PREFIX/lib/eves-text/eves-text"

# Обёртка в /usr/bin, чтобы не зависеть от разрешения символических ссылок.
cat > "$PREFIX/bin/$PKG" <<'SH'
#!/bin/sh
exec /usr/lib/eves-text/eves-text "$@"
SH
chmod 755 "$PREFIX/bin/$PKG"

# --- данные
install -m644 eves_text/resources/eves-text.desktop \
    "$PREFIX/share/applications/$PKG.desktop"
install -m644 packaging/eves-text.svg \
    "$PREFIX/share/icons/hicolor/scalable/apps/$PKG.svg"
install -m644 packaging/copyright "$PREFIX/share/doc/$PKG/copyright"

{
    echo "$PKG ($DEB_VERSION) unstable; urgency=medium"
    echo
    echo "  * Версия $VERSION."
    echo "  * Проект переименован в Eve's Text."
    echo "  * Иконка: белый лист с маленьким уголком и красной буквой E."
    echo "  * Нижняя панель форматирования выровнена по центру окна."
    echo "  * Номера строк по умолчанию выключены; у текста есть поля от краёв."
    echo "  * Проверка текста с ИИ (Ollama) через правый клик по выделению."
    echo "  * Кнопка «Выключить ИИ» выгружает модель из памяти."
    echo "  * Текстовый редактор: UTF-8/CP1251, проверка орфографии через gspell,"
    echo "    перенос строк, нижняя панель с размером шрифта и выравниванием"
    echo "    абзацев, табуляция, статистика слов и символов."
    echo
    echo " -- $MAINTAINER  $(LC_ALL=C date -R)"
} | gzip -9n > "$PREFIX/share/doc/$PKG/changelog.gz"

# --- нормализуем права: 644 для файлов, 755 для каталогов и запускаемых
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$PREFIX/bin/$PKG" "$PREFIX/lib/eves-text/eves-text"

# --- метаданные пакета
INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
mkdir -p "$STAGE/DEBIAN"
sed -e "s|@VERSION@|$DEB_VERSION|g" \
    -e "s|@INSTALLED_SIZE@|$INSTALLED_SIZE|g" \
    -e "s|@MAINTAINER@|$MAINTAINER|g" \
    packaging/control.in > "$STAGE/DEBIAN/control"
chmod 644 "$STAGE/DEBIAN/control"
install -m755 packaging/postinst "$STAGE/DEBIAN/postinst"
install -m755 packaging/postrm "$STAGE/DEBIAN/postrm"

# --- сборка
mkdir -p "$DIST"
DEB="$DIST/${PKG}_${DEB_VERSION}_${ARCH}.deb"
rm -f "$DEB"
dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$DEB" >/dev/null

echo
dpkg-deb --info "$DEB"

# --- проверки
if command -v lintian >/dev/null 2>&1; then
    echo "==> lintian"
    lintian "$DEB" || true
fi

if [ "${SKIP_TEST:-0}" != "1" ]; then
    echo "==> проверка содержимого пакета"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    dpkg-deb -x "$DEB" "$TMP"
    "$TMP/usr/lib/eves-text/eves-text" --version
    test -x "$TMP/usr/bin/$PKG"
    test -f "$TMP/usr/share/applications/$PKG.desktop"
    test -f "$TMP/usr/share/icons/hicolor/scalable/apps/$PKG.svg"
    echo "    структура пакета в порядке"
fi

echo
echo "Готово: $DEB ($(du -h "$DEB" | cut -f1))"
echo "Установка: sudo dpkg -i \"$DEB\""
