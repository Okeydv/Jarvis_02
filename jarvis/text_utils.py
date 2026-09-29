"""Работа с текстом: подготовка к озвучке, разбиение на предложения, слово-активатор."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

try:
    from num2words import num2words as _num2words
except ImportError:  # pragma: no cover - num2words есть в requirements
    _num2words = None


# ═══ Нормализация команд ════════════════════════════════════════════════

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, без пунктуации, одинарные пробелы."""
    text = (text or "").lower().replace("ё", "е").replace("_", " ")
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


_RU_EN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def translit_ru_en(text: str) -> str:
    """«телеграм» → «telegram»: помогает сопоставить русское название с английским."""
    return "".join(_RU_EN.get(ch, ch) for ch in text.lower())


# ═══ Слово-активатор и стоп-слова ════════════════════════════════════════

# Слова, которые допустимы перед «Джарвис»: «эй, Джарвис», «слушай, Джарвис».
_WAKE_PREFIXES = {"эй", "хей", "слушай", "окей", "ок", "ну", "а", "так", "привет", "алло"}
_STOP_FILLERS = {"все", "уже", "пожалуйста", "ну", "же", "да", "так", "пока", "сэр"}


def _is_wake_word(word: str, wake_words: Iterable[str], threshold: float) -> bool:
    for wake in wake_words:
        wake = normalize(wake)
        if not wake:
            continue
        if word == wake or similarity(word, wake) >= threshold:
            return True
    return False


def _is_split_wake_word(first: str, second: str, wake_words: Iterable[str], threshold: float) -> bool:
    if len(first) < 2 or len(second) < 2 or len(first) + len(second) > 10:
        return False
    for wake in wake_words:
        wake = normalize(wake)
        if wake and wake.startswith(first[:2]) and similarity(first + second, wake) >= threshold:
            return True
    return False


def find_wake_word(text: str, wake_words: Iterable[str], threshold: float = 0.8) -> tuple[bool, str]:
    """Проверяет, начинается ли фраза со слова-активатора.

    Возвращает (найдено, остаток фразы без активатора).
    """
    wake_words = list(wake_words)
    words = normalize(text).split()
    for start in range(min(3, len(words))):
        if start and words[start - 1] not in _WAKE_PREFIXES:
            break
        word = words[start]
        if _is_wake_word(word, wake_words, threshold):
            return True, " ".join(words[start + 1:])
        # «джа вис» — распознаватель иногда делит слово на два; оба куска — части активатора
        if start + 1 < len(words) and _is_split_wake_word(word, words[start + 1], wake_words, threshold):
            return True, " ".join(words[start + 2:])
    return False, normalize(text)


def is_stop_phrase(text: str, stop_words: Iterable[str], wake_words: Iterable[str] = ()) -> bool:
    """Фраза целиком — команда остановки: «стоп», «Джарвис, хватит», «всё, хватит»."""
    found, rest = find_wake_word(text, wake_words)
    words = (rest if found else normalize(text)).split()
    stops = {normalize(w) for w in stop_words}
    if not words or len(words) > 4:
        return False
    return any(w in stops for w in words) and all(w in stops or w in _STOP_FILLERS for w in words)


def contains_stop_word(text: str, stop_words: Iterable[str]) -> bool:
    """Есть ли стоп-слово в короткой фразе (для прерывания во время речи)."""
    words = normalize(text).split()
    if not words or len(words) > 5:
        return False
    stops = {normalize(w) for w in stop_words}
    return any(w in stops for w in words)


# ═══ Разбиение потока текста на предложения ══════════════════════════════

_BOUNDARY_RE = re.compile(r"[.!?…]+[\"»”’)\]]*(?=\s)|\n+")


class SentenceSplitter:
    """Собирает текст, приходящий кусочками, и отдаёт готовые предложения."""

    def __init__(self, min_len: int = 12, max_len: int = 300):
        self.min_len = min_len
        self.max_len = max_len
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        self._buf += chunk
        out: list[str] = []
        start = 0
        for match in _BOUNDARY_RE.finditer(self._buf):
            candidate = self._buf[start:match.end()].strip()
            if len(candidate) >= self.min_len:
                out.append(candidate)
                start = match.end()
        self._buf = self._buf[start:]
        while len(self._buf) > self.max_len:
            cut = _best_cut(self._buf, self.max_len)
            piece, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if piece:
                out.append(piece)
        return out

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []


