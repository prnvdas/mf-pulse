"""Print the GitHub Actions cron lines for the special sessions in portfolio.yaml.

Actions schedules are fixed in the workflow file, so each special session needs a
line in .github/workflows/special-session.yml. Run this after editing
`special_sessions:` and paste the output there. Times are converted IST -> UTC,
every 10 minutes (off the :00/:15/:30/:45 marks) from the session start to ~25 minutes after its end (so the
run after end+15min records the final read).
"""

from __future__ import annotations

import datetime as dt
import sys

from common import load_portfolio, now_ist, special_session

IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def main() -> None:
    if "--is-today" in sys.argv:   # exit 0 only if today (IST) is a configured special session
        sys.exit(0 if special_session(load_portfolio(), now_ist()) else 1)
    for sp in load_portfolio().get("special_sessions") or []:
        day = dt.date.fromisoformat(str(sp["date"]))
        sh, sm = map(int, sp["start"].split(":"))
        eh, em = map(int, sp["end"].split(":"))
        start = dt.datetime.combine(day, dt.time(sh, sm)) - IST_OFFSET
        end = dt.datetime.combine(day, dt.time(eh, em)) + dt.timedelta(minutes=25) - IST_OFFSET
        if start.date() != end.date():
            print(f"# {sp['name']}: window crosses midnight UTC -- add two lines by hand")
            continue
        print(f'    - cron: "3,13,23,33,43,53 {start.hour}-{end.hour} {start.day} {start.month} *"   # {sp["name"]} {sp["date"]}')


if __name__ == "__main__":
    main()
