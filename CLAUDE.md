# MF Pulse: working rules

A single-page dashboard (`docs/index.html` + JSON in `docs/data`), Python jobs in `src/`, GitHub Actions workflows, cron-job.org triggers.

## 1. Don't assume
- If a request can mean two things, say which one you took, or ask. Don't silently pick.
- Look before you change: read the file, the data, the test. Don't work from memory of how it "probably" is.
- Say what you don't know (a date, a setting, whether something ran). Never fill a gap with a guess.
- Numbers shown on the page must come from data, never from a guess. If the data is missing, show nothing or say so.

## 2. Minimum code
- The simplest thing that does the job. No extra options, settings, abstractions or "just in case" paths.
- Keep the page plain and readable for a beginner: one clear number or sentence first, detail folded away.
- Don't add a new section, file or dependency when an existing one can do it.

## 3. Touch only what you must
- Change only what the request needs. No drive-by refactors, renames or reformatting.
- Match the surrounding code: naming, comment density, style.
- Something unrelated looks wrong? Mention it at the end; don't fix it unasked.
- Never commit personal identifiers (PAN, phone, full name, folio numbers beyond the last 4) or the plain Groww statement. Never ask for, paste or store tokens or keys.

## 4. Define success criteria
Before starting, state how you will know it worked; before finishing, show that it did.
- **Page changes:** run the browser suites (`cd tests && node run.js`; from WSL: `cmd.exe /c "cd /d C:\mf-pulse\tests && node run.js"`) and look at a screenshot at desktop and phone width, light and dark. Check the exit code, not just the text.
- **Python changes:** `python -m unittest discover -s tests -p "test_*.py"` (needs `requirements.txt`).
- **Data files:** `python3 src/validate_data.py` must pass; no NaN reaches the page.
- Report faithfully: failures with their output, skipped steps as skipped. Don't call something done that wasn't verified.
- Push only after tests pass: `git pull --rebase --autostash && git push` (bots commit data files, so pulls are routine). Then confirm the CI run.

## Project notes
- Data comes from Yahoo (kept on purpose), AMFI/mfapi, Groww pages, NSE archives, Moneycontrol. Several are unofficial: expect them to break, and make failures visible, not silent.
- The daily recap email is off (`profile.recap_issue: false`). Folio last-4 labels stay. The GitHub token behind cron-job.org does not expire, so `github_token_expires` stays unset.
- A secret scan (gitleaks, `.gitleaks.toml`, `.github/workflows/secrets.yml`) runs on every push and weekly over the whole history. It also catches PAN and mobile numbers. If it fails, revoke or change the secret first: deleting the file does not remove it from history.
- Honest over flattering: forecasts are labelled as extrapolations, with their sample size, never as predictions.
