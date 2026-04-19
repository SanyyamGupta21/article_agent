# article_agent

A multi-persona news digest agent that pulls articles from RSS and Hacker News, summarizes them with a local Ollama model, and sends curated briefings to Telegram.

## Overview

```
RSS Feeds  ──┐
             ├──> fetch + dedupe (cross-run) ──> Ollama LLM ──> Telegram
Hacker News ─┘
```

The agent is designed to run on a schedule, deduplicate content across runs, validate summary quality, and audit delivery results.

## What this repo contains

- `src/ai_digest_agent.py` — main application, including fetch, dedupe, summarization, Telegram delivery, and scheduling.
- `config/personas/ai.yaml` — AI persona config.
- `config/personas/law.yaml` — Law persona config.
- `config/personas/ai_skills.md` — AI output template and quality checklist.
- `config/personas/law_skills.md` — Law output template and quality checklist.

- `test_components.py` — quick component-level smoke tests.

## Project structure

```
src/
  ai_digest_agent.py   — main agent (fetching, dedup, LLM, Telegram, scheduling)

config/
  personas/
    ai.yaml            — AI persona config (feeds, HN topics, LLM settings)
    law.yaml           — Law persona config
    ai_skills.md       — AI output template + quality checklist
    law_skills.md      — Law output template + quality checklist
```

## Key capabilities

- RSS feed ingestion with optional Hacker News search.
- Cross-run deduplication using a 7-day rolling history.
- Persona-aware Ollama summarization with structured templates.
- Output validation and fallback digest generation.
- Telegram delivery with configurable bot tokens and chat IDs.
- Windows Task Scheduler helper scripts for reliable recurring execution.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Update `.env` with your Telegram tokens and chat IDs, then run:

```powershell
python src/ai_digest_agent.py --persona ai --once
```

## Configuration

### Environment variables

Required:

```env
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat_id>
```

Persona-specific overrides:

```env
AI_TELEGRAM_BOT_TOKEN=<token>
AI_TELEGRAM_CHAT_ID=<chat_id>
LAW_TELEGRAM_BOT_TOKEN=<token>
LAW_TELEGRAM_CHAT_ID=<chat_id>
```

Optional:

```env
OLLAMA_MODEL=llama3.1:8b
OLLAMA_BASE_URL=http://localhost:11434
AI_INTERVAL_HOURS=8
LAW_INTERVAL_HOURS=12
LOG_LEVEL=INFO
```

## How it works

### 1. Fetch

- RSS feeds are loaded from persona YAML.
- `feedparser` parses each feed and extracts up to 8 items per feed.
- Hacker News queries the Algolia API when enabled and returns recent stories matching configured topics.
- Extracted fields include source, title, link, published date, and summary.

### 2. Deduplicate

- Items are deduplicated within each run and against a 7-day history.
- Deduplication keys use canonical URLs when available, with normalized titles as a fallback.
- History is stored in `logs/seen_items_{persona}.json` and pruned after 7 days.
- Keys are only saved after a successful Telegram send.

### 3. Summarize

- The first 15 deduped items are sent to Ollama.
- Prompts are built from persona config and the persona skill template.
- Model defaults are resolved in order: CLI flag, persona env var, global env var, default `llama3.1:8b`.
- Temperature is set low for deterministic summaries.

### 4. Validate and fallback

- Generated output is validated against expected section definitions.
- The agent checks for required sections, bullet counts, and link presence.
- It retries generation up to the configured retry limit.
- If validation still fails, it falls back to a linked digest format.

### 5. Send

- Messages are prepared for Telegram delivery and truncated to the configured max length.
- The bot sends text to the Telegram API with previews disabled.
- Success and error metadata are captured for audit.

### 6. Audit

- Each run writes a record to `logs/digest_runs.jsonl`.
- Records include status, item counts, validation state, Telegram result, and error details.

## Scheduling

### Windows Task Scheduler (recommended)

```powershell
.\setup_scheduler.ps1 -TaskName "AIDigestBot" -Persona "ai"
```

Manage the task:

```powershell
.\manage_scheduler.ps1 -Action status  -Persona ai
.\manage_scheduler.ps1 -Action on      -Persona ai
.\manage_scheduler.ps1 -Action off     -Persona ai
.\manage_scheduler.ps1 -Action run     -Persona ai
.\manage_scheduler.ps1 -Action monitor -Persona ai
```

### n8n

Import `n8n_workflow.json` or `n8n_workflow_law.json`. These workflows trigger the agent every 8 hours and alert on non-zero exits.

### Built-in scheduler

```bash
python src/ai_digest_agent.py --persona ai
```

Runs every `AI_INTERVAL_HOURS` using APScheduler.

## Common commands

```bash
python src/ai_digest_agent.py --persona ai --once
python src/ai_digest_agent.py --persona ai --once --model gemma4:12b
python src/ai_digest_agent.py --persona ai --once --model llama3.1:8b --compare-model gemma4:12b
python test_components.py
```

## Deduplication details

### Within a run

Items with the same canonical URL or normalized title are collapsed before summarization.

### Across runs

Seen item keys are stored with timestamps and pruned after 7 days. This prevents repeated coverage of the same article while allowing fresh content to reappear after one week.

## Notes

- The repo is intended for team members who need to understand, run, and extend the project.
- Focus is on clear configuration, reliable delivery, and structured summary quality.