def _best_cut(text: str, limit: int) -> int:
    """Позиция разреза не дальше limit: после знака препинания или по пробелу."""
    window = text[:limit]
    for pattern in (r"[.!?…]\s", r"[,;:—–]\s", r"\s"):
        positions = [m.end() for m in re.finditer(pattern, window)]
        if positions and positions[-1] > limit // 3:
            return positions[-1]
    return limit


def split_chunks(text: str, max_chars: int = 800) -> list[str]:
    """Режет текст на куски не длиннее max_chars по границам предложений."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+|\n+", text) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            cut = _best_cut(sentence, max_chars)
            head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


# ═══ Подготовка текста к синтезу речи ════════════════════════════════════

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # эмодзи, пиктограммы, флаги
    "\U00002600-\U000027BF"  # разные символы и дингбаты
    "\U00002300-\U000023FF"  # технические символы (⌚ ⏰)
    "\U00002B00-\U00002BFF"  # стрелки, звёзды
    "\U0000FE00-\U0000FE0F"  # селекторы вариантов
    "\U0000200D\U000020E3"   # ZWJ, keycap
    "\U000E0020-\U000E007F"  # теги
    "]+"
)

_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Единицы измерения после числа: (формы для 1 / 2–4 / 5+, род числительного)
_UNITS: list[tuple[str, tuple[str, str, str], str]] = [
    (r"%", ("процент", "процента", "процентов"), "m"),
    (r"(?:ГБ|Гб|GB|Gb)\b", ("гигабайт", "гигабайта", "гигабайт"), "m"),
    (r"(?:МБ|Мб|MB|Mb)\b", ("мегабайт", "мегабайта", "мегабайт"), "m"),
    (r"(?:КБ|Кб|KB|Kb)\b", ("килобайт", "килобайта", "килобайт"), "m"),
    (r"(?:ТБ|Тб|TB|Tb)\b", ("терабайт", "терабайта", "терабайт"), "m"),
    (r"(?:ГГц|GHz)", ("гигагерц", "гигагерца", "гигагерц"), "m"),
    (r"(?:МГц|MHz)", ("мегагерц", "мегагерца", "мегагерц"), "m"),
    (r"(?:°C|°С|℃|°)", ("градус", "градуса", "градусов"), "m"),
    (r"мин\b\.?", ("минута", "минуты", "минут"), "f"),
    (r"сек\b\.?", ("секунда", "секунды", "секунд"), "f"),
    (r"ч\b\.?", ("час", "часа", "часов"), "m"),
    (r"(?:руб\b\.?|₽)", ("рубль", "рубля", "рублей"), "m"),
    (r"(?:USD|\$)", ("доллар", "доллара", "долларов"), "m"),
    (r"(?:EUR|€)", ("евро", "евро", "евро"), "n"),
    (r"км\b", ("километр", "километра", "километров"), "m"),
    (r"кг\b", ("килограмм", "килограмма", "килограммов"), "m"),
    (r"шт\b\.?", ("штука", "штуки", "штук"), "f"),
    (r"тыс\b\.?", ("тысяча", "тысячи", "тысяч"), "f"),
    (r"млн\b\.?", ("миллион", "миллиона", "миллионов"), "m"),
    (r"млрд\b\.?", ("миллиард", "миллиарда", "миллиардов"), "m"),
]

_ABBREVIATIONS = {
    r"\bт\.\s?е\.": "то есть",
    r"\bт\.\s?д\.": "так далее",
    r"\bт\.\s?п\.": "тому подобное",
    r"\bт\.\s?к\.": "так как",
    r"\bи др\.": "и другие",
    r"\bпр\.": "прочее",
}

_EN_WORDS = {
    "jarvis": "джарвис", "sir": "сэр", "ok": "окей", "okay": "окей", "google": "гугл",
    "chrome": "хром", "youtube": "ютуб", "windows": "виндоус", "telegram": "телеграм",
    "discord": "дискорд", "steam": "стим", "spotify": "спотифай", "whatsapp": "ватсап",
    "zoom": "зум", "word": "ворд", "excel": "эксель", "powerpoint": "пауэрпоинт",
    "powershell": "пауэршелл", "notepad": "блокнот", "explorer": "эксплорер",
    "edge": "эдж", "firefox": "файрфокс", "yandex": "яндекс", "microsoft": "майкрософт",
    "office": "офис", "store": "стор", "apple": "эпл", "iphone": "айфон",
    "android": "андроид", "wifi": "вай-фай", "bluetooth": "блютус", "email": "имейл",
    "online": "онлайн", "offline": "офлайн", "ollama": "оллама", "gigachat": "гигачат",
    "gemini": "джемини", "qwen": "квен", "python": "пайтон", "stark": "старк",
    "tony": "тони", "iron": "айрон", "man": "мэн", "com": "ком", "ru": "ру",
    "org": "орг", "net": "нет", "www": "", "http": "", "https": "", "ctrl": "контрол",
    "alt": "альт", "shift": "шифт", "tab": "таб", "enter": "энтер", "esc": "эскейп",
    "escape": "эскейп", "delete": "делит", "win": "вин", "space": "пробел",
    "cpu": "процессор", "gpu": "видеокарта", "ram": "оперативная память",
    "pc": "пэ-ка", "vk": "вэ-ка", "code": "код", "vs": "ви-эс", "paint": "пейнт",
    "vpn": "ви-пи-эн", "api": "эй-пи-ай", "ai": "эй-ай", "it": "ай-ти",
}

_EN_LETTERS = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи",
    "h": "эйч", "i": "ай", "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн",
    "o": "оу", "p": "пи", "q": "кью", "r": "ар", "s": "эс", "t": "ти", "u": "ю",
    "v": "ви", "w": "дабл-ю", "x": "икс", "y": "уай", "z": "зед",
}

_EN_DIGRAPHS = [
    ("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("ck", "к"),
    ("qu", "кв"), ("oo", "у"), ("ee", "и"), ("ea", "и"), ("ou", "ау"), ("ai", "эй"),
    ("ay", "эй"), ("kh", "х"), ("zh", "ж"), ("ts", "ц"), ("ya", "я"), ("yu", "ю"),
    ("yo", "йо"), ("wh", "у"),
]

_EN_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
    "y": "и", "z": "з",
}


def ru_plural(number: int, forms: tuple[str, str, str]) -> str:
    """Согласование существительного с числом: 1 процент, 2 процента, 5 процентов."""
    n = abs(int(number)) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def _words(number: int | float, **kwargs) -> str:
    if _num2words is None:
        return str(number)
    try:
        return _num2words(number, lang="ru", **kwargs)
    except Exception:
        try:
            return _num2words(number, lang="ru")
        except Exception:
            return str(number)


def _number_words(raw: str, gender: str = "m", case: str = "n") -> tuple[str, bool]:
    """Число из строки прописью. Возвращает (текст, дробное ли число)."""
    raw = raw.replace(",", ".")
    if "." in raw:
        value = float(raw)
        if value.is_integer():
            return _words(int(value), gender=gender, case=case), False
        return _words(value), True
    return _words(int(raw), gender=gender, case=case), False


# Существительные женского и среднего рода после числа: «одна минута», «одно окно»
_FEMININE_STEMS = (
    "минут", "секунд", "недел", "тысяч", "штук", "копе", "строк", "вкладк", "страниц",
    "задач", "заметк", "песн", "папк", "программ", "игр", "ошибк", "книг", "част",
)
_NEUTER_STEMS = ("окн", "сообщени", "приложени", "уведомлени", "письм", "устройств", "задани", "ядр")
# Предлоги, после которых число стоит в родительном падеже: «из шестнадцати»
_GENITIVE_PREPOSITIONS = {
    "из", "до", "от", "около", "более", "менее", "свыше", "после", "без", "кроме", "против", "для",
}


def _prev_word(match: re.Match) -> str:
    words = re.findall(r"[^\W\d_]+", match.string[max(0, match.start() - 20):match.start()])
    return words[-1].lower() if words else ""


def _next_word(match: re.Match) -> str:
    found = re.match(r"\s*([^\W\d_]+)", match.string[match.end():match.end() + 30])
    return found.group(1).lower() if found else ""


def _gender_for(noun: str) -> str:
    noun = noun.replace("ё", "е")
    if noun.startswith(_FEMININE_STEMS):
        return "f"
    if noun.startswith(_NEUTER_STEMS):
        return "n"
    return "m"


def _replace_units(text: str) -> str:
    for pattern, forms, gender in _UNITS:
        regex = re.compile(r"(?<![\w.,])(-?\d+(?:[.,]\d+)?)\s*" + pattern)

        def repl(match: re.Match, forms=forms, gender=gender) -> str:
            raw = match.group(1)
            genitive = _prev_word(match) in _GENITIVE_PREPOSITIONS
            words, fractional = _number_words(raw.lstrip("-"), gender, "genitive" if genitive else "n")
            prefix = "минус " if raw.startswith("-") else ""
            number = int(float(raw.replace(",", ".")))
            if fractional:
                unit = forms[1]
            elif genitive:
                unit = forms[1] if number % 10 == 1 and number % 100 != 11 else forms[2]
            else:
                unit = ru_plural(number, forms)
            return f"{prefix}{words} {unit}"

        text = regex.sub(repl, text)
    # Валюта перед числом: $50, €10
    text = re.sub(r"\$\s?(\d+)", lambda m: f"{_words(int(m.group(1)))} {ru_plural(int(m.group(1)), ('доллар', 'доллара', 'долларов'))}", text)
    text = re.sub(r"€\s?(\d+)", lambda m: f"{_words(int(m.group(1)))} евро", text)
    return text


def _replace_dates_and_times(text: str) -> str:
    def time_repl(match: re.Match) -> str:
        hours, minutes = int(match.group(1)), int(match.group(2))
        if minutes == 0:
            tail = "ноль ноль"
        elif minutes < 10:
            tail = "ноль " + _words(minutes, gender="f")
        else:
            tail = _words(minutes, gender="f")
        return f"{_words(hours)} {tail}"

    def full_date_repl(match: re.Match) -> str:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return match.group(0)
        if year < 100:
            year += 2000
        return (f"{_words(day, to='ordinal', gender='n')} {_MONTHS_GEN[month - 1]} "
                f"{_words(year, to='ordinal', case='genitive')} года")

    def day_month_repl(match: re.Match) -> str:
        day = int(match.group(1))
        if not 1 <= day <= 31:
            return match.group(0)
        return f"{_words(day, to='ordinal', gender='n')}{match.group(2)}{match.group(3)}"

    def year_repl(match: re.Match) -> str:
        year, word = int(match.group(1)), match.group(3).lower()
        case = {"год": "n", "года": "genitive", "году": "prepositional", "годом": "instrumental"}.get(word, "genitive")
        noun = "года" if word.startswith("г.") else match.group(3)
        return f"{_words(year, to='ordinal', case=case)} {noun}"

    months = "|".join(_MONTHS_GEN)
    text = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", time_repl, text)
    text = re.sub(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})\b", full_date_repl, text)
    text = re.sub(rf"\b(\d{{1,2}})(\s+)({months})\b", day_month_repl, text, flags=re.IGNORECASE)
    text = re.sub(r"\b(\d{4})(\s*)(годом|году|года|год|г\.)", year_repl, text)
    return text


_ORDINAL_SUFFIXES = [
    (r"(?:ый|ий|ой|й)", {"to": "ordinal"}),
    (r"(?:ого|его|го)", {"to": "ordinal", "case": "genitive"}),
    (r"(?:ому|ему|му)", {"to": "ordinal", "case": "dative"}),
    (r"(?:ом|ем|м)", {"to": "ordinal", "case": "prepositional"}),
    (r"(?:ая|яя|я)", {"to": "ordinal", "gender": "f"}),
    (r"(?:ую|юю|ю)", {"to": "ordinal", "gender": "f", "case": "accusative"}),
    (r"(?:ое|ее|е)", {"to": "ordinal", "gender": "n"}),
    (r"(?:ых|их|х)", {"to": "ordinal", "case": "genitive", "plural": True}),
    (r"(?:ти|ми|и)", {"case": "genitive"}),
]


def numbers_to_words(text: str) -> str:
    """Все числа в тексте — прописью (num2words, lang="ru") с согласованием единиц."""
    # Пробел между буквами и цифрами: «2ГБ» → «2 ГБ», «Win10» → «Win 10»
    text = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", text)
    text = _replace_dates_and_times(text)
    # Разделители тысяч: «1 000 000» → «1000000»
    text = re.sub(r"\b\d{1,3}(?:[   ]\d{3})+\b", lambda m: re.sub(r"\D", "", m.group()), text)
    text = _replace_units(text)
    for suffix, kwargs in _ORDINAL_SUFFIXES:
        text = re.sub(
            rf"\b(\d+)\s?-\s?{suffix}\b",
            lambda m, kw=kwargs: _words(int(m.group(1)), **kw),
            text,
        )
    # Минус перед числом (но не дефис в диапазоне «5-7»)
    text = re.sub(r"(?:(?<=^)|(?<=[\s(]))[-−](?=\d)", "минус ", text)
    text = re.sub(r"\d+[.,]\d+", lambda m: _number_words(m.group())[0], text)

    def integer_repl(match: re.Match) -> str:
        case = "genitive" if _prev_word(match) in _GENITIVE_PREPOSITIONS else "n"
        return _words(int(match.group()), gender=_gender_for(_next_word(match)), case=case)

    return re.sub(r"\d+", integer_repl, text)


def _translit_word(word: str) -> str:
    low = word.lower()
    if low in _EN_WORDS:
        return _EN_WORDS[low]
    if len(low) == 1:
        return _EN_LETTERS.get(low, "")
    if word.isupper() and len(word) <= 5:
        return "-".join(_EN_LETTERS.get(ch, "") for ch in low)
    result: list[str] = []
    i = 0
    while i < len(low):
        for digraph, replacement in _EN_DIGRAPHS:
            if low.startswith(digraph, i):
                result.append(replacement)
                i += len(digraph)
                break
        else:
            ch = low[i]
            if ch == "c" and i + 1 < len(low) and low[i + 1] in "eiy":
                result.append("с")
            elif ch == "e" and i == len(low) - 1 and len(low) > 3:
                pass  # немая e на конце: code → код
            elif ch == "y" and i == 0:
                result.append("й")
            else:
                result.append(_EN_SINGLE.get(ch, ""))
            i += 1
    return "".join(result)


def latin_to_cyrillic(text: str) -> str:
    """Silero-ru не читает латиницу — переводим английские слова в русское звучание."""
    return re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", lambda m: _translit_word(m.group()), text)


def strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+•]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d{1,2}[.)]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*_=]{3,}\s*$", " ", text, flags=re.M)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = text.replace("|", " ")
    return text


def _simplify_links_and_paths(text: str) -> str:
    def url_repl(match: re.Match) -> str:
        host = re.sub(r"^www\.", "", match.group(1).lower())
        return host.replace(".", " точка ")

    text = re.sub(r"https?://([^\s/?#)\]]+)[^\s)\]]*", url_repl, text)

    def path_repl(match: re.Match) -> str:
        last = re.split(r"[\\/]", match.group(0).rstrip("\\/."))[-1]
        return "файл" if re.search(r"\.\w{1,5}$", last) else last

    return re.sub(r"\b[A-Za-z]:\\[^\s,;«»\"']*", path_repl, text)


def _lines_to_sentences(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    out = []
    for line in lines:
        if not line:
            continue
        if not re.search(r"[.!?…:;,]$", line):
            line += "."
        out.append(line)
    return " ".join(out)


def clean_for_speech(text: str) -> str:
    """Убирает markdown и эмодзи, числа — прописью, латиницу — кириллицей."""
    text = strip_markdown(text)
    text = _EMOJI_RE.sub(" ", text)
    text = _simplify_links_and_paths(text)
    text = _lines_to_sentences(text)
    for pattern, replacement in _ABBREVIATIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = numbers_to_words(text)
    text = latin_to_cyrillic(text)
    text = text.replace("&", " и ").replace("+", " плюс ").replace("=", " равно ")
    text = text.replace("№", " номер ").replace("@", " собака ")
    text = re.sub(r"[\\/*#_~<>^{}\[\]|`]", " ", text)
    text = re.sub(r"\s+([.,!?…:;])", r"\1", text)
    text = re.sub(r"([.,!?…])\1{2,}", r"\1", text)
    return " ".join(text.split())


_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def prepare_for_speech(text: str, max_chars: int = 800) -> list[str]:
    """Готовые к синтезу куски (≤ max_chars), в каждом есть что произнести."""
    cleaned = clean_for_speech(text)
    return [chunk for chunk in split_chunks(cleaned, max_chars) if _CYRILLIC_RE.search(chunk)]
