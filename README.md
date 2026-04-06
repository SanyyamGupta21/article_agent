# Python Multi-Persona News Agent (Ollama + Telegram)

This agent supports multiple personas (for example AI and Law) using one shared codebase. Each persona has its own sources, summary style, Telegram bot token, and chat target.

## What it does
- Loads persona config from `config/personas/<persona>.yaml`
- Fetches items from RSS feeds (and optional Hacker News topics)
- Summarizes with Ollama using persona-specific prompts
- Sends digest to persona-specific Telegram chat
- Supports one-shot run (`--once`) and scheduled interval mode

## 1) Setup
```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 2) Configure env
```bash
copy .env.example .env
```
Fill these values in `.env`:
- Global fallback values:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
- Recommended for multi-bot setup:
  - `AI_TELEGRAM_BOT_TOKEN`
  - `AI_TELEGRAM_CHAT_ID`
  - `LAW_TELEGRAM_BOT_TOKEN`
  - `LAW_TELEGRAM_CHAT_ID`
- Optional:
  - `AI_INTERVAL_HOURS`, `LAW_INTERVAL_HOURS`
  - `OLLAMA_MODEL` (default `llama3.1:8b`)
  - Validation toggles in persona YAML (`persona.validation.*`)
  - Optional pre-filter lists in persona YAML (`persona.require_keywords`, `persona.exclude_keywords`)

## 3) Ensure Ollama model is available
```bash
ollama pull llama3.1:8b
```
(or set `OLLAMA_MODEL` to another installed open-source model)

## 4) Configure sources
Persona files live in:
- `config/personas/ai.yaml`
- `config/personas/law.yaml`

## 5) Test once
```bash
python src/ai_digest_agent.py --persona ai --once
python src/ai_digest_agent.py --persona law --once
```

### Manual model override (single push)
```bash
python src/ai_digest_agent.py --persona ai --once --model gemma3:12b
```

### Manual A/B compare push (same inputs, two Telegram messages)
```bash
python src/ai_digest_agent.py --persona ai --once --compare-model gemma3:12b
```
Optional baseline override:
```bash
python src/ai_digest_agent.py --persona ai --once --model llama3.1:8b --compare-model gemma3:12b
```

## 6) Run on a schedule (Windows — recommended)

Use Windows Task Scheduler so notifications fire reliably even if you close the terminal.
Run **once as Administrator**:

```powershell
.\setup_scheduler.ps1 -TaskName "AIDigestBot" -Persona "ai"
.\setup_scheduler.ps1 -TaskName "LawDigestBot" -Persona "law"
```

This registers a task that:
- Runs `python src/ai_digest_agent.py --once --persona <name>` every **8 hours**
- Uses persona-specific env vars when present (for example `LAW_TELEGRAM_BOT_TOKEN`)
- Starts automatically — no terminal needs to stay open
- Fires even when you're not logged in

Useful follow-up commands:
```powershell
# Trigger immediately
Start-ScheduledTask -TaskName "AIDigestBot"
Start-ScheduledTask -TaskName "LawDigestBot"

# Check last run status
Get-ScheduledTask -TaskName "AIDigestBot" | Select-Object -ExpandProperty State
Get-ScheduledTask -TaskName "LawDigestBot" | Select-Object -ExpandProperty State

# Remove the task
Unregister-ScheduledTask -TaskName "AIDigestBot" -Confirm:$false
Unregister-ScheduledTask -TaskName "LawDigestBot" -Confirm:$false
```

### Easy on/off toggle for schedules
Use the helper script:

```powershell
# Show status for both personas
.\manage_scheduler.ps1 -Action status -Persona all

# Turn notifications ON/OFF
.\manage_scheduler.ps1 -Action on  -Persona ai
.\manage_scheduler.ps1 -Action off -Persona law

# Toggle both in one command
.\manage_scheduler.ps1 -Action toggle -Persona all

# Run one immediately
.\manage_scheduler.ps1 -Action run -Persona ai

# Monitor scheduler + delivery health
.\manage_scheduler.ps1 -Action monitor -Persona all
```

### Tracking and validation
The agent now writes run records and logs so you can audit what happened:

- Run history (JSON Lines): `logs/digest_runs.jsonl`
- App logs (rotating): `logs/agent.log`

Each run appends lifecycle records with fields like persona, status (`started`, `sent`, `failed`), timestamp, and Telegram message metadata when available.

Validation shortcut:

```powershell
.\manage_scheduler.ps1 -Action monitor -Persona all
```

This combines Task Scheduler state with the latest tracked run and marks validation as:
- `healthy` when last tracked send is recent enough
- `late` when send is older than expected interval + grace window
- `failed` when latest tracked run failed
- `unknown` when no tracked send exists yet

### Alternative: run manually in a persistent terminal
If you prefer not to use Task Scheduler, run the script and **keep the terminal open**:
```bash
python src/ai_digest_agent.py --persona ai
python src/ai_digest_agent.py --persona law
```
> **Warning:** Closing the terminal kills the process and stops all future notifications.

## Notes
- Hacker News data source: `https://hn.algolia.com/api/v1/search`
- Telegram message length can be limited; keep summaries concise.
