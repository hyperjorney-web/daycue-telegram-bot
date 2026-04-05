# Implementation Log

This file is a running engineering log for major product-system decisions and what has already been shipped.

## Current Product Shape

Daycue is a privacy-first Telegram bot for cycle-aware relationship support.

The current MVP already includes:

- daily cue
- forecast
- stats
- insights
- feedback loop
- multilingual UI
- privacy-friendly onboarding

## What Was Added In This Phase

### 1. Privacy-safe preference profile

Added support for a lightweight `/profile` flow inside the bot.

Stored fields:

- support preference
- what annoys her more
- overall style
- preferred reset type
- optional city
- optional short note

Why:

- better daily specificity
- future weather-aware recommendations
- future content ranking
- no need for real names or sensitive chat data

### 2. Database support

Added nullable user fields for preference memory:

- `support_preference`
- `what_annoys`
- `person_style`
- `rest_style`
- `optional_note`
- `city_name`

Added `feedback_reason` to `daily_feedback` for the next feedback iteration.

Why:

- keep the schema ready for learning without forcing a breaking migration later

### 3. Settings visibility

Settings now show the stored preference profile so users and future agents can verify what is known.

Why:

- transparency
- better debugging
- safer memory model

## What This Phase Deliberately Does Not Do Yet

- no automatic weather API calls
- no TMDB integration
- no LLM rewrite service in production path
- no chat history ingestion
- no exact location collection by default
- no visible astrology/numerology explanation

Why:

- keep the MVP safe
- keep privacy risks low
- avoid adding noisy complexity before the content bank is stronger

## Next Recommended Steps

1. Add feedback reasons to the UI.
2. Add anti-repetition memory for daily cues.
3. Add a larger SMS and action bank.
4. Add optional weather context using city only.
5. Add AI rewrite only after the content bank is strong enough.

## Operational Reminder

After shipping:

1. compile-check `bot.py`
2. push to `main`
3. let GitHub Actions deploy
4. verify Fly machine reaches `started`
5. if Fly leaves the new machine `stopped`, restart the active machine id manually
