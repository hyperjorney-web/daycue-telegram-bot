# Integrations And Research Stack

This file records the external resource stack Daycue should use for better daily advice, richer context, and more human copy.

## Principle

External services should enrich the advice, not replace the core.

Priority:

1. cycle and state engine
2. explicit user preferences
3. feedback history
4. context enrichers
5. copy rewrite layer

## Relationship Framework

Use Gottman-style concepts as the relationship backbone:

- bids for connection
- repair attempts
- gentle startup
- turning toward

Official sources:

- [The Gottman Institute](https://www.gottman.com/)
- [Communication resources](https://www.gottman.com/improve-communication-relationship/)
- [Bids for connection article](https://www.gottman.com/blog/turning-toward-our-children-answering-bids-for-connection/)

These are not used as a live API. They are the editorial and behavioral framework for the internal content bank.

## Weather Context

Recommended provider:

- [OpenWeather One Call API 3.0](https://openweathermap.org/api/one-call-3)

Use cases:

- weekend planning hints
- "go out today, weather drops tomorrow"
- walk vs at-home recommendations
- short trip or cafe ideas

Store only what is needed:

- city name
- timezone
- optional lat/lon later if the user explicitly shares location

Do not request exact location by default.

## Movies And Series Context

Recommended provider:

- [TMDB API](https://developer.themoviedb.org/reference/intro/getting-started)

Useful endpoints:

- discover movie
- discover tv
- genre filtering
- region and language filtering

Use cases:

- recommend a light comedy
- suggest a comfort-watch option
- recommend an at-home evening idea based on known taste

This should only be used after the product knows at least a rough rest preference such as:

- movie or series
- home time
- quiet night

## AI Copy Layer

Use AI for rewriting and ranking, not for inventing the relationship logic.

Recommended role:

- rewrite content-bank outputs into natural copy
- shorten robotic phrasing
- generate 2-3 ranked SMS variants
- adapt tone by locale

Do not use AI to decide:

- the cycle state
- the core advice type
- the user profile truth

The source of truth stays inside Daycue.

## Suggested System Flow

1. derive current state from cycle data
2. blend in explicit preferences
3. apply feedback-weighted ranking
4. optionally enrich with weather or media context
5. rewrite final cue through a controlled copy layer

## Integration Order

### Phase 1

- ship privacy-safe `/profile`
- store basic preference memory
- improve content bank coverage

### Phase 2

- add feedback reasons
- add anti-repetition
- rank content families by past helpfulness

### Phase 3

- add optional city and timezone-aware weather hints
- connect OpenWeather

### Phase 4

- add optional movie and series recommendations
- connect TMDB

### Phase 5

- add cheap AI rewrite layer for more human SMS copy
- keep all generation bounded by internal content rules

## Guardrails

- no scraping private conversations by default
- no hidden location collection
- no storing real names if a nickname works
- no medical claims
- no astrology or numerology explanations in the visible UI
- astrology and numerology remain cold-start hints only
