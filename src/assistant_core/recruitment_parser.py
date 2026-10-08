"""
src/assistant_core/recruitment_parser.py
Детерминированное распознавание: обращение по вакансии, явное согласие, отказ.

Принцип: при любом сомнении возвращаем False. Тогда диалог уходит владельцу,
а бот не отправляет презентацию и не закрывает диалог по ошибке.
"""
import re


VACANCY_TRIGGERS = (
    "интересует вакансия",
    "хочу узнать о вакансии",
    "пишу по поводу вакансии",
    "ваканси",
    "отклик",
    "по поводу работы",
    "насчет работы",
    "на счет работы",
    "по работе",
    "new work",
)

# Согласие и отказ ищутся только целыми словами (см. _has_phrase).
EXPLICIT_CONSENT_PHRASES = (
    "согласен",
    "согласна",
    "подходит",
    "подходят",
    "принимаю условия",
    "условия устраивают",
)

DECLINE_TRIGGERS = (
    "не подходит",
    "не подходят",
    "отказываюсь",
    "не согласен",
    "не согласна",
    "несогласен",
    "несогласна",
    "не интересно",
    "неинтересно",
    "уже нашел",
    "уже нашла",
    "не устраивает",
)

# Слова, при которых сообщение нельзя считать однозначным.
_NEGATIONS = frozenset({"не", "нет", "ни", "никак"})
_HEDGES = frozenset({
    "но", "однако", "если", "а", "хотя", "только", "пока", "подумаю", "вопрос",
})
_READ_WORDS = frozenset({"ознакомился", "ознакомилась", "прочитал", "прочитала"})
_NO_BUT_NOT_DECLINE = frozenset({"проблем", "проблема", "вопросов"})  # "нет проблем"
_MAX_CONSENT_WORDS = 15


def _normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    cleaned = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(cleaned.split())


def _has_phrase(norm: str, phrase: str) -> bool:
    """Фраза как целые слова: 'не подходит' не найдётся в 'мне подходит',
    а 'согласен' не найдётся в 'несогласен'."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", norm) is not None


def is_vacancy_inquiry(text: str, triggers=None) -> bool:
    """Определяет первичное обращение по вакансии. triggers — фразы из сценария (иначе встроенные)."""
    norm = _normalize(text)
    return any(_normalize(trig) in norm for trig in (triggers or VACANCY_TRIGGERS))


def is_explicit_consent(text: str, phrases=None) -> bool:
    """
    Строгая проверка согласия. Согласием НЕ являются: вопросы, отрицания,
    оговорки ('но', 'если'), 'ознакомился/прочитал', слишком длинные сообщения.
    """
    if "?" in text:
        return False
    norm = _normalize(text)
    words = norm.split()
    if not words or len(words) > _MAX_CONSENT_WORDS:
        return False
    ws = set(words)
    if ws & _NEGATIONS or ws & _HEDGES or ws & _READ_WORDS:
        return False
    if norm in ("да", "ок", "хорошо"):
        return True
    return any(_has_phrase(norm, _normalize(p)) for p in (phrases or EXPLICIT_CONSENT_PHRASES))


def is_explicit_decline(text: str, triggers=None) -> bool:
    """Явный отказ. Неоднозначные сообщения ('нет, но...') уходят владельцу."""
    if "?" in text:
        return False
    norm = _normalize(text)
    words = norm.split()
    ws = set(words)
    if ws & _HEDGES:
        return False
    if "нет" in ws and len(words) <= 3 and not ws & _NO_BUT_NOT_DECLINE:
        return True
    return any(_has_phrase(norm, _normalize(t)) for t in (triggers or DECLINE_TRIGGERS))