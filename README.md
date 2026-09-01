# besseleth

A weekly industry-briefing bot. Point it at an industry (e.g.
*neurotechnology*), and it:

- Pulls recent **arXiv** papers matching your keywords/categories (free, official API)
- Pulls **news** from RSS feeds — including a free Google News search feed by default (optionally NewsAPI.org too)
- Pulls **blogs** (company/lab blogs, researcher Substacks) from RSS — Substack needs no code, just its `/feed` URL
- Tracks a curated **conferences** watchlist, plus optional **conference news** (CFPs, accepted talks) via each conference's own RSS feed
- Finds **IRL events near you** — Luma calendars (iCal), Eventbrite organizers you follow, a curated local-meetup watchlist, and paste-in for anything else (see below — real geo-search APIs for events mostly don't exist for free, so paste-in is the reliable path)
- Pulls **Bluesky** posts (free public search API) and **X/Twitter** (paid API if you have a token, otherwise paste-in)
- Flags **LinkedIn** as a source with a compliant path (see below — no ToS-violating scraping)
- **Personalizes**: if an item mentions a company one of your contacts works at (e.g. a job posting at your friend's company), it's pulled into its own "For you specifically" section
- **Tracks industry trends**: a hand-maintained (optionally LLM-drafted) dataset of devices/systems and their metrics — e.g. for neurotech, information transfer rate, implant longevity, and FDA status — charted automatically into the report
- **Summarizes** each section with a free local LLM via [Ollama](https://ollama.ai) — falls back to a plain extractive summary if Ollama isn't running, so it never blocks
- Writes a Markdown report to `reports/`, and can email it via SMTP
- Dedupes across runs in a local SQLite DB, so re-running never repeats old items
- Ships a **browser dashboard** (`besseleth.web.app`) to read reports and explore the trends dataset as an interactive, adjustable-axis chart with a source link on every data point

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
cp devices.example.yaml devices.yaml   # for the trends feature — optional
# edit config.yaml: industry keywords, contacts, feeds, watchlists
```

Optional — for local LLM summaries:

```bash
# https://ollama.ai
ollama pull llama3.1
ollama serve
```

## Usage

besseleth is meant to run **continuously**, not as a one-off command you
have to remember to re-run. The easiest way: start the dashboard and leave
it running —

```bash
.venv/bin/python -m besseleth.web.app
```

— it fetches sources on `schedule.fetch_interval_hours` (default: every 6h)
and renders a report on `schedule.report_cron` (default: Monday 8am UTC)
for as long as the process is alive, no cron needed. Adjust both in
`config.yaml`'s `schedule` section. The dashboard's status bar shows when
it last ran and when it's next due, and has a **Run now** button for an
immediate fetch+report outside the schedule.

For a headless box (no browser), the same schedule runs without the web UI:

```bash
.venv/bin/python -m besseleth.cli serve
```

**Keeping it running on a Mac** — `nohup` is fine for a quick session, but
survives a terminal close, not a reboot or logout. For something that
comes back on its own, use `launchd`:

```xml
<!-- ~/Library/LaunchAgents/com.besseleth.serve.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.besseleth.serve</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/besseleth/.venv/bin/python</string>
    <string>-m</string><string>besseleth.web.app</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/besseleth</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/besseleth.log</string>
  <key>StandardErrorPath</key><string>/tmp/besseleth.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.besseleth.serve.plist
# stop it later with: launchctl unload ~/Library/LaunchAgents/com.besseleth.serve.plist
```

Prefer a one-shot, cron-triggered run instead of a standing process? That
still works — set `schedule.enabled: false` in `config.yaml` and:

```bash
.venv/bin/python -m besseleth.cli fetch    # scrape sources, store new items
.venv/bin/python -m besseleth.cli report   # summarize+render unreported items into a report
.venv/bin/python -m besseleth.cli run      # both, in one shot
```

```
0 8 * * MON  cd /path/to/besseleth && .venv/bin/python -m besseleth.cli run >> besseleth.log 2>&1
```

## Personalization

Add contacts to `config.yaml`:

```yaml
contacts:
  - name: "Jane Doe"
    company: "Neuralink"
    role: "Research Scientist"
```

Any scraped item (news, arXiv, or conference entry) whose text mentions
`Neuralink` gets flagged with `matched_contact: Jane Doe` and surfaced first
in the report, with an LLM-written one-liner on why it might matter — this
is how a job posting at a friend's company gets called out specifically.

## On LinkedIn

LinkedIn's Terms of Service prohibit automated scraping of linkedin.com and
it actively blocks/detects bot traffic, so this project **does not** scrape
LinkedIn directly. Instead, pick whichever of these fits:

**1. Paste/upload it yourself (default, free, zero setup)**

When you spot something worth tracking on LinkedIn — a job posting, a
contact's post, a company update — copy the text and either:

```bash
# drop a file (one snippet per file, or several separated by a "---" line)
mkdir -p linkedin_drops
echo "Neuralink - Research Scientist
https://www.linkedin.com/jobs/view/1234567890
We're hiring a research scientist for our neural interfaces team..." > linkedin_drops/note.txt

# or paste directly, no file needed
.venv/bin/python -m besseleth.cli linkedin-add
# (paste the text, then Ctrl-D)
```

Either way it's picked up on the next `fetch`/`run`, matched against your
`industry.keywords`, checked against your contacts' companies, and folded
into the weekly report — a pasted job posting at a friend's company gets
flagged in "For you specifically" exactly like a scraped one would.
Processed drop files move to `linkedin_drops/processed/` so re-running
doesn't re-ingest them.

**2. A licensed data provider** — Proxycurl, Coresignal, or similar
(pay-as-you-go API key). Implement the call in
`besseleth/scrapers/linkedin_scraper.py::fetch()`.

**3. LinkedIn's own Talent/Marketing APIs** — official, requires partner
approval: https://learn.microsoft.com/en-us/linkedin/

**4. Company newsroom/blog RSS feeds** — a free substitute for "what's
happening at company X". Add these to `sources.news.feeds` in
`config.yaml` and they flow through the normal news pipeline.

## Browser dashboard

```bash
.venv/bin/python -m besseleth.web.app
# -> http://127.0.0.1:5050
```

Local-only (no auth, don't expose it on the open internet as-is). Unlike
the CLI, this doesn't need a `besseleth.cli run` first — starting it also
starts the background schedule (see Usage above), so it fetches and
reports on its own from here on; the status bar up top shows what it's
doing and when. Two tabs:

- **Report** — the latest (or any past) weekly report, rendered from
  Markdown.
- **Trends explorer** — the device dataset as an interactive chart
  (Plotly, client-side, no static PNGs): pick any metric for the X axis
  — including **time** (`date_reported`), to see e.g. information transfer
  rate improving release over release — and any metric for the Y axis,
  color by FDA status or industry/academic. Hover a point to see its
  device/org; click it to open its `source_url` in a new tab. A plain
  table below repeats every point with an explicit source link, so
  nothing here is a number without a citation.

Uses Plotly.js from a CDN (`cdn.jsdelivr.net`), so it needs internet
access once, in the browser, to load the chart library — the report tab
and device table work regardless. To run fully offline, download
[`plotly.js-dist-min`](https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.32.0/plotly.min.js)
to `besseleth/web/static/plotly.min.js` and change the `<script src=...>`
in `besseleth/web/templates/dashboard.html` to `/static/plotly.min.js`.

## What can I paste in? (LinkedIn, events, social)

Three sources have no good free scraping/search API, so they all share the
same paste/upload mechanism (`besseleth/scrapers/manual_drop.py`):
**LinkedIn** (`linkedin_drops/`), **IRL events** (`event_drops/`), and
**social posts** (`social_drops/`).

For each, drop a `.txt` or `.md` file into the folder — one snippet per
file, or several separated by a line containing only `---`. There's no
required format (free text is fine), but a snippet parses best like this:

```
<title — first line, e.g. company/event/post name>
<optional second line — date, location, author, whatever>
<a URL, anywhere in the snippet>
<the rest — description/body text>
```

The **title** is just the file's first non-empty line, the **URL** is the
first `http(s)://` found anywhere in the snippet, and the **body** is the
whole snippet — that's it. Any plain-text copy-paste works: from the
browser, a forwarded email, OCR output, whatever's convenient. Concretely:

```bash
# a LinkedIn job posting
cat > linkedin_drops/note.txt << 'EOF'
Neuralink - Research Scientist, Neural Interfaces
San Francisco, CA · Posted 2 days ago
https://www.linkedin.com/jobs/view/1234567890
We're hiring a research scientist to join our neural interfaces team...
EOF

# a Luma event page
cat > event_drops/meetup.txt << 'EOF'
Neurotech SF Meetup — October Demo Night
Thu, Oct 9 · 6:00 PM PDT · San Francisco, CA
https://lu.ma/neurotech-sf-oct
Monthly meetup for neurotech founders and engineers. This month: live
BCI demos from three local startups.
EOF

# a tweet/Bluesky post
cat > social_drops/post.txt << 'EOF'
@some_researcher: "Just posted our new results on closed-loop DBS..."
https://x.com/some_researcher/status/1234567890
EOF
```

Everything you drop is picked up on the next `fetch`/`run`, matched
against `industry.keywords`, checked against your contacts' companies for
personalization, and folded into the weekly report. Processed files move
to `<dropbox>/processed/` so re-running never re-ingests them. Prefer not
to touch the filesystem? Use the CLI instead — same result, no file:

```bash
.venv/bin/python -m besseleth.cli linkedin-add   # then paste, Ctrl-D
.venv/bin/python -m besseleth.cli event-add
.venv/bin/python -m besseleth.cli social-add
```

## IRL events near you

Real "find events near me" search APIs mostly don't exist for free
anymore:

- **Eventbrite** removed its public general-search endpoint in 2019. A
  free personal token can still list events for **organizers you follow**
  (`sources.events.eventbrite.organizer_ids` + `EVENTBRITE_TOKEN` env var)
  — not a geo search, but useful if you already know who to watch.
- **Luma** has no search API, but does publish a free `.ics` calendar feed
  per calendar you follow — grab the link from a calendar's "Subscribe"
  button and add it to `sources.events.luma.luma_calendar_ical_urls`.

For actual geo-discovery, browse Luma/Eventbrite/Meetup yourself (they're
good at this on their own sites) and paste anything worth tracking into
`event_drops/` — see above. Also maintain a `sources.events.watchlist` in
config for recurring meetups/series you already know about, same idea as
the conferences watchlist.

## Industry trends (e.g. neurotech: ITR, longevity, FDA status)

`devices.yaml` (copy from `devices.example.yaml`) is a small, hand-maintained
dataset of devices/systems in your industry and their metrics:

```yaml
- name: "Neuralink N1"
  org: "Neuralink"
  org_type: "industry"          # "industry" or "academic"
  fda_status: "Breakthrough Device Designation"
  metrics:
    information_transfer_rate: 40   # bits/min
    longevity_days: 730
  source_url: "https://neuralink.com/"
  date_reported: "2025-06-01"
```

The metric keys are whatever `industry.trend_metrics` in `config.yaml`
defines — the neurotech example ships with information transfer rate,
implant longevity, and FDA status, but swap in whatever your industry
measures (e.g. `battery_life_hours`, `accuracy_pct`). Every `run` renders
a scatter chart per pair of numeric metrics (colored by FDA status, shaped
by industry vs. academic) plus an FDA-status bar chart, embeds them in the
report, and lists every tracked device in a table.

This is intentionally **not** fully automated — extracting a real number
like "62 bits/min" reliably out of a paper's prose is exactly the kind of
thing that goes quietly wrong unsupervised, and a trend chart is something
you want to trust. Instead, `device-suggest` gives you a head start: point
it at a scraped item and the local LLM drafts a `devices.yaml` entry for
you to review and paste in — nothing is written automatically.

```bash
.venv/bin/python -m besseleth.cli device-suggest --item-id <id-from-report-or-db>
```

## Project layout

```
besseleth/
  config.py          # loads config.yaml
  db.py              # sqlite store + dedup
  personalize.py     # contact/company matching
  summarizer.py      # Ollama-backed (or extractive) summaries
  report.py          # markdown rendering + email
  pipeline.py        # orchestrates scrape -> personalize -> summarize -> report
  cli.py             # fetch / report / run / serve / *-add / device-suggest commands
  scheduler.py        # background fetch/report jobs (used by both web.app and cli serve)
  scrapers/
    arxiv_scraper.py
    news_scraper.py
    blog_scraper.py
    conference_scraper.py    # watchlist + per-conference news feeds
    events_scraper.py        # Luma ical, Eventbrite organizers, watchlist, paste-in
    social_scraper.py        # Bluesky (free API), X (paid API or paste-in)
    linkedin_scraper.py      # paste-in — see above
    manual_drop.py           # shared paste/upload mechanism
  trends/
    store.py           # devices.yaml load/save + LLM-drafted suggestions
    plot.py             # static matplotlib charts embedded in the report
  web/
    app.py              # Flask dashboard: report viewer + interactive trends
    templates/dashboard.html
```

## Notes

- All scraping is best-effort against free/public sources; outbound network
  access must be available to the process running this (some sandboxed dev
  environments restrict outbound hosts — this is unrelated to the code).
- `data/besseleth.db` and `reports/*.md` are local, gitignored artifacts.
