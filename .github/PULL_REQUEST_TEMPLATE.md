## What & why

<!-- What changes, and what training question / bug motivates it. -->

## Checklist

- [ ] `python3 -m unittest discover -s tests` passes (no network, no `.env`, no `config.json` required)
- [ ] **Stdlib only** — no new third-party imports (hard rule; see CONTRIBUTING.md)
- [ ] Athlete numbers read through `config.py`, not hardcoded
- [ ] New run-metric consumers filter through `enrichment.is_real_run()`
- [ ] Docs updated where behavior changed (README table, ARCHITECTURE, METHODOLOGY, skills)
- [ ] No personal activity data in code, fixtures, or docs
