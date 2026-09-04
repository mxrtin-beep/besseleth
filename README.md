# besseleth

A weekly industry-briefing bot. Point it at an industry (e.g.
*neurotechnology*), and it:

- Pulls recent **arXiv** papers matching your keywords/categories (free, official API)
- Pulls **news** from RSS feeds — including a free Google News search feed by default (optionally NewsAPI.org too); add more from the dashboard's **Feeds** tab, no config-file editing needed
- Pulls **blogs** (company/lab blogs, researcher Substacks) from RSS — Substack needs no code, just its `/feed` URL; also addable from the Feeds tab
- Tracks a curated **conferences** watchlist, plus optional **conference news** (CFPs, accepted talks) via each conference's own RSS feed
- Finds **IRL events near you** — Luma calendars (iCal), Eventbrite organizers you follow, a curated local-meetup watchlist, and paste-in for anything else (see below — real geo-search APIs for events mostly don't exist for free, so paste-in is the reliable path)
- Pulls **Bluesky** posts (free public search API) and **X/Twitter** (paid API if you have a token, otherwise paste-in)
- Flags **LinkedIn** as a source with a compliant path (see below — no ToS-violating scraping)
- **Personalizes**: if an item mentions a company one of your contacts works at (e.g. a job posting at your friend's company), it's pulled into its own "For you specifically" section
- **Tracks industry trends**: a self-populating (auto-extracted, human-editable) dataset of devices/systems and their metrics — for neurotech: information transfer rate, implant longevity, electrode count, device type, material, and FDA status — plus a separate **companies** dataset for business metrics (funding, stock price — auto-refreshable for free for public tickers). Both accumulate forever, across every report; auto-added entries are tagged so they're distinct from ones you've verified.
- **Maps** the companies/labs behind your papers by location — geocoded free via OpenStreetMap, extracted by the same enrichment pass, no separate step; an org whose location was never mentioned in any item's own text gets a free web lookup (Wikidata) as a fallback, rather than staying unlocated forever.
- **Tracks job postings** at the companies/labs it's already found (from their Greenhouse/Lever/Ashby job-board API, not scraped HTML) — kept in sync so a closed posting is marked removed, not left stale; auto-detects each org's board with a manual override file (`job_boards.yaml`) for the ones it can't guess.
- **Summarizes** each section with a free local LLM via [Ollama](https://ollama.ai) — falls back to a plain extractive summary if Ollama isn't running, so it never blocks
- Writes a Markdown report to `reports/` on a configurable cadence (daily/weekly/monthly/whatever), and can email it — old reports are deletable, individually, from the dashboard
- Dedupes across runs in a local SQLite DB, so re-running never repeats old items; a **backfill** control lets you pull history further back than the usual lookback window
- Ships a **browser dashboard** (`besseleth.web.app`) — the whole app, really: read reports, explore devices/companies as an interactive adjustable-axis chart with a cited source on every point, and paste anything (LinkedIn, Bluesky/X, events, whatever) into one box that figures out what it is

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
cp devices.example.yaml devices.yaml       # for the trends feature — optional
cp companies.example.yaml companies.yaml   # for company/business tracking — optional
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
comes back on its own and stays out of the way while it does, use
`launchd`:

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
  <!-- Keeps it out of the way: macOS schedules Background-class + niced
       processes behind whatever you're actively using instead of
       competing with it for CPU, and LowPriorityIO does the same for
       disk. Combine with enrichment.pause_seconds and
       summarizer.num_thread in config.yaml (see config.example.yaml) so
       an enrich run is a trickle instead of a burst. -->
  <key>ProcessType</key><string>Background</string>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>/tmp/besseleth.log</string>
  <key>StandardErrorPath</key><string>/tmp/besseleth.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.besseleth.serve.plist
# stop it later with: launchctl unload ~/Library/LaunchAgents/com.besseleth.serve.plist
```

With this loaded, you never run `python -m besseleth.web.app` by hand —
launchd starts it at login and restarts it if it ever dies, so it's
always just running quietly at `http://localhost:5050` (or whatever port
you set) whenever you want to check the dashboard. Manually starting it
again yourself in a terminal on top of that would run two instances
against the same DB at once — don't do both.

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

Any item — scraped or pasted, from any source — whose text mentions
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
doing and when. Tabs:

- **Report** — the latest (or any past) report, rendered from Markdown;
  delete old ones from the sidebar.
- **Papers** — every arXiv/news/blog item besseleth has ever fetched, in
  one browsable table — not just this week's snapshot. Filter by date
  range, source, org, org type (industry/academic/government/nonprofit),
  modality, therapeutic target, and a minimum novelty score; sort by date
  or novelty. See "Papers table" below for what populates the columns.
- **Map** — the companies/labs behind those papers, plotted by location
  (free via OpenStreetMap), sized by how much has been fetched about
  each. See "Map" below.
- **Trends explorer** — devices and companies as an interactive chart
  (Plotly, client-side, no static PNGs) — a Devices/Companies toggle
  switches dataset. Pick any metric for the X axis — including **time**
  (`date_reported`), to see e.g. information transfer rate improving
  release over release, or a stock price/funding trend — and any metric
  for the Y axis; color devices by FDA status, org type, device type, or
  material. Hover a point to see its details; click it to open its cited
  `source_url` in a new tab. A plain table below repeats every point with
  human-readable metric tags and an explicit source link, so nothing here
  is a number without a citation.
- **Paste** — one box for LinkedIn/social/events/anything; see below.

Uses Plotly.js from a CDN (`cdn.jsdelivr.net`), so it needs internet
access once, in the browser, to load the chart library — the report tab
and device table work regardless. To run fully offline, download
[`plotly.js-dist-min`](https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.32.0/plotly.min.js)
to `besseleth/web/static/plotly.min.js` and change the `<script src=...>`
in `besseleth/web/templates/dashboard.html` to `/static/plotly.min.js`.

## What can I paste in? (LinkedIn, events, social — one box)

LinkedIn, IRL events, and social posts have no good free scraping/search
API, so all three share one mechanism you interact with as a **single
paste box**: the dashboard's **Paste** tab. Copy anything — a LinkedIn
post, a Bluesky/X post, a Luma/Eventbrite page, a Substack post, an
article, whatever — paste the text in (and its URL, if it's not already
in the pasted text), hit Add. besseleth looks at the URL's domain and
files it under the right source automatically:
`linkedin.com`→LinkedIn, `bsky.app`/`x.com`/`twitter.com`→social,
`lu.ma`/`eventbrite.com`/`meetup.com`→event, `substack.com`→blog,
`arxiv.org`→arXiv — anything else lands in a generic "📌 Clipped" section
rather than guessing wrong. You never have to pick which source it is;
just paste. The same thing works from the CLI: `besseleth.cli paste`
(reads stdin, or `--text`/`--url`), or force a specific source with
`linkedin-add`/`event-add`/`social-add` if you want to skip detection.

The Paste tab also lists everything you've pasted so far, newest first,
with a 🗑 to delete one outright — unlike a scraped item (which would just
come back on the next fetch), a paste is only ever removed if you remove
it. Same from the CLI: `besseleth.cli item-delete --item-id <id>`.

**Browser extension**: `extension/` is a one-click clipper — select text
on any page (LinkedIn, X, Bluesky, wherever) and a "+ besseleth" button
appears; click it and the selection + page URL go straight to `/api/paste`,
no copy-paste round trip through the dashboard. See `extension/README.md`
for installing it — a couple of clicks in `chrome://extensions` on
Chrome/Edge/Brave; on Safari it's an Xcode-wrapped build (Safari 16.4+ /
macOS 13.3+), same source, different packaging step — instructions for
both are there.

**Avoiding duplicate coverage**: a pasted item goes into the same pool as
everything scraped, so if you paste a LinkedIn post about a funding round
that a news feed also picked up, besseleth notices the overlap (by title
similarity, across all sources, not just within one) before building the
report and merges them into a single entry — keeping the more detailed
summary, folding in anything materially different from the other, and
noting `(Also reported via: linkedin)` so the merge is visible rather
than one version silently vanishing. See `besseleth/dedupe.py`.

Prefer files over a browser tab open? The same content also works dropped
as `.txt`/`.md` files into `linkedin_drops/`, `event_drops/`, or
`social_drops/` (one dropbox per source, since a file has no "paste box"
to auto-detect from) — one snippet per file, or several separated by a
line containing only `---`. There's no required format (free text is
fine), but a snippet parses best like this:

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

