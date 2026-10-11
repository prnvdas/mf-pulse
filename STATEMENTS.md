# Monthly Groww statement: one script

Your Groww holdings statement (.xlsx) contains your **name, PAN, mobile number and full folio numbers**, and this
repository is **public**, so the plain file is never committed (`MF-Holding/` and `*.xlsx` are git-ignored; a hook blocks
the mistake). Only an **encrypted** copy goes into `statements/`; the GitHub workflow decrypts it on its own temporary
disk and imports only sanitised figures (units, invested, folios by last 4 digits, XIRR).

## Everything, in one command
1. Download the holdings statement from Groww into `MF-Holding/`.
2. Run:

       python3 deploy/statement_setup.py

   First time it sets up what is missing (a random passphrase kept in `~/.config/mf-pulse/`, the `STATEMENT_KEY` GitHub
   secret, a git seatbelt, the cron-job.org monthly job), then encrypts, pushes, and watches GitHub import it.
   Every later month it skips the setup and just does the encrypt, push and watch.

`--dry-run` does everything except touch GitHub.

The one manual moment on first run: GitHub's secret. Either paste a token with *Secrets: Read and write* (it sets the
secret for you, needs `pip install pynacl`), or press Enter: the passphrase is put on your clipboard, the right GitHub
page opens, and you paste it (30 seconds).

## When
On the 3rd of each month an issue reminds you (your SIPs show in Groww the next day). If nothing for the month has been
uploaded by 20:00 IST, the workflow opens another. Uploading at any time triggers the import straight away.

Plain-text alternative: only if you make the repository private (GitHub Pro keeps Pages working); the published
`docs/` site is public either way.

## One-off: your full transaction history (exact XIRR, exact tax lots, a chart from day one)

The monthly holdings statement only has today's totals. Groww's **transaction report** lists every purchase and sale, and importing it once makes three things exact: each fund's XIRR, the tax lots, and the "Your money over time" chart (which then starts at your first purchase instead of 21 Aug 2026).

1. In Groww, download the mutual-fund transaction report for the **full history** ("since inception") as .xlsx or .csv, and put it in `MF-Holding/` next to your statement.
2. Check it first (nothing is written): `python src/import_transactions.py --dry-run`. It prints each fund's units from the report beside the units you hold; they must match within 1%, otherwise it refuses (a partial date range would draw a wrong chart).
3. Encrypt and push it, the same way as the statement: `python src/encrypt_statement.py --transactions --push`. The `transactions` workflow decrypts it with the same `STATEMENT_KEY`, keeps only dates, amounts and units in `config/transactions.csv`, and rebuilds the history, XIRR and tax lots.

Your name, PAN, phone and folio numbers are never copied. `config/transactions.csv` is visible in this public repository (every purchase date and amount); the chart it produces shows the same thing, so if that bothers you, keep the repository private instead.
