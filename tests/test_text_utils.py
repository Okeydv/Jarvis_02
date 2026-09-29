import pytest

from jarvis.text_utils import (
    SentenceSplitter,
    clean_for_speech,
    contains_stop_word,
    find_wake_word,
    is_stop_phrase,
    prepare_for_speech,
    ru_plural,
    split_chunks,
    translit_ru_en,
)

WAKE = ["джарвис", "джервис", "жарвис", "джарвиз"]
STOP = ["стоп", "хватит", "замолчи"]


@pytest.mark.parametrize("source, expected", [
    ("**Готово, сэр!** 😀", "Готово, сэр!"),
    ("Громкость 50%.", "Громкость пятьдесят процентов."),
    ("Загрузка 3,5%", "Загрузка три целых пять десятых процента"),
    ("занято 8 ГБ из 16 ГБ", "занято восемь гигабайт из шестнадцати гигабайт"),
    ("Сейчас 14:05", "Сейчас четырнадцать ноль пять"),
    ("29 сентября 2026 года", "двадцать девятое сентября две тысячи двадцать шестого года"),
    ("в 2025 году", "в две тысячи двадцать пятом году"),
    ("21 минута и 22 минуты", "двадцать одна минута и двадцать две минуты"),
    ("-5°C", "минус пять градусов"),
    ("1 000 000", "один миллион"),
    ("Нажмите Ctrl+C", "Нажмите контрол плюс си"),
    ("Открываю YouTube", "Открываю ютуб"),
    ("`код` и [ссылка](http://x.ru)", "код и ссылка"),
])
def test_clean_for_speech(source, expected):
    assert clean_for_speech(source).rstrip(".") == expected.rstrip(".")


def test_markdown_list_becomes_sentences():
    assert clean_for_speech("- один\n- два\n1. три") == "один. два. три."


def test_no_latin_digits_or_markup_left():
    text = clean_for_speech("## Итог\n* CPU: 45%, RAM 8 GB, Windows 11 ✅ https://example.com/page?x=1")
    assert not any(ch.isdigit() for ch in text)
    assert not any("a" <= ch.lower() <= "z" for ch in text)
    assert "#" not in text and "*" not in text and "✅" not in text


def test_prepare_for_speech_chunks_and_skips_empty():
    long_text = "Это предложение для проверки длины. " * 60
    chunks = prepare_for_speech(long_text, 800)
    assert len(chunks) >= 2
    assert all(len(c) <= 800 for c in chunks)
    assert prepare_for_speech("😀 🎉") == []


def test_split_chunks_hard_cut_without_punctuation():
    text = "слово " * 400
    chunks = split_chunks(text, 800)
    assert all(len(c) <= 800 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_ru_plural():
    forms = ("процент", "процента", "процентов")
    assert [ru_plural(n, forms) for n in (1, 2, 5, 11, 21, 104, 111)] == [
        "процент", "процента", "процентов", "процентов", "процент", "процента", "процентов"]


def test_sentence_splitter_streaming():
    splitter = SentenceSplitter()
    out = []
    for piece in ["Добрый ", "вечер, сэр. Открываю", " блокнот. Температура 3.5 гра", "дуса! Всё", " готово"]:
        out += splitter.feed(piece)
    out += splitter.flush()
    assert out == ["Добрый вечер, сэр.", "Открываю блокнот.", "Температура 3.5 градуса!", "Всё готово"]


def test_sentence_splitter_merges_short_and_splits_long():
    splitter = SentenceSplitter(min_len=12, max_len=100)
    assert splitter.feed("Да. Нет. ") == []  # короткие фразы копятся
    out = splitter.feed("Хорошо, сэр. ")
    assert out == ["Да. Нет. Хорошо, сэр."]
    out = splitter.feed("слово, " * 40)
    assert out and all(len(s) <= 100 for s in out)


@pytest.mark.parametrize("phrase, found, rest", [
    ("Джарвис, открой блокнот", True, "открой блокнот"),
    ("джарвис", True, ""),
    ("Эй, Джарвис, который час?", True, "который час"),
    ("джервис включи музыку", True, "включи музыку"),
    ("джа вис открой", True, "открой"),
    ("открой джарвис блокнот", False, None),
    ("дарвин сказал", False, None),
    ("жарко сегодня", False, None),
    ("я джарвис", False, None),
])
def test_find_wake_word(phrase, found, rest):
    result, command = find_wake_word(phrase, WAKE)
    assert result is found
    if found:
        assert command == rest


@pytest.mark.parametrize("phrase", [
    "да рвись", "жарить рыбу", "дарвинизм", "чарльз дарвин", "джаз играет", "ярмарка", "ну и дела",
    "я джарвиса видел", "вызови такси", "давай", "джинсы купить", "джо байден", "жираф", "дар вис",
])
def test_wake_word_false_positives(phrase):
    assert find_wake_word(phrase, WAKE)[0] is False


def test_stop_phrases():
    assert is_stop_phrase("стоп", STOP)
    assert is_stop_phrase("Джарвис, хватит!", STOP, WAKE)
    assert is_stop_phrase("всё, хватит", STOP)
    assert not is_stop_phrase("хватит играть музыку", STOP)
    assert not is_stop_phrase("открой блокнот", STOP)
    assert contains_stop_word("ну стоп же", STOP)
    assert not contains_stop_word("это длинная фраза где есть стоп и много слов", STOP)


def test_translit():
    assert translit_ru_en("телеграм") == "telegram"
    assert translit_ru_en("ютуб") == "yutub"