## Industry trends (ITR, longevity, electrode count, device type, material, FDA status — and business metrics)

`devices.yaml` and `companies.yaml` (copy from their `.example.yaml`
files) hold devices/systems and companies in your industry and their
metrics, accumulating forever — nothing here is ever reset by a later
report. **Both build themselves automatically**: the same enrichment
pass that fills in the Papers table (see below) also drafts an entry
whenever an item reports concrete numbers, and appends it —

```yaml
- name: "Synchron CNS implant"
  org: "Synchron"
  org_type: "industry"
  fda_status: "unknown"
  metrics:
    information_transfer_rate: 15
    electrode_count: 16
    device_type: "CNS implant"
  source_url: "https://example.com/synchron-funding"   # the actual item, never a homepage
  date_reported: "2026-08-01"
  notes: "Auto-extracted by besseleth from a scraped item — verify before trusting."
  auto_extracted: true
```

— tagged `auto_extracted: true` (shown as an **auto** badge in the
dashboard) so it's visibly distinct from something you've reviewed, and
**additive only**: if an entry for that name+org already exists,
nothing gets appended or overwritten — auto-extraction only ever adds
new rows, so a number you've corrected by hand stays corrected. Every
entry keeps `source_url` pointing at the actual item, so a wrong
auto-extracted number is easy to spot-check.

