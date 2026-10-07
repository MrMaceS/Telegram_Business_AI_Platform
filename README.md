# Telegram Business ассистент для кандидатов

Локальный ассистент на базе **Telegram Business Bot API** для первичного отбора кандидатов по вакансии. Работает на правилах и утверждённых владельцем текстах — без платных AI API и облачных сервисов.

**Код проекта:** `TG-LOCAL-WIN-01`  
**Версия ядра:** `0.2.0`  
**ТЗ:** `TZ.md` (5.0 от 6 октября 2026)

---

## Что делает ассистент

Детерминированный сценарий в личных диалогах Business-аккаунта владельца:

```
Обращение по вакансии
        ↓
Полный текст условий (vacancy_conditions.txt, без изменений)
        ↓
Явное согласие кандидата (строгая проверка: не «да», не «ознакомился», не вопрос)
        ↓
Запись согласия (текст, UTC, chat ID, версия условий)
        ↓
PDF-презентация компании (company_presentation.pdf)
        ↓
Ссылка-приглашение в группу «New work»
```

Ассистент **не**:
- не принимает решений о кандидатах — решение остаётся владельцу;
- не обещает работу, оплату, сроки;
- не добавляет людей в группу принудительно;
- не отвечает на сообщения вне выбранной Business-области;
- не использует userbot / MTProto.

При отказе — благодарность и завершение автообщения. При неизвестном вопросе — одно уведомление кандидату и эскалация владельцу.

---

## Требования

| Компонент | Минимум |
|---|---|
| ОС | Windows 11 Pro (64-bit) |
| CPU / RAM | Pentium Gold 7505 / 8 ГБ (тестовая конфигурация заказчика) |
| Python | 3.12+ (64-bit, с `py launcher`) |
| Сеть | Интернет, доступ к `api.telegram.org` |
| Telegram | Business-аккаунт владельца, подключённый бот с правом `can_reply` |
| Ollama / LLM | **Не требуется.** Сценарий работает на правилах (`AI_MODE=faq`). |

Никаких платных AI API, Make, хостинга или облачных сервисов.

---

## Структура проекта

```
TG-LOCAL-WIN-01/
├── README.md, START_HERE.md, TZ.md          # документация и ТЗ
├── CHANGELOG.md, MANIFEST_SHA256.txt        # версия и целостность
├── pyproject.toml, windows.ps1              # конфиг и лаунчер
├── .env.example, .gitignore
│
├── docs/                                    # протоколы и инструкции
│   ├── ACCEPTANCE.md        # приёмка ТЗ 5.0
│   ├── AUDIT.md, AUDIT_SPEC5.md
│   ├── IMPLEMENTATION_STATUS.md
│   ├── INSTALL_CHECKLIST.md # чек-лист установки на Windows
│   ├── LICENSES.md
│   ├── OWNER_GUIDE.md       # памятка владельцу (команды)
│   ├── TEAM_ROLES.md
│   ├── TASK_STATE.json
│   └── TEST_PROTOCOL.md, TEST_RESULTS.txt
│
├── examples/
│   ├── config.example.json
│   ├── recruitment.scenario.json
│   └── materials/
│       ├── vacancy_conditions.txt
│       ├── company_presentation.pdf
│       └── group_welcome.txt
│
├── src/assistant_core/                      # ядро 0.2.0
│   ├── main.py              # точка входа, long polling
│   ├── workflow.py          # рекрутинговый workflow (машина состояний)
│   ├── recruitment_parser.py# распознавание вакансии / согласия / отказа
│   ├── storage.py           # SQLite, состояния кандидатов, согласия
│   ├── telegram.py          # официальный Bot API адаптер
│   ├── config.py            # загрузка конфигурации
│   ├── ai.py                # FAQ-режим / опциональный Ollama
│   ├── ops.py               # backup / restore / healthcheck
│   └── locking.py           # защита от двух инстансов
│
├── tests/                                   # автотесты (моки)
│   ├── test_core.py                         # 23 теста ядра
│   ├── test_candidate_state.py              # состояния кандидатов
│   ├── test_full_coverage.py                # parser, config, ops, workflow
│   ├── test_acceptance_recruitment.py       # A01–B10 по ТЗ 5.0
│   ├── test_telegram.py                     # Telegram-слой, admission
│   └── acceptance_harness.py                # обвязка приёмочных тестов
│
└── tools/
    ├── check_model.py       # smoke-тест Ollama (опционально)
    └── inspect_telegram.py  # получение ID до запуска бота
```

