## What & why

<!-- One paragraph: what this changes and the motivation. Link the issue (e.g. Closes #123). -->

## Checklist (the non-negotiables)

- [ ] `ruff check src tests` and `mypy` are clean.
- [ ] `pytest -m "not integration"` is green — **including the tenant-isolation gate**.
- [ ] New data paths have a leakage test; new context decisions have a replay test.
- [ ] No placeholders (`TODO`/`FIXME`/`dummy`/`NotImplementedError`) and no empty stub packages.
- [ ] Stays inside the scope boundary (not an inference engine / vector DB / agent framework).
- [ ] ADR added/updated if a significant decision changed.
- [ ] Conventional-commit title (`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`).

## Notes

<!-- Trade-offs, follow-ups, screenshots, anything reviewers should know. -->
