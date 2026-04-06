# Article Agent — Workflow Reference

A multi-persona news digest agent that fetches articles, summarises them with a local LLM (Ollama), and delivers daily briefs to Telegram.

---

## Overview

```
RSS Feeds  ──┐
             ├──> fetch + dedupe (cross-run) ──> Ollama LLM ──> Telegram
Hacker News ─┘
```

Each run:
1. Pulls articles from configured RSS feeds and Hacker News.
2. Deduplicates within the batch **and** against a rolling 7-day history of already-sent items.
3. Sends the deduplicated list to Ollama for summarisation using a persona-aware prompt.
4. Validates output shape (sections, bullets, links), retries once if invalid, then falls back to linked digest if configured.
5. Formats and delivers the summary to a Telegram channel.
6. Persists the sent item keys so they are skipped in future runs.

---

## Architecture

```
src/
  ai_digest_agent.py   — main agent (fetching, dedup, LLM, Telegram, scheduling)

config/
  personas/
    ai.yaml            — AI persona config (feeds, HN topics, LLM settings)
    law.yaml           — Law persona config
    ai_skills.md       — AI output template + quality checklist
    law_skills.md      — Law output template + quality checklist

logs/
  agent.log            — rotating app log (INFO+)
  digest_runs.jsonl    — append-only run audit trail (JSON Lines)
  seen_items_ai.json   — cross-run dedup history for AI persona
  seen_items_law.json  — cross-run dedup history for Law persona
```

---

## Detailed Data Flow

### 1. Fetch

**RSS** (`fetch_rss_items`):
- Reads `rss_feeds` list from the persona YAML.
- Uses `feedparser` to parse each feed; extracts up to 8 items per feed.
- Fields extracted: `source`, `title`, `link`, `published`, `summary` (HTML-stripped).

**Hacker News** (`fetch_hn_items`):
- Only runs when `hacker_news.include: true` in the persona YAML.
- Queries the Algolia HN API for stories matching each configured `topic`.
- Returns stories from the last 24 hours based on `created_at_i` window.

### 2. Deduplicate

**`dedupe_items(items, history_keys)`**:

```
For each item:
  key = item_dedupe_key(item)
  where key uses canonicalized URL when available
  and a normalized title fallback when URL is missing
  if key in seen_this_run OR key in history_keys → skip
  else → include, mark seen
Return up to max_items (default 35)
```

**Cross-run history** (`load_seen_keys` / `save_seen_keys`):
- Stored in `logs/seen_items_{persona}.json`.
- Each key is timestamped; entries older than 7 days are pruned on every load.
- Keys are saved **only after a successful Telegram send**, so a failed run does not permanently suppress items.

This means an article that appeared in Monday's digest will not appear again until next Monday (7-day TTL), even if it is still live in the RSS feed.

### 3. Summarise

**`summarize_with_ollama(items, config, persona)`**:
- Takes the first 15 deduped items.
- Builds a structured prompt (`build_summarization_prompt`) with:
  - Persona name, brief description, and style requirements from the YAML.
  - Output section headings from the YAML.
  - The output template extracted from the `*_skills.md` file.
- Posts to `http://localhost:11434/api/chat` (Ollama local server).
- Uses `temperature: 0.2` for deterministic output.
- Timeout: 60 seconds. Falls back to a "fetched N items, analysis unavailable" message on error.

### 4. Validate, Retry, and Fallback

**`summarize_with_quality_controls(items, config, persona)`**:
- Validates generated output with `validate_summary_output` against persona section definitions.
- Checks that each expected section exists and has bullets.
- Uses bullet bounds when section labels include counts (for example `5-8 bullets`).
- Enforces a `Link:` line in each bullet block when enabled.
- Retries generation up to `persona.validation.max_retries`.
- If still invalid and enabled, falls back to `build_linked_digest`.

