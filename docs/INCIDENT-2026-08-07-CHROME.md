# Инцидент Chrome 07.08.2026 — diagnose-only

## Ограничения и текущий статус

- Проект LPTracker `102497`, воронка `426203`.
- После двух circuit-breaker действует окончательный **NO-GO**.
- B4/B5 не устанавливать; VDS, CRM, очередь, сценарий и лимиты не менять.
- До отдельного GO разрешены только локальный код, offline-тесты и наблюдение.
- CAPTCHA не обходить: остановить начало очереди и ждать оператора.

## Факты двух run

### `20260807-070512-e60ba9`

- аварийная остановка 08:23 МСК после пяти последовательных technical;
- 15 обработано: 2 CRM-лида, 3 `retry_phone`, 2 `inactive`, 8 technical;
- строки 852–854 и 857–858 завершались сообщением о неполной загрузке;
- строка 859 завершилась сообщением «нет ответа расширения»;
- между сериями были успешные строки 851 и 856: отказ был перемежающимся, а не
  постоянным отключением расширения.

### `20260807-080214-180bf0`

- аварийная остановка 05:20 UTC после пяти последовательных technical;
- 16 ссылок: 3 CRM-лида, 3 `retry_phone`, 3 `inactive`, 7 technical;
- финальные строки 922–925 и 860 завершались `page_not_ready` примерно через
  91–92 секунды каждая.

## Точная программная точка ожидания

В расширении `1.0.7` функция `navigateTab()` завершалась только при одновременном
выполнении двух условий:

1. Chrome прислал `chrome.tabs.onUpdated` со `status === "complete"`;
2. фактический pathname полностью совпал с pathname URL из очереди.

При первой неудаче `getManagedTab()` скрыто удалял старую managed tab, создавал
новую и повторял навигацию. Интервалы около 91 секунды точно соответствуют двум
последовательным таймаутам по 45 секунд. Значит, команды были получены service
worker, но gate `status=complete + exact pathname` не подтвердился ни на старой,
ни на новой вкладке.

Ретроспективно нельзя доказать, какая из двух физических причин была первой:

- renderer действительно не завершал загрузку/отрисовку;
- Avito выполнил канонический redirect со сменой slug, а строгий pathname отверг
  уже отрисованное то же объявление.

Версия `1.0.7` не сохраняла `tab.status`, фактический URL, Avito ID, состояние DOM
или ошибку content script, поэтому более точное различение после факта невозможно.
Это ограничение доказательств, а не предположение о выключенном расширении.

Сообщение строки 859 «нет ответа расширения» также не доказывает отключение:
старый bridge имел timeout около 115 секунд, но state machine расширения могла
потратить 90 секунд на две навигации, затем до 15 секунд на соединение с content
script и до 10 секунд на DOM номера. Bridge мог завершиться раньше самой команды.

## Live evidence после инцидента

- 14:53 МСК: `chrome://extensions`, Avito CRM Local Bridge `1.0.7` включён,
  developer mode включён, service worker активен, явного Errors нет.
- 14:58 МСК: DevTools service worker — Console пустая, `No Issues`; подключение
  произошло после run, поэтому исторических сообщений нет.
- В видимой панели Chrome отдельной Avito managed tab не было; видны Google Drive
  и `Расширения`.
- AnyDesk показывал экран, но UI-control не принимал клики. Refresh, restart, B4
  и другие действия не выполнялись.

Эти наблюдения исключают простую версию «расширение сейчас выключено», но не
восстанавливают историческое состояние renderer или managed tab.

## Локальный патч `1.12.25` / extension `1.0.8`

- startup canary рендера Avito до обработки первой строки и без клика;
- readiness по фактически отрисованному DOM, без требования `readyState=complete`;
- сравнение по числовому Avito ID, допускающее каноническую смену slug;
- отдельные классы `browser_infra` и `listing_mismatch`;
- максимум одна пересозданная managed tab и один повтор той же строки;
- настраиваемый backoff, затем обязательный canary перед следующей строкой;
- idle heartbeat через активный long-poll во время worker;
- timeout bridge по полному верхнему пределу state machine;
- JSONL-диагностика обеих попыток: elapsed, tab status, discarded, ID, DOM probe,
  classification и ошибка content script;
- полные URL, телефоны, токены и содержимое страницы в журнал не попадают.

## Условия будущего GO

