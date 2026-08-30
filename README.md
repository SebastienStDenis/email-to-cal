# email-to-cal

Flag an email in Mail. It becomes a calendar event.

That is the whole interface. You decide what deserves a calendar entry by flagging it
blue - from your phone, your laptop, anywhere - and the service reads that email, writes
the events it describes to your iCloud calendar, and takes the flag off. If it cannot,
the flag stays on and your phone tells you why.

```
you flag an email  →  iCloud IMAP       →  extraction        →  iCloud Calendar
the import colour     every folder         JSON-LD, .ics        one event per commitment
                      every minute         text, HTML           the flag comes off
                      done → unflag        PDFs and images      Pushover push
                      failed → stays       read by Claude       or why not
```

## Why it is built this way

**You say which emails are events.** Nothing scans your inbox and nothing guesses. You
give an email one colour of flag from whichever device is in your hand, and that message
- only that message - is read. Deciding it yourself is simpler and more predictable than
a heuristic deciding for you. A flag rather than a mailbox because the email never has
to move: it stays filed wherever you already keep it, before and after.

**The flag is the queue.** There is no cursor and no window to re-scan: whatever carries
the flag is what is still to do. A message that was read has its flag taken off and is
left exactly where it stands, which is what finishing means; a message that failed keeps
its flag, which is what retrying means. Either way your phone is told - what was added,
or what went wrong and a link straight back to the email.

Bodies are read without marking anything as read, and the only write to the mailbox is
clearing the flag once the events are on your calendar.