Nothing here is invented — the LLM is instructed to use `null`/omit
rather than guess, and a metric that isn't concretely reported in the
text just doesn't get added. That said, an LLM extracting numbers from
prose is still fallible; treat `auto_extracted: true` entries as a draft,
not ground truth, until you've spot-checked them (or flip the field to
`false` once you have). If you'd rather review every entry *before* it's
added instead of after, `device-suggest` remains available as the
manual, nothing-written-until-you-paste-it-in path:

```bash
.venv/bin/python -m besseleth.cli device-suggest --item-id <id-from-report-or-db>
```

The metric keys are whatever `industry.trend_metrics`/`company_metrics`
in `config.yaml` define — the neurotech defaults cover information
transfer rate, implant longevity, electrode count, device type,
material, and FDA status, but swap in whatever your industry measures
(e.g. `battery_life_hours`, `accuracy_pct`). Stock price for any company
with a `stock_ticker` set refreshes for free:

```bash
.venv/bin/python -m besseleth.cli company-refresh-stock
```

Every `run` renders a scatter chart per pair of numeric metrics (colored
by FDA status, shaped by industry vs. academic) plus an FDA-status bar
chart, embeds them in the report, and lists every tracked device/company
in a table with human-readable metric tags (not raw `key=value` pairs)
and its cited source. The dashboard's **Trends explorer** tab is the
live, interactive version of the same two datasets — a Devices/Companies
toggle, adjustable X/Y axes (including **time**, to watch a metric
improve release over release), color by any categorical field, click a
point to open its source.

## Papers table (filter by date, org, modality, therapeutic target, novelty)

