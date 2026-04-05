# Daycue Telegram Bot

Daycue is a Telegram bot for cycle-aware relationship support. It helps a partner understand today's context, get a practical cue, and track how helpful the advice is over time.

## What Is In Production

- `Today` daily support cue
- `Forecast` with phase shifts and fertility window
- `Stats` with current dimensions and estimated hormone picture
- `Insights` from user feedback
- `Settings` with partner profile and recent period history
- `/profile` for privacy-safe preference memory
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
- [PRODUCT.md](/Users/zkblook/Documents/Playground/daycue/PRODUCT.md): product thesis, MVP framing, and output rules
- [PERSONALIZATION_SYSTEM.md](/Users/zkblook/Documents/Playground/daycue/PERSONALIZATION_SYSTEM.md): layered personalization model and feedback priority
- [CONTENT_SYSTEM.md](/Users/zkblook/Documents/Playground/daycue/CONTENT_SYSTEM.md): voice system, content bank strategy, and writing rules
- [PRIVACY.md](/Users/zkblook/Documents/Playground/daycue/PRIVACY.md): privacy-first product rules and data minimization
- [ROADMAP.md](/Users/zkblook/Documents/Playground/daycue/ROADMAP.md): current gaps and next milestones
- [CHANGELOG.md](/Users/zkblook/Documents/Playground/daycue/CHANGELOG.md): high-level product and system changes over time
- [HANDOFF.md](/Users/zkblook/Documents/Playground/daycue/HANDOFF.md): next-agent orientation and operational notes
- [INTEGRATIONS.md](/Users/zkblook/Documents/Playground/daycue/INTEGRATIONS.md): official external resource stack and integration order
- [IMPLEMENTATION_LOG.md](/Users/zkblook/Documents/Playground/daycue/IMPLEMENTATION_LOG.md): running engineering log and current phase notes

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
2. Read [HANDOFF.md](/Users/zkblook/Documents/Playground/daycue/HANDOFF.md)
3. Read [PRODUCT.md](/Users/zkblook/Documents/Playground/daycue/PRODUCT.md) and [CONTENT_SYSTEM.md](/Users/zkblook/Documents/Playground/daycue/CONTENT_SYSTEM.md)
4. Run `git status`
5. Compile-check `bot.py`
6. If shipping, prefer pushing to `main` and letting GitHub Actions deploy
7. If Actions fails, use the manual Fly deploy path in the runbook

## Security

Do not paste secrets into chat logs or repo files.

If any of these were exposed, rotate them:

- GitHub Personal Access Token
- `TELEGRAM_BOT_TOKEN`
- database password inside `DATABASE_URL`
- `FLY_API_TOKEN`
