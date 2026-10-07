"""
src/assistant_core/recruitment_parser.py
Детерминированное распознавание: обращение по вакансии, явное согласие, отказ.
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
    "на счёт работы",
    "по работе",
    "new work"
)

EXPLICIT_CONSENT_PHRASES = (
    "да условия мне подходят я согласен перейти к следующему этапу",
    "да условия мне подходят я согласна перейти к следующему этапу",
    "да условия мне подходят я согласен на перейти к следующему этапу",
    "да условия подходят я согласен",
    "да условия подходят я согласна",
    "условия подходят я согласен",
    "условия подходят я согласна",
    "я согласен с условиями",
    "я согласна с условиями",
    "согласен с условиями",
    "согласна с условиями",
    "принимаю условия",
    "условия устраивают",
    "да я согласен",
    "да я согласна",
    "да согласен",
    "да согласна",
    "согласен",
    "согласна",
    "подходит"
)

DECLINE_TRIGGERS = (
    "нет",
    "не подходит",
    "не подходят",
    "отказываюсь",
    "не согласен",
    "не согласна",
    "не интересно",
    "уже нашел",
    "уже нашла",
    "не устраивает"
)


def _normalize(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    return " ".join(cleaned.split())


def is_vacancy_inquiry(text: str) -> bool:
    """Определяет первичное обращение по вакансии."""
    norm = _normalize(text)
    return any(trig in norm for trig in VACANCY_TRIGGERS)


def is_explicit_consent(text: str) -> bool:
    """
    Строгая проверка согласия.
    Вопросы ('Да?'), оговорки ('но', 'если') и реплики 'ознакомился/прочитал' согласием НЕ являются.
    """
    if "?" in text:
        return False
    norm = _normalize(text)
    words = set(norm.split())
    if any(stop_word in words for stop_word in ("но", "однако", "если")):
        return False
    if any(read_word in words for read_word in ("ознакомился", "ознакомилась", "прочитал", "прочитала")):
        return False
    if norm in ("да", "согласен", "согласна"):
        return True
    return any(phrase in norm for phrase in EXPLICIT_CONSENT_PHRASES)


def is_explicit_decline(text: str) -> bool:
    """Определяет явный отказ кандидата."""
    if "?" in text:
        return False
    norm = _normalize(text)
    words = set(norm.split())
    if "нет" in words and len(words) <= 3:
        return True
    return any(trig in norm for trig in DECLINE_TRIGGERS)