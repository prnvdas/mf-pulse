# Reliable updates: starting the estimate on demand

GitHub's built-in scheduler is best-effort. Runs can arrive hours late or be dropped
(this happened on 5 Oct 2026). Two optional add-ons make updates dependable. Both
call the same endpoint: GitHub's "run workflow" API for `estimate.yml`.

| Option | What it does | Needs |
|---|---|---|
| **A. Token in the dashboard** | The Refresh button asks GitHub for a fresh update. The page also asks automatically if the data is >20 min old during market hours, or if no final close read exists after 15:45 IST. | A token, pasted once into the page's "Reliable updates" box |
| **B. External cron (cron-job.org)** | A free service pokes GitHub on a clock that does not depend on GitHub's scheduler. Works even when you aren't looking at the page. | A second token, plus about 5 minutes of setup |

You can do either or both. Use a **separate token for each** so you can revoke one without breaking the other.

## 1. Create a token (fine-grained, repo-only)

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → *Generate new token*.
2. Name: `mf-pulse-browser` (or `mf-pulse-cron`). Expiration: at most 1 year (put a reminder in your calendar).
3. Repository access: **Only select repositories** → `prnvdas/mf-pulse`.
4. Repository permissions: **Actions: Read and write**. Leave everything else at "No access". (Metadata: read-only is added automatically.)
5. Generate, then copy the `github_pat_…` value. GitHub shows it only once.

The token can only start and read workflows in this one repository. It cannot read your code, change files, or touch other repos. Never paste it in chat, in an issue or in a commit.

## 2A. Browser option

Open the dashboard → scroll to the footer → **Reliable updates (optional)** → paste the token → Save.
The token stays in this browser's local storage only; the page never displays it again. Remove it from the same box any time.
Repeat on each device/browser you want this on.

Behaviour: Refresh during market hours requests a fresh run and waits for the new data (about 2–3 minutes). There is a 3-minute cooldown between requests, and the automatic catch-up runs at most once per 10 minutes.

## 2B. cron-job.org option (recommended)

1. Sign up at https://cron-job.org (free). In Settings, set the timezone to **Asia/Kolkata**.
2. Create **four** cron jobs. All use the same method, headers and token; only the URL, schedule and body differ.

   Common settings (Advanced tab): request method **POST**; headers
   - `Authorization: Bearer github_pat_...` (your cron token)
   - `Accept: application/vnd.github+json`
   - `X-GitHub-Api-Version: 2022-11-28`
   - `User-Agent: mf-pulse-cron`
   - `Content-Type: application/json`

   Success is HTTP **204**. Turn on "notify on failure".

   | # | Purpose | URL ends with | Schedule (IST) | Request body |
   |---|---|---|---|---|
   | 1 | Live numbers | `.../workflows/estimate.yml/dispatches` | Mon-Fri, every 15 min, 09:20-15:35 | `{"ref":"main"}` |
   | 2 | End-of-day final read | `.../workflows/estimate.yml/dispatches` | Mon-Fri 15:55 (and again 16:25) | `{"ref":"main","inputs":{"force":"true"}}` |
   | 3 | Morning outlook (US lean) | `.../workflows/outlook.yml/dispatches` | Mon-Fri 07:45 (and again 09:05) | `{"ref":"main"}` |
   | 4 | Nightly NAV grading | `.../workflows/reconcile.yml/dispatches` | Mon-Fri 23:30 | `{"ref":"main"}` |

   Full URL pattern: `https://api.github.com/repos/prnvdas/mf-pulse/actions/workflows/<file>/dispatches`

Overlapping runs are harmless: each workflow queues behind itself, and a later intraday run never downgrades a same-day final.

## Test from a terminal

```bash
curl -i -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/prnvdas/mf-pulse/actions/workflows/estimate.yml/dispatches \
  -d '{"ref":"main"}'
```

`HTTP/2 204` means it started; check the Actions tab. 401 = bad/expired token, 403 = missing "Actions: Read and write", 404 = wrong repo or token can't see it.

## Limits

- This does not make GitHub faster: a requested run still queues and takes about 1–2 minutes, plus about 1 minute for Pages to publish.
- The estimate is still computed from Yahoo prices (about 15 min delayed intraday); the final read comes from closing prices.
- Tokens expire. If Refresh says "the token was rejected", create a new one and replace it.