---

## Быстрый старт

Полный чек-лист — в `docs/INSTALL_CHECKLIST.md`. Краткий порядок:

```powershell
# 1. Распаковать архив, например в C:\TelegramAssistant
# 2. Разрешить скрипт для текущего процесса (не глобально!)
Set-ExecutionPolicy -Scope Process Bypass

# 3. Первоначальная настройка (ввод токена BotFather скрыт, DPAPI)
.\windows.ps1 setup

# 4. Владелец в Telegram:
#    - BotFather → бот → Business mode
#    - Telegram → Business → Chatbots → выбрать бот, права: can_reply
#    - Открыть личный чат с ботом → /start

# 5. Получить реальные ID (бот остановлен!)
.\windows.ps1 inspect

# 6. Заполнить config.json реальными ID из inspect (не из публичных «ботов для ID»)
#    Удалить из конфига DEMO-001, старые FAQ и contacts-заглушку 100000002

# 7. NTFS-права и питание
.\windows.ps1 harden
# (от администратора)
.\windows.ps1 power

# 8. Проверка и тесты
.\windows.ps1 check
.\windows.ps1 test

# 9. Запуск (бот стартует в STOP)
.\windows.ps1 run
# В чате владельца: «Ядро запущено в STOP» → /resume

# 10. Реальная приёмка по docs/TEST_PROTOCOL.md раздел 2
#     (тестовый второй аккаунт-кандидат)

# 11. Автозапуск
.\windows.ps1 autostart-install
# → перезагрузка → бот поднялся в STOP → /resume
```

---

## Команды владельца

Все команды — в личном чате с ботом. Подробности — в `docs/OWNER_GUIDE.md`.

| Команда | Действие |
|---|---|
| `/status` | Общий STOP, режимы, этапы кандидатов |
| `/resume` | Снять общую паузу (ручные диалоги не возобновляются) |
| `/stop` | Запретить новые отправки в рабочие диалоги |
| `/manual <chat>` | Передать диалог человеку |
| `/auto <chat>` | Вернуть автоответы в диалоге |
| `/reply <chat> <текст>` | Отправить кандидату утверждённый текст |
| `/resume_candidate <chat>` | Возобновить диалог после эскалации |
| `/pending` | Неясные/неуспешные отправки и события для сверки |

После перезапуска ноутбука бот всегда стартует в **STOP** — это защита. Владелец пишет `/resume`.

---

## Состояния кандидата

Каждый кандидат проходит через этапы (сохраняются в SQLite):

```
NEW → CONDITIONS_SENT → CONSENT_RECORDED → PRESENTATION_SENT → INVITE_SENT
                                        ↘ DECLINED
                                        ↘ AWAITING_OWNER  (вопрос/правка/удаление)
```

- Повторные «интересует вакансия» / «согласен» **не дублируют** пакет.
- После рестарта бот **не выдаёт** PDF/ссылку повторно.
- Согласие на старую версию условий **не переносится**.
- Правка/удаление сообщения согласия → пауза, эскалация владельцу.

---

## Безопасность

