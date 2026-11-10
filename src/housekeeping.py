"""Keeps the GitHub side tidy: deletes old workflow runs and old Pages deployments, and closes alert issues whose reason has passed.

Uses only the job's own token (GH_TOKEN, scope set in .github/workflows/cleanup.yml). It never touches code, data or the live site:
only run history, deployment records and issues. GitHub allows that token ~1,000 requests an hour, so the first big clean-up paces
itself (it sleeps until the limit resets) and later daily runs are tiny.

  python src/housekeeping.py            do it
  python src/housekeeping.py --dry      only say what would be deleted or closed (as ::notice lines, visible in the run)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

KEEP_DAYS = 3                  # finished workflow runs older than this are deleted
KEEP_DEPLOYMENTS = 30          # the newest Pages deployments are kept (the live one is always among them)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# ----------------------------------------------------------------------------- decisions (pure, unit-tested)
def alert_reason(title: str, today: dt.date, tests_green: bool, statement_months: set[str]) -> str | None:
    """Why this open alert issue no longer needs to stay open, or None if it does."""
    if title.startswith(("Tests are failing after", "Undid a change that broke the tests")):
        return "The tests pass again." if tests_green else None
    m = re.match(r"Market closed on \w+ (\d{1,2} \w{3} \d{4})", title)
    if m:
        try:
            return "That day has passed." if dt.datetime.strptime(m.group(1), "%d %b %Y").date() < today else None
        except ValueError:
            return None
    m = re.match(r"Upload your Groww statement . (\w+) (\d{4})", title)
    if m:
        try:
            month = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %Y").strftime("%Y-%m")
        except ValueError:
            return None
        return "That month's statement is imported." if month in statement_months else None
    return None


def statement_months(names: list[str]) -> set[str]:
    """'2026-11-04.xlsx.enc' -> '2026-11' (the order-history files, tx-*, are not holdings statements)."""
    return {n[:7] for n in names if re.match(r"\d{4}-\d{2}-\d{2}\.xlsx\.enc$", n)}


def older_deployments(deployments: list[dict], keep: int) -> list[dict]:
    """Everything but the newest `keep` (GitHub lists newest first, but do not rely on it)."""
    return sorted(deployments, key=lambda d: d.get("created_at", ""), reverse=True)[keep:]


# ----------------------------------------------------------------------------- GitHub API
class Api:
    def __init__(self, repo: str, token: str):
        self.repo, self.token, self.calls = repo, token, 0

    def call(self, method: str, path: str, body: dict | None = None):
        url = path if path.startswith("http") else f"https://api.github.com/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mf-pulse-housekeeping"}
        for _ in range(4):
            self.calls += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=60) as r:
                    self._pace(r.headers)
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                    self._sleep_until(e.headers)
                    continue
                return e.code, None
            except (urllib.error.URLError, TimeoutError):
                time.sleep(5)
        return 0, None

    def _pace(self, h) -> None:
        if int(h.get("x-ratelimit-remaining") or 999) < 20:
            self._sleep_until(h)

    @staticmethod
    def _sleep_until(h) -> None:
        wait = max(5, int(h.get("x-ratelimit-reset") or 0) - int(time.time()) + 5)
        print(f"[info] rate limit reached; waiting {wait // 60} min {wait % 60} s", flush=True)
        time.sleep(wait)


def notice(text: str) -> None:
    print(f"::notice title=housekeeping::{text}", flush=True)


# ----------------------------------------------------------------------------- the three jobs
def close_alerts(api: Api, dry: bool) -> None:
    today = dt.datetime.now(IST).date()
    _, runs = api.call("GET", "/actions/workflows/tests.yml/runs?branch=main&status=completed&per_page=1")
    last = ((runs or {}).get("workflow_runs") or [{}])[0]
    green = last.get("conclusion") == "success"
    months = statement_months([p.name for p in Path("statements").glob("*.xlsx.enc")])
    _, issues = api.call("GET", "/issues?state=open&per_page=100")
    n = 0
    for i in issues or []:
        if "pull_request" in i:
            continue
        why = alert_reason(i["title"], today, green, months)
        if not why:
            continue
        n += 1
        print(f"[alert] #{i['number']} {i['title'][:70]}: {why}", flush=True)
        if not dry:
            api.call("POST", f"/issues/{i['number']}/comments", {"body": why + " Closed automatically."})
            api.call("PATCH", f"/issues/{i['number']}", {"state": "closed", "state_reason": "completed"})
    notice(f"{n} alert issue(s) {'would be' if dry else ''} closed")


def delete_runs(api: Api, dry: bool) -> None:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    total = 0
    for _ in range(12):                                      # GitHub returns at most 1,000 results per filtered search: repeat until none are left
        ids = []
        for page in range(1, 11):
            _, d = api.call("GET", f"/actions/runs?per_page=100&status=completed&created=%3C{cutoff}&page={page}")
            got = [r["id"] for r in (d or {}).get("workflow_runs", [])]
            ids += got
            if len(got) < 100:
                break
        if not ids:
            break
        if dry:
            _, d = api.call("GET", f"/actions/runs?per_page=1&status=completed&created=%3C{cutoff}")
            notice(f"{(d or {}).get('total_count', len(ids))} finished run(s) older than {cutoff} would be deleted (first batch: {len(ids)})")
            return
        for k, rid in enumerate(ids):
            code, _ = api.call("DELETE", f"/actions/runs/{rid}")
            total += code == 204
            if k % 200 == 199:
                print(f"[info] deleted {total} run(s) so far", flush=True)
    notice(f"deleted {total} finished run(s) older than {cutoff}")


def delete_deployments(api: Api, dry: bool) -> None:
    deps = []
    for page in range(1, 60):
        _, d = api.call("GET", f"/deployments?per_page=100&page={page}")
        deps += d or []
        if not d or len(d) < 100:
            break
    old = older_deployments(deps, KEEP_DEPLOYMENTS)
    if dry:
        notice(f"{len(old)} of {len(deps)} deployment record(s) would be deleted (the newest {KEEP_DEPLOYMENTS} stay)")
        return
    gone = 0
    for k, dep in enumerate(old):
        code, _ = api.call("DELETE", f"/deployments/{dep['id']}")
        if code == 422:                                      # still marked active: mark it inactive, then retry once
            api.call("POST", f"/deployments/{dep['id']}/statuses", {"state": "inactive"})
            code, _ = api.call("DELETE", f"/deployments/{dep['id']}")
        gone += code == 204
        if k % 200 == 199:
            print(f"[info] deleted {gone} deployment record(s) so far", flush=True)
    notice(f"deleted {gone} of {len(old)} old deployment record(s)")


def main() -> None:
    dry = "--dry" in sys.argv
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", "")
    if not repo or not token:
        sys.exit("[error] GITHUB_REPOSITORY and GH_TOKEN are needed (they are set inside the workflow)")
    api = Api(repo, token)
    print(f"[info] {'DRY RUN: nothing is changed' if dry else 'cleaning up'}", flush=True)
    close_alerts(api, dry)
    delete_runs(api, dry)
    delete_deployments(api, dry)
    print(f"[done] {api.calls} API call(s)", flush=True)


if __name__ == "__main__":
    main()
