"""Per-instance configuration. Credentials never belong in JSON."""
from dataclasses import dataclass
from pathlib import Path
import json
import os

DEFAULT_DECLINE = 'Спасибо за отклик! Желаем успехов.'
DEFAULT_PDF_CAPTION = 'Условия согласованы. Направляю презентацию компании:'
MAX_TEXT = 3900      # одно сообщение Telegram
MAX_CAPTION = 1000   # подпись к файлу


def _material_path(value, materials_dir):
    """Как для conditions/presentation: путь как есть, иначе файл с тем же именем в materials_dir."""
    p = Path(value).resolve()
    return p if p.is_file() else (materials_dir / Path(value).name).resolve()


def _text(sc, key, default, limit=MAX_TEXT):
    value = sc.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'Scenario "{key}" must be non-empty text up to {limit} characters')
    return value


def _phrases(sc, key):
    """Пустой кортеж = встроенный список recruitment_parser."""
    value = sc.get('phrases', {}).get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x.strip() for x in value):
        raise ValueError(f'Scenario "phrases.{key}" must be a non-empty list of phrases')
    return tuple(value)


def _after_consent(sc, materials_dir, presentation):
    """Сообщения после согласия, по порядку. Пустой кортеж = PDF, затем приглашение (как раньше)."""
    items = sc.get('after_consent')
    if items is None:
        return ()
    if not isinstance(items, list) or not items:
        raise ValueError('Scenario "after_consent" must be a non-empty list')
    result, ids = [], set()
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f'after_consent #{n}: must be an object')
        kind, sid = item.get('type'), str(item.get('id', ''))
        # id входит в ключ отправки: по нему бот узнаёт уже выданный шаг после рестарта
        if not sid.isidentifier() or sid in ids:
            raise ValueError(f'after_consent #{n}: "id" must be a unique latin name')
        ids.add(sid)
        if kind == 'document':
            # Без "file" — presentation_file сценария: путь к презентации хранится в одном месте
            path = _material_path(item['file'], materials_dir) if item.get('file') else presentation
            if not path.is_file():
                raise ValueError(f'after_consent #{n}: file not found')
            caption = item.get('caption', sc.get('presentation_caption', DEFAULT_PDF_CAPTION))
            if not isinstance(caption, str) or len(caption) > MAX_CAPTION:
                raise ValueError(f'after_consent #{n}: caption up to {MAX_CAPTION} characters')
            result.append({'id': sid, 'type': kind, 'file': path, 'caption': caption,
                           'name': item.get('name', 'презентация')})
        elif kind == 'text':
            text = item.get('text', '')
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise ValueError(f'after_consent #{n}: text must be 1..{MAX_TEXT} characters')
            result.append({'id': sid, 'type': kind, 'text': text, 'name': item.get('name', 'сообщение')})
        else:
            raise ValueError(f'after_consent #{n}: type must be "document" or "text"')
    return tuple(result)


@dataclass(frozen=True)
class Config:
    client_id: str
    owner_id: int
    business_owner_id: int
    contacts: tuple[int, ...]
    data_dir: Path
    materials_dir: Path
    instructions: str
    faqs: tuple[dict, ...]
    tasks: dict

    scenario_path: Path
    conditions_version: str
    conditions_file: Path
    presentation_file: Path
    group_invite_url: str
    unknown_question_text: str
    invite_message_text: str

    model: str = ''
    daily_ai_calls: int = 100
    max_file_bytes: int = 20_000_000

    # Редактируемый сценарий (recruitment.scenario.json); значения по умолчанию = прежнее поведение
    decline_message_text: str = DEFAULT_DECLINE
    presentation_caption: str = DEFAULT_PDF_CAPTION
    vacancy_phrases: tuple[str, ...] = ()
    consent_phrases: tuple[str, ...] = ()
    decline_phrases: tuple[str, ...] = ()
    after_consent: tuple[dict, ...] = ()

    def material(self, name: str) -> Path:
        root = self.materials_dir.resolve()
        p = (root / name).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise ValueError('Material must be an existing file inside materials_dir')
        if p.stat().st_size > self.max_file_bytes:
            raise ValueError('Material exceeds configured limit')
        return p

    @classmethod
    def load(cls, path: str):
        base_dir = Path(path).resolve().parent
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        if raw.get('schema_version') != 1:
            raise ValueError('Unsupported config schema')

        scenario_rel = raw.get('recruitment_scenario', 'examples/recruitment.scenario.json')
        scenario_path = (base_dir / scenario_rel).resolve()
        if not scenario_path.is_file():
            scenario_path = Path(scenario_rel).resolve()
        if not scenario_path.is_file():
            raise ValueError('Recruitment scenario file not found')

        sc = json.loads(scenario_path.read_text(encoding='utf-8'))

        materials_dir = Path(os.environ.get('MATERIALS_DIR',
                                            raw.get('materials_dir', 'examples/materials'))).resolve()

        cond_file = _material_path(sc['conditions_file'], materials_dir)
        pres_file = _material_path(sc['presentation_file'], materials_dir)

        if not cond_file.is_file() or not pres_file.is_file():
            raise ValueError('Required recruitment materials are missing')

        cfg = cls(
            client_id=raw['client_id'],
            owner_id=int(raw['owner_id']),
            business_owner_id=int(raw['business_owner_id']),
            contacts=tuple(int(x) for x in raw.get('contacts', [])),
            data_dir=Path(os.environ.get('DATA_DIR', raw.get('data_dir', './data'))),
            materials_dir=materials_dir,
            instructions=raw.get('instructions', ''),
            faqs=tuple(raw.get('faqs', [])),
            tasks=raw.get('tasks', {}),
            scenario_path=scenario_path,
            conditions_version=sc.get('conditions_version', '2026-10-06-v1'),
            conditions_file=cond_file,
            presentation_file=pres_file,
            group_invite_url=sc['group_invite_url'],
            unknown_question_text=sc.get('unknown_question', 'Передам ваш вопрос Владимиру для ответа.'),
            invite_message_text=sc.get('invite_message', ''),
            model=os.environ.get('LOCAL_MODEL', ''),
            daily_ai_calls=int(raw.get('daily_ai_calls', 100)),
            max_file_bytes=min(int(raw.get('max_file_bytes', 20_000_000)), 20_000_000),
            decline_message_text=_text(sc, 'decline_message', DEFAULT_DECLINE),
            presentation_caption=_text(sc, 'presentation_caption', DEFAULT_PDF_CAPTION, MAX_CAPTION),
            vacancy_phrases=_phrases(sc, 'vacancy'),
            consent_phrases=_phrases(sc, 'consent'),
            decline_phrases=_phrases(sc, 'decline'),
            after_consent=_after_consent(sc, materials_dir, pres_file),
        )
        if not cfg.client_id or min(cfg.owner_id, cfg.business_owner_id) <= 0:
            raise ValueError('Set client_id and real numeric owner IDs')
        if cfg.owner_id in cfg.contacts or cfg.business_owner_id in cfg.contacts:
            raise ValueError('Owners must not be developer contacts')
        return cfg