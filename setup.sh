#!/usr/bin/env bash
# Research Lab — one-shot setup for a fresh Ubuntu/Debian cloud VM.
# Run from inside the research-lab folder:  bash setup.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "== Research Lab setup in $DIR =="

echo "[1/5] Python + cron ..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip cron curl
sudo systemctl enable --now cron 2>/dev/null || true
pip3 install --break-system-packages -r "$DIR/requirements.txt" 2>/dev/null \
  || pip3 install -r "$DIR/requirements.txt"

echo "[2/5] Node.js 22 (for Claude Code) ..."
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

echo "[3/5] Claude Code ..."
npm install -g @anthropic-ai/claude-code 2>/dev/null \
  || sudo npm install -g @anthropic-ai/claude-code

echo "[4/5] Environment ..."
if [ ! -f "$DIR/.env" ]; then cp "$DIR/.env.example" "$DIR/.env"
  echo "  -> created .env — EDIT IT and add your ANTHROPIC_API_KEY."; fi

echo "[5/5] Scheduling the daily job (weekdays 5:30pm server time) ..."
CRON="30 17 * * 1-5 cd $DIR && set -a && . $DIR/.env && python3 daily_job.py >> $DIR/daily.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'daily_job.py' ; echo "$CRON" ) | crontab -

echo
echo "== Done. =="
crontab -l | grep daily_job.py && echo "  ^ your daily schedule"
echo
echo "Next:"
echo "  1) Edit .env and add ANTHROPIC_API_KEY (and confirm the model)."
echo "  2) Authenticate Claude Code:   claude   (login once)"
echo "  3) Smoke test:                 set -a && . ./.env && python3 run_once.py"
echo "  4) Run a full day now:         set -a && . ./.env && python3 daily_job.py"
echo "  5) Ask for changes anytime:    cd $DIR && claude"
