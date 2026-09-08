# Установка и ввод в эксплуатацию

## 1. Подготовка Windows-компьютера

Установите:

1. Git for Windows.
2. Python 3.11 или новее с опцией `Add Python to PATH`.
3. Tesseract OCR (Windows build). Путь обычно
   `C:\Program Files\Tesseract-OCR\tesseract.exe`.

Система рассчитана на интерактивную Windows-сессию: Playwright Chromium или Chrome Stable должен быть виден,
чтобы оператор при необходимости мог войти или выйти из Avito и вручную пройти
проверку. Вход в Avito необязателен. Не запускайте систему как скрытый Windows service.

Для режима обычного пользовательского Chrome без Playwright установите локальное
расширение по инструкции [CHROME_EXTENSION.md](CHROME_EXTENSION.md). Оно не решает
капчу автоматически: текущая строка ждёт ручного решения в той же вкладке.

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

Релиз `1.12.43` с обязательным инкогнито нельзя выкатывать поверх работающего
worker. Сначала остановите run и убедитесь, что управляющие флаги запуска сняты.
Код и расширение `1.0.19` проверяются под оператором; автоматический production-run
разрешается только после отдельного GO.

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

Если Avito не показывает телефон в поставляемом Playwright Chromium, но показывает
его в обычном Chrome на том же Windows-компьютере, можно включить установленный Chrome Stable:

```dotenv
AVITO_BROWSER_CHANNEL=chrome
AVITO_PROFILE_DIR=C:\avito-crm\data\browser-profile-chrome
```

Сначала закройте worker и все окна Playwright, затем откройте `avito-profile`, войдите в Avito
и вручную проверьте одно объявление. Старый `data\browser-profile` не удаляйте, не копируйте
и не открывайте одновременно с новым Chrome-профилем. Пустой `AVITO_BROWSER_CHANNEL` сохраняет
прежний Chromium fallback.

Если сам запуск Chrome Stable через Playwright уже вызывает проверку, не меняйте
сетевой адрес и не повторяйте перезагрузки. Используйте `AVITO_BROWSER_DRIVER=chrome_extension`:
это обычный Chrome-профиль и локальное расширение, без WebDriver и remote debugging.

## 4. Формат таблицы

Первая строка — заголовки. Обязательна только колонка `Ссылка`. Служебные
колонки система добавит сама: `Статус`, `Телефон`, `CRM lead ID`,
`Шаг воронки`, `Попытка завода в CRM`, `Повторный CRM ID`,
`Попытки повторного открытия`, `Ошибка`, `Попытки`, `Обработано`, `Run ID`.

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

# Откройте общий профиль; войдите, выйдите или оставьте гостевой режим,
# затем закройте Playwright-браузер. Состояние сохранится в AVITO_PROFILE_DIR.
.\.venv\Scripts\python.exe -m avito_crm avito-profile

# Получите один номер без записи в CRM
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

Для режима `chrome_extension` перед любым capture дополнительно проверьте:

1. `chrome://extensions`: расширение `1.0.19` включено, разрешения `scripting` и
   `debugger` приняты, **Разрешить использование в режиме инкогнито** включено,
   Console service worker без ошибок;
2. страница Avito вручную отрисовывается и реагирует на прокрутку в окне инкогнито;
3. два последовательных `avito-extension-test --max-clicks 1` на разных ранее
   успешных объявлениях проходят под оператором без CRM и очереди;
4. `logs/chrome-extension-health.jsonl` содержит итог `healthy` и не содержит полных URL;
5. при искусственном offline-тесте неготового renderer run завершается с нулём
   обработанных строк.

Только после этого запускайте полный режим.

## 6. Production

Для ежедневной работы используйте ярлык **«Avito в CRM»** на рабочем столе. Если ярлык был удалён,
его можно восстановить одноразовой командой:

```powershell
.\scripts\create-shortcut.ps1
```

Полная настройка GUI и Google: [`GOOGLE_GUI.md`](GOOGLE_GUI.md).

После успешной проверки GUI можно включить запуск сотрудником без RDP:
[`REMOTE_CONTROL.md`](REMOTE_CONTROL.md). Установка создаёт отдельную Windows-задачу;
она не меняет `.env`, браузерный профиль и данные очереди.

Терминальная альтернатива:

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

Для rollback `1.12.43` сначала остановите worker, затем откатите одновременно код
и unpacked extension к совместимой паре `1.12.37` / `1.0.18` на commit
`eacb41fc4cfb90f5988a1b08318fb1cab58424ab`. Не смешивайте версии:
проверка совместимости намеренно блокирует такой запуск. Rollback не должен менять
Google Sheets, LPTracker, `.env`, `data/` или профиль обычного Chrome.
