# Daycue Telegram Bot

Daycue is a Telegram bot for cycle-aware relationship support. It helps a partner understand today's context, get a practical cue, and track how helpful the advice is over time.

## What Is In Production

- `Today` daily support cue
- `Forecast` with phase shifts and fertility window
- `Stats` with current dimensions and estimated hormone picture
- `Insights` from user feedback
- `Settings` with partner profile and recent period history
- `Helpful / Not helpful` feedback loop
- simpler onboarding:
  - short date formats like `5.4`, `05/04`, `today`, `yesterday`
  - time presets like `Morning`, `After work`, `Evening`
  - privacy-friendly `partner label` instead of requiring a real name
  - language selection `en / sv / ru`

## Project Files

- [bot.py](/Users/zkblook/Documents/Playground/daycue/bot.py): main Telegram bot application
- [fly.toml](/Users/zkblook/Documents/Playground/daycue/fly.toml): Fly app config
- [Procfile](/Users/zkblook/Documents/Playground/daycue/Procfile): process entrypoint
- [requirements.txt](/Users/zkblook/Documents/Playground/daycue/requirements.txt): Python deps
- [.github/workflows/fly-deploy.yml](/Users/zkblook/Documents/Playground/daycue/.github/workflows/fly-deploy.yml): GitHub Actions auto-deploy workflow
- [DEPLOYMENT.md](/Users/zkblook/Documents/Playground/daycue/DEPLOYMENT.md): runbook for deploys, secrets, checks, and troubleshooting

## Runtime Requirements

Required secrets:

- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`

Recommended:

- `TZ_DEFAULT`
- `COPY_CACHE_SECONDS`

The bot stores user data in Postgres/Supabase keyed by Telegram `chat_id`.

## Local Sanity Check

Before shipping changes:

```bash
cd /Users/zkblook/Documents/Playground/daycue
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m py_compile bot.py
```

## Deploy

There are two supported paths:

1. Automatic deploy via GitHub Actions on every push to `main`
2. Manual deploy via `flyctl`

The exact commands and verification steps are documented in [DEPLOYMENT.md](/Users/zkblook/Documents/Playground/daycue/DEPLOYMENT.md).

## Operator Notes

- The Fly app name is `daycue-telegram-bot`
- The production URL is [daycue-telegram-bot.fly.dev](https://daycue-telegram-bot.fly.dev/)
- The bot is a polling worker, not a normal HTTP web app
- Fly may warn that the app is not listening on `0.0.0.0:8080`; that warning is expected for this Telegram polling setup

## For The Next Agent

If you return to this project later, start here:

1. Read [DEPLOYMENT.md](/Users/zkblook/Documents/Playground/daycue/DEPLOYMENT.md)
2. Run `git status`
3. Compile-check `bot.py`
4. If shipping, prefer pushing to `main` and letting GitHub Actions deploy
5. If Actions fails, use the manual Fly deploy path in the runbook

## Security

Do not paste secrets into chat logs or repo files.

If any of these were exposed, rotate them:

- GitHub Personal Access Token
- `TELEGRAM_BOT_TOKEN`
- database password inside `DATABASE_URL`
- `FLY_API_TOKEN`
