"""Проверка обновлений Eve's Text на GitHub (без GTK)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = "daniel-uv/eves-text"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
USER_AGENT = "Eve's-Text-Updater"
TIMEOUT = 20
CHUNK_SIZE = 256 * 1024


class UpdateError(Exception):
    """Не удалось проверить обновление или скачать файл."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    asset_name: str
    asset_url: str
    asset_size: int
    page_url: str


def platform_kind() -> str:
    """Какая сборка нужна этой системе."""
    return "windows" if sys.platform.startswith("win") else "linux"


def tag_version(tag: str) -> str:
    """``v0.6.5`` и ``win-v0.6.5`` -> ``0.6.5``."""
    return re.sub(r"^(?:win-)?v", "", tag.strip())


def version_key(text: str) -> tuple[int, ...]:
    """Сравнимые числа версии: ``0.6.10`` больше ``0.6.9``."""
    parts = re.findall(r"\d+", text)
    return tuple(int(part) for part in parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def _find_asset(assets: list[dict], kind: str) -> dict | None:
    if kind == "windows":
        # Установщик предпочтительнее портативного ZIP.
        suffixes = (".exe", ".zip")
    else:
        suffixes = (".deb",)
    for suffix in suffixes:
        for asset in assets:
            name = str(asset.get("name") or "")
            if not name.lower().endswith(suffix):
                continue
            if suffix == ".exe" and "setup" not in name.lower():
                continue
            return asset
    return None


def select_release(
    releases: list[dict], current: str, kind: str
) -> ReleaseInfo | None:
    """Самая новая подходящая версия новее ``current`` или ``None``."""
    best: ReleaseInfo | None = None
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "")
        if kind == "windows":
            if not tag.startswith("win-v"):
                continue
        elif not tag.startswith("v"):
            continue
        version = tag_version(tag)
        if not is_newer(version, current):
            continue
        asset = _find_asset(list(release.get("assets") or []), kind)
        if asset is None:
            continue
        info = ReleaseInfo(
            version=version,
            tag=tag,
            asset_name=str(asset.get("name") or ""),
            asset_url=str(asset.get("browser_download_url") or ""),
            asset_size=int(asset.get("size") or 0),
            page_url=str(release.get("html_url") or ""),
        )
        if not info.asset_url:
            continue
        if best is None or is_newer(info.version, best.version):
            best = info
    return best


def fetch_releases(timeout: float = TIMEOUT) -> list[dict]:
    """Список релизов с GitHub API."""
    request = urllib.request.Request(
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"Не удалось проверить обновления: {exc}") from exc
    if not isinstance(data, list):
        raise UpdateError("GitHub вернул неожиданный ответ.")
    return data


def download_asset(
    url: str,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = TIMEOUT,
) -> Path:
    """Скачивает файл обновления, не оставляя недокачанный ``.part``."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with partial.open("wb") as output:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
        partial.replace(destination)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        try:
            partial.unlink()
        except OSError:
            pass
        raise UpdateError(f"Не удалось скачать обновление: {exc}") from exc
    return destination


def launch_windows_installer(path: Path) -> tuple[bool, str]:
    """Запускает скачанный установщик Windows с повышением прав (UAC)."""
    if platform_kind() != "windows":
        return False, "Автоматическая установка доступна только в Windows."
    path = Path(path)
    if not path.is_file():
        return False, f"Файл обновления не найден:\n{path}"

    import ctypes  # локально: на Linux у ctypes нет windll

    parameters = "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS"
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(
            None,
            "runas",
            str(path),
            parameters,
            str(path.parent),
            1,
        )
    except (AttributeError, OSError) as exc:
        return False, f"Не удалось запустить установщик:\n{exc}"

    code = int(result or 0)
    if code > 32:
        return True, ""
    if code == 5:
        return False, "Запуск установщика отменён или запрещён Windows (UAC)."
    return False, f"Не удалось запустить установщик (код {code})."


def launch_linux_installer(path: Path) -> tuple[bool, str]:
    """Устанавливает скачанный ``.deb`` через pkexec.

    pkexec показывает системный запрос пароля администратора. Функция
    ждёт завершения dpkg, поэтому вызывается из фонового потока.
    """
    if platform_kind() != "linux":
        return False, "Автоматическая установка доступна только в Linux."
    path = Path(path)
    if not path.is_file():
        return False, f"Файл обновления не найден:\n{path}"

    pkexec = shutil.which("pkexec")
    if pkexec is None:
        return False, (
            "Не найден pkexec (PolicyKit), поэтому автоматическая установка "
            "недоступна. Установите обновление вручную:\n"
            f"sudo dpkg -i «{path}»"
        )
    dpkg = shutil.which("dpkg")
    if dpkg is None:
        return False, (
            "Не найден dpkg, поэтому автоматическая установка недоступна. "
            f"Откройте пакет вручную:\n{path}"
        )

    try:
        result = subprocess.run(
            [pkexec, dpkg, "-i", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "Установка не завершилась за 5 минут и была прервана."
    except OSError as exc:
        return False, f"Не удалось запустить установку:\n{exc}"

    if result.returncode == 0:
        return True, ""

    output = (result.stdout or "").strip()
    if result.returncode in (126, 127):
        return False, "Установка отменена: не удалось подтвердить права администратора."
    message = f"dpkg завершился с кодом {result.returncode}."
    if output:
        message += "\n\n" + output[-1200:]
    return False, message


def restart_after_exit() -> tuple[bool, str]:
    """Запускает установленную версию после выхода текущего процесса.

    Новый процесс не должен стартовать раньше старого: иначе он лишь
    активирует уже запущенное приложение. Небольшой помощник ждёт, пока
    завершится текущий PID, и только потом запускает редактор.
    """
    if platform_kind() != "linux":
        return False, "Перезапуск после обновления доступен только в Linux."

    entry = Path("/usr/lib/eves-text/eves-text")
    if entry.is_file():
        command = [str(entry)]
    else:
        executable = shutil.which("eves-text")
        command = [executable] if executable else [sys.executable, "-m", "eves_text"]

    waiter = (
        'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; '
        'shift; exec "$@"'
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", waiter, "eves-text-update", str(os.getpid()), *command],
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"Не удалось перезапустить приложение:\n{exc}"
    return True, ""
