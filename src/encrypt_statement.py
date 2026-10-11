"""Encrypt your Groww holdings statement so it can be committed to a PUBLIC repository safely.

The statement contains your name, mobile number, PAN and full folio numbers, so the plain .xlsx must never be
committed (MF-Holding/ and *.xlsx are git-ignored as a seatbelt). This script turns the newest statement in
MF-Holding/ into statements/<as-on date>.xlsx.enc (AES-256, key derived with PBKDF2 from your passphrase). The
`statement` workflow decrypts it with the STATEMENT_KEY repository secret, imports only the sanitised figures,
and the plaintext never leaves the runner's temporary disk.

    python src/encrypt_statement.py [statement.xlsx] [--push]
    python src/encrypt_statement.py --transactions [report.xlsx|.csv] [--push]   # the Groww transaction report (full history)

The passphrase comes from the STATEMENT_KEY environment variable or a hidden prompt. Use a long random one:
    openssl rand -base64 32
and store the SAME value in GitHub -> Settings -> Secrets and variables -> Actions -> STATEMENT_KEY
(and in your password manager: lose it and you simply encrypt the next statement with a new key).
"""
from __future__ import annotations

import argparse
import getpass
import glob
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile

from common import ROOT
from import_statement import read_statement
from import_transactions import is_transaction_report, load_rows, parse

OPENSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000"]


def run(args: list[str], key: str) -> None:
    subprocess.run(args + ["-pass", "env:STATEMENT_KEY"], check=True, env={**os.environ, "STATEMENT_KEY": key})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--push", action="store_true", help="also git add, commit and push the encrypted file")
    ap.add_argument("--out-dir", help="where to write the encrypted file (default: statements/)")
    ap.add_argument("--transactions", action="store_true", help="encrypt the transaction report instead of the holdings statement")
    args = ap.parse_args()

    files = sorted(glob.glob(str(ROOT / "MF-Holding" / "*.*")), key=os.path.getmtime, reverse=True)
    files = [f for f in files if f.lower().endswith((".xlsx", ".csv"))]
    if args.transactions:
        src = args.file or next((f for f in files if is_transaction_report(f)), None)
        if not src:
            sys.exit("No transaction report found: put Groww's mutual-fund transaction report in the MF-Holding/ folder.")
        from common import load_portfolio
        tx, _ = parse(src, load_portfolio())              # also proves it is a real transaction report
        if not tx:
            sys.exit("That file has no usable transactions.")
        label, rows_n = "tx-" + tx[-1]["date"].isoformat() + os.path.splitext(src)[1].lower(), len(tx)
    else:
        def holdings(f):
            try:
                return f.lower().endswith(".xlsx") and read_statement(f)["as_on"] is not None
            except Exception:  # noqa: BLE001
                return False
        src = args.file or next((f for f in files if holdings(f)), None)
        if not src:
            sys.exit("No statement found: put the Groww holdings statement (.xlsx) in the MF-Holding/ folder.")
        st = read_statement(src)                          # also proves it is a real holdings statement
        if not st["as_on"]:
            sys.exit("Couldn't read the 'HOLDINGS AS ON <date>' line; is this the Groww holdings statement?")
        label, rows_n = f"{st['as_on']}.xlsx", len(st["rows"])
    keyfile = pathlib.Path(os.environ.get("MFP_CONFIG_DIR") or pathlib.Path.home() / ".config" / "mf-pulse") / "statement.key"
    key = os.environ.get("STATEMENT_KEY") or (keyfile.read_text().strip() if keyfile.exists() else "") or getpass.getpass("Passphrase (the STATEMENT_KEY secret) (hidden): ")
    if len(key) < 20:
        sys.exit("Use a long random passphrase (20+ characters), e.g. the output of: openssl rand -base64 32")
    out = pathlib.Path(args.out_dir or ROOT / "statements") / f"{label}.enc"
    out.parent.mkdir(exist_ok=True)
    run(OPENSSL + ["-salt", "-in", src, "-out", str(out)], key)
    # prove it round-trips with this passphrase before anything is committed
    with tempfile.TemporaryDirectory() as td:
        back = os.path.join(td, "x.xlsx")
        run(OPENSSL + ["-d", "-in", str(out), "-out", back], key)
        same = hashlib.sha256(open(src, "rb").read()).digest() == hashlib.sha256(open(back, "rb").read()).digest()
    if not same:
        out.unlink()
        sys.exit("Round-trip check failed; nothing was written.")
    print(f"[ok] {out}  ({rows_n} {'transactions' if args.transactions else 'folio rows'}); decrypts back to the identical file")
    if args.push and not args.out_dir:
        subprocess.run(["git", "add", str(out)], check=True, cwd=ROOT)
        subprocess.run(["git", "commit", "-m", f"{'transactions' if args.transactions else 'statement'} {label[:-5] if label.endswith('.xlsx') else label} (encrypted)"], check=True, cwd=ROOT)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "-q", "origin", "main"], check=True, cwd=ROOT)
        subprocess.run(["git", "push", "-q", "origin", "main"], check=True, cwd=ROOT)
        print("[ok] pushed: the " + ("`transactions`" if args.transactions else "`statement`") + " workflow imports it within a minute or two")
    elif not args.out_dir:
        print("Next:  git add statements && git commit -m 'statement' && git push   (the workflow imports it automatically)")


if __name__ == "__main__":
    main()