1. Worker остановлен, B4/B5 сняты; обновление выполняется под оператором.
2. Код `1.12.25` и unpacked extension `1.0.8` обновлены одновременно.
3. После ручного restart обычного Chrome service-worker Console без ошибок.
4. Ручная страница Avito отрисовывается и реагирует.
5. Один операторский `avito-extension-test --max-clicks 1` завершён успешно.
6. В `chrome-extension-health.jsonl` есть `healthy` и полная диагностика canary.
7. Затем разрешён только один canary-run с жёстким пределом одной строки.
8. Автономный B4 допустим только после отдельного подтверждённого GO центральной
   задачи. При CAPTCHA — ожидание оператора без refresh-цикла.

## Follow-up canary `1.12.25` / `1.0.8`

После deploy `e71ce41` startup navigation прошла, но сообщение «команда клика
отправлена» было ложноположительным: кнопка «Показать телефон» осталась закрытой.
Расширение сразу перешло к OCR и трижды сняло закрытую кнопку. CRM/live run и очередь
не затрагивались.

Точный root cause: `content.js` приравнивал dispatch `button.click()` к успешному
reveal и не проверял post-click DOM transition. Дополнительно текст «Временный номер»
преждевременно разрешал OCR. Патч `1.12.26` / `1.0.9` добавляет click outcome contract,
один bounded recovery и запрет OCR до DOM-confirmation.

## Live canary `1.12.26` / `1.0.9`

Точный операторский вывод доказал, что тест не завис: он сам завершился после
первого неподтверждённого клика, одного повтора и ошибки
`click_not_effective`; PowerShell остановил объединённую команду до CRM. Кнопка
визуально осталась закрытой, очередь и CRM не затронуты. Значит, version mismatch
и отсутствие bounded timeout не были причиной этого canary: оба synthetic
content-script click dispatch были выполнены, но Avito не принял их как реальную
активацию пользователя.

Патч `1.12.27` / extension `1.0.10` заменяет synthetic dispatch на ограниченный
browser-level click через `chrome.debugger` и CDP `Input.dispatchMouseEvent`.
Service worker проверяет текущие command ID, managed tab ID и Avito listing ID,
всегда снимает debugger в `finally`, допускает не более одного recovery и по-прежнему
запрещает OCR без подтверждённого reveal. Version handshake принудительно
перезагружает вкладку со старым/отсутствующим content script до клика.

## Startup navigation regression `1.12.27` / `1.0.10`

После deploy обе попытки startup health probe завершались примерно через 45 секунд:
`tabStatus=complete`, `probe=null`, `content_script_unavailable`, `Receiving end does
not exist`; видимая surface оставалась `about:blank`. Расширение было включено,
service worker и bridge активны, host access к Avito разрешён, Errors отсутствовали.

Root cause: сразу после `tabs.update` Avito URL находился в `pendingUrl`, static
content script с `run_at=document_idle` ещё закономерно отсутствовал. Recovery
ошибочно вызывал `tabs.reload` до commit, тем самым отменяя pending navigation и
перезагружая committed `about:blank`.

Патч `1.12.28` / extension `1.0.11` запрещает probe/reload/injection при наличии
`pendingUrl`, сохраняет готовность по DOM без требования `status=complete` и после
commit даёт static script grace-период. Затем допускается ровно один programmatic
fallback через `chrome.scripting.executeScript`, только в main frame ожидаемой
Avito surface и с повторной exact-tab/same-listing проверкой. Content bootstrap
идемпотентен. JSONL пишет только privacy-safe `actualSurface`, `pendingSurface` и
статус injection, без полного URL.

## Trusted-click viewport regression `1.12.28` / `1.0.11`

Две CDP dispatch вернули success, но DOM reveal не изменился. Координаты
кнопки измерялись content script до `chrome.debugger.attach`; attach может
добавить infobar и изменить viewport, поэтому CDP мог получать устаревший
центр. Точное совпадение click target с другим элементом ретроспективно не
наблюдалось, поэтому это root cause с высокой, но не live-confirmed уверенностью.

Патч `1.12.29` / extension `1.0.12` выполняет attach до измерения. После attach
вкладка фокусируется, content script заново ищет кнопку и проверяет
hit target, затем service worker повторно проверяет command/tab/listing/STOP и
сразу посылает CDP mouse events. Pre-attach coordinates не передаются. Любая
ошибка attach/content/revalidation/input завершается fail-closed, debugger снимается
в `finally`, OCR до reveal confirmation не запускается.
