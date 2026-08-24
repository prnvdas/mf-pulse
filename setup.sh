#!/usr/bin/env bash
# One-command deploy for MF Pulse.
#
#   bash setup.sh                 # uses repo name "mf-pulse"
#   bash setup.sh my-repo-name
#
# Does everything the GitHub web UI does: creates a private repo, pushes,
# grants the workflow write access, enables Pages, and triggers the first run.
#
# Needs: git, gh (https://cli.github.com), python3
set -euo pipefail

REPO="${1:-mf-pulse}"
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    ! %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m    ✓ %s\033[0m\n' "$*"; }

# --- preflight -------------------------------------------------------------
say "Checking prerequisites"
for cmd in git gh python3; do
  command -v "$cmd" >/dev/null || { echo "Missing: $cmd"; exit 1; }
  ok "$cmd"
done

gh auth status >/dev/null 2>&1 || {
  warn "Not logged in to GitHub."
  echo "    Run: gh auth login"
  exit 1
}
OWNER=$(gh api user --jq .login)
ok "authenticated as $OWNER"

# --- sanity-check the config before publishing anything --------------------
say "Checking your config"
python3 - <<'PY'
import sys, pathlib, re
cfg = pathlib.Path("config/portfolio.yaml").read_text()
problems = []

if "120505" in cfg or "118825" in cfg:
    problems.append("AMFI scheme codes are still my placeholders — verify them against "
                    "https://www.amfiindia.com/spages/NAVAll.txt")
if "amfi_code: null" in cfg:
    problems.append("At least one fund has no amfi_code set")

empty = []
for p in sorted(pathlib.Path("config/holdings").glob("*.yaml")):
    t = p.read_text()
    if re.search(r"holdings:\s*\[\]", t):
        empty.append(p.stem)
if empty:
    problems.append(f"No holdings imported for: {', '.join(empty)} — these funds will be "
                    "estimated purely from a benchmark proxy")

if problems:
    print("\033[33m")
    for x in problems:
        print(f"    ! {x}")
    print("\033[0m    The deploy will still work; the numbers just won't mean much.")
else:
    print("\033[32m    ✓ config looks complete\033[0m")
PY

read -r -p "
Continue? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "Stopped."; exit 0; }

# --- repo ------------------------------------------------------------------
say "Creating private repo $OWNER/$REPO"
if gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  ok "already exists, reusing"
  git remote get-url origin >/dev/null 2>&1 || \
    git remote add origin "https://github.com/$OWNER/$REPO.git"
else
  [ -d .git ] || git init -q
  cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.venv/
*.xlsx
prices.tsv
EOF
  git add -A
  git diff --staged --quiet || git commit -qm "MF Pulse: initial commit"
  gh repo create "$OWNER/$REPO" --private --source=. --remote=origin
  ok "created"
fi

say "Pushing"
git branch -M main
git add -A
git diff --staged --quiet || git commit -qm "MF Pulse: sync"
git push -q -u origin main
ok "pushed"

# --- Actions permissions ---------------------------------------------------
say "Granting workflows write access"
gh api -X PUT "repos/$OWNER/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false >/dev/null
ok "workflows can commit their results"

# --- Pages -----------------------------------------------------------------
say "Enabling GitHub Pages"
PAGES_OK=0
if gh api -X POST "repos/$OWNER/$REPO/pages" \
     -f "source[branch]=main" -f "source[path]=/docs" >/dev/null 2>&1; then
  PAGES_OK=1; ok "enabled"
elif gh api "repos/$OWNER/$REPO/pages" >/dev/null 2>&1; then
  PAGES_OK=1; ok "already enabled"
else
  warn "Pages refused — this repo is private and Pages on private repos needs GitHub Pro."
fi

# --- first run -------------------------------------------------------------
say "Triggering the first estimate"
gh workflow run estimate.yml --repo "$OWNER/$REPO" >/dev/null 2>&1 \
  && ok "queued — watch it with: gh run watch --repo $OWNER/$REPO" \
  || warn "couldn't queue automatically; Actions tab -> estimate -> Run workflow"

# --- what now --------------------------------------------------------------
cat <<EOF

────────────────────────────────────────────────────────────
Repo:  https://github.com/$OWNER/$REPO  (private)
EOF

if [ "$PAGES_OK" = 1 ]; then
cat <<EOF
Site:  https://$OWNER.github.io/$REPO/   (first build takes ~2 min)

⚠  That URL is PUBLIC. Pages access control is Enterprise-only, so your
   holdings and balance would be readable by anyone with the link.
   For a portfolio this size, put it behind Cloudflare Access instead:
EOF
else
cat <<EOF
Site:  not enabled. Two options:
EOF
fi

cat <<EOF

   Cloudflare Pages (free, private, ~4 min):
     1. dash.cloudflare.com -> Workers & Pages -> Create -> Pages
     2. Connect to Git -> authorize -> pick $REPO
     3. Build command: (leave empty)   Output directory: docs
     4. Zero Trust -> Access -> Applications -> Add self-hosted
        -> your .pages.dev domain -> policy: Emails -> your address

   Or skip hosting entirely and run it locally:
     python3 -m http.server -d docs 8000
────────────────────────────────────────────────────────────
EOF
