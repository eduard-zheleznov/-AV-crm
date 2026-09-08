# Изолированный Windows-узел Avito CRM

Эта схема запускает Chrome и Avito CRM в отдельной Windows 11 VM.
Хозяин компьютера продолжает работать в своей Windows: мышь, клавиатура и вкладки
между системами не смешиваются. VM может работать без видимого окна, а доступ к ней
настраивается отдельно.

## Что это решает и не решает

- Решает: изоляцию Chrome, фокуса, скриншотов, секретов и удалённого доступа.
- Не решает: смену внешнего IP. VM с NAT выходит в интернет через тот же домашний IP,
  что и хост. Другой домашний IP появится только если сама VM работает на другом доверенном ПК.
- Не добавляет: CAPTCHA-solving, stealth, spoofing или proxy rotation. При проверке Avito процесс ждёт ручного действия.

## Требования к хосту

Безопасный production-порог комплекта:

- 64-битная Windows 10/11, VT-x или AMD-V в UEFI/BIOS;
- минимум 12 ГБ RAM, рекомендуется 16 ГБ;
- минимум 4 логических CPU;
- не менее 100 ГБ свободно для VM 80 ГБ и запаса;
- SSD. HDD не блокирует тест, но под нагрузкой заметно мешает работе хозяина;
- компьютер включён, не спит, его основной пользователь не вышел из Windows.

Скрипт preflight только читает характеристики. `BLOCKED` означает, что VM не создаётся.
Историческая диагностика LENOVO показывала около 6 ГБ RAM и HDD. Если это тот же ПК, полная VM будет
остановлена до любых изменений. Для production нужны апгрейд RAM/SSD или другой ПК.

Windows 11 в VM требует не менее 4 ГБ RAM, 64 ГБ диска, 2 vCPU, UEFI и vTPM. Скрипт создаёт
6 ГБ RAM / 2 vCPU / 80 ГБ dynamic VDI, EFI, TPM 2.0 и NAT. Общий буфер, drag-and-drop, USB и звук отключены.

## Что потребуется вручную

Полностью автоматизировать безопасно нельзя только три вещи:

