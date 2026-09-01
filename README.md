# besseleth

A weekly industry-briefing bot. Point it at an industry (e.g.
*neurotechnology*), and it:

- Pulls recent **arXiv** papers matching your keywords/categories (free, official API)
- Pulls **news** from RSS feeds — including a free Google News search feed by default (optionally NewsAPI.org too)
- Tracks a curated **conferences/events** watchlist you maintain in config
- Flags **LinkedIn** as a source with a compliant path (see below — no ToS-violating scraping)
- **Personalizes**: if an item mentions a company one of your contacts works at (e.g. a job posting at your friend's company), it's pulled into its own "For you specifically" section
- **Summarizes** each section with a free local LLM via [Ollama](https://ollama.ai) — falls back to a plain extractive summary if Ollama isn't running, so it never blocks
- Writes a Markdown report to `reports/`, and can email it via SMTP
- Dedupes across runs in a local SQLite DB, so re-running never repeats old items

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml: industry keywords, contacts, feeds, watchlist
```

Optional — for local LLM summaries:

```bash
# https://ollama.ai
ollama pull llama3.1
ollama serve
```

## Usage

```bash
.venv/bin/python -m besseleth.cli fetch    # scrape sources, store new items
.venv/bin/python -m besseleth.cli report   # summarize+render unreported items into a report
.venv/bin/python -m besseleth.cli run      # both, in one shot
```

Schedule it weekly with cron:

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
LinkedIn directly. `besseleth/scrapers/linkedin_scraper.py` is a stub with
instructions for wiring in a compliant source instead:

- LinkedIn's own Talent/Marketing APIs (official, partner-approved)
- A licensed data provider (e.g. Proxycurl, Coresignal)
- Company newsroom/blog RSS feeds as a free substitute — add these to
  `sources.news.feeds` in `config.yaml` and they'll flow through the normal
  news pipeline and personalization matching.

## Project layout

```
besseleth/
  config.py        # loads config.yaml
  db.py             # sqlite store + dedup
  personalize.py    # contact/company matching
  summarizer.py     # Ollama-backed (or extractive) summaries
  report.py         # markdown rendering + email
  pipeline.py        # orchestrates scrape -> personalize -> summarize -> report
  cli.py            # `fetch` / `report` / `run` commands
  scrapers/
    arxiv_scraper.py
    news_scraper.py
    conference_scraper.py
    linkedin_scraper.py   # stub — see above
```

## Notes

- All scraping is best-effort against free/public sources; outbound network
  access must be available to the process running this (some sandboxed dev
  environments restrict outbound hosts — this is unrelated to the code).
- `data/besseleth.db` and `reports/*.md` are local, gitignored artifacts.
