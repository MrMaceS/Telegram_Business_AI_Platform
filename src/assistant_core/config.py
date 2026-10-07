"""Per-instance configuration. Credentials never belong in JSON."""
from dataclasses import dataclass
from pathlib import Path
import json
import os


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

        cond_file = Path(sc['conditions_file']).resolve()
        if not cond_file.is_file():
            cond_file = (materials_dir / Path(sc['conditions_file']).name).resolve()

        pres_file = Path(sc['presentation_file']).resolve()
        if not pres_file.is_file():
            pres_file = (materials_dir / Path(sc['presentation_file']).name).resolve()

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
            max_file_bytes=min(int(raw.get('max_file_bytes', 20_000_000)), 20_000_000)
        )
        if not cfg.client_id or min(cfg.owner_id, cfg.business_owner_id) <= 0:
            raise ValueError('Set client_id and real numeric owner IDs')
        if cfg.owner_id in cfg.contacts or cfg.business_owner_id in cfg.contacts:
            raise ValueError('Owners must not be developer contacts')
        return cfg