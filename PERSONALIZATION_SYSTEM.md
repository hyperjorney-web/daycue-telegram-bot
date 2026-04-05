# Personalization System

## Goal

Make daily advice feel specific without collecting unnecessary private data.

## Privacy-First Principle

We only store data that improves the daily cue.

We do not store:

- real names by default
- chat history
- precise location by default
- unnecessary health data
- photos
- diagnoses

We do store:

- nickname
- cycle details
- DOB if the user wants personalization
- derived personality hints
- short preference answers
- feedback

## Personalization Layers

### 1. Cycle Layer

The main truth source.

This determines:

- day in cycle
- phase
- estimated hormones
- energy
- irritability
- sensitivity
- need for space
- connection openness

### 2. Astrology Layer

Used as a cold-start guess only.

Input:

- date of birth

Output:

- tone bias
- support style bias
- practical vs warm bias

This should not be directly shown in the UI as the reason for advice.

### 3. Numerology Layer

Also a cold-start guess.

Input:

- date of birth

Output:

- communication style bias
- pacing bias
- direct vs soft wording bias

Like astrology, it should stay mostly internal.

### 4. Preference Layer

This is stronger than astrology/numerology.

Initial low-friction questions:

- what helps more: space / warmth / practical help / talking
- what annoys more: too many questions / pressure / passivity / coldness
- person style: calm / expressive / private / social
- rest style later: home / walk / cafe / movie / food / quiet time

### 5. Feedback Layer

This is the most important learning layer over time.

Base feedback:

- helpful
- not helpful

Recommended reason tags:

- good_tone
- good_action
- good_text
- too_generic
- too_soft
- too_pushy
- not_like_her
- wrong_timing

## Decision Priority

When layers disagree:

1. cycle state wins
2. explicit user preference wins
3. repeated feedback wins
4. astrology/numerology lose

## Daily Advice Composition

Inputs:

- cycle state
- personality guess
- preferences
- feedback history
- context:
  - day of week
  - time of day
  - together or apart
  - weather later

Outputs:

- short context line
- one action
- one text message
- one avoid line
- one optional idea

## Content Strategy

Do not generate the final message from scratch every time.

Use:

- relationship skill cards
- SMS bank
- avoid bank
- practical idea bank

Then personalize lightly.

## Anti-Repetition

Repeated days must not feel identical.

The system should rotate across:

- action family
- text family
- tone family
- practical suggestion type

Example:

Two luteal days can still differ:

- one day: reduce decisions
- next day: repair tone
- next day: remove one burden
- next day: give space without coldness

## Weather and Local Context

Telegram does not reliably give city/country by default.

We should only use local context if the user explicitly shares:

- city
- or location

Then we can add:

- weather-aware suggestions
- weekend planning suggestions
- “use today, weather drops tomorrow” logic

## Future Context Integrations

Potential optional enrichments:

- weather API
- movie suggestions API
- weekend planning ideas

These should remain optional, not blockers for the core daily cue.
