# Chrome command recovery — 1.12.66 / extension 1.0.32

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
`chrome://extensions`, verify version 1.0.32 and keep incognito permission enabled.
The prior Python process is restarted by the installer. It intentionally refuses
an old extension version, so both updates are required. Existing settings and
recipients stay unchanged. Do not add a second copy of the extension.

Live acceptance on the Windows host remains necessary: start through the pult,
solve any genuine challenge manually, and check progress plus Diagnostics.
