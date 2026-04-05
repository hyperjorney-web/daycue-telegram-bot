# Privacy Notes

## Product Position

Daycue should be privacy-first by default.

The product deals with:

- cycle data
- relationship signals
- partner preferences

This is sensitive.

## Core Rules

- no real names required
- nickname only by default
- no private chat ingestion by default
- no hidden scraping
- no exact location by default
- no storing data that is not clearly useful for daily advice quality

## Safe Defaults

Use:

- `partner_nickname`
- `partner_dob` only when the user wants deeper personalization
- minimal cycle dates
- short preference answers
- lightweight feedback

Do not require:

- address
- city
- workplace
- medical history
- relationship history

## Location Policy

We should only use location-based features if the user explicitly shares:

- city
- or Telegram location

Reason:

- Telegram does not reliably give city/country automatically
- weather features are useful but optional

## Messaging Data Policy

Do not store:

- raw SMS history
- partner chats
- imported message logs

If a future memory feature exists, it should store:

- short structured preferences only
- not raw personal text

## Editable Memory

Any stored preference should be:

- visible
- editable
- removable

Examples:

- likes walks
- needs space when tired
- dislikes too many questions
- prefers practical help

## Security Hygiene

Never commit secrets to the repo.

Rotate immediately if exposed:

- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`
- `FLY_API_TOKEN`
- GitHub PATs

## Product Copy Principle

We should be honest in the UI:

- we estimate
- we personalize
- we learn from feedback

We should not imply:

- diagnosis
- guaranteed psychological truth
- accurate personality profiling from astrology/numerology

Those layers are only starting guesses.
