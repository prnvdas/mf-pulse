# Deploying

Three targets, all built. They share one HTTP contract — `GET /?symbols=A.NS,B.NS`
returning the same JSON — so the dashboard can't tell them apart. Point **Price
source → Worker** at whichever URL you want and it just works.

## Read this first: one writer of record

The estimator is stateless, so running three copies is harmless.

`state.json` is **not** stateless. It holds your unit counts, last NAV, and the
prediction history. Three deployments each keeping their own copy means your units
drift apart and the accuracy log forks into three partial records that all look
plausible.

Pick one writer. GitHub Actions is the sensible choice — git already versions the
history and the diffs are readable.

| Deployment | Runs estimator | Runs reconciler | Serves prices |
|---|---|---|---|
| GitHub Actions + Pages | yes | **yes — writer of record** | no |
| AWS Lambda + S3 | on demand | no (`ScheduleEnabled=false`) | yes |
| Lightsail | on demand | no (disable the timer) | yes |

---

## 1. GitHub Actions + Pages — ₹0

The baseline. Runners have no CORS restrictions, so `estimate.py` calls Yahoo
directly; no proxy needed for the scheduled path.

1. Push the repo. Make it **private** — it holds your holdings and units.
2. Settings → Actions → General → Workflow permissions → **Read and write**
3. Settings → Pages → Source: **main** / **docs**
4. Actions → `estimate` → **Run workflow**

Live at `https://<you>.github.io/<repo>/`, refreshing every 15 min, reconciling
nightly. 2,000 Actions minutes/month free on private repos; this uses ~300.

Caveat: GitHub disables scheduled workflows after 60 days with no user activity.
Push something monthly.

---

## 2. AWS Lambda + S3 — roughly ₹30–50/month

Function URL serves prices; S3 holds state. arm64 Graviton, scales to zero.

```bash
cd deploy/aws-lambda
sam build
sam deploy --guided --parameter-overrides ScheduleEnabled=false
```

Take `PriceEndpoint` from the outputs and paste it into the dashboard under
**Price source → Worker**.

Costs at your volume: ~800 invocations/month at 512MB arm64 sits inside the Lambda
free tier indefinitely; S3 storage is a few MB. Realistic bill is ₹0–40.

`ScheduleEnabled=false` is deliberate — it deploys the Function URL without the
EventBridge rules, so Lambda never touches state. Flip to `true` only if you want
Lambda to *replace* GitHub Actions rather than sit alongside it.

---

## 3. Lightsail — $3.50/month (~₹300)

An always-on box. Nginx serves the dashboard, FastAPI serves prices, systemd timers
run the estimator.

1. Lightsail → Create instance → Ubuntu 24.04 → **$3.50 plan** (512MB)
2. Networking → open HTTP (80)
3. SSH in:

```bash
sudo bash /path/to/bootstrap.sh https://github.com/<you>/mf-pulse.git
```

Dashboard at `http://<instance-ip>/`, prices at `http://<instance-ip>/api`.

If Actions is your writer of record:

```bash
sudo systemctl disable --now mfpulse-reconcile.timer
```

The $3.50 plan includes three months free. It's also the only target where the
FastAPI endpoint is a real service you can extend.

Note it's HTTP only as configured. If you serve the dashboard from GitHub Pages
(HTTPS) and point it at this box (HTTP), the browser blocks the mixed content.
Either use the Lightsail-hosted dashboard, or put a certificate on it.

---

## Which to actually use

Do **1** — free, and it runs without you.

Add **2** for the refresh button. Lambda beats the Cloudflare Worker here only if you
want the AWS practice; on merit the Worker is free and simpler.

**3** if you'd rather have a box you can SSH into and extend. ₹300/month to avoid
serverless is a fair trade if the FastAPI service is something you want to build on.

---

## Troubleshooting

Press **Diagnose** in the dashboard — it reports origin, configured URL, whether the
fetch connected, the raw response, and a verdict.

`nse_error: nse 403` is **not** a failure. NSE blocks datacenter IPs; AWS and
Cloudflare ranges are both common targets. Every target falls back to Yahoo — numbers
stay correct, just 15 minutes delayed.

Lightsail logs:

```bash
journalctl -u mfpulse-api -f
systemctl list-timers 'mfpulse*'
```
