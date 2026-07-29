#!/bin/bash
# Pushes the generated dashboard to the separate PUBLIC repo that GitHub Pages
# serves from. The agent repo itself stays private — GitHub Pages isn't
# available on private repos on the free plan, and we don't want the agent's
# code, prompts or workflow config published just to host a stats page.
#
# Authenticates with an SSH deploy key scoped to that one repo rather than a
# personal access token, so a leak here can't reach anything else in the
# account. Only public/index.html is copied across — nothing from data/,
# agent/ or the workflows ever reaches the public repo.
set -euo pipefail

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
cp public/index.html "$tmp/repo/index.html"
# Stops Pages running the site through Jekyll, which would ignore some paths.
touch "$tmp/repo/.nojekyll"

cd "$tmp/repo"
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add index.html .nojekyll

if git diff --cached --quiet; then
  echo "Dashboard unchanged; nothing to publish."
else
  git commit -m "Update dashboard"
  git push
  echo "Dashboard published to ${DASHBOARD_REPO}."
fi
