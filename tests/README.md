# Tests

Everything that can break the dashboard without anyone noticing is checked here.

| What | Where | Runs |
|---|---|---|
| The page in a real browser: 12 clock states x 2 widths, every data file broken 7 ways, charts, hide-amounts, menu, notices | `suites/*.js` over `fixtures/data` | `node run.js` |
| The real `docs/data` with the real clock: does the live site still render what the nightly jobs produced | `suites/live.js` | `node run.js live` |
| SIP detection, transaction import, snapshots, fund health, config overlay, data validity | `test_backend.py` | `python -m unittest discover -s tests -p "test_*.py"` |
| Every `docs/data/*.json` is strict JSON (no NaN) | `src/validate_data.py` | also run by every workflow before it commits |

Locally: `cd tests && npm install && npx playwright install chromium && npm test`.

`fixtures/data` is a frozen copy of `docs/data`, so the suites don't change when the nightly jobs update the real files.
When the page gains a feature that needs new data, copy the new file in: `cp docs/data/*.json tests/fixtures/data/`.
In GitHub Actions the workflow `tests.yml` runs all of it on every change to the page or the code.
