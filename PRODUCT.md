# Daycue Product Blueprint

## One-Line Product

Daycue is a privacy-first Telegram bot that helps a partner understand today's cycle context and take one better action.

## Product Thesis

The user does not want education.

The user wants:

- one clear action for today
- one message they can actually send
- one thing to avoid

The partner should feel:

- understood
- less managed
- less burdened by explanation

## MVP Positioning

This is not:

- a medical cycle tracker
- a fertility app
- a therapy bot
- a generic relationship quotes bot

This is:

- cycle-aware relationship support
- action-first guidance
- short, practical, copyable daily advice

## Core Daily Output

Every daily message should answer:

1. What kind of day is this?
2. What should I do?
3. What can I text her?
4. What should I avoid?

Target output shape:

- `Today:` short context line
- `Do this:` one concrete action
- `Text her:` one copyable message
- `Avoid:` one concrete mistake
- `Extra:` one optional practical idea

## Product Rules

- Advice must be short
- Advice must be concrete
- Advice must sound human
- Advice must never sound clinical
- Advice must never sound mystical in the UI
- Internal personalization can exist, but should not be exposed directly
- Feedback matters more than astrology over time

## Personalization Stack

Layer 1:

- cycle day
- phase
- estimated hormones
- estimated support dimensions

Layer 2:

- astrology as a cold-start personality guess

Layer 3:

- numerology as a secondary communication-style guess

Layer 4:

- explicit user preferences

Layer 5:

- feedback from the user on what actually felt right

Priority:

- cycle state first
- explicit preferences second
- feedback third
- astrology and numerology only as initial bias

## Success Criteria

We know the product is getting better when:

- daily advice feels less generic
- users say “that sounds like her”
- helpful feedback increases
- repeated days stop sounding repetitive
- more messages feel copyable without editing

## Current Content Problem

The current system logic is already stronger than the text voice.

Main issue:

- advice quality is bottlenecked by wording, not by structure

This means the next biggest product improvement is:

- better voice system
- larger content bank
- better anti-repetition

## Next Product Milestone

Move from:

- phase templates

To:

- state-driven relationship skill cards
- copy bank
- progressive profiling
- feedback-weighted personalization