**Extraction is tiered so the reliable sources win.** Airlines and ticketing platforms
embed [schema.org](https://schema.org/) JSON-LD in their HTML because Gmail and Outlook
read it, which hands over exact flight numbers, airport codes, and times with no
guessing. Failing that, a `.ics` attachment. Failing that, plain text, then rendered
HTML, then PDFs and images passed to the model as vision input - which is how boarding
passes and e-tickets get read.

**Timezones are resolved deterministically.** An explicit zone in the email beats an
offline IATA airport lookup, which beats a city match, which beats your configured time
zone. A Tokyo → Los Angeles flight gets `Asia/Tokyo` on the departure and
`America/Los_Angeles` on the arrival, and renders correctly in both.

**Locations are written as full addresses**, because a calendar geocodes the location
string and only shows a map, directions, and travel time when it resolves. The model
collects the address in parts - venue, street, city, region, postal code, country - from
wherever the email states them. Flights are filled in from the offline airport dataset
instead, so a flight from `LGA` gets `Laguardia Airport, New York, US` rather than an
airport code.

**Flagging the same email twice writes the same event.** Each event gets a UID derived
from the message and the event's identity, and that UID names the resource on the
server. A retry, or a flag put back on by hand, rewrites the event instead of adding a
second one.

**Everything is configured in the app.** There is no file to fill in before first boot.
You start it, open `/settings`, and type your Apple ID, your Anthropic key and your
Pushover keys into the Connections tab; each one is proved against its service before it
is kept, and takes effect at once. Credentials are stored in `data/secrets.env`, which
the app writes with mode `0600` and never reads back out into a page: the tab shows
whether each connection is set and offers a box to replace it, and **Remove** is the only
way to clear one.

Preferences - the flag colour, the calendar, the categories, the time zone - live in the
database instead. No value has two homes, so there is never a question of which one
wins.

The environment is still read, and outranked. Setting `ANTHROPIC_API_KEY` in the
container's environment or in a `.env` beside `docker-compose.yml` seeds it for a
deployment that has never had one typed in; the moment somebody saves one on the
settings page, that is the value.

**All of the state is one directory.** `data/` holds the SQLite database and that
secrets file, and nothing outside it survives the container. One volume to mount, one
thing to back up, and one thing to delete to start over.

## What you need

Three accounts, all typed into **Settings → Connections**, and nothing has to be in
place before the first boot.

### iCloud app-specific password

The mail loop signs in to `imap.mail.me.com` and the calendar writes go to
`caldav.icloud.com`, both as you, and **one app-specific password covers both**. With
two-factor authentication on, neither service accepts your Apple ID password.

At [appleid.apple.com](https://appleid.apple.com), **Sign-In and Security →
App-Specific Passwords → +**, name it `email-to-cal`, and paste the generated
`xxxx-xxxx-xxxx-xxxx` string into the **iCloud** row on the Connections tab, along with
the address you sign in with.

> Changing your Apple ID password **revokes every app-specific password**, this one
> included. Generate a new one, paste it into the same box and save.

Only one connection is ever held: iCloud allows about five per account, and your phone
and your Mac are already using some of them.

### A flag colour of its own

Pick one of Apple Mail's flag colours, give it to this app, and never use it for anything
else. The default is **blue**. There is nothing to create: the flag already exists on
every device you are signed in on.

To add an email to your calendar, flag it: on the iPhone, **open it, tap the More button,
tap Flag**, then pick the colour. On the Mac, select it and choose the colour from the
**flag button in the toolbar**. Within a minute the events are on your calendar, you get
a push naming them, and the flag comes off - the email itself does not move.

Every folder is searched on each pass, so it makes no difference where the mail is filed
or whether you file it after flagging. Junk, Deleted Messages, and Drafts are skipped.

Rename the flag in Mail on the Mac if you want your own word for it - **click the flag
name in the sidebar, click it again, and type**. That name is a label on that Mac and
never leaves it; the colour is what travels, and the colour is what this watches for.

> **Red is not on the list.** Apple encodes a flag's colour as up to three IMAP keywords,
> and red is the index they all leave unset - so a red flag is indistinguishable from a
> plain flag set by anything else. The other six are unambiguous.

### A calendar

Open the Calendar app on any of your devices and make an iCloud calendar - or use one you
already have - then pick it under **Settings → Preferences**. The list comes from your
account over CalDAV, so there is no name to type and nothing to spell wrong.

Every calendar the service writes to has to exist already: iCloud does not let a CalDAV
client create one.

### Anthropic API key

From [console.anthropic.com](https://console.anthropic.com), into the **Anthropic** row
on the Connections tab. Only the email you flag is ever read, so a personal mailbox costs
very little.

### Pushover

Pushes reach the phone through [Pushover](https://pushover.net), which is hosted, so
there is no notification server in the stack to keep alive.

1. Sign up at [pushover.net](https://pushover.net). Your **user key** is on the dashboard
   you land on.
2. Register this service as an application at
   [pushover.net/apps/build](https://pushover.net/apps/build) - a name is all it asks
   for - and copy the **API token** it hands back.
3. Install the Pushover app on the phone and sign in as the same user.

Both go in the **Pushover** row on the Connections tab. It is the one connection that is
optional: without it, a failure is only visible on the Email page.

## Run it

```sh
git clone https://github.com/sebastienstdenis/email-to-cal
cd email-to-cal
docker compose pull
docker compose up -d
```

There is nothing to fill in first. That is the whole install: one container, one volume,
one port. The portal has no login of its own, so the compose file binds it to
`127.0.0.1:8080`; on a remote server, reach it with `ssh -L 8080:localhost:8080
your-server`.

Open [http://localhost:8080/settings](http://localhost:8080/settings). It opens on
**Connections**, which lists what is still to do, in order. Work down it:

1. **iCloud.** Your Apple ID and the app-specific password you made above.
2. **Anthropic.** The API key.
3. **Pushover.** The application token and your user key.

Each row saves on its own and is proved before it is kept: a password iCloud rejects, or
a key Anthropic refuses, is said beside the button and not saved. Nothing you type is
ever shown back: a row that is already set shows an empty box that means *leave this
alone*, and **Remove** is what clears it.

Then finish on **Preferences**:

1. Pick the **calendar** events go to.
2. Pick the **flag colour**, `blue` by default, and decide not to use that colour for
   anything else.
3. Set the **time zone** used when an email does not say where the event happens.

**Advanced**, on the same tab, holds the log level.

Then flag an email.

If you would rather a deployment come up already knowing a credential, put it in the
container's environment, either through an `environment:` block or a `.env` beside
`docker-compose.yml`, using the names `ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD`,
`ANTHROPIC_API_KEY`, `PUSHOVER_TOKEN`, `PUSHOVER_USER_KEY`, `WATCHTOWER_URL` and
`WATCHTOWER_TOKEN`. That only ever seeds:
anything saved on the settings page wins over it from then on.

Pushing to `main` publishes a `linux/amd64` + `linux/arm64` image to
`ghcr.io/sebastienstdenis/email-to-cal:latest`, so updating is:

```sh
docker compose pull && docker compose up -d
```

Or from the app: the **Version** row under **Settings → Preferences → Advanced** says
which commit is running and whether the registry holds a newer one, and with Watchtower
in the stack it has an **Update** button. See [`docs/updates.md`](docs/updates.md).

### Running from a checkout

```sh
uv sync --all-groups
uv run email-to-cal serve
```

Open `http://127.0.0.1:8080/settings` and fill it in there, exactly as in the container.
It writes `./data/state.sqlite` and `./data/secrets.env` into the checkout rather than
into the container volume; `email-to-cal serve --host 0.0.0.0` if you want it off the
loopback.

## Categories

Everything lands on your calendar unless you say otherwise. A category routes a kind of
event to its own calendar, and the description is what the model reads - so it says what
belongs, not just what the category is called:

| Name | Description | Calendar |
| --- | --- | --- |
| travel | Flights, trains, and hotel stays. | Travel |
| music | Concerts and gigs I have tickets for. | Music |

An event that matches no category, or that comes back with a name you never configured,
goes to the main calendar. Categories are added one at a time under **Settings →
Preferences**, and each calendar named is proved against the account before the
category is kept.

## When it cannot be done

The flag stays on, and that is deliberate - a failed email stays visible exactly where
you left it.

- **Nothing to put on a calendar.** The model read the email and found no event. You get
  a push straight away, because asking again would get the same answer.
- **Something was down.** The calendar server, the model, the network. It is retried
  after two minutes, then after ten. If it still fails, you get a push and the service
  stops trying.

Either way the email keeps its flag and waits at the top of the **Email** page with
**Try again** and **Ignore** beside it. **Try again** hands it straight back to the
watcher, which reads it again on its next pass - as long as it still carries the flag.
**Ignore** is what takes the flag off without reading the email again. Unflagging it in
Mail drops it too, and an email once ignored is read again if you flag it again: the
flag came off with that decision, so one on it now is you overruling it.

Anything that stops the service - an expired app password, a rejected API key - arrives
as a high-priority push, and the Email page says so with a **Restart** button.

## Commands

| Command | Purpose |
|---|---|
| `email-to-cal serve` | The portal plus the watcher; how the container runs |
| `email-to-cal run` | The watcher alone |
| `email-to-cal check` | Exercise every external dependency, then exit |
| `email-to-cal replay a.eml` | Run one .eml through extraction and print the iCalendar; `--write` puts it on the calendar |
| `email-to-cal healthcheck` | Used by the container HEALTHCHECK |

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

### The stylesheet

The screens are built from [Basecoat](https://basecoatui.com), which is shadcn/ui as
plain HTML classes, so a card or a badge is the library's rather than something invented
here. `styles/app.css` is the source; `src/email_to_cal/static/email-to-cal.css` is the
compiled result and is committed, so a checkout and the image both run without Node.
After editing a template or the source stylesheet:

```sh
npm install
npm run build
```

The image is still pure Python: it copies the compiled file and never runs a build.

## Backups

**A copy of the `data` volume is a copy of your credentials**: `data/secrets.env` holds
your Apple ID and its app-specific password, your Anthropic key, and your Pushover token
and user key, all in plain text. The database beside it holds your preferences and the
record of what has been processed, and no secret. Treat the volume the way you would
treat the password itself.
