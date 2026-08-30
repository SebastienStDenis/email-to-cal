# email-to-cal

Flag an email in Mail. It becomes a calendar event.

That is the whole interface. You decide what deserves a calendar entry by flagging it
blue - from your phone, your laptop, anywhere - and the service reads that email, writes
the events it describes to your iCloud calendar, and takes the flag off. If it cannot,
the flag stays on and your phone tells you why.

One Apple ID and one app-specific password cover both the mail and the calendar.

## How it works

```
iCloud IMAP           MIME extraction      model            CalDAV
every folder     →    JSON-LD           →  extract the   →  write to iCloud
search for the        .ics                 events           unflag the message
blue flag             text/plain                            push to your phone
                      HTML → text
                      PDF / images
```

Extraction is tiered so the reliable sources win. Airlines and ticketing platforms embed
[schema.org](https://schema.org/) JSON-LD in their HTML because Gmail and Outlook read
it, which hands over exact flight numbers, airport codes, and times with no guessing.
Failing that, a `.ics` attachment. Failing that, plain text, then rendered HTML, then
PDFs and images passed to the model as vision input - which is how boarding passes and
e-tickets get read.

Timezones are resolved deterministically: an explicit zone in the email beats an offline
IATA airport lookup, which beats the address's city, which beats your configured default.
A Tokyo → Los Angeles flight gets `Asia/Tokyo` on the departure and `America/Los_Angeles`
on the arrival, and renders correctly in both. Only the city field itself is read, and a
stated country has to agree with it - a lunch at a "Boston Pizza" in Ottawa stays in
Ottawa's zone, and London, Ontario is not five hours ahead of itself.

Locations are written as full addresses, because a calendar geocodes the location string
and only shows a map, directions, and travel time when it resolves. The model collects
the address in parts - venue, street, city, region, postal code, country - from wherever
the email states them, and they are rendered into one line in the order a geocoder
expects. Flights are filled in from the offline airport dataset instead, so a flight from
`LGA` gets `Laguardia Airport, New York, US` rather than an airport code.

Each event gets a UID derived from the message and the event's identity, and that UID
names the resource on the server. Flagging the same email again rewrites the same event
instead of adding a second one.

## Prerequisites

### An iCloud app-specific password

Apple's 2FA blocks plain passwords for IMAP and CalDAV clients, so you need an
app-specific one: [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security →
App-Specific Passwords. Give this service its own so you can revoke it independently.

> Changing your Apple ID password revokes **every** app-specific password. If the service
> starts failing to authenticate, that is almost always why.

### A calendar to write to

Create it in the Calendar app, on any iCloud account, and note its exact name. Every
calendar you configure has to exist already - the service writes to them and never
creates one.

### An Anthropic API key

From [console.anthropic.com](https://console.anthropic.com). Only the email you flag is
ever read, so a personal mailbox costs very little. To spend nothing at all, use a
[local model](#running-a-local-model-instead) instead.

## Run it

```sh
git clone https://github.com/sebastienstdenis/email-to-cal
cd email-to-cal
uv sync --all-groups
uv run email-to-cal serve
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and the portal takes it from there:

1. **Settings** - your iCloud address and app password, an Anthropic key, the name of
   your main calendar. Save.
2. **Status** - **Check connections** exercises the mailbox, the model, and the calendars
   end to end, and lists every calendar it can see.
3. Flag an email blue in Mail. Within a minute the event is on your calendar and the flag
   is gone.

Everything lands in `data/` inside your checkout: the configuration (`config.json`) and
the state database. The other commands read the same configuration, so once set up you
can also run it headless:

```sh
uv run email-to-cal check   # exercises the mailbox, the model, and the calendar
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
      # Holds config.json and state.sqlite. Back this up.
      - data:/app/data

volumes:
  data:
```

Then:

```sh
docker compose up -d
```

Open [http://localhost:8080](http://localhost:8080), through the SSH tunnel if the server
is remote, and configure exactly as above. Upgrade with
`docker compose pull && docker compose up -d`. The equivalent bare `docker run`:

```sh
docker run -d --name email-to-cal --restart unless-stopped \
  -p 127.0.0.1:8080:8080 -v email-to-cal-data:/app/data \
  ghcr.io/sebastienstdenis/email-to-cal:latest
```

## Which flag, and which mail

The **blue** flag by default, changeable to any of the seven in Settings. Apple stores a
flag colour as `\Flagged` plus a `$MailFlagBitN` keyword per bit of the colour index, so
a message flagged blue on an iPhone reads as blue everywhere. Rename the colour to
whatever you like in Mail - "Calendar", say; the colour is what counts, not the label.

Only the configured colour is processed. Red is left alone by default because it is what
every Mail client reaches for first, and your other colours keep whatever meaning you
already give them.

Every folder is searched on each pass, so it makes no difference where the mail is filed
or whether you file it after flagging. Junk, Deleted Messages, and Drafts are skipped.

Nothing else about the mailbox is touched: messages are fetched without marking them
read, and the only write is clearing the flag once the events are on your calendar.

## Categories

Everything lands on your main calendar unless you say otherwise. A category routes a kind
of event to its own calendar, and the description is what the model reads - so it says
what belongs, not just what the category is called:

| Name | Description | Calendar |
| --- | --- | --- |
| travel | Flights, trains, and hotel stays. | Travel |
| music | Concerts and gigs I have tickets for. | Music |

An event that matches no category, or that comes back with a name you never configured,
goes to the main calendar. Every calendar named here has to exist in the Calendar app
before the watcher starts; a name that matches nothing fails at startup rather than on
the first email that routes to it.

## When it cannot be done

The flag stays on, and that is deliberate - a failed email stays visible exactly where
you left it.

- **Nothing to put on a calendar.** The model read the email and found no event. You get
  a push straight away, because asking again would get the same answer.
- **Something was down.** The calendar server, the model, the network. It is retried
  after two minutes, then after ten. If it still fails, you get a push and the service
  stops trying.

Either way the email keeps its flag and appears under **Still flagged** on the Status
page, with **Try again** to run it once more. Unflagging it in Mail drops it.

## Phone notifications

Optional pushes through [Pushover](https://pushover.net). Register an application for
this service at [pushover.net/apps/build](https://pushover.net/apps/build), then enter its
API token and your user key in the portal.

Both outcomes are pushed. A created event arrives silently and opens the Calendar app on
the day of the event; a failure arrives as a normal push and opens the email it came
from. A time in the push is always the local time where the event happens, and the push
names that zone when it is not your own - so a 19:30 concert in London reads as London's
19:30, which is what the calendar will show you when you get there. Anything that stops the service - an expired app password, a rejected API key -
arrives at high priority. The event itself carries a link back to the email, so the way
back is always there.

Delivery is best-effort: a Pushover outage is logged and ignored, never allowed to stall
mail processing. `check` validates the keys without sending anything.

## Running a local model instead

Set the model to **Local model** in the portal and point it at an
[Ollama](https://ollama.com) server. Nothing leaves the machine and nothing is billed.

```sh
ollama pull gpt-oss:20b
```

A local model reads text only. PDF attachments contribute their text layer, so an
e-ticket with an empty body still shows the model its flight, but a scanned ticket or a
boarding-pass photo comes back with no events - which arrives as a failure notification.

## Commands

```sh
email-to-cal serve         # the portal plus the watcher; how the container runs
email-to-cal run           # the watcher alone
email-to-cal check         # exercise every dependency, then exit
email-to-cal replay a.eml  # run one .eml through extraction and print the iCalendar
email-to-cal healthcheck   # used by the container HEALTHCHECK
```

`replay` takes `--write` to actually put the events on the calendar.

## Operational notes

- **One IMAP connection, always.** iCloud allows only a handful per account and shares
  that budget with your phone and laptop. The service holds exactly one, and backs off
  exponentially if Apple pushes back.
- **Back up the data volume.** It holds your configuration and the record of what has
  been processed. Reprocessed mail will not duplicate anything already on your calendar.
- **One bad email cannot wedge the loop.** Failures are recorded per message, retried a
  couple of times, then set aside.
- **The portal has no authentication.** It holds your credentials, so publish its port to
  localhost only, as the compose file above does, and use an SSH tunnel from anywhere
  else.

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
