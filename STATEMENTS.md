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
