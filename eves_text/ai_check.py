"""Проверка орфографии, грамматики и пунктуации локальной моделью через Ollama.

Модуль не зависит от GTK: его можно вызывать из фонового потока.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Адрес локальной службы Ollama и модель. Можно переопределить переменными
# окружения, не меняя код.
BASE_URL = os.environ.get("RU_EDITOR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL_NAME = os.environ.get("RU_EDITOR_AI_MODEL", "qwen2.5:7b-instruct")

# Ограничение на размер одного запроса: длинный текст режется по строкам,
# чтобы модель не теряла конец фрагмента.
MAX_CHUNK = 4000
TIMEOUT = 300

SYSTEM_PROMPT = (
    "Ты — корректор русского текста. Исправь только орфографию, пунктуацию "
    "и грамматику. Не переписывай текст, не меняй стиль, порядок слов и смысл. "
    "Сохрани переводы строк, отступы и табуляцию. "
    "Верни только исправленный текст, без пояснений, комментариев и кавычек."
)


class AIError(RuntimeError):
    """Ошибка, текст которой можно показать пользователю."""


def _post(path: str, payload: dict, timeout: float = TIMEOUT) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001 - тело ответа может быть не JSON
            pass
        if exc.code == 404 or "not found" in detail:
            raise AIError(
                f"Модель «{MODEL_NAME}» не загружена.\n"
                f"Выполните в терминале: ollama pull {MODEL_NAME}"
            ) from exc
        raise AIError(f"Ollama вернула ошибку {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AIError(
            "Не удалось подключиться к Ollama.\n"
            "Проверьте, что служба запущена: systemctl status ollama"
        ) from exc
    except TimeoutError as exc:
        raise AIError("Ollama не ответила за отведённое время. Попробуйте ещё раз.") from exc


def _get(path: str, timeout: float = 5) -> dict:
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise AIError(
            "Не удалось подключиться к Ollama.\n"
            "Проверьте, что служба запущена: systemctl status ollama"
        ) from exc


def model_installed() -> bool:
    """Загружена ли нужная модель (быстрая проверка без генерации)."""
    try:
        tags = _get("/api/tags")
    except AIError:
        return False
    names = {item.get("name", "") for item in tags.get("models", [])}
    return MODEL_NAME in names or f"{MODEL_NAME}:latest" in names


def loaded_size() -> int:
    """Сколько байт занимает модель в памяти (0, если не загружена)."""
    try:
        data = _get("/api/ps")
    except AIError:
        return 0
    for item in data.get("models", []):
        name = item.get("name") or item.get("model") or ""
        if name == MODEL_NAME or name.startswith(MODEL_NAME.split(":")[0]):
            try:
                return int(item.get("size", 0))
            except (TypeError, ValueError):
                return 0
    return 0


def unload_model(model: str = MODEL_NAME) -> int:
    """Выгружает модель из памяти. Возвращает освобождённые байты."""
    size = loaded_size()
    # keep_alive=0 — документированный способ сразу выгрузить модель.
    _post("/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
    return size


def _clean_answer(text: str) -> str:
    """Убирает случайные пояснения и блоки кода вокруг ответа модели."""
    result = text.strip()
    if result.startswith("```"):
        lines = result.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        result = "\n".join(lines)

    pairs = {"«": "»", "“": "”", '"': '"'}
    for opening, closing in pairs.items():
        if result.startswith(opening) and result.endswith(closing):
            result = result[1:-1]
            break
    return result.strip("\n")


def split_chunks(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Режет текст на куски не больше limit символов по границам строк."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append("".join(current))
                current = []
                size = 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) > limit and current:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks or [text]


def _check_chunk(text: str, model: str) -> str:
    answer = _post(
        "/api/chat",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "options": {"temperature": 0, "num_predict": 4096},
        },
    )
    content = answer.get("message", {}).get("content", "")
    if not content.strip():
        raise AIError("Модель вернула пустой ответ. Попробуйте ещё раз.")
    return _clean_answer(content)


def check_text(text: str, model: str = MODEL_NAME) -> str:
    """Возвращает исправленный текст. Бросает AIError при проблемах."""
    if not text.strip():
        return text

    parts: list[str] = []
    for chunk in split_chunks(text):
        corrected = _clean_answer(_check_chunk(chunk, model))
        # Сохраняем переводы строк на стыке кусков: модель иногда их срезает.
        trailing = len(chunk) - len(chunk.rstrip("\n"))
        parts.append(corrected.rstrip("\n") + "\n" * trailing)

    result = "".join(parts)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result
