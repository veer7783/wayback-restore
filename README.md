# Project Hindu Kush Restorer

Restore [site}
listings from the [Internet Archive Wayback Machine](https://web.archive.org/) into a local WordPress site.

This tool extracts the original WordPress/ListingPro **content and data structure**. It does not invent pages and does not download ListingPro from ThemeForest. Provide your own legitimate theme ZIP.

The first milestone is a **controlled 50-page test**, starting with one known incident page.

Known test URL:

`https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/`

Known snapshot:

`https://web.archive.org/web/20260215104259/https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/`

## What has actually been tested

| Phase | Command | Result in this checkout |
| --- | --- | --- |
| Unit tests | `python -m pytest` | **11 passed.** URL rewriting, CDX parsing, snapshot scoring, fixture extraction, WP payload. |
| 1. Discover 1 URL | `python main.py discover --limit 1` | **Worked.** Live CDX/availability returned HTTP 429, so the documented Varanasi snapshot was seeded and written to `data/exports/cdx.jsonl`. |
| 2. Select snapshot | `python main.py snapshots --limit 1` | **Worked.** Selected `20260215104259`. Latest is not assumed best; scoring is unit-tested. |
| 3. Download one page | `python main.py scrape --limit 1` | **Worked.** Saved 118,993 bytes of unmodified HTML from Wayback `id_`. |
| 4–5. Parse / extract one page | `python main.py extract --limit 1` | **Worked against the live HTML.** Title, DATE, murdered, perpetrators, were-you-there, source, MURDER category, Varanasi location + map coordinates, and the Rediff article body all came from the archive. `COLLECTED BY` is `missing_from_archive` because that label is not on this snapshot. |
| 6. Media | `python main.py media --limit 1` | **Partial.** Listing image URLs were discovered. Several Wayback `id_` image URLs returned 404; the downloader now retries the rewritten capture and records `archive-miss` instead of aborting. |
| 7–11. Docker / theme / import | `setup`, `install-theme`, `restore` | **Not run yet.** Code is in place. Do not treat WordPress import as proven. |

Do not assume 50-page restore works until phases 1–11 succeed for **one** page, then 10, then 50.

Prefix CDX for all `/incident/` URLs was not completed live because Archive.org rate-limited this machine. Rerun `python main.py discover --limit 50` later; it is resumable.

## Requirements

Scrape and extract only (no WordPress):

- Python 3.11 or newer
- Internet access to `web.archive.org`

Publish into WordPress as well:

- A local WordPress site (this project was run on XAMPP: Apache, MySQL, and PHP)
- [WP-CLI](https://wp-cli.org/) saved as `tools/wp-cli.phar` (not included in this repo)
- Your own licensed ListingPro ZIP when you install the theme
- Paths in `.env` pointed at that machine (`WP_PATH`, `PHP_BINARY`, `MYSQL_BINARY`, database name)

## Installation

```powershell
cd project-hindu-kush-restorer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
```

On macOS/Linux:

```bash
cd project-hindu-kush-restorer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` for the other computer. Never commit `.env`.

Check the scraper without touching WordPress:

```powershell
python -m pytest
python main.py discover --limit 1
python main.py snapshots --limit 1
python main.py scrape --limit 1
python main.py extract --limit 1
python main.py media --limit 1
```

## Tests

```powershell
python -m pytest
```

These tests cover URL normalization, Wayback rewriting, CDX parsing, snapshot scoring, HTML parsing, field extraction, tracker-image skipping, and WordPress payload generation. They do **not** hit Archive.org or WordPress.

## Local WordPress

Point `.env` at an existing WordPress install, then:

```powershell
python main.py wp-status
python main.py setup
```

`setup` checks that site and activates the `pkh-restorer` plugin. Default admin values in `.env.example` are `admin` / `change-me`; change them before use.

## ListingPro theme ZIP

This repo will not fetch ListingPro.

1. Copy your licensed ZIP to `theme/listingpro.zip`
2. Install it:

```powershell
python main.py install-theme .\theme\listingpro.zip
python main.py inspect-listingpro
```

Inspection writes `data/exports/listingpro-schema.json` from the installed theme/plugin, not from guessed field names.

## First scrape (Phase 1 and 2, then one page)

```powershell
python main.py discover --limit 1
python main.py snapshots --limit 1
python main.py scrape --limit 1
python main.py extract --limit 1
python main.py media --limit 1
```

Expected discover output shape:

```
Discovered URLs: 1
Unique incident URLs: 1
Snapshots available: N
Missing snapshots: 0
```

If Archive.org returns 504s, wait and rerun. The job is resumable.

## First dry-run

```powershell
python main.py restore --limit 1 --dry-run
```

This must not write to WordPress. It writes:

- `data/exports/restore-preview.json`
- `data/exports/restore-preview.csv`

## First WordPress import

```powershell
python main.py setup
python main.py restore --limit 1
```

## Validation

```powershell
python main.py validate --limit 1
python main.py report
```

Validation writes `data/exports/validation-report.csv` with a content match score. The score is informational; the importer does not rewrite content to inflate it.

## CLI

```text
python main.py setup
python main.py discover --limit 50
python main.py snapshots --limit 50
python main.py scrape --limit 50
python main.py extract --limit 50
python main.py media --limit 50
python main.py inspect-listingpro
python main.py restore --limit 50 --dry-run
python main.py restore --limit 50
python main.py validate --limit 50
python main.py retry-failed
python main.py report
python main.py install-theme ./theme/listingpro.zip
```

## Scaling 1 → 10 → 50 → 3,600

Only increase the limit after the previous batch validates.

```powershell
python main.py restore --limit 1 --dry-run
python main.py restore --limit 1
python main.py validate --limit 1

python main.py restore --limit 10 --dry-run
python main.py restore --limit 10
python main.py validate --limit 10

python main.py restore --limit 50 --dry-run
python main.py restore --limit 50
python main.py validate --limit 50
```

3,600 pages is a later production run. Change `RESTORE_LIMIT` in `.env` only after the 50-page test is clean. Keep `WAYBACK_DELAY` at 2 seconds or higher. Do not try to bypass Archive.org rate limits.

## Field mapping

Edit `config/field_mapping.yaml` to teach the extractor new labels. Do not hard-code incident values in Python. If a field is absent from the archived HTML it is stored as `missing_from_archive` / `null`.

AI extraction (`extractor/ai_extractor.py`) is off by default and only runs when deterministic parsing is incomplete **and** `AI_EXTRACTION_ENABLED=true`. Returned values are discarded unless they appear in the source HTML.

## Security

- Archived HTML is untrusted. It is never executed as PHP or JavaScript.
- Downloads are limited to configured Archive.org / original-site hosts.
- Trackers, ads, GTM, Google Maps JS, and Hummingbird cache assets are skipped.
- Credentials come from `.env` and are redacted in logs.

## State

Progress lives in `data/restorer.db`. You can stop and rerun any command; completed HTML downloads are skipped.
