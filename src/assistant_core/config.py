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
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        if raw.get('schema_version') != 1:
            raise ValueError('Unsupported config schema')
        cfg = cls(
            client_id=raw['client_id'], owner_id=int(raw['owner_id']),
            business_owner_id=int(raw['business_owner_id']),
            contacts=tuple(int(x) for x in raw['contacts']),
            data_dir=Path(os.environ.get('DATA_DIR', raw.get('data_dir', './data'))),
            materials_dir=Path(os.environ.get('MATERIALS_DIR', raw['materials_dir'])),
            instructions=raw['instructions'], faqs=tuple(raw.get('faqs', [])),
            tasks=raw.get('tasks', {}), model=os.environ.get('LOCAL_MODEL', ''),
            daily_ai_calls=int(raw.get('daily_ai_calls', 100)),
            max_file_bytes=min(int(raw.get('max_file_bytes', 20_000_000)), 20_000_000))
        if not cfg.client_id or min(cfg.owner_id, cfg.business_owner_id) <= 0:
            raise ValueError('Set client_id and real numeric owner IDs')
        if not cfg.contacts or any(c <= 0 for c in cfg.contacts) or len(set(cfg.contacts)) != len(cfg.contacts):
            raise ValueError('Provide unique positive permitted contact IDs')
        if cfg.owner_id in cfg.contacts or cfg.business_owner_id in cfg.contacts:
            raise ValueError('Owners must not be developer contacts')
        if not 1 <= cfg.daily_ai_calls <= 10000 or cfg.max_file_bytes <= 0:
            raise ValueError('Invalid resource limits')
        ids = [x['id'] for x in cfg.faqs]
        if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError('FAQ IDs must be unique strings')
        if len(cfg.instructions) > 3000 or len(json.dumps(cfg.faqs, ensure_ascii=False)) > 4000:
            raise ValueError('Keep starter knowledge within configured context budget')
        for faq in cfg.faqs:
            if not isinstance(faq.get('answer'), str) or not 1 <= len(faq['answer']) <= 3000:
                raise ValueError('FAQ answer must be 1..3000 characters')
        for key, task in cfg.tasks.items():
            if not key or not isinstance(task.get('brief'), str) or len(task['brief']) > 2500:
                raise ValueError('Invalid task ID or brief')
            for file in task.get('files', []):
                cfg.material(file)
        return cfg
