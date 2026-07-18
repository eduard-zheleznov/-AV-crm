# Development workflow

1. Keep `main` stable and create one feature/fix branch per task.
2. Put secrets only in a local `.env` or the host secret store.
3. Test with mock integrations and `--dry-run` first.
4. Run the real browser in a persistent local profile and handle any challenge
   manually; do not attempt to bypass it.
5. Verify queue resume, duplicate handling, CRM field discovery, and stop-limit
   behavior before production use.
6. Commit only source, tests, safe examples, and documentation.

Before commit:

```bash
ruff check src tests
ruff format --check src tests
pytest
git diff --check
git grep -nE '(password|token|secret)\s*[:=]' -- ':!*.example' ':!docs/*'
```

Runtime phone numbers, CRM IDs, browser sessions, screenshots, and errors can
contain personal or operational data. They must stay on the remote computer and
outside Git.