- **Токен** вводится локально через `setup`, хранится в `private/bot-token.dpapi` (DPAPI текущего Windows-пользователя). Не попадает в Git, ZIP, логи, тексты ошибок.
- **NTFS-права** на `private/`, `data/`, `examples/materials/`, `config.json` — только текущий пользователь, SYSTEM, Administrators (`.\windows.ps1 harden`).
- **Защита от path escape** при загрузке материалов и скачивании файлов.
- **Fail closed** при отзыве прав (`can_reply=false`), смене владельца, чужом webhook.
- **Нет облачных fallback**, нет платных API, нет доступа модели к секретам.
- Журнал статусов — без токена; содержимое истории — персональные данные.
- Один инстанс на data_dir — `ProcessLock` (Windows + POSIX).

Перед промышленным запуском: включить шифрование диска (BitLocker / Device Encryption) и согласовать с владельцем срок хранения переписки кандидатов (автоудаления нет).

---

## Backup и restore

```powershell
# Перед обновлением: /stop, закрыть бота
.\windows.ps1 backup
# → создаёт TelegramAssistant-backups\backup-<timestamp>

# Restore (вручную, в НОВУЮ пустую папку):
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m assistant_core.ops restore .\backup-<timestamp> .\restored-data
# После restore: /pending, ничего не повторять вслепую
```

Restore всегда переводит бот в **STOP** и помечает неясные отправки как `UNKNOWN`.

---

## Тесты

```powershell
.\windows.ps1 test
```

Запускает:
- `test_core.py` — 23 офлайн-теста ядра (моки Telegram/AI);
- `test_candidate_state.py` — состояния и согласия;
- `test_full_coverage.py` — parser, config, ops, workflow, main;
- `test_acceptance_recruitment.py` — приёмочные A01–B10 по ТЗ 5.0;
- `test_telegram.py` — Telegram-слой, admission, sync_connection.

**Важно:** автотесты работают на моках в Linux. Реальная проверка на Windows 11 Pro заказчика с настоящим Telegram Business-аккаунтом — отдельный шаг (`docs/TEST_PROTOCOL.md` раздел 2, проверки R01–R17).

---

## Повседневная работа

- Ноутбук включён, питание от сети, интернет.
- После перезагрузки — бот в STOP, владелец пишет `/resume`.
- Раз в неделю: проверка свободного места на диске + `.\windows.ps1 backup` (и копия вне ноутбука).
- Срок хранения переписки и порядок удаления — согласуется с владельцем отдельно.

---

## Документация

| Файл | Назначение |
|---|---|
| `TZ.md` | Техническое задание 5.0 (приоритет над README) |
| `START_HERE.md` | Точка входа для команды |
| `docs/INSTALL_CHECKLIST.md` | Пошаговая установка на Windows |
| `docs/OWNER_GUIDE.md` | Памятка владельцу |
| `docs/TEST_PROTOCOL.md` | Протокол приёмки (A01–B10, R01–R17) |
| `docs/ACCEPTANCE.md` | Шаблон акта приёмки |
| `docs/TEAM_ROLES.md` | Распределение ролей в команде |
| `docs/IMPLEMENTATION_STATUS.md` | Статус реализации |
| `docs/LICENSES.md` | Лицензии Python / Ollama / qwen3 |

---

## Статус

- ✅ Ядро 0.2.0: Telegram, SQLite, STOP, backup, DPAPI, NTFS, ProcessLock.
- ✅ Рекрутинговый workflow: машина состояний, parser, идемпотентность, эскалация.
- ✅ Автотесты A01–B10 на моках.
- ⏳ Реальная приёмка на Windows заказчика и настоящем Telegram Business-аккаунте — **NOT RUN** (проверяется командой при установке, см. `docs/TEST_PROTOCOL.md` раздел 2).

---

## Лицензии и ограничения

Исходный код ядра предоставлен заказчику для использования и коммерческой упаковки в пределах условий передачи. Встроенных сторонних runtime-зависимостей нет. Python — PSF; Ollama и qwen3:1.7b — отдельные компоненты со своими лицензиями (см. `docs/LICENSES.md`).

Автотесты и аудит не доказывают качество работы на реальном Windows/Telegram и не являются эксплуатационной приёмкой.