1. Один раз скачать [Windows 11 ISO с сайта Microsoft](https://www.microsoft.com/software-download/windows11)
   и иметь действительную лицензию Windows для VM.
2. В открывшейся `chrome://extensions` загрузить unpacked-папку и включить
   **«Разрешить использование в режиме инкогнито»**. Chrome не позволяет включить это скрыто.
3. Один раз привязать Chrome Remote Desktop внутри VM к рабочему Google-аккаунту и задать PIN.

Остальное делают скрипты: VirtualBox, Windows VM, Git, Python, Chrome, Tesseract, точный commit,
`.env`, Google JSON, STOP, пассивная Windows-задача и проверки.

## Порядок установки

### 1. Read-only preflight на хосте

Загрузите `bootstrap-worker-vm-host.ps1` из точного release commit, сверьте его SHA-256 и запустите.
Без `-Create` он только читает характеристики. Команда и хеш выдаются в release handoff.

### 2. Создание VM на хосте

После `READY`:

```powershell
& $Bootstrap -ReleaseCommit <RELEASE_COMMIT> -Create -SourceRoot D:\avito-crm -WindowsIso "C:\Users\USER\Downloads\Win11.iso" -InstallVirtualBox
```

Команда:

- экспортирует `.env` и Google JSON в отдельную ACL-защищённую папку;
- ставит Oracle VirtualBox 7.2.8 через `winget`, если он ещё не установлен;
- просит уникальный пароль только для гостевой Windows;
- создаёт VM и запускает unattended-установку Windows;
- подключает папку настроек как read-only `AvitoCrmTransfer`;
- регистрирует headless-запуск VM при входе хозяина в Windows.

Пароль не печатается. Временный password-file имеет ограниченный ACL и удаляется в `finally`.
Пароль VM нельзя повторно использовать в других системах.

### 3. Одна bootstrap-команда внутри VM

Откройте PowerShell от имени администратора внутри VM. Однострочная команда из handoff скачивает
проверенный `install-worker-node.ps1` и передаёт ему exact commit. Он ставит Git, Python, Chrome,
Tesseract, Avito CRM и подготавливает extension bridge. Playwright Chromium не скачивается.

Ожидаемый финал:

```text
WORKER NODE INSTALLED: app=1.12.43; extension=1.0.19; commit=<RELEASE_COMMIT>
STOP включён; Google-пульт не устанавливался; CRM и очередь не затронуты.
```

### 4. Два ручных флажка Chrome

В открытом Chrome:

1. режим разработчика → **Загрузить распакованное расширение** → `C:\avito-crm\chrome-extension`;
2. **Сведения** → **Разрешить использование в режиме инкогнито**.

Версия должна быть `1.0.19`.

### 5. Импорт и read-only проверка

Внутри VM:

```powershell
Set-Location C:\avito-crm
.\scripts\complete-worker-node.ps1 -TransferDir "\\VBOXSVR\AvitoCrmTransfer"
```

Скрипт сверяет SHA-256 пакета, версии и commit, импортирует секреты, включает защиту
времени, выполняет чтение Google/очереди/LPTracker без записи и регистрирует пульт в состоянии
`Disabled`. B4/B5, CRM и очередь не изменяются.

На хосте отключите read-only share:

```powershell
.\scripts\disconnect-worker-vm-transfer.ps1
```

После контроля VM удалите с хоста `AvitoCrm-secure-transfer-*`: это временная копия секретов.

### 6. Независимый доступ и автовход

Внутри VM откройте <https://remotedesktop.google.com/headless>, войдите в рабочий Google-аккаунт,
выберите Windows, выполните показанную Google команду в PowerShell и задайте PIN. После этого
хозяин не нужен для каждого подключения.

Для восстановления после перезагрузки VM нужен вход в её Windows-профиль. Команда
`.\scripts\prepare-worker-vm-autologon.ps1` скачивает только подписанный Microsoft Sysinternals Autologon и открывает его.
Введите пароль VM и нажмите `Enable`. Windows хранит его как LSA secret, но администратор хоста всё равно
может получить доступ к диску VM. Поэтому хост должен быть доверенным.

### 7. Ручной smoke без CRM

Пока пульт `Disabled` и STOP есть, откройте Chrome в инкогнито и вручную проверьте Avito.
Затем запустите один fail-safe offline-тест:

```powershell
C:\avito-crm\scripts\test-worker-node.ps1 -Url "AVITO_URL"
```

Этот тест не пишет CRM и очередь. Он временно снимает STOP и гарантированно возвращает его в `finally`.

### 8. Единственный активный узел

Локальные lock-файлы не защищают от двух разных компьютеров. Перед переносом production:

1. в Google убедиться, что B4/B5 выключены;
2. на старом узле выполнить `.\scripts\disable-worker-node.ps1` и дождаться `OLD NODE DISABLED`;
3. на новом узле выполнить `.\scripts\enable-worker-node.ps1 -ConfirmPreviousNodeStopped`.

Активатор сам читает B4/B5 и отказывается стартовать, если хотя бы один флаг включён. При любой ошибке
он возвращает STOP и `Disabled`.

После активации сначала выполняется один `avito-extension-test` без CRM, затем отдельно согласуется
canary 1 с живой CRM-записью.

## Влияние на хозяина и безопасность

- Окна и Chrome не перехватывают фокус основной Windows.
- Нагрузка VM: до 6 ГБ RAM, 2 vCPU и дисковые операции Chrome/OCR. На 16 ГБ + SSD обычная офисная работа
  должна оставаться комфортной; на 12 ГБ возможны паузы под нагрузкой.
- Общий буфер и host folders после импорта отключены. Секреты остаются в guest `C:\avito-crm\.env`
  и `C:\avito-crm\data`.
- Хозяин-администратор технически может скопировать диск VM. Используйте только доверенный компьютер,
  отдельные CRM/Google/Chrome-учётные данны и 2FA.
- Хозяину нужна понятная «красная кнопка»: отключить VM в VirtualBox. После этого пульт не имеет heartbeat
  и не принимает новые команды.

## Backup и rollback

После полного smoke и до production выключите VM и создайте снимок `golden-ready`. В VirtualBox:
**Snapshots → Take**. Он включает Windows, Chrome, extension и локальные настройки.

При откате:

1. на VM — `disable-worker-node.ps1`;
2. на прежнем узле — `enable-worker-node.ps1 -ConfirmPreviousNodeStopped`;
3. восстановить VM из `golden-ready` только когда она пассивна.

Удаление VM не входит в автоматический rollback: скрипты никогда не удаляют VM, диск, `.env`, Google-очередь
или CRM-лиды.
