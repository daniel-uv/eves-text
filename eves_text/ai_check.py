"""Проверка орфографии, грамматики и пунктуации локальной моделью через Ollama.

Модуль не зависит от GTK: его можно вызывать из фонового потока.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Адрес локальной службы Ollama и модель. Значения по умолчанию можно
# переопределить переменными окружения, а во время работы — configure().
DEFAULT_BASE_URL = os.environ.get(
    "RU_EDITOR_OLLAMA_URL", "http://127.0.0.1:11434"
).rstrip("/")
DEFAULT_MODEL = os.environ.get("RU_EDITOR_AI_MODEL", "qwen2.5:7b-instruct")
BASE_URL = DEFAULT_BASE_URL
MODEL_NAME = DEFAULT_MODEL

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


def configure(base_url: str | None = None, model: str | None = None) -> None:
    """Меняет адрес службы и модель на время работы приложения."""
    global BASE_URL, MODEL_NAME
    if base_url:
        BASE_URL = base_url.rstrip("/")
    if model:
        MODEL_NAME = model


class AIError(RuntimeError):
    """Ошибка, текст которой можно показать пользователю."""


class AICancelled(AIError):
    """Длительная операция прервана пользователем (например, скачивание)."""


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
                f"Модель «{MODEL_NAME}» не найдена.\n"
                "Скачайте её вручную: меню «ИИ» → «Скачать модель…»."
            ) from exc
        raise AIError(f"Ollama вернула ошибку {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AIError(
            "Не удалось подключиться к Ollama.\n"
            "Проверьте, что Ollama установлена и запущена."
        ) from exc
    except TimeoutError as exc:
        raise AIError("Ollama не ответила за отведённое время. Попробуйте ещё раз.") from exc


def _get(path: str, timeout: float = 5) -> dict:
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise AIError(
            "Не удалось подключиться к Ollama.\n"
            "Проверьте, что Ollama установлена и запущена."
        ) from exc
    except ValueError as exc:
        raise AIError("Ollama вернула некорректный ответ.") from exc


def _model_matches(name: str, model: str) -> bool:
    """Совпадает ли имя из /api/tags с запрошенной моделью."""
    if name == model:
        return True
    # Ollama дописывает :latest моделям без тега.
    if ":" not in model:
        return name == f"{model}:latest"
    return False


def model_state(model: str | None = None) -> str:
    """Состояние модели: «installed», «missing» или «offline».

    «offline» означает, что служба Ollama недоступна: отличить отсутствие
    модели от незапущенной службы иначе нельзя.
    """
    model = model or MODEL_NAME
    try:
        tags = _get("/api/tags")
    except AIError:
        return "offline"
    for item in tags.get("models", []):
        if _model_matches(item.get("name", ""), model):
            return "installed"
    return "missing"


def model_installed(model: str | None = None) -> bool:
    """Загружена ли нужная модель (быстрая проверка без генерации)."""
    return model_state(model) == "installed"


def pull_model(
    model: str | None = None,
    on_progress=None,
    cancel=None,
) -> bool:
    """Скачивает модель через /api/pull.

    on_progress(status, completed, total) вызывается по ходу загрузки из
    потока-вызывающего (в UI его нужно переправлять через GLib.idle_add).
    ``cancel`` — threading.Event: при установке скачивание прекращается.
    Возвращает True, если модель действительно скачалась, и False, если
    она уже была установлена. При отмене бросает AICancelled.
    """
    model = model or MODEL_NAME
    if model_installed(model):
        return False

    payload = json.dumps({"model": model, "stream": True}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/api/pull",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            # Ollama отвечает потоком NDJSON: по одному объекту на строку.
            for raw in response:
                if cancel is not None and cancel.is_set():
                    raise AICancelled("Скачивание модели отменено.")
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    raise AIError(
                        "Ollama не смогла скачать модель: "
                        f"{event['error']}"
                    )
                if on_progress is not None:
                    on_progress(
                        str(event.get("status", "")),
                        _as_int(event.get("completed")),
                        _as_int(event.get("total")),
                    )
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001 - тело ответа может быть не JSON
            pass
        raise AIError(
            f"Ollama вернула ошибку {exc.code}: {detail or exc.reason}"
        ) from exc
    except OSError as exc:
        raise AIError(
            "Соединение с Ollama прервалось во время скачивания.\n"
            "Проверьте, что служба запущена, и попробуйте ещё раз."
        ) from exc

    if cancel is not None and cancel.is_set():
        raise AICancelled("Скачивание модели отменено.")
    if not model_installed(model):
        raise AIError(
            f"Модель «{model}» скачалась, но Ollama её не видит.\n"
            "Попробуйте ещё раз или выполните: ollama pull " + model
        )
    return True


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def loaded_size(model: str | None = None) -> int:
    """Сколько байт занимает модель в памяти (0, если не загружена)."""
    model = model or MODEL_NAME
    try:
        data = _get("/api/ps")
    except AIError:
        return 0
    for item in data.get("models", []):
        name = item.get("name") or item.get("model") or ""
        if name == model or name.startswith(model.split(":")[0]):
            try:
                return int(item.get("size", 0))
            except (TypeError, ValueError):
                return 0
    return 0


def unload_model(model: str | None = None) -> int:
    """Выгружает модель из памяти. Возвращает освобождённые байты."""
    model = model or MODEL_NAME
    size = loaded_size(model)
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


def check_text(text: str, model: str | None = None) -> str:
    """Возвращает исправленный текст. Бросает AIError при проблемах."""
    model = model or MODEL_NAME
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
