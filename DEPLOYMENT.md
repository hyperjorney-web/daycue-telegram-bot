# Deployment Runbook

This document explains how to run, ship, verify, and troubleshoot the Daycue Telegram bot.

## Production Targets

- Fly app: `daycue-telegram-bot`
- GitHub repo: `hyperjorney-web/daycue-telegram-bot`
- Primary branch: `main`

## Required Secrets

### Fly

The Fly app must have these secrets:

- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`

Check current Fly secrets:

```bash
export PATH="$HOME/.fly/bin:$PATH"
flyctl secrets list -a daycue-telegram-bot
```

Set or update a secret:

```bash
export PATH="$HOME/.fly/bin:$PATH"
flyctl secrets set "DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@db.YOUR_PROJECT.supabase.co:5432/postgres?sslmode=require" -a daycue-telegram-bot
```

### GitHub Actions

To allow automatic deploy on push to `main`, GitHub must have:

- `FLY_API_TOKEN`

Repo path:

- `Settings`
- `Secrets and variables`
- `Actions`
- `New repository secret`

Name:

```text
FLY_API_TOKEN
```

Value:

```bash
export PATH="$HOME/.fly/bin:$PATH"
flyctl auth token
```

## Automatic Deploy

GitHub Actions workflow file:

- [.github/workflows/fly-deploy.yml](/Users/zkblook/Documents/Playground/daycue/.github/workflows/fly-deploy.yml)

Behavior:

- runs on push to `main`
- can also be started manually with `workflow_dispatch`

Normal flow:

1. commit changes locally
2. push to `main`
3. open GitHub `Actions`
4. confirm `Deploy to Fly` succeeds

If the workflow fails with:

```text
Error: no access token available. Please login with 'flyctl auth login'
```

then `FLY_API_TOKEN` is missing or invalid in GitHub Actions secrets.

## Manual Deploy

Use this when:

- GitHub Actions is not configured yet
- Actions is failing and you need a direct ship
- you want to verify a release immediately

Commands:

```bash
export PATH="$HOME/.fly/bin:$PATH"
cd /Users/zkblook/Documents/Playground/daycue
flyctl deploy -a daycue-telegram-bot
```

## Manual Restart

If the bot seems stale or the machine is stopped:

```bash
export PATH="$HOME/.fly/bin:$PATH"
flyctl machines list -a daycue-telegram-bot
flyctl machines restart MACHINE_ID -a daycue-telegram-bot
```

Note:

- this app has occasionally ended a rolling deploy with the new machine in `stopped`
- if that happens, restart the exact machine id shown by `flyctl machines list`
- then verify `flyctl status` shows `started`

## Status Checks

### Fly status

```bash
export PATH="$HOME/.fly/bin:$PATH"
flyctl status -a daycue-telegram-bot
```

Healthy signal:

- machine state is `started`

### Fly logs

```bash
export PATH="$HOME/.fly/bin:$PATH"
flyctl logs -a daycue-telegram-bot --no-tail
```

Healthy signals in logs:

- `DB connected + schema ensured`
- `Daycue boot`
- `Application started`
- Telegram `sendMessage "HTTP/1.1 200 OK"`

### Local syntax check

```bash
cd /Users/zkblook/Documents/Playground/daycue
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m py_compile bot.py
```

## Common Failures

### 1. Bot is deployed but not responding in Telegram

Check:

```bash
flyctl status -a daycue-telegram-bot
flyctl logs -a daycue-telegram-bot --no-tail
```

Most likely causes:

- machine is stopped
- `DATABASE_URL` is wrong
- `TELEGRAM_BOT_TOKEN` is invalid

### 2. Database connection error on startup

Typical error:

```text
socket.gaierror: [Errno -2] Name or service not known
```

Meaning:

- `DATABASE_URL` exists
- but the database host in that URL is wrong or not resolvable

Fix:

- get a fresh Supabase/Postgres connection string
- include `?sslmode=require`
- update Fly secret
- restart machine

### 3. GitHub push rejects workflow file

Typical error:

```text
refusing to allow a Personal Access Token to create or update workflow ... without workflow scope
```

Fix:

- use a GitHub classic PAT
- include scopes:
  - `repo`
  - `workflow`

### 4. GitHub Action fails with no access token

Typical error:

```text
Error: no access token available. Please login with 'flyctl auth login'
```

Fix:

- add or update `FLY_API_TOKEN` in GitHub Actions secrets

## Release Checklist

Before shipping:

1. `git status`
2. `python3 -m py_compile bot.py`
3. review changed files
4. commit only intended files
5. push to `main`
6. confirm GitHub Action passes or run manual `flyctl deploy`
7. confirm `flyctl status`
8. if Fly shows `stopped`, restart the machine by id
9. test the bot in Telegram with `/start`

## Telegram Functional Check

After deploy, test:

1. `/start`
2. onboarding language selection
3. short date input like `5.4`
4. preset time selection like `Morning`
5. `Today`
6. `Stats`
7. `Insights`
8. `Settings`
9. `/update_period`
10. `Helpful / Not helpful`

## Data Model Notes

Data is stored server-side in Postgres/Supabase, not on-device.

Current persistence:

- `users`
- `period_log`
- `daily_feedback`
- optional `copy_strings`

Period updates are append-friendly:

- active dates in `users` are updated
- history is retained in `period_log`

## Privacy Notes

The product now supports a partner label instead of requiring a real name.

For extra safety:

- avoid storing real names unless needed
- rotate tokens if exposed
- rotate database password if it was pasted into chat or logs

## Operational Notes For Future Agents

If you are a future agent touching this repo:

1. Read [README.md](/Users/zkblook/Documents/Playground/daycue/README.md)
2. Read this file fully
3. Verify current prod with `flyctl status`
4. Check logs before assuming Telegram is the problem
5. Prefer preserving `period_log` and `daily_feedback`
6. Do not remove history tables during schema changes
