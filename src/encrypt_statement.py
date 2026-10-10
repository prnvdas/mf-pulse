"""Encrypt your Groww holdings statement so it can be committed to a PUBLIC repository safely.

The statement contains your name, mobile number, PAN and full folio numbers, so the plain .xlsx must never be
committed (MF-Holding/ and *.xlsx are git-ignored as a seatbelt). This script turns the newest statement in
MF-Holding/ into statements/<as-on date>.xlsx.enc (AES-256, key derived with PBKDF2 from your passphrase). The
`statement` workflow decrypts it with the STATEMENT_KEY repository secret, imports only the sanitised figures,
and the plaintext never leaves the runner's temporary disk.

    python src/encrypt_statement.py [statement.xlsx] [--push]

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
import subprocess
import sys
import tempfile

from common import ROOT
from import_statement import read_statement

OPENSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000"]


def run(args: list[str], key: str) -> None:
    subprocess.run(args + ["-pass", "env:STATEMENT_KEY"], check=True, env={**os.environ, "STATEMENT_KEY": key})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--push", action="store_true", help="also git add, commit and push the encrypted file")
    args = ap.parse_args()

    src = args.file or max(glob.glob(str(ROOT / "MF-Holding" / "*.xlsx")), key=os.path.getmtime, default=None)
    if not src:
        sys.exit("No statement found: put the Groww .xlsx in the MF-Holding/ folder.")
    st = read_statement(src)                          # also proves it is a real holdings statement
    if not st["as_on"]:
        sys.exit("Couldn't read the 'HOLDINGS AS ON <date>' line; is this the Groww holdings statement?")
    key = os.environ.get("STATEMENT_KEY") or getpass.getpass("Passphrase (the STATEMENT_KEY secret) (hidden): ")
    if len(key) < 20:
        sys.exit("Use a long random passphrase (20+ characters), e.g. the output of: openssl rand -base64 32")
    out = ROOT / "statements" / f"{st['as_on']}.xlsx.enc"
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
    print(f"[ok] {out.relative_to(ROOT)}  ({len(st['rows'])} folio rows, as on {st['as_on']}); decrypts back to the identical file")
    if args.push:
        subprocess.run(["git", "add", str(out)], check=True, cwd=ROOT)
        subprocess.run(["git", "commit", "-m", f"statement {st['as_on']} (encrypted)"], check=True, cwd=ROOT)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "-q", "origin", "main"], check=True, cwd=ROOT)
        subprocess.run(["git", "push", "-q", "origin", "main"], check=True, cwd=ROOT)
        print("[ok] pushed: the `statement` workflow imports it within a minute or two")
    else:
        print("Next:  git add statements && git commit -m 'statement' && git push   (the workflow imports it automatically)")


if __name__ == "__main__":
    main()
