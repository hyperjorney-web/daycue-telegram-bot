# Changelog

## 2026-04-05

### Product / Logic

- added privacy-first personalization model
- documented layered personalization:
  - cycle
  - astrology
  - numerology
  - preferences
  - feedback

### Bot Behavior

- shortened `Today` output
- removed long explanatory blocks from the morning message
- shifted daily advice toward practical relationship skill cards
- added `Text her` as a first-class part of the daily output
- improved ovulatory messaging so it leans more toward initiative, playfulness, and concrete plans

### Copy / UX Direction

- identified that main remaining weakness is content voice, not system logic
- defined need for:
  - stronger SMS bank
  - larger cue bank
  - anti-repetition
  - editorial tone rules

### Documentation

- added product blueprint
- added personalization system documentation
- added content system documentation
- added privacy notes
- added roadmap
- added integrations stack documentation
- added implementation log

### Profile Memory

- added privacy-safe preference profile fields to the user model
- added `/profile` conversational flow for lightweight preference memory
- added optional `city_name` for future weather-aware recommendations
- added `feedback_reason` field to prepare the next feedback iteration

## 2026-04-04

### UI / Messaging

- removed exposed internal personalization language from the visible UI
- stopped showing astrology/numerology reasoning directly in `Today`
- simplified visible explanation style

### Content

- shortened daily advice blocks
- removed the awkward “why this tone today” style explanation from the core message

## 2026-04-03

### Core Bot

- fixed critical bot startup issues
- improved phase and hormone modeling
- added stats, insights, and settings views
- added feedback loop
- improved onboarding
- added language selection
- added zodiac and numerology cold-start layers
- documented deploy and operations workflow
