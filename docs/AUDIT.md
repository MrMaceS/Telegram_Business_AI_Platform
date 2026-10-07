# Независимый аудит пакета TG-LOCAL-WIN-01
Дата: 6 октября 2026 года. Версия исходной заготовки: 0.2.0; ТЗ: 4.0.

**Вердикт: PASS для передачи команде как исходной заготовки с раскрытыми ограничениями. Это НЕ эксплуатационная приёмка и НЕ подтверждение готового установленного ассистента.**

Проверены исходники, тесты, Windows launcher, локальный модельный benchmark, инструкции подключения, ТЗ и критерии фактической приёмки. Код аудитором не изменялся. Реальный Telegram, Ollama и платные API не вызывались.

Повторно выполнено: 23/23 автоматических теста — PASS, Linux, подставные Telegram/AI. Подтверждены исправления трёх ранее воспроизведённых дефектов: один ответ по последнему сообщению пачки; восстановление стадии admission после durable receipt; завершение скачивания не отменяет ACCEPTED. Учтены STOP/смена режима, защита от дублей и неопределённой доставки, права владельца и разрешённых контактов, сохранение opaque файлов, локальное хранение/восстановление.

В windows.ps1 устранены вложенные двойные кавычки в Python -c, зависевшие от legacy parsing Windows PowerShell. Скрипт, DPAPI и Windows-ветка ProcessLock здесь не выполнялись. README отдельно требует NTFS-защиту и отключение облачных функций самого Ollama; chmod не объявляется заменой Windows ACL. Нет платного AI-адаптера или автоматического облачного fallback.

FAQ означает точный утверждённый текст ответа, который выбирает модель; семантический выбор может ошибаться. Это прямо раскрыто, а качественный тест на реальных вопросах обязателен до AUTO. Произвольный текст модели остаётся черновиком владельцу. Задания/файлы обозначены как дополнительный этап проверки; свободная автономная переписка и проверка комплектности не объявляются готовыми.

Word-ТЗ: все 28 непустых нормализованных строк совпадают с TZ.md по содержанию и порядку. Удалены только Markdown-префиксы заголовков и inline backticks.

Остаётся NOT RUN: реальный Windows/PowerShell/DPAPI, Telegram Business, локальная модель и её качество/скорость/RAM, автозапуск, NTFS, установка у заказчика и второй реальный клиент. Эти проверки не заменяются данным PASS. Оценка 2–4 часа относится к ограниченной проверке связки; 15–20 человеко-часов требует подтверждения команды и не измерена этим аудитом.

Упаковка: устаревшие compose.yaml и Dockerfile удалены; отсутствие обоих файлов перепроверено. Финальный ZIP/его фактический состав этим manifest не проверялся.

## Привязка к проверенным файлам
SHA256 приведённого ниже UTF-8 manifest (строки с LF, завершающий LF): 1aeacf9ec5fcfeb2458485d06d8dc0fee46a0976526a7b238a79d118ba7a0e31

```
7f6aff3dfac98ddc77e261a55119ce52575fd330fa949d1090346adc66812c69  README.md
a1b7bde4c07256aeb2dbda716d6ac86625f8180241ed5d2973fe396f0be46773  START_HERE.md
7ab5f4ebaba0dc96c937749804064464def089b99d6ea483fff0e3ad319764ad  TZ.md
607dae46f0ab910cf338b75e7c6c7e45a2da31cfc096795e3d9dcc1846237df8  Telegram_Business_AI_Platform_TZ.docx
afc86aa15341dafef19a651811f56ff91b3a7e086c7097e9dc4a5af6a98b9691  docs/ACCEPTANCE.md
f2d8cfffa8a92c39891206a077bce2db480a672dd06c7ae44446a615c9300ef5  docs/IMPLEMENTATION_STATUS.md
e45a44a81ebb6abde4e38f8386bbaa65b0f81666ef3bd0509156e0f0f7dd2d56  docs/LICENSES.md
8b7fa671743c9dd6c8c5ae960d4318012119f65607a25221d3a6bcf8092f3e4b  docs/TEST_RESULTS.txt
f445c845ff546f76f7de733eec6b28284a42b7da92e156176cbc436709c27475  examples/config.example.json
5a08f599cba1dea25e10b0b2e577ec14c2cd927f204c2af52000747beefe5060  examples/materials/demo_task.md
bd49c702b96368e3b58b24e9d639d0704f71edf8ef27b063819f5b92aa7ee06f  pyproject.toml
4287b031bea16fbf97c06faabcadf830e385579175331c19ccb91d44406a7da8  src/assistant_core/__init__.py
f2082457ed8a1db0517e5fe2149194566b01fd6a8499b3dd754aae98af66c78e  src/assistant_core/ai.py
3d6a465fc3e73f0844cecf151357eb43be0218c7526f4aa6ab7cdca718d4f56e  src/assistant_core/config.py
fa421e0e4f3a43a1da6224432d62ec85aadb8f5ff84f04196ab2cf7f211f5b32  src/assistant_core/locking.py
b50eec0526ebde57a23f1e1187c50305be23c0ca11ef1c2220411ad438136f02  src/assistant_core/main.py
abe2e7f021316ebab6518be1dcf4265a543994ddbc03ee876e5a9e7560ffc759  src/assistant_core/ops.py
661e03aebb4992cae1799b3cb2afb0f84d90c656b38fc0a87090e2b450189def  src/assistant_core/storage.py
32e6fe40e76cb9c17fb8446183f35eedce4b62216acff4099c3a5c9b7edbbe0a  src/assistant_core/telegram.py
5b7cc77897f19333c8bc2088c84c962597059565f702ecb097217e1ae2a334ae  src/assistant_core/workflow.py
75ee34bc70acfbb4374de37295b83fea4a6411ddbbfad015f167318e8e75225e  tests/test_core.py
54e95ce9e231ef72f3bbdbee174e618d7ecac8d50ddcfc76044ac4fed574eb80  tools/check_model.py
03d630a333f736e519e833d0ef9a00b7b69405656dbf64bcae52365476770c07  tools/inspect_telegram.py
4b3159655ea09026fdc89ddaedaf35ecc027b800f91cbc8e1b6c65e49ee3e99a  windows.ps1
```
