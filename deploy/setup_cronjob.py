#!/usr/bin/env python3
"""Create (or update) the cron-job.org jobs that keep MF Pulse's data fresh.

GitHub's own scheduler is best-effort and has skipped or delayed most runs, so these jobs
call GitHub's "run workflow" API on a clock that doesn't depend on it. See TRIGGERS.md.

You need two secrets, both asked for with a hidden prompt (or read from the environment) --
never put them in a file or paste them anywhere:

  GH_TOKEN         GitHub fine-grained token: only repo prnvdas/mf-pulse, Actions: Read and write
  CRONJOB_API_KEY  cron-job.org Console -> Settings -> API -> create key

Run:   python deploy/setup_cronjob.py            (check token, create/update the jobs)
       python deploy/setup_cronjob.py --dry-run  (print what it would do, change nothing)
       python deploy/setup_cronjob.py --list     (show the jobs and their last status)

Safe to re-run: jobs are matched by title and updated in place, never duplicated.
Standard library only.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.environ.get("MFP_REPO", "prnvdas/mf-pulse")
GITHUB_API = os.environ.get("GITHUB_API_BASE", "https://api.github.com")
CRONJOB_API = os.environ.get("CRONJOB_API_BASE", "https://api.cron-job.org")
TZ = "Asia/Kolkata"
WEEKDAYS = [1, 2, 3, 4, 5]          # cron-job.org: 0 = Sunday

# title -> (workflow file, hours, minutes, weekdays, body)   (all times IST)
PLAN = {
    "Live numbers": ("estimate.yml", list(range(9, 16)), [5, 20, 35, 50], WEEKDAYS, {"ref": "main"}),
    "Final read": ("estimate.yml", [15, 16], [55], WEEKDAYS, {"ref": "main", "inputs": {"force": "true"}}),
    "Morning outlook": ("outlook.yml", [7, 9], [15], WEEKDAYS, {"ref": "main"}),
    "Nightly grading": ("reconcile.yml", [23], [30], WEEKDAYS, {"ref": "main"}),
    "GIFT Nifty evening": ("gift.yml", [18, 21, 23], [5], [0, 1, 2, 3, 4], {"ref": "main"}),
    "GIFT Nifty morning": ("gift.yml", [8], [35], WEEKDAYS, {"ref": "main"}),
    "Monthly statement check": ("statement.yml", [20], [0], [-1], {"ref": "main"}, [3]),
    "Nightly grading retry": ("reconcile.yml", [3], [30], [2, 3, 4, 5, 6], {"ref": "main"}),
}


def call(method: str, url: str, headers: dict, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "mf-pulse-setup", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def secret(name: str, prompt: str) -> str:
    v = os.environ.get(name) or getpass.getpass(prompt + " (hidden): ").strip()
    if not v:
        sys.exit(f"{name} is required.")
    return v


def gh_headers(token: str) -> dict:
    return {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}


def check_github_token(token: str) -> bool:
    """Start one real (harmless) outlook run; 204 proves the exact request cron-job.org will send."""
    status, _ = call("POST", f"{GITHUB_API}/repos/{REPO}/actions/workflows/outlook.yml/dispatches",
                     gh_headers(token), {"ref": "main"})
    why = {
        204: "OK -- GitHub accepted it (a new 'outlook' run should appear in the Actions tab).",
        401: "the token was rejected: mistyped, revoked or expired. Create a new one.",
        403: "the token lacks 'Actions: Read and write' on this repository. Edit the token's permissions.",
        404: f"GitHub can't see {REPO} or the workflow with this token: check the token's repository access "
             "(it must include this repo) and that it is a fine-grained token.",
        422: "GitHub rejected the request (is the workflow file on the 'main' branch?).",
    }.get(status, f"unexpected HTTP {status}")
    print(f"GitHub token check: HTTP {status} -- {why}")
    return status == 204


def job_payload(title: str, token: str) -> dict:
    wf, hours, minutes, wdays, body, *rest = PLAN[title]
    mdays = rest[0] if rest else [-1]
    return {"job": {
        "title": title,
        "url": f"{GITHUB_API}/repos/{REPO}/actions/workflows/{wf}/dispatches",
        "enabled": True,
        "saveResponses": True,
        "requestMethod": 1,                                   # POST
        "requestTimeout": 30,
        "schedule": {"timezone": TZ, "expiresAt": 0, "hours": hours, "minutes": minutes,
                     "mdays": mdays, "months": [-1], "wdays": wdays},
        "extendedData": {"headers": {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mf-pulse-cron",
            "Content-Type": "application/json",
        }, "body": json.dumps(body)},
        "notification": {"onFailure": True, "onFailureCount": 2, "onSuccess": False, "onDisable": True},
    }}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--list", action="store_true", help="list the jobs and their last status")
    ap.add_argument("--skip-token-check", action="store_true", help="don't start a test run on GitHub")
    args = ap.parse_args()

    if args.dry_run:
        for title, (wf, hours, minutes, wdays, body, *rest) in PLAN.items():
            print(f"- {title}: POST {wf}  hours={hours} minutes={minutes} weekdays={wdays} days-of-month={rest[0] if rest else 'every'} body={json.dumps(body)}")
        return 0

    key = secret("CRONJOB_API_KEY", "cron-job.org API key")
    ch = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    status, data = call("GET", f"{CRONJOB_API}/jobs", ch)
    if status != 200:
        print(f"cron-job.org rejected the API key (HTTP {status}). Create one at Console -> Settings -> API.")
        return 1
    existing = {j["title"]: j for j in data.get("jobs", [])}

    if args.list:
        names = {0: "not run yet", 1: "OK", 4: "HTTP error"}
        for j in data.get("jobs", []):
            print(f"{j['title']:<24} enabled={j['enabled']}  last status: {names.get(j['lastStatus'], j['lastStatus'])}  "
                  f"next: {time.strftime('%a %d %b %H:%M', time.localtime(j['nextExecution'])) if j.get('nextExecution') else '-'}")
        return 0

    token = secret("GH_TOKEN", "GitHub token (github_pat_...)")
    if not token.startswith("github_pat_"):
        print("That doesn't look like a fine-grained GitHub token (should start with github_pat_).")
        return 1
    if not args.skip_token_check and not check_github_token(token):
        print("Fix the token first, then run this again. Nothing was created.")
        return 1

    failures = 0
    for title in PLAN:
        payload = job_payload(title, token)
        if title in existing:
            status, resp = call("PATCH", f"{CRONJOB_API}/jobs/{existing[title]['jobId']}", ch, payload)
            verb = "updated"
        else:
            status, resp = call("PUT", f"{CRONJOB_API}/jobs", ch, payload)
            verb = "created"
        ok = status == 200
        failures += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {title}: {verb if ok else f'HTTP {status} {resp}'}")
        time.sleep(float(os.environ.get("MFP_SLEEP", "13")))   # cron-job.org allows 5 job creations per minute
    if failures:
        print(f"\n{failures} job(s) failed -- see above. Re-running is safe.")
        return 1
    print("\nDone. In the cron-job.org console, open any job and press 'Test run': a healthy one shows 204.")
    print("Tomorrow, check the repo's Actions tab: runs with event 'workflow_dispatch' should appear on schedule.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
