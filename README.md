# email-to-cal

Watches an iCloud mailbox and turns the events you actually committed to into Google
Calendar entries, routed to per-category calendars. Runs as a Docker container on your
own server, configured entirely through environment variables.

The point is not to find dates in email — that part is easy. The point is to only create
events you actually signed up for. "You've bought tickets for Radiohead" becomes a
calendar entry. "Concerts near you this weekend" does not.

## How it works

```
iCloud IMAP (IDLE)  →  MIME extraction  →  Claude  →  timezone resolution  →  Google Calendar
                       JSON-LD              is this a       IATA → IANA          idempotent insert
                       .ics                 commitment?     city → IANA          per-category routing
                       text/plain           extract         default
                       HTML → text          categorise
                       PDF / images
```

Extraction is tiered so the reliable sources win. Airlines and ticketing platforms embed
[schema.org](https://schema.org/) JSON-LD in their HTML because Gmail and Outlook read it,
which hands us exact flight numbers, airport codes, and times with no guessing. Failing
that, a `.ics` attachment. Failing that, plain text, then rendered HTML, then PDFs and
images passed to the model as vision input — which is how boarding passes and e-tickets
get read.

Timezones are resolved deterministically wherever possible. Flights use an offline IATA
airport database, so a Tokyo → Los Angeles flight gets `Asia/Tokyo` on the departure and
`America/Los_Angeles` on the arrival, and renders correctly in both. Web search is
available for venues that resolve no other way, but it is off by default because the
deterministic paths cover almost everything.

Re-delivering the same email is a no-op: every event gets a deterministic id derived from
the message and the event's identity, so a duplicate insert is recognised and ignored
rather than double-booking you.

## Setup

### 1. iCloud app-specific password

Apple's 2FA blocks plain passwords for IMAP clients, so you need an app-specific one:
[appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific
Passwords. Give this service its own so you can revoke it independently.

> Changing your Apple ID password revokes **every** app-specific password. If the service
> starts failing to authenticate, that is almost always why. It logs a clear message and
> exits rather than hammering Apple with retries.

### 2. Google Cloud project

1. Create a project and enable the **Google Calendar API**.
2. APIs & Services → Credentials → Create Credentials → **OAuth client ID** → **Desktop
   app**. Download the JSON as `credentials.json`.
3. **Set the OAuth app's publishing status to "In production."**

Step 3 is not optional. An app left in **Testing** issues refresh tokens that expire after
**7 days**, and the service will silently stop working every week. Publishing does *not*
require Google's verification review — you will see a one-time "Google hasn't verified
this app" screen (Advanced → Go to … ), and Google explicitly exempts apps used only by
their author.

### 3. Authorise

Run the consent flow once, on a machine with a browser:

```sh
uv run email-to-cal auth-google --credentials ./credentials.json --token ./data/token.json
```

Copy `credentials.json` and `token.json` into the `data/` directory on your server. The
container refreshes and rewrites `token.json`, so the directory must be writable by uid
`10001`.

### 4. Configure and run

```sh
cp .env.example .env          # then edit it
cp docker-compose.example.yml docker-compose.yml
mkdir -p data && cp credentials.json token.json data/
docker compose up -d
```

`.env.example` documents every setting. Verify everything before trusting it:

```sh
docker compose run --rm email-to-cal check
```

That validates the config, logs into IMAP, reaches the Anthropic API, refreshes the Google
token, and resolves or creates every configured calendar — then exits.

**Leave `DRY_RUN=true` for the first day.** The service will log the exact Google Calendar
payloads it would create without writing anything, which is the cheapest way to find out
whether the gate and the category descriptions are tuned the way you want.

## Categories

`CATEGORIES` is a JSON array of `{name, description, calendar}` triples:

```json
[
  {
    "name": "travel",
    "description": "Flights, trains, ferries, and hotel stays. Anything involving getting to or staying somewhere away from home.",
    "calendar": "Sebastiens Travels"
  },
  {
    "name": "music",
    "description": "Concerts, gigs, festivals, and club nights the recipient has tickets for.",
    "calendar": "Music"
  }
]
```

The `description` is what the model matches an event against, so write it for the model:
say what belongs *and* what does not. Anything that matches no category goes to
`DEFAULT_CALENDAR`. Calendars that do not exist yet are created on first run.

If JSON in an environment variable offends you, set `CATEGORIES_FILE` to a mounted YAML
file with the same shape instead.

## Commands

| Command | What it does |
| --- | --- |
| `run` | Watch the mailbox and create events. The default. |
| `check` | Validate config and every external dependency, then exit. |
| `auth-google` | Run the Google OAuth consent flow and write `token.json`. |
| `replay FILE.eml` | Push one saved email through the real pipeline. Add `--dry-run`. |
| `healthcheck` | Used by the container `HEALTHCHECK`; checks the loop heartbeat. |

`replay` is the tool for tuning. Save a message that was handled wrongly, run it, and read
the model's `gate_reasoning`:

```sh
docker compose run --rm email-to-cal replay /data/samples/weird.eml --dry-run
```

## Tuning the gate

Two settings control how eager the service is:

- `MIN_CONFIDENCE` (default `0.75`) — events below this are logged with the reason and
  dropped. Raise it if you get junk, lower it if real bookings are being missed.
- `ANTHROPIC_EFFORT` (default `medium`) — how hard the model thinks. `high` catches more
  awkward emails at higher cost.

Model responses are cached in the state database keyed by content, so replays and restarts
never re-bill you for the same email.

## Operational notes

- **One IMAP connection, always.** iCloud allows only a handful per account and shares that
  budget with your phone and laptop. The service holds exactly one, cycling `IDLE` every
  five minutes, and backs off exponentially if Apple pushes back.
- **The mailbox is never modified.** Messages are fetched with `BODY.PEEK[]`, so nothing is
  marked read, flagged, or moved. Processing state lives entirely in `/data/state.sqlite`.
- **Back up `data/`.** It holds your Google token and the record of what has been
  processed. Losing it means re-authorising and, depending on
  `FIRST_RUN_LOOKBACK_DAYS`, potentially reprocessing mail — though the deterministic event
  ids mean that still will not duplicate anything already in your calendar.
- **A poison email cannot wedge the loop.** Failures are logged per-message and the cursor
  advances.

## Scopes

The service requests `https://www.googleapis.com/auth/calendar` because creating a missing
calendar needs `calendars.insert` and routing needs `calendarList.list`.

If you would rather it could never touch your existing calendars, swap `SCOPES` in
`src/email_to_cal/gcal.py` for `https://www.googleapis.com/auth/calendar.app.created`, set
`DEFAULT_CALENDAR` to a name that does not exist yet, and re-run `auth-google`. The service
will then create and write to only its own calendars.

## Development

```sh
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
python tests/make_fixtures.py   # regenerate the .eml fixtures
```

CI runs the same checks plus a Docker build. Pushes to `main` publish
`ghcr.io/<owner>/email-to-cal:latest`; `v*` tags publish semver tags. Both are multi-arch
(amd64 and arm64) and need no configured secrets.
