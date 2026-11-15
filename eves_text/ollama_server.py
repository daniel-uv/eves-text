"""Отдельная служба Ollama с выбранной папкой моделей.

Системная Ollama хранит модели в своей папке и читает OLLAMA_MODELS только
при запуске. Чтобы пользователь мог сам выбрать место, приложение запускает
второй экземпляр `ollama serve` на свободном порту и передаёт ему нужную
папку. Системная служба при этом не меняется.

Модуль не зависит от GTK.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

READY_TIMEOUT = 30.0


class OllamaStartError(RuntimeError):
    """Не удалось запустить отдельную службу Ollama."""


def find_ollama() -> str | None:
    """Путь к исполняемому файлу ollama или None."""
    found = shutil.which("ollama")
    if found and Path(found).is_file():
        return found

    if sys.platform == "win32":
        candidates: list[Path] = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Ollama" / "ollama.exe")
        candidates.append(
            Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
        )
    else:
        candidates = [
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
            Path("/opt/homebrew/bin/ollama"),
            Path.home() / ".local" / "bin" / "ollama",
        ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def default_models_dir() -> Path:
    """Папка, которую приложение предлагает по умолчанию."""
    configured = os.environ.get("OLLAMA_MODELS")
    if configured:
        return Path(configured)
    return Path.home() / ".ollama" / "models"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _log_tail(path: Path | None, limit: int = 1200) -> str:
    if path is None:
        return ""
    try:
        data = path.read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", "replace").strip()


class ManagedOllama:
    """Второй экземпляр Ollama, привязанный к папке моделей."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._base_url: str | None = None
        self._models_dir: Path | None = None
        self._log_path: Path | None = None
        self._lock = threading.Lock()
        # Растёт при каждом stop(): запуск, начавшийся раньше, отменяется.
        self._generation = 0

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def models_dir(self) -> Path | None:
        return self._models_dir

    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def ensure(
        self, models_dir: str | Path, log_path: str | Path | None = None
    ) -> str:
        """Запускает службу для папки (если ещё не запущена) и возвращает URL."""
        directory = Path(models_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        log = Path(log_path).expanduser() if log_path is not None else None

        with self._lock:
            if (
                self._process is not None
                and self._process.poll() is None
                and self._models_dir == directory
                and self._base_url is not None
            ):
                return self._base_url

            self._generation += 1
            generation = self._generation
            previous = self._process
            self._process = None
            self._base_url = None
            self._models_dir = None
            self._log_path = None

        if previous is not None:
            self._terminate(previous)

        executable = find_ollama()
        if executable is None:
            raise OllamaStartError(
                "Не найден исполняемый файл Ollama.\n"
                "Установите Ollama с https://ollama.com и запустите приложение снова."
            )

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"

        environment = os.environ.copy()
        environment["OLLAMA_HOST"] = f"127.0.0.1:{port}"
        environment["OLLAMA_MODELS"] = str(directory)

        options: dict = {}
        if sys.platform == "win32":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            options["cwd"] = str(Path(executable).parent)
        else:
            options["start_new_session"] = True

        log_file = None
        try:
            if log is not None:
                log.parent.mkdir(parents=True, exist_ok=True)
                log_file = open(log, "wb")
                output = log_file
            else:
                output = subprocess.DEVNULL

            process = subprocess.Popen(
                [executable, "serve"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                **options,
            )
        except OSError as exc:
            raise OllamaStartError(f"Не удалось запустить Ollama: {exc}") from exc
        finally:
            if log_file is not None:
                log_file.close()

        with self._lock:
            cancelled = self._generation != generation
            if not cancelled:
                self._process = process
                self._base_url = base_url
                self._models_dir = directory
                self._log_path = log

        if cancelled:
            self._terminate(process)
            raise OllamaStartError("Запуск Ollama отменён.")

        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{base_url}/api/tags", timeout=1
                ) as response:
                    response.read()
            except OSError:
                with self._lock:
                    cancelled = self._generation != generation
                if cancelled:
                    self._terminate(process)
                    raise OllamaStartError("Запуск Ollama отменён.")
                if process.poll() is not None:
                    self._forget(generation)
                    tail = _log_tail(log)
                    message = "Ollama завершилась сразу после запуска."
                    if tail:
                        message += f"\n\n{tail}"
                    raise OllamaStartError(message)
                time.sleep(0.15)
                continue

            with self._lock:
                cancelled = self._generation != generation
            if cancelled:
                self._terminate(process)
                raise OllamaStartError("Запуск Ollama отменён.")
            return base_url

        self._forget(generation)
        self._terminate(process)
        tail = _log_tail(log)
        message = (
            f"Ollama не успела запуститься за {READY_TIMEOUT:.0f} секунд.\n"
            "Проверьте, что Ollama установлена и работает."
        )
        if tail:
            message += f"\n\n{tail}"
        raise OllamaStartError(message)

    def stop(self) -> None:
        """Останавливает только запущенную приложением службу."""
        with self._lock:
            self._generation += 1
            process = self._process
            self._process = None
            self._base_url = None
            self._models_dir = None
            self._log_path = None
        if process is not None:
            self._terminate(process)

    def _forget(self, generation: int) -> None:
        """Забывает службу, если её не успели заменить через stop()."""
        with self._lock:
            if self._generation != generation:
                return
            self._process = None
            self._base_url = None
            self._models_dir = None
            self._log_path = None

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                return
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
