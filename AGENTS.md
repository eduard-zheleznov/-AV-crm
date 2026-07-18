# Repository rules

- Treat `main` as the stable baseline. Work in a task-specific branch.
- Never commit `.env`, API tokens, passwords, browser profiles, screenshots,
  phone numbers, CRM identifiers, queue state, logs, databases, or Google
  service-account credentials.
- Keep runtime state under `data/`, logs under `logs/`, and diagnostics under
  `output/`; all are ignored by Git.
- Default to dry-run for integration checks. Creating or modifying live CRM
  records requires an explicit non-dry run.
- Do not add CAPTCHA solving, fingerprint spoofing, proxy rotation, or other
  anti-bot bypasses. Pause for manual intervention when Avito challenges the
  browser session.
- Preserve idempotency: normalize phone numbers, search CRM before creation,
  and record the queue outcome before moving to the next row.
- Run tests and a secret scan before every commit.

