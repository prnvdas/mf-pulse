# Monthly Groww statement: automatic import

Your Groww holdings statement (.xlsx) contains your **name, PAN, mobile number and full folio numbers**, and this
repository is **public**. So the plain file is never committed (`MF-Holding/` and `*.xlsx` are git-ignored, and an
optional hook blocks the mistake). Instead it is encrypted first; only the encrypted copy goes into `statements/`.

## One-time setup (5 minutes)
1. Make a long random passphrase and keep it in your password manager: `openssl rand -base64 32`
2. GitHub, repo **Settings, Secrets and variables, Actions, New repository secret**: name `STATEMENT_KEY`, value = that passphrase.
3. Optional seatbelt: `git config core.hooksPath deploy/hooks` (refuses to commit a plain .xlsx/.pdf).
4. To get the 3rd-of-month check from cron-job.org too: `python3 deploy/setup_cronjob.py` again (adds "Monthly statement check").

## Every month
1. Download the holdings statement from Groww and drop it into `MF-Holding/` (on or after the day your SIPs show up).
2. `python src/encrypt_statement.py --push`  (asks for the passphrase; encrypts, verifies, commits, pushes).
3. The `statement` workflow decrypts it on GitHub's runner, imports only sanitised figures (units, invested, folios
   by last 4 digits, XIRR), recomputes the tax view and commits `docs/data` and `config/folios.json`.

On the 3rd, an issue reminds you; if no statement for the month has been uploaded by 20:00 IST, the workflow opens
another. Nothing personal is ever written to the repo: no name, PAN, phone or full folio number.

Plain-text alternative: if you make the repository private (GitHub Pro/Team keeps Pages working), the plain file could be
committed, but the published `docs/` site stays public either way.