**Model selection** (resolved in priority order):
1. `--model` CLI flag (for this run only).
2. `{PERSONA_UPPER}_OLLAMA_MODEL` environment variable.
3. `OLLAMA_MODEL` environment variable.
4. Default: `llama3.1:8b`.

### 5. Format and Send

**`prepare_single_message(text, max_len=3400)`**:
- Normalises blank lines, de-indents 4-space blocks to 2-space.
- Truncates at the last newline before `max_len` and appends "(Shortened for single-message delivery.)".

**`send_telegram(text, token, chat_id)`**:
- Posts to `https://api.telegram.org/bot{token}/sendMessage`.
- Web page previews disabled for cleaner Telegram display.
- Timeout: 30 seconds.

### 6. Audit Logging

Every run appends two records to `logs/digest_runs.jsonl`:

| Field | Values |
|-------|--------|
| `status` | `started`, `sent`, `failed`, `started_compare`, `sent_compare` |
| `items_deduped` | Count of items after deduplication |
| `rss_items` / `hn_items` | Raw counts before dedup |
| `dedupe_skipped` / `persona_filtered` | Count of items dropped by dedupe and keyword filters |
| `quality_status` | `validated`, `fallback_linked_digest`, etc. |
| `quality_errors` | Validation failures captured for diagnosis |
| `telegram_ok` | `true` / `false` |
| `telegram_message_id` | Telegram message ID on success |
| `error` | Error message on failure (first 500 chars) |

---

## Configuration

### Environment Variables (`.env`)

```env
# Required
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat_id>

# Persona-specific overrides (take precedence over global)
AI_TELEGRAM_BOT_TOKEN=<token>
AI_TELEGRAM_CHAT_ID=<chat_id>
LAW_TELEGRAM_BOT_TOKEN=<token>
LAW_TELEGRAM_CHAT_ID=<chat_id>

# Optional
OLLAMA_MODEL=llama3.1:8b
OLLAMA_BASE_URL=http://localhost:11434
AI_INTERVAL_HOURS=8
LAW_INTERVAL_HOURS=12
LOG_LEVEL=INFO
```

### Persona YAML Keys

| Key | Purpose |
|-----|---------|
| `persona.name` | Display name used in digest header |
| `persona.system_prompt` | LLM system role message (defines persona scope) |
| `persona.brief_description` | Framing injected into the user prompt |
| `persona.style_requirements` | List of bullet-point style rules for the LLM |
| `persona.output_sections` | Section headings the LLM must produce |
| `persona.output_template_file` | Path to `*_skills.md` for structural guidance |
| `persona.max_words` | Word limit passed to the LLM |
| `persona.single_message_max_chars` | Truncation limit for Telegram (default 3400) |
| `persona.validation.enabled` | Enable or disable output validation |
| `persona.validation.max_retries` | Retry count after validation failure |
| `persona.validation.require_link_per_bullet` | Require `Link:` line in every bullet block |
| `persona.validation.fallback_to_linked_digest` | Fallback to linked digest when validation keeps failing |
| `persona.require_keywords` / `persona.exclude_keywords` | Optional pre-LLM inclusion/exclusion filters |
| `rss_feeds` | List of RSS feed URLs |
| `hacker_news.include` | Whether to query Hacker News |
| `hacker_news.topics` | Search terms for HN Algolia API |

---

## Scheduling

### Windows Task Scheduler (recommended)

```powershell
# Register a recurring task (Admin may be required for "run when not logged in")
.\setup_scheduler.ps1 -TaskName "AIDigestBot" -Persona "ai"

# Manage the task
.\manage_scheduler.ps1 -Action status  -Persona ai
.\manage_scheduler.ps1 -Action on      -Persona ai
.\manage_scheduler.ps1 -Action off     -Persona ai
.\manage_scheduler.ps1 -Action run     -Persona ai     # trigger immediately
.\manage_scheduler.ps1 -Action monitor -Persona ai     # health check
```

### n8n

