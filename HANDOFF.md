# Handoff For The Next Agent

## Current Truth

This repo is a live Telegram MVP for cycle-aware relationship support.

Production app:

- Fly app: `daycue-telegram-bot`
- Repo: `hyperjorney-web/daycue-telegram-bot`

Main file:

- [bot.py](/Users/zkblook/Documents/Playground/daycue/bot.py)

## What Matters Most Right Now

The main bottleneck is not infrastructure.

The main bottleneck is:

- content quality
- natural language quality
- repetition control

The logic is already good enough to support a stronger content layer.

## Key Product Direction

Do not keep adding visible complexity.

Prioritize:

- shorter daily messages
- more human copy
- more practical actions
- more copyable SMS suggestions
- better personalization from feedback

## Architecture Direction

Current system should evolve toward:

1. state engine
2. preference memory
3. content bank
4. feedback-weighted ranking

Avoid:

- exposing astrology/numerology as visible explanation
- long morning messages
- generic wellness language

## Files To Read First

1. [README.md](/Users/zkblook/Documents/Playground/daycue/README.md)
2. [DEPLOYMENT.md](/Users/zkblook/Documents/Playground/daycue/DEPLOYMENT.md)
3. [PRODUCT.md](/Users/zkblook/Documents/Playground/daycue/PRODUCT.md)
4. [PERSONALIZATION_SYSTEM.md](/Users/zkblook/Documents/Playground/daycue/PERSONALIZATION_SYSTEM.md)
5. [CONTENT_SYSTEM.md](/Users/zkblook/Documents/Playground/daycue/CONTENT_SYSTEM.md)
6. [ROADMAP.md](/Users/zkblook/Documents/Playground/daycue/ROADMAP.md)
7. [CHANGELOG.md](/Users/zkblook/Documents/Playground/daycue/CHANGELOG.md)

## Recommended Next Work

### 1. Expand the content bank

- 100+ SMS lines
- 50+ action lines
- 50+ avoid lines
- 50+ extra ideas

### 2. Add anti-repetition

- avoid repeating the same content family too often
- track recently used cue ids

### 3. Add progressive profiling

- 3 low-friction preference questions
- store short preference memory

### 4. Add better feedback reasons

- too generic
- too soft
- too pushy
- good text
- good action
- not like her

### 5. Add context later

- optional city/location
- weather-aware suggestions
- weekend planning hints

## Operational Note

Fly sometimes finishes a deploy but leaves the new machine in `stopped`.

If that happens:

1. run Fly status
2. restart the current machine by id
3. verify it reaches `started`

This is an operational quirk, not necessarily a code regression.
