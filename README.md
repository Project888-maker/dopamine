# Dopamine Autonomous AI Project Factory

Dopamine runs a single tiny MVP through the full autonomous factory chain:

```text
research → brainstorm → architect → build → review → save → report
```

V1 is intentionally constrained so one trigger produces one small deployable project instead of several overbuilt or truncated apps.

## Repository and AWS paths

- GitHub repo: `Project888-maker/dopamine`
- AWS working folder: `/home/ubuntu/pipeline`
- Generated run outputs: `/home/ubuntu/pipeline/runs/` (ignored by git)

## Required `.env` variables

Create `/home/ubuntu/pipeline/.env` on AWS. Do not commit it.

```bash
OPENROUTER_KEY=your_openrouter_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_owner_chat_id
VERCEL_TOKEN=your_vercel_token_if_shipping_to_vercel
```

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are optional for local smoke tests, but both must exist for Celery `report_task` to send a Telegram summary.

## Setup commands

```bash
cd /home/ubuntu
if [ ! -d pipeline/.git ]; then
  git clone git@github.com:Project888-maker/dopamine.git pipeline
fi
cd /home/ubuntu/pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install celery redis openai python-telegram-bot requests
```

Make sure Redis is installed and running:

```bash
sudo apt-get update
sudo apt-get install -y redis-server
sudo systemctl enable --now redis-server
```

## Start the worker manually

Run this from `/home/ubuntu/pipeline`:

```bash
source .venv/bin/activate
set -a
source .env
set +a
celery -A tasks worker --loglevel=info --concurrency=1
```

## Trigger a smoke test

In a second SSH session, run:

```bash
cd /home/ubuntu/pipeline
source .venv/bin/activate
set -a
source .env
set +a
python trigger_pipeline.py "AI tools, SaaS, developer utilities"
```

The command queues one Celery chain and prints the task id. Watch worker logs until the report step completes, then inspect the newest folder under `runs/`.

## Telegram bot

Start the bot manually:

```bash
cd /home/ubuntu/pipeline
source .venv/bin/activate
set -a
source .env
set +a
python telegram_bot.py
```

From the owner chat, use:

```text
/run
/run AI tools, SaaS, developer utilities
/list
/show 1
/ship 1
```

## systemd services on AWS

Install or refresh the service templates:

```bash
cd /home/ubuntu/pipeline
sudo cp systemd/dopamine-worker.service /etc/systemd/system/dopamine-worker.service
sudo cp systemd/dopamine-bot.service /etc/systemd/system/dopamine-bot.service
sudo systemctl daemon-reload
sudo systemctl enable dopamine-worker dopamine-bot
sudo systemctl restart dopamine-worker dopamine-bot
```

Check service status and logs:

```bash
sudo systemctl status dopamine-worker --no-pager
sudo systemctl status dopamine-bot --no-pager
journalctl -u dopamine-worker -f
journalctl -u dopamine-bot -f
```

## Pull, restart, and test on AWS

Use these exact commands after this change is merged:

```bash
cd /home/ubuntu/pipeline
git pull --ff-only
source .venv/bin/activate
pip install --upgrade celery redis openai python-telegram-bot requests
sudo cp systemd/dopamine-worker.service /etc/systemd/system/dopamine-worker.service
sudo cp systemd/dopamine-bot.service /etc/systemd/system/dopamine-bot.service
sudo systemctl daemon-reload
sudo systemctl restart dopamine-worker dopamine-bot
sudo systemctl status dopamine-worker --no-pager
sudo systemctl status dopamine-bot --no-pager
set -a
source .env
set +a
python trigger_pipeline.py "AI tools, SaaS, developer utilities"
journalctl -u dopamine-worker -f
```

## Git hygiene

The repo ignores secrets, logs, generated outputs, and Python caches:

```text
.env
*.log
runs/
test_output/
__pycache__/
```