Unlike the weekly report (a rolling snapshot of what's new), the
dashboard's **Papers** tab is a standing index of every arXiv/news/blog
item besseleth has ever fetched, filterable and sortable. After each
fetch, besseleth asks the local LLM to tag every new item with:

- **org** — the company/lab/institution the item is about
- **org_type** — industry / academic / government / nonprofit / unknown
- **modality** — EEG, ECoG, CNS implant, PNS implant, EMG, fMRI, fNIRS,
  or another short label if none fit
- **therapeutic_target** — what it addresses: motor, speech, vision,
  hearing, memory, mood/psychiatric, epilepsy, pain, other
- **novelty_score** (1-5) — how surprising the item is **compared to
  other recent items on the same topic** (besseleth pulls a handful of
  similar items from the DB and includes them in the prompt so the score
  is relative, not just "does this sound impressive in isolation"),
  with a one-sentence rationale shown on hover

This is bounded per fetch (`enrichment.max_items_per_run`, default 20) so
one fetch cycle can't trigger unbounded LLM calls — it catches up over
successive fetches if there's a backlog, or run it against everything at
once:

```bash
.venv/bin/python -m besseleth.cli enrich --all
```

...or hit **Enrich now** on the Papers tab. Requires
`summarizer.backend: "ollama"` to actually extract anything (Ollama
running) — without it, items are marked `org_type: unknown` etc. rather
than left unprocessed forever, since there's nothing more to learn
without an LLM. The vocab above is a *suggestion* in the prompt, not a
hard enum, so an unusual paper isn't forced into the wrong bucket — the
filter dropdowns are populated from whatever values actually show up.

## Map

The same enrichment pass also asks for a **location** — "City, Country"
for the org's relevant site/HQ, only filled in when it's actually stated
or clearly implied by the text, never guessed. When present, it's
geocoded for free via [OpenStreetMap/Nominatim](https://www.openstreetmap.org/copyright)
(rate-limited to ~1 request/second per Nominatim's usage policy, cached
in `.geocode_cache.json` so a place is only ever looked up once) and
stored on the item.

The dashboard's **Map** tab plots one marker per company/lab — sized by
how much has been fetched about it, colored by org type — over a world
map (client-side Plotly, like the trends charts). Click a marker to jump
to the Papers tab pre-filtered to that org. A plain table beneath lists
the same data for when you'd rather scan than pan/zoom. Locations aren't
retroactive — items enriched before this feature won't have one until
you re-run `besseleth.cli enrich --all`.

## Backfilling history

Each source's `days_back` in config.yaml controls its normal lookback
window (default ~8 days) — enough for the recurring schedule, but thin
context right after setup or after being away a while. Backfill further
back on demand: the dashboard's status bar has a date picker + "Backfill"
button, or from the CLI —

```bash
.venv/bin/python -m besseleth.cli fetch --since 2026-01-01
```

This only widens each source's lookback for that one run (never narrows
it below the configured default) and stores whatever new items it finds
in the same DB — it doesn't touch devices.yaml/companies.yaml, which stay
hand-maintained.

## Reports: cadence and cleanup

`schedule.report_cron` (in `config.yaml`) is a plain 5-field cron
expression, so the report cadence is whatever you want, not just weekly:

```yaml
schedule:
  report_cron: "0 8 * * *"      # daily
  report_cron: "0 8 * * MON"    # weekly (default)
  report_cron: "0 8 1 * *"      # monthly
  report_cron: "0 8 1 1 *"      # yearly
```

Reports accumulate in `reports/` — each is its own dated file, never
overwritten. Delete one you don't need from the dashboard sidebar (🗑 next
to its date) or `besseleth.cli report-delete <report-id>`. To prune
automatically instead, set `reports.keep_last: N` in config.yaml to keep
only the N most recent (0/omitted = keep everything).

## Project layout

```
besseleth/
  config.py          # loads config.yaml
  db.py              # sqlite store + dedup
  personalize.py     # contact/company matching
  summarizer.py      # Ollama-backed (or extractive) summaries
  report.py          # markdown rendering + email
  pipeline.py        # orchestrates scrape -> personalize -> summarize -> report
  cli.py             # fetch / report / run / serve / *-add / *-delete / enrich commands
  scheduler.py        # background fetch/report jobs (used by both web.app and cli serve)
  enrich.py            # LLM tagging (org, modality, target, location, novelty) + auto-populates devices/companies.yaml
  geocode.py            # free OpenStreetMap/Nominatim geocoding, rate-limited + cached, for the Map tab
  dedupe.py            # collapses near-duplicate items across sources before they hit the report
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
    store.py             # devices.yaml load/save + auto-append + manual device-suggest
    company_store.py     # companies.yaml load/save + auto-upsert + free stock-price refresh
    format.py            # human-readable metric formatting (shared by report + web)
    plot.py               # static matplotlib charts embedded in the report
  web/
    app.py                # Flask dashboard: reports, papers, map, trends, paste box, scheduler status
    templates/dashboard.html
extension/                # browser extension: select text anywhere -> POST /api/paste (see extension/README.md)
```

## Notes

- All scraping is best-effort against free/public sources; outbound network
  access must be available to the process running this (some sandboxed dev
  environments restrict outbound hosts — this is unrelated to the code).
- `data/besseleth.db` and `reports/*.md` are local, gitignored artifacts.
