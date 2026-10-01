"""Зрение: Джарвис смотрит на экран через vision-модель (как UI-TARS) — описывает, что видит,
читает текст и ошибки, находит элемент по описанию, чтобы на него нажать."""

from __future__ import annotations

import io
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable

from .llm.base import LLMBackend, LLMError

log = logging.getLogger(__name__)

MAX_WIDTH = 1600
VISION_ORDER = ("qwen", "gemini", "ollama")  # кого пробовать, если текущая модель не видит
KEY_FOR = {"qwen": ("BAZAARLINK_API_KEY", "QWEN_API_KEY"), "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
NO_VISION_HINT = (
    "Выберите Qwen или Gemini (они понимают изображения) или скачайте для Ollama модель со зрением, например "
    "ollama pull qwen2.5vl:7b, и укажите её в config.yaml → ollama.vision_model."
)
LOCATE_PROMPT = (
    "You see a screenshot of a Windows desktop. Find the UI element described as: «{target}». "
    "Answer with JSON only, no other text: {{\"found\": true, \"x\": <0-1000>, \"y\": <0-1000>, "
    "\"label\": \"<what exactly you found, in Russian>\"}} where x and y are the CENTER of the element in "
    "normalized coordinates (0,0 is the top-left corner, 1000,1000 is the bottom-right corner of the image). "
    "If the element is not visible, answer {{\"found\": false}}."
)


@dataclass
class Location:
    x: int          # координаты на экране (в пикселях)
    y: int
    label: str
    preview: object  # PIL.Image: фрагмент экрана с отметкой — для окна подтверждения


def capture():
    """Снимок основного экрана (его координаты совпадают с координатами мыши)."""
    from PIL import ImageGrab

    return ImageGrab.grab()


def encode(image, max_width: int = MAX_WIDTH) -> bytes:
    picture = image.convert("RGB")
    if picture.width > max_width:
        picture = picture.resize((max_width, round(picture.height * max_width / picture.width)))
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def parse_location(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("found", "x" in data):
        return None
    try:
        x, y = float(data["x"]), float(data["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return None
    return {"x": x, "y": y, "label": str(data.get("label") or "")}


def preview(image, x: int, y: int, size: int = 360):
    """Фрагмент экрана вокруг точки клика с красным прицелом."""
    from PIL import ImageDraw

    half = size // 2
    left, top = max(0, x - half), max(0, y - half)
    crop = image.convert("RGB").crop((left, top, min(image.width, left + size), min(image.height, top + size)))
    draw = ImageDraw.Draw(crop)
    cx, cy = x - left, y - top
    for radius, width in ((18, 4), (6, 3)):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(239, 68, 68), width=width)
    return crop


class VisionService:
    def __init__(self, config, get_backend: Callable[[str], LLMBackend], current: Callable[[], str]):
        self.config = config
        self.get_backend = get_backend
        self.current = current

    def backend(self) -> LLMBackend:
        preferred = str(self.config.get("vision.backend", "auto") or "auto")
        names = [preferred] if preferred != "auto" else [self.current()] + [n for n in VISION_ORDER if n != self.current()]
        errors = []
        for name in names:
            if name in KEY_FOR and not any(os.environ.get(k) for k in KEY_FOR[name]) and name != self.current():
                continue  # облачная модель без ключа
            try:
                backend = self.get_backend(name)
            except LLMError as exc:
                errors.append(exc.message)
                continue
            if backend.supports_vision():
                return backend
        raise LLMError("Нет модели, которая умеет смотреть на экран.", NO_VISION_HINT)

    def look(self, question: str, image=None) -> str:
        image = image if image is not None else capture()
        prompt = (question.strip() or "Опиши, что на экране.") + (
            "\nОтвечай по-русски и по делу. Это снимок экрана компьютера пользователя.")
        return self.backend().vision(prompt, encode(image)).strip()

    def locate(self, target: str, image=None) -> Location | None:
        image = image if image is not None else capture()
        answer = self.backend().vision(LOCATE_PROMPT.format(target=target), encode(image))
        found = parse_location(answer)
        log.info("Поиск «%s» на экране: %s", target, answer[:200])
        if found is None:
            return None
        x = round(found["x"] / 1000 * (image.width - 1))
        y = round(found["y"] / 1000 * (image.height - 1))
        return Location(x=x, y=y, label=found["label"] or target, preview=preview(image, x, y))
