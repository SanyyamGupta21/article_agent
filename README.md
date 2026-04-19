# Personal Digest Agent

A personalized agent that pulls from newsletters, blogs, and RSS feeds to create concise daily briefings. Summaries are tailored using local LLMs and delivered directly to Telegram for quick, focused reading.

---

## 📬 Example Output

> *(To Be Updated)*

```text
🧠 AI Digest — 12 Feb

1. OpenAI releases new reasoning model  
→ Improves multi-step planning and tool usage  
🔗 link  

2. Nvidia announces next-gen GPUs  
→ Focus on inference optimization  
🔗 link  
```

---

## 💡 Why I built this

I was already checking multiple sources—blogs, newsletters—but the overhead was deciding *what’s actually worth opening*.

This agent acts as a directional layer:
- surfaces what matters  
- points me to the right sources  
- keeps everything in one place  

So instead of juggling tabs, I get a quick briefing and decide where to go deeper.

---

## ✨ Features

* 🧠 Persona-based summaries
* 🔁 Cross-run deduplication (7-day history)
* 🤖 Local LLM support via Ollama
* 📬 Automated Telegram delivery

---

## 🗂 Project Structure

```
src/
  ai_digest_agent.py        # Main pipeline

config/
  personas/
    ai.yaml                 # Persona config
    law.yaml
    ai_skills.md            # Output templates + validation
    law_skills.md

tests/
  test_components.py
```

---

## ⚡ Quick Start

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Configure `.env`

```env
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat_id>
OLLAMA_MODEL=<model>
```

*(Optional persona overrides)*

```env
AI_TELEGRAM_BOT_TOKEN=<token>
AI_TELEGRAM_CHAT_ID=<chat_id>
```

---

### Run the agent

```bash
python src/ai_digest_agent.py --persona ai --once
```

### Expected Output

* Telegram message with summaries
* Logs written to `logs/digest_runs.jsonl`

---

## 🧠 How it works

```
RSS Feeds  ──┐
             ├──> Fetch → Deduplicate → Summarize → Validate → Telegram
Hacker News ─┘
```

Each run follows a simple flow:
1. Pull articles from RSS feeds and Hacker News  
2. Remove duplicates within the batch and across a 7-day history  
3. Summarize using Ollama with persona-aware prompts  
4. Validate structure; retry once or fall back  
5. Send the digest to Telegram  
6. Store sent items to prevent repeats

---

## 🧪 Experimentation & Debugging

Useful for evaluating models and prompt behavior.

```bash
# Baseline run
python src/ai_digest_agent.py --persona ai --once

# Try different model
python src/ai_digest_agent.py --persona ai --once --model gemma4:12b

# Compare models (A/B testing)
python src/ai_digest_agent.py --persona ai --once \
  --model llama3.1:8b \
  --compare-model gemma4:12b

# Component tests
python test_components.py
```

---

## ⏱ Scheduling

### Windows Task Scheduler

```powershell
# Create a scheduled task for the AI persona
.\setup_scheduler.ps1 -TaskName "AIDigestBot" -Persona "ai"
```

Manage:

```powershell
# Check current task status
.\manage_scheduler.ps1 -Action status  -Persona ai

# Enable scheduled runs
.\manage_scheduler.ps1 -Action on      -Persona ai

# Disable scheduled runs
.\manage_scheduler.ps1 -Action off     -Persona ai

# Trigger a manual run (without waiting for schedule)
.\manage_scheduler.ps1 -Action run     -Persona ai
```

---

## ⚠️ Known Limitations

| Issue | Severity | Workaround |
|------|----------|-----------|
| Ollama must be running locally | 🔴 High | Ensure Ollama is started before scheduler runs |
| No semantic cross-run dedup | 🟠 Medium | LLM instructed to drop near-duplicates in output |
| LLM may occasionally include off-topic items | 🟠 Medium | Tighten system prompt if needed |
| No retry on Telegram send failure | 🟢 Low | Re-run manually with `--once` |
| RSS feeds may silently return empty | 🟢 Low | Check `logs/agent.log` for warnings |
| Single-message Telegram limit (3400 chars) | 🟢 Low | Adjust `single_message_max_chars` in persona YAML |

---
