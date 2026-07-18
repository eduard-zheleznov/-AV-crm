# Установка и ввод в эксплуатацию

## 1. Подготовка Windows-компьютера

Установите:

1. Git for Windows.
2. Python 3.11 или новее с опцией `Add Python to PATH`.
3. Tesseract OCR (Windows build). Путь обычно
   `C:\Program Files\Tesseract-OCR\tesseract.exe`.

Система рассчитана на интерактивную Windows-сессию: Chromium должен быть виден,
чтобы оператор мог войти в Avito и вручную пройти проверку. Не запускайте её как
скрытый Windows service.

## 2. Установка проекта

```powershell
git clone https://github.com/eduard-zheleznov/-AV-crm.git C:\avito-crm
cd C:\avito-crm
git switch feature/adopt-proven-avito-flow
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

Обновление кода не затрагивает `data/`, `.env`, браузерный профиль или очередь.
Перед обновлением остановите worker и сохраните backup рабочей таблицы.

## 3. Локальные секреты

Откройте `C:\avito-crm\.env` и замените placeholder-значения. Не отправляйте
этот файл в GitHub. Предпочтительно задавать `LPTRACKER_PROJECT_ID`, поскольку
названия проектов могут совпадать.

Для Google создайте service account в Google Cloud, включите Google Sheets API,
скачайте JSON в отдельный защищённый каталог (например
`C:\ProgramData\AvitoCrm\google-service-account.json`) и откройте рабочую таблицу
на email `client_email` из JSON с правом редактора. В `.env` укажите:

```dotenv
GOOGLE_CREDENTIALS_FILE=C:\ProgramData\AvitoCrm\google-service-account.json
GOOGLE_SPREADSHEET_ID=идентификатор_между_d_и_edit_в_URL
GOOGLE_WORKSHEET=Лист1
```

JSON не должен находиться внутри репозитория.

## 4. Формат таблицы

Первая строка — заголовки. Обязательна только колонка `Ссылка`. Служебные
колонки система добавит сама: `Статус`, `Телефон`, `CRM lead ID`, `Ошибка`,
`Попытки`, `Обработано`, `Run ID`.

Не переименовывайте колонки во время работающего процесса. Для другого набора
названий используйте переменные `QUEUE_*_COLUMN` в `.env`.

## 5. Staging-последовательность

Рекомендуемый вариант — единый мастер из
[`FIRST_TEST.md`](FIRST_TEST.md):

```powershell
.\scripts\first-test.ps1 -Source xlsx -File ".\queue-template.xlsx" -Sheet "Лист1"
```

Либо те же шаги вручную:

```powershell
# Никаких записей в CRM
.\scripts\doctor.ps1 -Source google -OnlineCrm

# Войдите в Avito в открывшемся браузере; профиль сохранится в data\browser-profile
.\scripts\run.ps1 -Source google -Limit 1 -Mode capture

# Сверьте распознанный номер со страницей объявления и со строкой таблицы

# Создайте один тестовый лид из уже распознанного номера
.\scripts\run.ps1 -Source google -Limit 1 -Mode crm -Live
```

В LPTracker проверьте:

- номер контакта;
- название лида `Авито — <ID объявления>`;
- значение поля `Тег+ для новых с Ав и Ян`;
- отсутствие второго контакта с тем же номером.

Только после этого запускайте полный режим.

## 6. Production

```powershell
.\scripts\run.ps1 -Source google -Limit 10 -Mode full -Live
```

Для Task Scheduler используйте `Run only when user is logged on`, рабочий каталог
`C:\avito-crm` и команду PowerShell с `scripts\run.ps1`. Не ставьте параллельные
запуски: локальный lock всё равно отклонит второй worker.

## 7. Rollback

```powershell
.\scripts\stop.ps1
git status
git switch --detach prod-20260718-baseline
```

Baseline не содержит worker-кода, поэтому практический rollback production после
первого принятия должен указывать на последний проверенный release tag. Runtime
данные не удаляются. Созданные CRM-лиды автоматически не удаляются — это
осознанная защита от разрушительных откатов.
