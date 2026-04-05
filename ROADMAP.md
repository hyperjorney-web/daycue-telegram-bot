# Roadmap

## Current Stage

We are in a Telegram MVP validation stage.

Goal:

- validate whether daily cycle-aware relationship advice is useful
- improve precision and voice quality
- learn what feels “right for her” via feedback

## What Is Already Done

- Telegram bot onboarding
- cycle and phase modeling
- estimated hormone and state model
- `Today`
- `Forecast`
- `Stats`
- `Insights`
- `Settings`
- language support
- feedback loop
- astrology cold-start layer
- numerology cold-start layer
- practical relationship skill-card engine
- Fly deploy
- GitHub auto-deploy

## Current Gaps

- daily copy still needs stronger editorial quality
- anti-repetition is still basic
- explicit user preference memory is still thin
- feedback reasons are still too shallow
- weather and local context not yet connected
- recommendation context like “at home / apart / evening / weekend” is still limited

## Next Milestone

### Milestone A: Voice Quality

- build larger SMS bank
- build larger cue bank
- build stronger anti-repetition
- rewrite robotic phrases

### Milestone B: Progressive Profiling

- add 3-question preference mini-profile
- add optional free-text note
- add preference memory table

### Milestone C: Better Feedback

- add reason tags
- use them to reweight style choices
- reduce astrology/numerology influence over time

### Milestone D: Contextual Suggestions

- optional location or city
- weather-aware suggestions
- weekday/weekend suggestions
- evening planning and pre-shift suggestions

### Milestone E: Content Expansion

- movie/series recommendation logic
- home date ideas
- low-energy ideas
- ovulatory and high-energy date ideas

## Longer-Term Path

Potential future directions:

- PWA or mobile app
- better privacy UX
- richer partner memory
- local-first or hybrid data architecture
- better content testing and ranking

## What Not To Do Yet

- do not overbuild medical logic
- do not overbuild astrology features in the UI
- do not ask too many onboarding questions
- do not ingest private chat history
- do not turn the product into a therapy simulator
