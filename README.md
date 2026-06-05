# Dopamine Niche-Based Product Opportunity Engine

Dopamine researches any niche, finds useful product opportunities, scores them, designs a beautiful UI, builds the best project, reviews it, deploys it, and reports the live URL.

```text
research → score → architect → ui_designer → build → review → save → deploy → report
```

## Generation Modes

### simple_static (default)
- `index.html` only (plus optional `README.md`)
- Vanilla HTML/CSS/JS — no React, no Next.js, no package.json
- Safest deployment mode, works by opening in browser
- Auto-deploys to Vercel

### premium_nextjs (experimental)
- Valid Next.js App Router project
- Allowed files: `package.json`, `app/page.tsx|jsx`, `app/layout.tsx|jsx`, `app/globals.css`, `README.md`
- Beautiful UI, Vercel deployable
- No Edge Runtime, no Stripe, no auth, no database
- Tailwind allowed only if full correct config is generated

## Niche + Goal Workflow

```bash
python trigger_pipeline.py "dental clinics" "patient lead generation"
python trigger_pipeline.py "Amazon sellers" "profit calculator"
python trigger_pipeline.py "real estate" "lead generation"
python trigger_pipeline.py "restaurants" "booking conversion"
python trigger_pipeline.py "fitness coaches" "client onboarding"
```

### With generation mode flag

```bash
python trigger_pipeline.py "dental clinics" "patient lead generation" --mode premium_nextjs
```

### Backward Compatible

```bash
python trigger_pipeline.py "simple static HTML tools for founders"
```

## How It Works

1. **Research** — Finds 25 niche-specific product opportunities (calculators, checklists, quote generators, comparison tools, audit tools)
2. **Score** — Scores each on demand, usefulness, simplicity, monetization, SEO, and UI potential. Only builds if total_score >= 75.
3. **Architect** — Produces a technical spec matched to the generation mode
4. **UI Designer** — Creates a premium interface specification (headline, layout, inputs, outputs, CTA, trust elements)
5. **Build** — Generates complete code following both the architect spec and UI designer spec
6. **Review** — Strict quality gate: checks deployability, usefulness, UI quality, niche relevance, monetization angle, mobile usability. Pass only if quality_score >= 75.
7. **Save** — Writes all files to `runs/<timestamp>/`
8. **Deploy** — Auto-deploys approved `simple_static` and `premium_nextjs` projects to Vercel
9. **Report** — Sends a rich Telegram report with niche, goal, selected idea, scores, quality metrics, and live URL

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
KIMI_API_KEY=your_kimi_key_optional
KIMI_BASE_URL=https://api.moonshot.ai/v1
KIMI_MODEL=kimi-k2.6
KIMI_BUILD_ENABLED=true
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
python trigger_pipeline.py "dental clinics" "patient lead generation"
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
/run dental clinics patient lead generation
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
python trigger_pipeline.py "dental clinics" "patient lead generation"
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

## Approved project reports

Every completed run writes `_report.txt` in the run directory and sends the same summary to Telegram when reporting credentials are configured. For each approved project the report includes:

- niche and goal
- generation mode
- selected idea title and total score
- quality score and score breakdown
- project type
- folder path
- generated files
- required environment variables
- install command
- run command
- test command
- deploy eligibility
- live URL when a deploy step attaches one
- reason when the project was not auto-deployed

If no idea scores >= 75, the pipeline stops before build and reports the `no_build_reason`.

## Vercel auto-deploy

Approved projects are deployed automatically to Vercel when all conditions are true:

- the project is approved by review (quality_score >= 75)
- `project_type` is `static_web`, `static_site`, or `premium_nextjs`
- the generated folder contains the required entry files:
  - `simple_static`: `index.html` or `public/index.html`
  - `premium_nextjs`: `package.json` + `app/page.tsx|jsx|js`
- `VERCEL_TOKEN` exists in `/home/ubuntu/pipeline/.env`
- `npx` is installed

Telegram bot projects and Python API projects are not auto-deployed yet.

## Model roles

| Role        | Model                        | Notes                          |
|-------------|------------------------------|--------------------------------|
| research    | perplexity/sonar             | Live web access                |
| score       | moonshotai/kimi-k2           | Reasoning + scoring            |
| architect   | anthropic/claude-sonnet-4-5  | Technical spec                 |
| ui_designer | anthropic/claude-sonnet-4-5  | Interface design               |
| build       | anthropic/claude-sonnet-4-5  | Code generation                |
| review      | anthropic/claude-sonnet-4-5  | Quality gatekeeper (strong)    |
| report      | google/gemini-2.5-flash      | Concise summary                |

TODO: upgrade architect/build/review to `claude-sonnet-4-6` when the exact OpenRouter provider ID is confirmed.

## Safety guardrails

- No Stripe, Supabase, LangChain, Playwright, Edge Runtime
- No auth, no database in V1
- No broken hybrid structures
- Telegram bots only when explicitly requested
- Generated Telegram bots must use `PROJECT_TELEGRAM_BOT_TOKEN`; `TELEGRAM_BOT_TOKEN` is reserved for the Dopamine reporting bot
