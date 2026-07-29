#!/bin/bash
# One-command on/off switch for the daily automation.
# Requires: GitHub CLI (gh) installed and logged in — Claude Code will have
# already run `gh auth login` while setting this repo up.
#
# Usage:
#   ./toggle.sh off     # pause the daily uploads
#   ./toggle.sh on       # resume the daily uploads
#   ./toggle.sh status   # check current state

WORKFLOW="daily.yml"

case "$1" in
  off)
    gh workflow disable "$WORKFLOW"
    echo "Paused. No more automatic runs until you turn it back on."
    ;;
  on)
    gh workflow enable "$WORKFLOW"
    echo "Resumed. Daily uploads are active again."
    ;;
  status)
    gh workflow view "$WORKFLOW"
    ;;
  *)
    echo "Usage: ./toggle.sh [off|on|status]"
    exit 1
    ;;
esac