Import `n8n_workflow.json` (AI) or `n8n_workflow_law.json` (Law).  
The workflows trigger every 8 hours and send a Telegram alert if the agent exits with a non-zero code.

### Built-in APScheduler

```bash
python src/ai_digest_agent.py --persona ai
```

Runs every `AI_INTERVAL_HOURS` hours (default 8) using APScheduler's `BlockingScheduler`.

---

## Common Commands

```bash
# One-shot run (manual trigger)
python src/ai_digest_agent.py --persona ai --once

# One-shot with a specific model
python src/ai_digest_agent.py --persona ai --once --model gemma4:12b

# A/B model comparison (sends two digests to Telegram)
python src/ai_digest_agent.py --persona ai --once --model llama3.1:8b --compare-model gemma4:12b

# Test individual components
python test_components.py
```

---

## Deduplication — How It Works

### Within a single run (in-memory)

Items with the same canonical URL (or normalized title if no URL) are collapsed before reaching the LLM.

### Across runs (persistent, 7-day rolling window)

After every successful Telegram send, the agent writes `logs/seen_items_{persona}.json`:

```json
{
  "keys": {
    "https://example.com/article-1": "2025-04-01T08:00:00+00:00",
    "https://example.com/article-2": "2025-04-01T08:00:00+00:00"
  },
  "updated_utc": "2025-04-01T08:00:05+00:00"
}
```

On the next run, these keys are loaded and any matching item is excluded **before** the LLM call.  
Entries are pruned when they exceed 7 days old (`SEEN_ITEMS_TTL_DAYS = 7`).

**What this solves:**  
RSS feeds often keep the same articles live for days or weeks.  
Without cross-run deduplication, the same article could appear in every digest.

**What this does not solve:**  
Semantically similar articles published under different URLs (e.g., the same story covered by two outlets) are still treated as distinct items. The LLM's style requirements include "remove near-duplicates" to mitigate this in the output.

---

## Categorisation — How It Works

There is no rule-based classifier. The LLM categorises items into sections based on:

1. **System prompt** — defines the persona's domain and explicitly tells the LLM to skip off-topic items.
2. **`style_requirements`** in the YAML — bullet-point rules the LLM must follow.
3. **`output_sections`** in the YAML — the exact section headings the LLM must produce.
4. **Output template** in `*_skills.md` — structural example the LLM mirrors.

### Reducing miscategorisation

There is now optional pre-LLM filtering via persona config:
- `persona.require_keywords`
- `persona.exclude_keywords`

These are applied in `apply_persona_filters` before summarisation.

The system prompt for each persona now explicitly instructs the LLM to:
- Include **only** items relevant to its domain (AI/ML for the AI persona; law/courts for the Law persona).
- Skip items from general-news sources that lack a clear domain angle.
- Omit empty sections rather than adding filler.

If miscategorisation persists, strengthen `persona.system_prompt` in the relevant YAML or add more specific exclusion rules to `style_requirements`.

### Skills folder guidance (best practice)

Use a hybrid model:
- Keep `*_skills.md` files for writing style and editorial voice.
- Enforce critical constraints in structured config/code (required sections, bullet count bounds, mandatory links, banned phrases).

This avoids relying only on prompt obedience while keeping persona writing quality flexible.

---

## Known Limitations

| Issue | Severity | Workaround |
|-------|----------|-----------|
| No semantic cross-run dedup | Medium | LLM instructed to drop near-duplicates in output |
| LLM may occasionally include off-topic items | Medium | System prompt explicitly forbids this; tighten if needed |
| Single-message Telegram limit (3400 chars) | Low | Adjust `single_message_max_chars` in persona YAML |
| Ollama must be running locally | High | Ensure Ollama is started before the scheduler runs |
| No retry on Telegram send failure | Low | Re-run manually with `--once` |
| RSS feeds may silently return empty | Low | Check `logs/agent.log` for WARNING messages |
