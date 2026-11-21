#!/usr/bin/env bash
# Сборка переносимого Windows-бандла Eve's Text.
#
# Запускать в оболочке MSYS2 UCRT64 (или из GitHub Actions):
#   bash windows/build-portable.sh
#
# Результат: windows/dist/Eve's Text/ — готовая папка приложения,
# внутри bin\python.exe, все DLL GTK, словарь и лаунчер.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${MSYS2_PREFIX:-/ucrt64}"
NAME="Eve's Text"
DIST_ROOT="$ROOT/windows/dist"
DIST="$DIST_ROOT/$NAME"
VERSION="$(sed -n 's/.*__version__ = "\([^"]*\)".*/\1/p' "$ROOT/eves_text/__init__.py" | head -1)"

if [ -z "$VERSION" ]; then
    echo "Не удалось определить версию" >&2
    exit 1
fi
if [ ! -x "$PREFIX/bin/python.exe" ]; then
    echo "Не найден $PREFIX/bin/python.exe — запустите скрипт в MSYS2 UCRT64" >&2
    exit 1
fi

PYVER="$("$PREFIX/bin/python.exe" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

echo "==> Eve's Text $VERSION для Windows (Python $PYVER)"

rm -rf "$DIST_ROOT"
mkdir -p \
    "$DIST/bin" \
    "$DIST/lib" \
    "$DIST/share" \
    "$DIST/app" \
    "$DIST/etc"

# --- Python и все DLL рантайма -------------------------------------------
cp "$PREFIX/bin/python.exe" "$PREFIX/bin/pythonw.exe" "$DIST/bin/"
cp "$PREFIX/bin/"*.dll "$DIST/bin/"

for exe in gdk-pixbuf-query-loaders.exe gtk-query-immodules-3.0.exe; do
    if [ -f "$PREFIX/bin/$exe" ]; then
        cp "$PREFIX/bin/$exe" "$DIST/bin/"
    fi
done

cp -a "$PREFIX/lib/python$PYVER" "$DIST/lib/"
find "$DIST/lib/python$PYVER" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- GTK: typelibs, загрузчики, модули ввода, enchant ---------------------
cp -a "$PREFIX/lib/girepository-1.0" "$DIST/lib/"
cp -a "$PREFIX/lib/gdk-pixbuf-2.0" "$DIST/lib/"
cp -a "$PREFIX/lib/gtk-3.0" "$DIST/lib/"
if [ -d "$PREFIX/lib/enchant-2" ]; then
    cp -a "$PREFIX/lib/enchant-2" "$DIST/lib/"
fi
# Кэши содержат абсолютные пути сборочной машины — генерируем при первом запуске.
rm -f "$DIST/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
rm -f "$DIST/lib/gtk-3.0/3.0.0/immodules.cache"

# --- GSettings, иконки, переводы, MIME -----------------------------------
mkdir -p "$DIST/share/glib-2.0"
cp -a "$PREFIX/share/glib-2.0/schemas" "$DIST/share/glib-2.0/"
if command -v glib-compile-schemas >/dev/null 2>&1; then
    # cd + "." — чтобы не зависеть от преобразования POSIX-путей в Windows.
    ( cd "$DIST/share/glib-2.0/schemas" && glib-compile-schemas . )
fi

# Без файлов тем GTK на Windows не найдёт Adwaita-dark: меню и нижние панели
# останутся светлыми после переключения на тёмную тему.
mkdir -p "$DIST/share/themes"
for theme in Adwaita Adwaita-dark; do
    if [ -d "$PREFIX/share/themes/$theme" ]; then
        cp -a "$PREFIX/share/themes/$theme" "$DIST/share/themes/"
    fi
done

# Подсветка синтаксиса и стили GtkSourceView (oblivion/kate и т.д.).
if [ -d "$PREFIX/share/gtksourceview-4" ]; then
    cp -a "$PREFIX/share/gtksourceview-4" "$DIST/share/"
fi

# Порядок провайдеров enchant (hunspell/nuspell/winspell).
if [ -d "$PREFIX/share/enchant-2" ]; then
    cp -a "$PREFIX/share/enchant-2" "$DIST/share/"
fi

mkdir -p "$DIST/share/icons"
if [ -d "$PREFIX/share/icons/Adwaita" ]; then
    cp -a "$PREFIX/share/icons/Adwaita" "$DIST/share/icons/"
fi
# adwaita-icon-theme 51 — только index.theme; сами значки лежат в AdwaitaLegacy.
if [ -d "$PREFIX/share/icons/AdwaitaLegacy" ]; then
    cp -a "$PREFIX/share/icons/AdwaitaLegacy" "$DIST/share/icons/"
fi
if [ -d "$PREFIX/share/icons/hicolor" ]; then
    cp -a "$PREFIX/share/icons/hicolor" "$DIST/share/icons/"
fi
mkdir -p "$DIST/share/icons/hicolor/scalable/apps"
cp "$ROOT/eves_text/resources/eves-text.svg" \
   "$DIST/share/icons/hicolor/scalable/apps/eves-text.svg"
# PNG-копии нужны на случай, если SVG-загрузчик gdk-pixbuf не подхватился.
if [ -d "$ROOT/windows/assets/icons/hicolor" ]; then
    cp -a "$ROOT/windows/assets/icons/hicolor/." "$DIST/share/icons/hicolor/"
fi
# В пакетах MSYS2 лежат icon-theme.cache, собранные без наших PNG: GTK
# читает кэш и не видит иконку приложения. Пересобираем кэши тем.
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    for theme in Adwaita AdwaitaLegacy hicolor; do
        if [ -f "$DIST/share/icons/$theme/index.theme" ]; then
            ( cd "$DIST/share/icons" && gtk-update-icon-cache -f -q "$theme" ) || true
        fi
    done
fi

if [ -d "$PREFIX/share/locale" ]; then
    cp -a "$PREFIX/share/locale" "$DIST/share/"
fi
if [ -d "$PREFIX/share/mime" ]; then
    cp -a "$PREFIX/share/mime" "$DIST/share/"
fi

# --- Шрифты (fontconfig) --------------------------------------------------
if [ -d "$PREFIX/etc/fonts" ]; then
    cp -a "$PREFIX/etc/fonts" "$DIST/etc/"
fi

# --- Словарь hunspell -----------------------------------------------------
mkdir -p "$DIST/share/hunspell"
cp "$ROOT/windows/hunspell/"*.aff "$ROOT/windows/hunspell/"*.dic "$DIST/share/hunspell/"
cp "$ROOT/windows/hunspell/LICENSE.txt" "$DIST/share/hunspell/LICENSE.txt"

# --- Код приложения и лаунчер --------------------------------------------
cp -a "$ROOT/eves_text" "$DIST/app/"
find "$DIST/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DIST/app" -name '*.pyc' -delete 2>/dev/null || true
cp "$ROOT/windows/launcher.py" "$DIST/app/launcher.py"
cp "$ROOT/windows/check_runtime.py" "$DIST/app/check_runtime.py"

# --- Нативный GUI-лагер ---------------------------------------------------
# Он нужен, чтобы Windows показывала Eve's Text в «Приложениях по умолчанию»
# как обычную программу, а не как pythonw.exe с параметрами.
CC="${CC:-$PREFIX/bin/gcc.exe}"
if [ ! -x "$CC" ]; then
    CC="$(command -v gcc || true)"
fi
if [ -z "$CC" ] || [ ! -x "$CC" ]; then
    echo "Не найден GCC — нужен mingw-w64-ucrt-x86_64-gcc" >&2
    exit 1
fi
# GCC на MSYS2 не преобразует путь с апострофом в каталоге, поэтому собираем
# в windows/ (без апострофа) и только потом кладём exe в папку приложения.
TMP_EXE="$ROOT/windows/eves-text-launcher.exe"
"$CC" -municode -mwindows -O2 -s \
    "$ROOT/windows/launcher.c" -o "$TMP_EXE" -lshell32
if [ ! -f "$TMP_EXE" ]; then
    echo "Лаунчер не собран" >&2
    exit 1
fi
mv "$TMP_EXE" "$DIST/Eve's Text.exe"
if [ ! -f "$DIST/Eve's Text.exe" ]; then
    echo "Лаунчер не собран" >&2
    exit 1
fi

# --- Ярлыки и документы ---------------------------------------------------
cp "$ROOT/windows/eves-text.vbs" "$DIST/Eve's Text.vbs"
cp "$ROOT/windows/eves-text.bat" "$DIST/eves-text.bat"
if [ -f "$ROOT/windows/assets/eves-text.ico" ]; then
    cp "$ROOT/windows/assets/eves-text.ico" "$DIST/eves-text.ico"
fi
cp "$ROOT/LICENSE" "$DIST/LICENSE"
cp "$ROOT/README.md" "$DIST/README.md"
if [ -f "$ROOT/windows/README.md" ]; then
    cp "$ROOT/windows/README.md" "$DIST/README-Windows.md"
fi
printf '%s\n' "$VERSION" > "$DIST/VERSION.txt"

echo "==> содержимое:"
du -sh "$DIST"
echo "==> готово: $DIST"
