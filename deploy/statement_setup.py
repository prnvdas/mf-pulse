#!/usr/bin/env python3
"""One script for the monthly Groww statement: set up whatever is missing, then encrypt, push and watch it import.

    python3 deploy/statement_setup.py            # first time AND every month
    python3 deploy/statement_setup.py --dry-run  # everything except GitHub (no secret, no push, no cron job)

What it does, skipping any step that is already done:
  1. Passphrase: makes a long random one the first time and keeps it in ~/.config/mf-pulse/statement.key
     (outside the repo, owner-only). Nothing is ever printed unless you have to type it into GitHub by hand.
  2. GitHub secret STATEMENT_KEY: set automatically if you give it a token with "Secrets: Read and write"
     (needs `pip install pynacl`); otherwise it copies the passphrase to your clipboard, opens the right
     GitHub page and waits while you paste it (30 seconds).
  3. Git seatbelt: a hook that refuses to commit a plain statement.
  4. cron-job.org: adds the "Monthly statement check" job (asks for your two keys; skip with N).
  5. Encrypts the newest statement in MF-Holding/, pushes ONLY the encrypted copy, then watches the GitHub
     `statement` workflow and reports whether it imported.

The plain statement (name, PAN, phone) never leaves your computer. Standard library only (the encrypt step
uses the project's .venv2 if present, because it needs openpyxl).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = os.environ.get("MFP_REPO", "prnvdas/mf-pulse")
CONF = pathlib.Path(os.environ.get("MFP_CONFIG_DIR") or pathlib.Path.home() / ".config" / "mf-pulse")
KEYFILE, MARKER = CONF / "statement.key", CONF / "setup.json"
PY = str(ROOT / ".venv2" / "bin" / "python") if (ROOT / ".venv2" / "bin" / "python").exists() else sys.executable


def say(msg: str) -> None:
    print(msg, flush=True)


def ask(prompt: str, default: str = "n") -> bool:
    a = input(f"{prompt} [{'Y/n' if default == 'y' else 'y/N'}] ").strip().lower()
    return (a or default).startswith("y")


def first_available(cmds: list[list[str]]):
    for c in cmds:
        if shutil.which(c[0]):
            return c
    return None


def to_clipboard(text: str) -> bool:
    c = first_available([["clip.exe"], ["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]])
    if not c:
        return False
    try:
        subprocess.run(c, input=text.encode(), check=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def open_url(url: str) -> None:
    c = first_available([["wslview"], ["explorer.exe"], ["xdg-open"], ["open"]])
    if c:
        subprocess.run(c + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gh(method: str, path: str, token: str | None = None, body: dict | None = None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "mf-pulse-statement", "Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


# ---------------------------------------------------------------------------- steps
def get_key() -> tuple[str, bool]:
    CONF.mkdir(parents=True, exist_ok=True)
    if KEYFILE.exists():
        return KEYFILE.read_text().strip(), False
    key = secrets.token_urlsafe(32)
    KEYFILE.write_text(key + "\n")
    os.chmod(KEYFILE, 0o600)
    return key, True


def marker() -> dict:
    try:
        return json.loads(MARKER.read_text())
    except (OSError, ValueError):
        return {}


def save_marker(**kw) -> None:
    m = marker()
    m.update(kw)
    MARKER.write_text(json.dumps(m))
    os.chmod(MARKER, 0o600)


def seal(public_key_b64: str, value: str) -> str:
    from nacl import encoding, public          # PyNaCl, only needed for the automatic route
    box = public.SealedBox(public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder()))
    return base64.b64encode(box.encrypt(value.encode())).decode()


def set_secret_auto(key: str, token: str) -> bool:
    try:
        import nacl  # noqa: F401
    except ImportError:
        say("   (PyNaCl isn't installed here, so I can't set it automatically; use the by-hand route below.)")
        return False
    st, pk = gh("GET", f"/repos/{REPO}/actions/secrets/public-key", token)
    if st != 200:
        say(f"   GitHub refused the token (HTTP {st}). It needs 'Secrets: Read and write' on {REPO}.")
        return False
    st, _ = gh("PUT", f"/repos/{REPO}/actions/secrets/STATEMENT_KEY", token,
               {"encrypted_value": seal(pk["key"], key), "key_id": pk["key_id"]})
    ok = st in (201, 204)
    say("   secret STATEMENT_KEY set on GitHub." if ok else f"   couldn't set the secret (HTTP {st}).")
    return ok


def step_secret(key: str, fresh: bool, dry: bool) -> None:
    fp = hashlib.sha256(key.encode()).hexdigest()[:16]
    if marker().get("secret_for") == fp:
        say("2. GitHub secret: already set for this passphrase.")
        return
    say("2. GitHub secret STATEMENT_KEY: not set yet for this passphrase.")
    if dry:
        say("   [dry-run] skipped")
        return
    token = getpass.getpass("   Paste a GitHub token with 'Secrets: Read and write' for this repo to set it automatically\n"
                            "   (hidden; or just press Enter to paste the passphrase on GitHub by hand): ").strip()
    if token and set_secret_auto(key, token):
        save_marker(secret_for=fp)
        return
    copied = to_clipboard(key)
    url = f"https://github.com/{REPO}/settings/secrets/actions/new"
    say(f"   By hand: GitHub will open on the 'New secret' page.\n   Name:  STATEMENT_KEY\n   Value: "
        + ("the passphrase is on your clipboard: just paste it." if copied else "(no clipboard available; it is printed below)"))
    if not copied:
        say(f"          {key}")
    open_url(url)
    say(f"   If the page didn't open: {url}")
    input("   Press Enter once you have saved the secret on GitHub... ")
    save_marker(secret_for=fp)
    if copied:
        to_clipboard("")                           # don't leave the passphrase on the clipboard


def step_hook(dry: bool) -> None:
    cur = subprocess.run(["git", "config", "core.hooksPath"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if cur == "deploy/hooks":
        say("3. Git seatbelt: already on.")
        return
    if not dry:
        subprocess.run(["git", "config", "core.hooksPath", "deploy/hooks"], cwd=ROOT, check=True)
    say("3. Git seatbelt: switched on (refuses to commit a plain statement)." if not dry else "3. Git seatbelt: [dry-run] skipped")


def step_cron(dry: bool) -> None:
    if marker().get("cron_done"):
        say("4. cron-job.org monthly job: already set up.")
        return
    if dry:
        say("4. cron-job.org: [dry-run] skipped")
        return
    if not ask("4. Add the 'Monthly statement check' job on cron-job.org now? (needs your cron-job.org API key and GitHub token)"):
        say("   skipped (run `python3 deploy/setup_cronjob.py` later; GitHub's own monthly schedule still runs.)")
        return
    rc = subprocess.run([sys.executable, str(ROOT / "deploy" / "setup_cronjob.py")], cwd=ROOT).returncode
    if rc == 0:
        save_marker(cron_done=True)


def step_push(key: str, dry: bool) -> pathlib.Path | None:
    say("5. Encrypting your newest statement from MF-Holding/ ...")
    env = {**os.environ, "STATEMENT_KEY": key}
    if dry:
        out = pathlib.Path("/tmp/mfp-dry-run")
        out.mkdir(exist_ok=True)
        r = subprocess.run([PY, "-W", "ignore", str(ROOT / "src" / "encrypt_statement.py"), "--out-dir", str(out)], cwd=ROOT / "src", env=env)
        if r.returncode:
            sys.exit("   encryption failed")
        say("   [dry-run] encrypted into /tmp/mfp-dry-run (not pushed)")
        return None
    t0 = dt.datetime.now(dt.timezone.utc)
    r = subprocess.run([PY, "-W", "ignore", str(ROOT / "src" / "encrypt_statement.py"), "--push"], cwd=ROOT / "src", env=env)
    if r.returncode:
        sys.exit("   encrypt/push failed (see above); nothing was imported.")
    return t0


def watch(t0) -> None:
    say("6. Watching the GitHub import (up to 4 minutes) ...")
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(10)
        st, j = gh("GET", f"/repos/{REPO}/actions/workflows/statement.yml/runs?per_page=5")
        if st != 200:
            continue
        for run in j.get("workflow_runs", []):
            created = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if created < t0 - dt.timedelta(seconds=90):
                continue
            if run["status"] != "completed":
                break
            if run["conclusion"] == "success":
                say("   ✓ imported. The dashboard's units, folios and tax view are updated "
                    f"(run: {run['html_url']}).")
                return
            _, jobs = gh("GET", run["jobs_url"].replace("https://api.github.com", ""))
            failed = [s["name"] for jb in jobs.get("jobs", []) for s in jb["steps"] if s["conclusion"] == "failure"]
            say(f"   ✗ the run failed at: {', '.join(failed) or 'unknown step'}  ({run['html_url']})")
            if any("Decrypt" in f for f in failed):
                say("   Most likely the STATEMENT_KEY secret is missing or doesn't match your passphrase. Re-run this script and\n"
                    "   choose to set the secret again (delete ~/.config/mf-pulse/setup.json first to be asked).")
            return
        else:
            continue
    say("   no result yet; check the Actions tab in a minute.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="do everything except GitHub (no secret, no push, no cron job)")
    args = ap.parse_args()
    if not shutil.which("openssl"):
        sys.exit("openssl is required (it ships with Linux, macOS and WSL).")
    if not list((ROOT / "MF-Holding").glob("*.xlsx")):
        sys.exit("Put your Groww holdings statement (.xlsx) in the MF-Holding/ folder first.")
    key, fresh = get_key()
    say(f"1. Passphrase: {'created and saved to ' + str(KEYFILE) if fresh else 'found in ' + str(KEYFILE)}")
    step_secret(key, fresh, args.dry_run)
    step_hook(args.dry_run)
    step_cron(args.dry_run)
    t0 = step_push(key, args.dry_run)
    if t0:
        watch(t0)


if __name__ == "__main__":
    main()
