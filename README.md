# email-to-cal

Watches an iCloud mailbox and turns the events you actually committed to into Google
Calendar entries, routed to per-category calendars. Configured from a small web portal;
runs from a checkout or as a Docker container.

The point is not to find dates in email - that part is easy. The point is to only create
events you actually signed up for. "You've bought tickets for Radiohead" becomes a
calendar entry. "Concerts near you this weekend" does not.

## How it works

```
iCloud IMAP (IDLE)  →  MIME extraction  →  local filter  →  Claude  →  timezone resolution  →  Google Calendar
                       JSON-LD              (optional)        is this a     IATA → IANA           idempotent insert
                       .ics                 discard obvious   commitment?   city → IANA           per-category routing
                       text/plain           junk for free     extract       default
                       HTML → text                            categorise
                       PDF / images
```

The local filter is an optional cost cut: a free model served by
[Ollama](https://ollama.com) on the same machine discards obvious junk before it costs
an API call - see [The local junk filter](#the-local-junk-filter-optional).

Extraction is tiered so the reliable sources win. Airlines and ticketing platforms embed
[schema.org](https://schema.org/) JSON-LD in their HTML because Gmail and Outlook read it,
which hands us exact flight numbers, airport codes, and times with no guessing. Failing
that, a `.ics` attachment. Failing that, plain text, then rendered HTML, then PDFs and
images passed to the model as vision input - which is how boarding passes and e-tickets
get read.

Timezones are resolved deterministically: an explicit zone in the email beats an offline
IATA airport lookup, which beats a city match, which beats your configured default. A
Tokyo → Los Angeles flight gets `Asia/Tokyo` on the departure and `America/Los_Angeles`
on the arrival, and renders correctly in both.

Locations are written as full addresses, because Google Calendar geocodes the location
string and only shows a map, directions, and travel time when it resolves. The model
collects the address in parts - venue, street, city, region, postal code, country - from
wherever the email states them, and they are rendered into one line in the order a
geocoder expects. Flights are filled in from the offline airport dataset instead, so a
flight from `LGA` gets `Laguardia Airport, New York, US` rather than an airport code.

Re-delivering the same email is a no-op: every event gets a deterministic id derived from
the message and the event's identity, so a duplicate insert is recognised and ignored
rather than double-booking you. The same booking arriving in a *different* email - a
reminder, an updated itinerary - is caught fuzzily instead: an event on the target
calendar starting within an hour (configurable under Advanced settings) with a
near-identical title or the same booking reference means nothing new is created.

## Prerequisites

Whichever way you run it, you need three credentials. Have them ready before you start.

### iCloud app-specific password

Apple's 2FA blocks plain passwords for IMAP clients, so you need an app-specific one:
[appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific
Passwords. Give this service its own so you can revoke it independently.

> Changing your Apple ID password revokes **every** app-specific password. If the service
> starts failing to authenticate, that is almost always why. It logs a clear message and
> exits rather than hammering Apple with retries.

### Google OAuth client

A one-time, five-minute setup in the Google Cloud console. You come out of it with two
strings: a client id and a client secret.

1. At [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. **APIs & Services → Library** → enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform): set it up for
   **External** users, then set the publishing status to **In production**.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID → Desktop
   app**. Copy the client id and client secret.

Step 3 is not optional. An app left in **Testing** issues refresh tokens that expire
after **7 days**, and the service will silently stop working every week. Publishing does
*not* require Google's verification review - you will see a one-time "Google hasn't
verified this app" screen (Advanced → Go to …), and Google explicitly exempts apps used
only by their author.

### Anthropic API key

From [console.anthropic.com](https://console.anthropic.com). Extraction uses one model
call per email, cached by content, so a personal mailbox costs very little - and the
optional [local junk filter](#the-local-junk-filter-optional) cuts it further.

## Run it

```sh
git clone https://github.com/sebastienstdenis/email-to-cal
cd email-to-cal
uv sync --all-groups
uv run email-to-cal serve
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and the portal takes it from there:

1. **Settings** - paste the three credentials from the prerequisites, name your
   categories, save.
2. **Connect Google Calendar** - one click through Google's consent screen. Do this from
   a browser where the portal is reachable as `localhost`, because Google returns the
   authorisation to a loopback address.
3. **Status** - watch it work: watcher state, recent events, failures, and a "Run
   checks" button that exercises IMAP, Anthropic, and Google end to end.

New installs start in **preview mode**: the service logs exactly what it would put on
your calendar without writing anything. Watch a day of mail, tune the category
descriptions, then switch preview mode off in Settings.

Everything lands in `data/` inside your checkout: the configuration (`config.json`),
the Google token, and the state database. The other commands read the same
configuration, so once set up you can also run it headless:

```sh
uv run email-to-cal check   # exercises IMAP, Anthropic, and Google, then exits
uv run email-to-cal run     # the watcher without the portal
```

Environment variables override the saved configuration for one-offs:
`LOG_LEVEL=DEBUG uv run email-to-cal run`.

## Run it with Docker

The published image, for keeping it running on a server. Create a `docker-compose.yml`:

```yaml
services:
  email-to-cal:
    image: ghcr.io/sebastienstdenis/email-to-cal:latest
    container_name: email-to-cal
    restart: unless-stopped
    ports:
      # The portal has no login, so keep it bound to localhost. On a remote server,
      # reach it with: ssh -L 8080:localhost:8080 your-server
      - "127.0.0.1:8080:8080"
    volumes:
      # Holds config.json, token.json, and state.sqlite. Back this up.
      - data:/app/data

volumes:
  data:
```

Then:

```sh
docker compose up -d
```

Open [http://localhost:8080](http://localhost:8080) (through the SSH tunnel if the
server is remote - Google's consent step needs the portal reachable as `localhost`) and
configure exactly as above. Upgrade with `docker compose pull && docker compose up -d`.
The equivalent bare `docker run`, if you don't use compose:

```sh
docker run -d --name email-to-cal --restart unless-stopped \
  -p 127.0.0.1:8080:8080 -v email-to-cal-data:/app/data \
  ghcr.io/sebastienstdenis/email-to-cal:latest
```

## Which folders it reads

The watched folder (default `INBOX`) gets a live IDLE connection. That matters because
of a race: if you read a booking confirmation on your phone and archive it before the
service has seen it, the move deletes it from INBOX and gives it a fresh UID somewhere
else, and it is gone as far as the watcher is concerned. The window is seconds in steady
state and wide open during restarts and deploys.

Swept folders close it. They get a catch-up pass every sweep interval (default 15
minutes) on the *same* connection - iCloud allows only about five connections per
account and your phone and Mac already use some, so a second push connection is the
wrong trade. New mail always lands in the watched folder first, so a sweep is
sufficient; swept folders never backfill, and anything already handled is skipped by
Message-ID.

## Categories

Each category is a `(name, description, calendar)` row in the portal:

| Name | Description | Calendar |
| --- | --- | --- |
| travel | Flights, trains, ferries, and hotel stays. Anything involving getting to or staying somewhere away from home. | Sebastiens Travels |
| music | Concerts, gigs, festivals, and club nights the recipient has tickets for. | Music |

The description is what the model matches an event against, so write it for the model:
say what belongs *and* what does not. Anything that matches no category goes to the
default calendar. Calendars that do not exist yet are created on first run.

## Commands

The container runs `serve` by default. The rest exist for the repo path and for
debugging:

| Command | What it does |
| --- | --- |
| `serve` | The web portal plus the watcher. What the container runs. |
| `run` | Watch the mailbox and create events, headless. |
| `check` | Validate config and every external dependency, then exit. |
| `replay FILE.eml` | Push one saved email through the real pipeline. Add `--dry-run`. |
| `eval-local` | Measure the local junk filter against cached Claude verdicts on your own mail. |
| `healthcheck` | Used by the container `HEALTHCHECK`. |

`replay` is the tool for tuning. Save a message that was handled wrongly, run it, and
read the model's `gate_reasoning`:

```sh
uv run email-to-cal replay samples/weird.eml --dry-run
```

## Tuning the gate

Two settings control how eager the service is:

- **Minimum confidence** (default `0.75`) - events below this are logged with the reason
  and dropped. Raise it if you get junk, lower it if real bookings are being missed.
- **Effort** (default `medium`) - how hard the model thinks. `high` catches more awkward
  emails at higher cost.

Model responses are cached in the state database keyed by content, so replays and
restarts never re-bill you for the same email.

## The local junk filter (optional)

Claude reads every email that arrives, ads included, at API prices. The junk filter
puts a free model in front of it: a small open-weight model served by
[Ollama](https://ollama.com) on the same machine answers one question per email -
*could this plausibly contain a personal commitment?* - and discards the obvious
noes (newsletters, promos, digests, receipts for things already over) before they
cost an API call. On a typical inbox that is most of the mail.

The design is deliberately lopsided, because the two mistakes are not equal:

- The filter is **only allowed to reject**; anything it is unsure about passes. A
  wrongly passed email costs one API call; a wrongly discarded one is a booking that
  silently never reaches your calendar.
- Emails carrying **.ics or JSON-LD data bypass the filter entirely** - senders embed
  those for real bookings, and a cheap model gets no veto over them. PDF attachments
  contribute their text layer, so a "boarding pass attached" email with an empty body
  still shows the filter its flight.
- **Every failure fails open.** Ollama down, model not pulled, garbage output: the
  email goes to Claude exactly as if the filter were off. The filter can only ever
  save money, never mail.

Claude still makes every real decision - the commitment gate, the extraction, the
categories - so nothing about quality changes.

**Setup.** Run Ollama next to the service and pull the model (roughly 16 GB of free
RAM for the default). With the Docker deployment, add a sibling container:

```yaml
  ollama:
    image: ollama/ollama:latest
    container_name: ollama
    restart: unless-stopped
    volumes:
      - ollama:/root/.ollama    # model weights, survives updates
```

```sh
docker compose up -d
docker exec ollama ollama pull gpt-oss:20b
```

Then tick **Filter junk with a free local model first** in the portal's Claude card
(Ollama server under Advanced: `http://ollama:11434` for the sibling container), and
run the checks on the Status page.

**Measure before you trust it.** If the service has been running for a while, every
past Claude verdict is cached. `eval-local` replays your real mail through the filter
and checks each would-be discard against what Claude actually concluded, without a
single API call:

```sh
docker exec -it email-to-cal email-to-cal eval-local --days 90
```

It reports how many API calls the filter would have saved and - most importantly -
**wrong discards**: emails Claude considered real bookings that the filter would have
dropped. The exit code is non-zero when any exist. Filter verdicts computed during the
eval are cached, so enabling the filter afterwards starts warm.

## Phone notifications

Optional pushes through [Pushover](https://pushover.net). Register an application for
this service at [pushover.net/apps/build](https://pushover.net/apps/build), then enter
its API token and your user key in the portal.

A created-event push carries a link to the event itself, so tapping it opens the event
in Google Calendar.

Notifications are sent at the priority they deserve: created events arrive silently,
per-message failures arrive as a normal push, and anything that stops the service - an
expired app password, a rejected API key - arrives at high priority, because mail goes
unread until someone acts. Created-event and error notifications can each be switched
off in the portal; sounds and quiet hours are the Pushover app's job.

Delivery is best-effort. A Pushover outage is logged and ignored, never allowed to
stall mail processing. `check` validates the keys against Pushover's validation
endpoint without sending anything.

## Operational notes

- **One IMAP connection, always.** iCloud allows only a handful per account and shares
  that budget with your phone and laptop. The service holds exactly one, cycling `IDLE`
  every five minutes, and backs off exponentially if Apple pushes back.
- **The mailbox is never modified.** Messages are fetched with `BODY.PEEK[]`, so nothing
  is marked read, flagged, or moved. Processing state lives entirely in
  `data/state.sqlite`.
- **Back up the data volume.** It holds your configuration, your Google token, and the
  record of what has been processed. Losing it means reconfiguring and re-authorising -
  though the deterministic event ids mean reprocessed mail still will not duplicate
  anything already in your calendar.
- **A poison email cannot wedge the loop.** Failures are logged per-message and the
  cursor advances; written-off messages show up on the Status page.
- **The portal has no authentication.** It holds your credentials, so publish its port
  to localhost only (as the compose file above does) and use an SSH tunnel from
  anywhere else.

## Scopes

The service requests `https://www.googleapis.com/auth/calendar` because creating a
missing calendar needs `calendars.insert` and routing needs `calendarList.list`.

If you would rather it could never touch your existing calendars, swap `SCOPES` in
`src/email_to_cal/gcal.py` for `https://www.googleapis.com/auth/calendar.app.created`,
set the default calendar to a name that does not exist yet, and re-authorise. The
service will then create and write to only its own calendars.

## Development

```sh
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
python tests/make_fixtures.py   # regenerate the .eml fixtures
```

CI runs the same checks plus a Docker build. Pushes to `main` publish
`ghcr.io/sebastienstdenis/email-to-cal:latest`; `v*` tags publish semver tags. Both are
multi-arch (amd64 and arm64) and need no configured secrets.
