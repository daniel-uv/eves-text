#!/usr/bin/env python3
"""Запуск Eve's Text из Windows-бандла (MSYS2 UCRT64).

Скрипт настраивает окружение GTK (typelibs, схемы GSettings, загрузчики
gdk-pixbuf, модули ввода, шрифты, словарь hunspell) и запускает приложение.
Кэши плагинов генерируются один раз в %LOCALAPPDATA%\\Eve's Text\\cache.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path


def base_dir() -> Path:
    """Корень бандла: ...\\Eve's Text (на уровень выше app\\)."""
    return Path(__file__).resolve().parent.parent


def local_app_data() -> Path:
    root = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or str(Path.home())
    )
    path = Path(root) / "Eve's Text"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _subprocess_options() -> dict:
    """Не показывать окно консоли при запуске служебных программ Windows."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def make_cache(
    exe: Path,
    target: Path,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> Path | None:
    """Сгенерировать кэш плагинов, если он ещё не создан."""
    if not exe.exists():
        return None
    try:
        if target.exists() and target.stat().st_mtime >= exe.stat().st_mtime:
            return target
    except OSError:
        pass

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            [str(exe), *(args or [])],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
            check=False,
            **_subprocess_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return None

    output = result.stdout or ""
    if result.returncode != 0 or not output.strip():
        return None
    # Парсер gdk-pixbuf добавляет модуль в список только после пустой строки.
    # Если обрезать хвост, последний загрузчик (pixbufloader_svg) потеряется.
    if not output.endswith("\n\n"):
        output = output.rstrip("\n") + "\n\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
    except OSError:
        return None
    return target


def configure_enchant(base: Path) -> None:
    """Указать enchant, что провайдеры лежат в бандле, а не в /ucrt64.

    Enchant ищет провайдеры (enchant_hunspell.dll и т.д.) относительно
    каталога установки, который в MSYS2-сборке зашит как /ucrt64. При
    переносе папки этот путь не существует, поэтому префикс задаём явно
    до первого обращения к gspell.
    """
    dll = base / "bin" / "libenchant-2-2.dll"
    if not dll.exists():
        return
    try:
        import ctypes

        library = ctypes.CDLL(str(dll))
        set_prefix = library.enchant_set_prefix_dir
        set_prefix.argtypes = [ctypes.c_char_p]
        set_prefix.restype = None
        prefix = str(base)
        try:
            raw = prefix.encode("mbcs")
        except UnicodeEncodeError:
            raw = prefix.encode("utf-8", "replace")
        set_prefix(raw)
    except Exception:  # noqa: BLE001 - без enchant приложение всё равно стартует
        pass


APP_USER_MODEL_ID = "DanielUsov.EvesText"


def set_app_user_model_id() -> None:
    """Привязать окно GTK (процесс pythonw.exe) к AUMID приложения.

    Нативный лаунчер задаёт AUMID только себе, а окно принадлежит дочернему
    процессу. Без вызова здесь Windows группирует окно с pythonw.exe и
    показывает системную иконку вместо иконки Eve's Text.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        set_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [ctypes.c_wchar_p]
        set_id.restype = None
        set_id(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001 - на старых Windows функции может не быть
        pass


def configure(base: Path) -> None:
    """Прописать пути бандла в переменные окружения GTK/GLib."""
    bin_dir = base / "bin"
    lib = base / "lib"
    share = base / "share"

    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    os.environ["GI_TYPELIB_PATH"] = str(lib / "girepository-1.0")
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(share / "glib-2.0" / "schemas")
    os.environ["FONTCONFIG_PATH"] = str(base / "etc" / "fonts")
    os.environ["XDG_DATA_DIRS"] = str(share)
    os.environ["EVES_TEXT_DATA_DIR"] = str(share)
    # Enchant ищет словари hunspell по DICPATH.
    os.environ["DICPATH"] = str(share / "hunspell")
    # GTK иначе возьмёт переводы из /ucrt64/share/locale.
    os.environ.setdefault("GTK_LOCALEDIR", str(share / "locale"))
    # Файлы из Проводника принимает собственный COM IDropTarget окна.
    # eves_text.win_dnd до импорта Gtk убирает experimental OLE2-флаг GDK,
    # чтобы GDK не регистрировал свой приёмник на том же HWND.

    cache_dir = local_app_data() / "cache"

    loaders = lib / "gdk-pixbuf-2.0" / "2.10.0" / "loaders"
    # Без аргументов query-loaders пропускает pixbufloader_svg.dll (librsvg
    # кладёт его без префикса lib), поэтому перечисляем загрузчики явно.
    loader_args = (
        [str(item) for item in sorted(loaders.glob("*.dll"))]
        if loaders.is_dir()
        else []
    )
    loaders_cache = make_cache(
        bin_dir / "gdk-pixbuf-query-loaders.exe",
        cache_dir / "loaders-2.cache",
        {"GDK_PIXBUF_MODULEDIR": str(loaders)},
        args=loader_args,
    )
    if loaders_cache is not None:
        os.environ["GDK_PIXBUF_MODULE_FILE"] = str(loaders_cache)

    immodules_cache = make_cache(
        bin_dir / "gtk-query-immodules-3.0.exe",
        cache_dir / "immodules.cache",
    )
    if immodules_cache is not None:
        os.environ["GTK_IM_MODULE_FILE"] = str(immodules_cache)

    # Без этого GTK на Windows может искать переводы и иконки в /ucrt64.
    os.environ.setdefault("GTK_DATA_PREFIX", str(base))

    configure_enchant(base)


def show_error(log_path: Path) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "Не удалось запустить Eve's Text.\n\n"
            f"Подробности записаны в файл:\n{log_path}",
            "Eve's Text",
            0x10,
        )
    except Exception:  # noqa: BLE001 - без GUI всё равно останется лог
        pass


def run(base: Path) -> int:
    configure(base)
    os.chdir(Path.home())
    sys.path.insert(0, str(base / "app"))
    from eves_text.application import main as app_main

    return app_main(sys.argv)


def main() -> int:
    base = base_dir()
    set_app_user_model_id()
    try:
        return run(base)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - пишем лог и показываем окно
        log_path = local_app_data() / "crash.log"
        try:
            log_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        show_error(log_path)
        return 1


if __name__ == "__main__":
    sys.exit(main())
