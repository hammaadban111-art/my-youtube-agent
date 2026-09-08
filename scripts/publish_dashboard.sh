#!/bin/bash
# Pushes the generated dashboard to the separate PUBLIC repo that GitHub Pages
# serves from. The agent repo itself stays private — GitHub Pages isn't
# available on private repos on the free plan, and we don't want the agent's
# code, prompts or workflow config published just to host a stats page.
#
# Authenticates with an SSH deploy key scoped to that one repo rather than a
# personal access token, so a leak here can't reach anything else in the
# account. Only public/index.html and public/data.json are copied across —
# nothing from agent/, the raw data/ records, or the workflows ever reaches
# the public repo. data.json holds exactly the figures the page already
# displayed when they were baked into the HTML; splitting them out is what
# lets an open tab refresh without a reload.
#
# --only-if-changed: skip the commit when the ONLY difference is data.json's
# own freshness stamp. Every build rewrites generated_at and next_runs, so a
# byte comparison always reports a change and always commits. That would make
# even a quiet weekly maintenance run produce a needless commit, Pages rebuild
# and deployment. Without this flag a job that changed nothing still consumes
# the public dashboard's weekly deployment budget.
set -euo pipefail

ONLY_IF_CHANGED=""
for arg in "$@"; do
  case "$arg" in
    --only-if-changed) ONLY_IF_CHANGED=yes ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

: "${DASHBOARD_REPO:?DASHBOARD_REPO variable not set}"
: "${DASHBOARD_DEPLOY_KEY:?DASHBOARD_DEPLOY_KEY secret not set}"

if [ ! -f public/index.html ]; then
  echo "No dashboard to publish (public/index.html missing)."
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

install -m 700 -d "$tmp/ssh"
printf '%s\n' "$DASHBOARD_DEPLOY_KEY" > "$tmp/ssh/key"
chmod 600 "$tmp/ssh/key"
export GIT_SSH_COMMAND="ssh -i $tmp/ssh/key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

git clone --depth 1 "git@github.com:${DASHBOARD_REPO}.git" "$tmp/repo"

# Checked BEFORE the copy overwrites the published copies, so "did anything
# real change?" is answered against what the site is actually serving.
if [ -n "$ONLY_IF_CHANGED" ]; then
  if python3 scripts/dashboard_changed.py "$tmp/repo" public; then
    echo "Dashboard has real changes; publishing."
  else
    echo "Dashboard unchanged apart from its freshness stamp; nothing to publish."
    exit 0
  fi
fi

cp public/index.html "$tmp/repo/index.html"
# The page fetches this client-side to refresh without a reload; without it
# published the dashboard renders its "could not load data.json" state.
if [ -f public/data.json ]; then
  cp public/data.json "$tmp/repo/data.json"
fi
# Stops Pages running the site through Jekyll, which would ignore some paths.
touch "$tmp/repo/.nojekyll"

cd "$tmp/repo"
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add index.html data.json .nojekyll 2>/dev/null || git add index.html .nojekyll

if git diff --cached --quiet; then
  echo "Dashboard unchanged; nothing to publish."
else
  git commit -m "Update dashboard"
  git push
  echo "Dashboard published to ${DASHBOARD_REPO}."
fi
