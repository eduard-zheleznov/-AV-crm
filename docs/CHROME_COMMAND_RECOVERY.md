# Chrome command recovery — 1.12.67 / extension 1.0.33

## October 9: dispatched command without a reply

The Windows health journal showed successful `health_probe` results followed by
`reveal_phone` dispatches for the same queue row, then only `command_timeout`.
This proves missing command completion, not queue exhaustion or a scheduled
pause. The historical log did not capture the last browser API call, so it does
not prove whether Chrome terminated the worker or a native API promise stalled.

This release closes both unprotected paths:

- Chrome tab/window/permission/injection API promises have a 10-second deadline.
  An infrastructure failure returns to Python instead of blocking the sequential
  loop indefinitely. Content message promises are excluded: confirmed manual
  CAPTCHA waiting still has no deadline.
- While executing a command, an independent five-second pulse calls a Chrome
  extension API and reports the current stage to the local bridge. This uses
  Chrome's documented idle-timer reset (minimum Chrome 110); the pulse stops at
  command completion and does not run during idle queue time. Repeated identical
  stages refresh liveness without growing the status-event queue or health log.
- If a new service-worker instance connects while a command is dispatched,
  Python explicitly fails the old command without redispatching a phone click.
  Late results cannot override that failure. The existing bounded same-row
  browser recovery then applies.

The worker's actual API-stall path, following command, repeated progress,
worker-instance replacement and late-result rejection have synthetic regression
coverage. No CAPTCHA bypass, CRM mutation or extra click loop was added.
The exact Windows incident still needs post-update live acceptance; do not
describe a successful standalone health check as proof of that acceptance.

## Failure and fix

The Python bridge clears a command after its deadline. It subsequently reports
`inactive` for that command. Previously the extension's content-reply waiter
handled only `cancelled`, so a lost reply could block its sequential poll loop
and prevent subsequent health probes from running.

The extension now handles both terminal states and reloads only its own managed
document to dispose of the old content command. Content replies and probes have
independent deadlines. A confirmed manual challenge suspends the content-reply
deadline; after clearance the deadline restarts. STOP and inactive commands still
cancel manual waiting. Network calls used by polling, cancellation and event
delivery have bounded timeouts.

Status events must match both the managed tab and current command ID. Late
events from a previous command cannot alter a new CAPTCHA notification session.
No automatic CAPTCHA solving or additional phone clicks have been introduced.

## Verification

`tests/js/test-command-lifecycle-core.cjs` exercises the real worker functions
with synthetic Chrome APIs: expired bridge commands, explicit STOP, a lost
content reply, managed document cleanup and successful following command. It
also verifies manual waiting beyond the normal deadline, resolution and a
missing reply after resolution. No live CRM data or browser session is used.

After installing the Python update, reload the existing unpacked extension at
`chrome://extensions`, verify version 1.0.33 and keep incognito permission enabled.
The prior Python process is restarted by the installer. It intentionally refuses
an old extension version, so both updates are required. Existing settings and
recipients stay unchanged. Do not add a second copy of the extension.

Live acceptance on the Windows host remains necessary: start through the pult,
solve any genuine challenge manually, and check progress plus Diagnostics.
