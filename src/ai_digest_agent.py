import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import textwrap
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv


LOGGER = logging.getLogger("ai_digest_agent")


DEFAULT_PERSONA = "ai"
RUN_HISTORY_FILE = Path("logs") / "digest_runs.jsonl"
APP_LOG_FILE = Path("logs") / "agent.log"
SEEN_ITEMS_TTL_DAYS = 7  # How long to remember seen items across runs


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append_run_record(record: dict) -> None:
    path = Path(os.environ.get("RUN_HISTORY_FILE", str(RUN_HISTORY_FILE)))
    ensure_parent_dir(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def iso_utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def setup_file_logging(level_name: str) -> None:
    ensure_parent_dir(APP_LOG_FILE)
    root = logging.getLogger()

    # Avoid duplicate handlers if main() is called multiple times.
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "").endswith(
            str(APP_LOG_FILE).replace("/", os.sep)
        ):
            return

    file_handler = RotatingFileHandler(APP_LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    root.addHandler(file_handler)


@dataclass
class Item:
    source: str
    title: str
    link: str
    published: str
    summary: str


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def fetch_rss_items(feed_urls: Iterable[str], limit_per_feed: int = 8) -> List[Item]:
    items: List[Item] = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            feed_meta = getattr(feed, "feed", {})
            source = as_text(feed_meta.get("title"), url) if isinstance(feed_meta, dict) else url
            for entry in feed.entries[:limit_per_feed]:
                title = as_text(entry.get("title"), "(untitled)").strip()
                link = as_text(entry.get("link"), "")
                published = as_text(entry.get("published", entry.get("updated", "")), "")
                summary_raw = as_text(entry.get("summary", entry.get("description", "")), "")
                items.append(
                    Item(
                        source=source,
                        title=title,
                        link=link,
                        published=published,
                        summary=strip_html(summary_raw),
                    )
                )
        except Exception as exc:
            LOGGER.warning("RSS fetch failed for %s: %s", url, exc)
    return items


def fetch_hn_items(topics: Iterable[str], days_back: int = 1, limit_per_topic: int = 10) -> List[Item]:
    items: List[Item] = []
    from_ts = int((dt.datetime.now(dt.UTC) - dt.timedelta(days=days_back)).timestamp())

    for topic in topics:
        params = {
            "query": topic,
            "tags": "story",
            "numericFilters": f"created_at_i>{from_ts}",
            "hitsPerPage": limit_per_topic,
        }
        try:
            resp = requests.get("https://hn.algolia.com/api/v1/search", params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            for hit in data.get("hits", []):
                title = (hit.get("title") or hit.get("story_title") or "(untitled)").strip()
                link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                created = hit.get("created_at", "")
                summary = f"HN points={hit.get('points', 0)}, comments={hit.get('num_comments', 0)}, topic={topic}"
                items.append(
                    Item(
                        source="Hacker News",
                        title=title,
                        link=link,
                        published=created,
                        summary=summary,
                    )
                )
        except Exception as exc:
            LOGGER.warning("HN fetch failed for topic '%s': %s", topic, exc)

    return items


def strip_html(text: str) -> str:
    if not text:
        return ""
    # Deterministic cleanup for feed text in non-LLM linked digest mode.
    out = html.unescape(as_text(text, ""))
    out = re.sub(r"<[^>]+>", " ", out)
    return " ".join(out.split())


def parse_iso_utc(timestamp: str) -> Optional[dt.datetime]:
    value = as_text(timestamp, "").strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def canonicalize_url(url: str) -> str:
    raw = as_text(url, "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"

    try:
        split = urlsplit(raw)
    except Exception:
        return raw.lower().rstrip("/")

    scheme = split.scheme.lower() or "https"
    if scheme not in {"http", "https"}:
        return raw.lower().rstrip("/")

    netloc = split.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = split.path.rstrip("/") or "/"

    noisy_prefixes = ("utm_", "mc_")
    noisy_exact = {"gclid", "fbclid", "igshid", "mkt_tok", "ref_src", "ref_url"}
    query_pairs = parse_qsl(split.query, keep_blank_values=False)
    kept_pairs = [
        (k, v)
        for (k, v) in query_pairs
        if k and k.lower() not in noisy_exact and not k.lower().startswith(noisy_prefixes)
    ]
    kept_pairs.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(kept_pairs, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_title_key(title: str) -> str:
    text = strip_html(title).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def item_dedupe_key(item: Item) -> str:
    link_key = canonicalize_url(item.link)
    if link_key:
        return f"url:{link_key}"
    return f"title:{normalize_title_key(item.title)}"


def _seen_items_path(persona: str) -> Path:
    return Path("logs") / f"seen_items_{persona}.json"


def load_seen_keys(persona: str, ttl_days: int = SEEN_ITEMS_TTL_DAYS) -> set:
    """Load previously seen item keys for a persona, pruning entries older than ttl_days."""
    path = _seen_items_path(persona)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=ttl_days)
        keys = set()
        for k, ts in data.get("keys", {}).items():
            parsed = parse_iso_utc(as_text(ts, ""))
            if parsed and parsed > cutoff:
                keys.add(k)
        return keys
    except Exception as exc:
        LOGGER.warning("Could not load seen keys for persona '%s': %s", persona, exc)
        return set()


def save_seen_keys(persona: str, new_keys: Iterable[str], ttl_days: int = SEEN_ITEMS_TTL_DAYS) -> None:
    """Persist new item keys for a persona, merging with existing history and pruning stale entries."""
    path = _seen_items_path(persona)
    ensure_parent_dir(path)

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8")).get("keys", {})
        except Exception:
            pass

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=ttl_days)
    pruned = {}
    for k, ts in existing.items():
        parsed = parse_iso_utc(as_text(ts, ""))
        if parsed and parsed > cutoff:
            pruned[k] = ts

    now_str = iso_utc_now()
    for key in new_keys:
        if key and key not in pruned:
            pruned[key] = now_str

    try:
        path.write_text(
            json.dumps({"keys": pruned, "updated_utc": now_str}, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.warning("Could not save seen keys for persona '%s': %s", persona, exc)


def dedupe_items(items: List[Item], max_items: int = 35, history_keys: Optional[set] = None) -> List[Item]:
    """Deduplicate items within this batch and against cross-run history.

    history_keys: set of previously seen keys (link/title) from prior runs.
    Items whose key appears in history_keys are treated as already-sent and skipped.
    """
    seen: set[str] = set(history_keys or ())
    deduped: List[Item] = []
    for item in items:
        key = item_dedupe_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped


def apply_persona_filters(items: List[Item], persona_cfg: dict) -> tuple[List[Item], int]:
    required = [as_text(v).strip().lower() for v in persona_cfg.get("require_keywords", []) if as_text(v).strip()]
    excluded = [as_text(v).strip().lower() for v in persona_cfg.get("exclude_keywords", []) if as_text(v).strip()]

    if not required and not excluded:
        return items, 0

    kept: List[Item] = []
    skipped = 0
    for item in items:
        text = " ".join([item.title, item.summary, item.source]).lower()
        if required and not any(word in text for word in required):
            skipped += 1
            continue
        if excluded and any(word in text for word in excluded):
            skipped += 1
            continue
        kept.append(item)

    return kept, skipped


def resolve_env(name: str, persona: str) -> Optional[str]:
    persona_key = f"{persona.upper()}_{name}"
    return os.environ.get(persona_key) or os.environ.get(name)


def resolve_config_path(explicit_config: Optional[str], persona: str) -> str:
    if explicit_config:
        return explicit_config

    persona_cfg = Path("config") / "personas" / f"{persona}.yaml"
    if persona_cfg.exists():
        return str(persona_cfg)

    # Backward compatibility with the original single-persona setup.
    return "config/feeds.yaml"


def load_output_template(persona_cfg: dict) -> str:
    template_path = as_text(persona_cfg.get("output_template_file"), "").strip()
    if not template_path:
        return ""

    path = Path(template_path)
    if not path.is_absolute():
        path = Path.cwd() / path

    if not path.exists():
        LOGGER.warning("Output template file not found: %s", path)
        return ""

    try:
        raw = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        LOGGER.warning("Failed to read output template file %s: %s", path, exc)
        return ""

    marker = "## Output Template"
    idx = raw.find(marker)
    if idx == -1:
        return raw

    section = raw[idx + len(marker):].strip()
    lines = section.splitlines()
    extracted: List[str] = []
    for line in lines:
        if line.startswith("## "):
            break
        extracted.append(line)
    return "\n".join(extracted).strip()


def build_summarization_prompt(
    items: List[Item],
    persona_name: str,
    brief_description: str,
    style_requirements: List[str],
    output_sections: List[str],
    max_words: int,
    output_template: str = "",
    additional_instructions: str = "",
) -> str:
    serialized = []
    for idx, item in enumerate(items, start=1):
        serialized.append(
            textwrap.dedent(
                f"""
                [{idx}] source: {item.source}
                title: {item.title}
                published: {item.published}
                link: {item.link}
                context: {item.summary[:350]}
                """
            ).strip()
        )

    content_block = "\n\n".join(serialized)

    style_block = "\n".join(f"- {line}" for line in style_requirements)
    output_block = "\n".join(f"{idx}) {line}" for idx, line in enumerate(output_sections, start=1))
    heading_block = "\n".join(f"- {line}" for line in output_sections)
    template_block = ""
    if output_template:
        template_block = (
            "\n\nOutput style template (follow this structure and heading style exactly):\n"
            f"{output_template}"
        )

    extra_block = ""
    if additional_instructions.strip():
        extra_block = f"\n\nAdditional compliance requirements:\n{additional_instructions.strip()}"

    return textwrap.dedent(
        f"""
        Create a daily {persona_name} intelligence brief from the items below.

        Style requirements:
        {style_block}

        Output format:
        {output_block}

        {template_block}

        {extra_block}

        Formatting requirements:
        - Use plain text formatting suitable for Telegram (no markdown, no HTML).
        - Use section headers exactly in this style:
        {heading_block}
        - Under each section header, use bullet points only (no paragraph blocks).
        - Use exactly two spaces for indented detail lines (for example: "  Why it matters: ...", "  Link: ...").
        - Keep each bullet crisp and scannable.
        - For each key bullet, include a direct source line in this format: "Link: <url>".
        - Keep each bullet self-contained with its own context and link.
        - Do not add a separate citations section at the end.

        Keep the whole output under {max_words} words.

        Important:
        - Return only the final digest content.
        - Do not include planning notes, instructions, checklists, or template labels.
        - Do not include the phrase "Output Template".

        Brief framing:
        {brief_description}

        ITEMS:
        {content_block}
        """
    ).strip()


def summarize_with_ollama(
    items: List[Item],
    config: Optional[dict] = None,
    persona: str = DEFAULT_PERSONA,
    model_override: Optional[str] = None,
    base_url_override: Optional[str] = None,
    additional_instructions: str = "",
) -> str:
    if not items:
        return "No qualifying items found in this run."

    # Limit to first 15 items for faster processing
    items = items[:15]

    config = config or {}
    persona_cfg = config.get("persona", {})
    persona_name = persona_cfg.get("name", persona.title())
    brief_description = persona_cfg.get("brief_description", "Focus on high-signal updates and practical impact.")
    style_requirements = persona_cfg.get(
        "style_requirements",
        [
            "Focus on technical insights only.",
            "Use concise bullets.",
            "No marketing fluff, hype, or generic phrasing.",
            "Prioritize novel engineering ideas, benchmarks, methods, and tooling updates.",
        ],
    )
    output_sections = persona_cfg.get(
        "output_sections",
        [
            "Top technical takeaways (5-8 bullets)",
            "Notable model/tool releases (bullets)",
            "Research and benchmarking signals (bullets)",
            "What to watch next (3 bullets)",
        ],
    )
    max_words = int(persona_cfg.get("max_words", 320))
    output_template = load_output_template(persona_cfg)

    model = (as_text(model_override).strip() if model_override else "") or resolve_env("OLLAMA_MODEL", persona) or "llama3.1:8b"
    base_url = (
        (as_text(base_url_override).strip() if base_url_override else "")
        or resolve_env("OLLAMA_BASE_URL", persona)
        or "http://localhost:11434"
    ).rstrip("/")
    system_prompt = persona_cfg.get(
        "system_prompt",
        "You are a technical research analyst. Be concise and concrete.",
    )

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": build_summarization_prompt(
                    items=items,
                    persona_name=persona_name,
                    brief_description=brief_description,
                    style_requirements=style_requirements,
                    output_sections=output_sections,
                    max_words=max_words,
                    output_template=output_template,
                    additional_instructions=additional_instructions,
                ),
            },
        ],
        "options": {"temperature": 0.2},
    }

    try:
        LOGGER.info("Sending request to Ollama (timeout=60s)...")
        resp = requests.post(f"{base_url}/api/chat", json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        message = data.get("message", {})
        content = message.get("content", "").strip()
        if not content:
            raise RuntimeError("Ollama returned an empty summary.")
        return content
    except requests.exceptions.Timeout:
        LOGGER.error("Ollama request timed out after 60s")
        return f"Brief Summary:\n\nFetched {len(items)} top items today. " \
               f"(Full AI analysis temporarily unavailable)"
    except Exception as exc:
        LOGGER.error("Ollama error: %s", exc)
        return f"Brief Summary:\n\nFetched {len(items)} top items today. " \
               f"(Analysis unavailable: {str(exc)[:50]})"


def load_items_for_run(config_path: str, persona: str = DEFAULT_PERSONA) -> tuple[dict, dict, List[Item], dict]:
    config = load_config(config_path)
    rss_urls = config.get("rss_feeds", [])
    hn_cfg = config.get("hacker_news", {})
    persona_cfg = config.get("persona", {})

    rss_items = fetch_rss_items(rss_urls)
    hn_items: List[Item] = []
    if hn_cfg.get("include", True):
        hn_items = fetch_hn_items(hn_cfg.get("topics", []))

    raw_count = len(rss_items) + len(hn_items)
    history_keys = load_seen_keys(persona)
    items = dedupe_items(rss_items + hn_items, history_keys=history_keys)
    dedupe_skipped = raw_count - len(items)

    items, persona_filtered = apply_persona_filters(items, persona_cfg)

    stats = {
        "rss_items": len(rss_items),
        "hn_items": len(hn_items),
        "raw_items": raw_count,
        "dedupe_skipped": dedupe_skipped,
        "persona_filtered": persona_filtered,
    }

    LOGGER.info(
        "Fetched %d RSS + %d HN items -> %d kept (raw=%d, dedupe_skipped=%d, persona_filtered=%d)",
        len(rss_items),
        len(hn_items),
        len(items),
        raw_count,
        dedupe_skipped,
        persona_filtered,
    )
    return config, persona_cfg, items, stats


def normalize_heading(text: str) -> str:
    s = as_text(text, "").strip().lower()
    s = s.rstrip(":")
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def parse_bullet_bounds(section_label: str) -> tuple[Optional[int], Optional[int]]:
    label = as_text(section_label, "")
    m_range = re.search(r"\((\d+)\s*-\s*(\d+)\s*bullets?\)", label, flags=re.IGNORECASE)
    if m_range:
        return int(m_range.group(1)), int(m_range.group(2))

    m_exact = re.search(r"\((\d+)\s*bullets?\)", label, flags=re.IGNORECASE)
    if m_exact:
        val = int(m_exact.group(1))
        return val, val

    return None, None


def validate_summary_output(summary: str, output_sections: List[str], require_link_per_bullet: bool = True) -> List[str]:
    lines = [line.rstrip() for line in as_text(summary, "").splitlines()]
    expected = [as_text(s, "").strip() for s in output_sections if as_text(s, "").strip()]
    if not expected:
        return []

    key_map = {normalize_heading(name): name for name in expected}
    section_order = list(key_map.keys())
    section_lines: dict[str, List[str]] = {key: [] for key in section_order}

    current_key: Optional[str] = None
    for line in lines:
        stripped = line.strip()
        nrm = normalize_heading(stripped)
        if nrm in key_map:
            current_key = nrm
            continue
        if current_key:
            section_lines[current_key].append(line)

    errors: List[str] = []
    for key in section_order:
        label = key_map[key]
        content = section_lines.get(key, [])
        if not content:
            errors.append(f"Missing section header or content: {label}")
            continue

        bullet_idxs = [idx for idx, line in enumerate(content) if line.lstrip().startswith("- ")]
        bullet_count = len(bullet_idxs)
        if bullet_count == 0:
            errors.append(f"No bullets found under section: {label}")
            continue

        min_bullets, max_bullets = parse_bullet_bounds(label)
        if min_bullets is not None and bullet_count < min_bullets:
            errors.append(f"Section '{label}' has too few bullets ({bullet_count} < {min_bullets})")
        if max_bullets is not None and bullet_count > max_bullets:
            errors.append(f"Section '{label}' has too many bullets ({bullet_count} > {max_bullets})")

        if require_link_per_bullet:
            for pos, start_idx in enumerate(bullet_idxs):
                end_idx = bullet_idxs[pos + 1] if pos + 1 < len(bullet_idxs) else len(content)
                block = content[start_idx:end_idx]
                has_link = any("link:" in row.lower() for row in block)
                if not has_link:
                    errors.append(f"Missing link line for a bullet in section: {label}")
                    break

    return errors


def summarize_with_quality_controls(
    items: List[Item],
    config: dict,
    persona: str,
    model_override: Optional[str] = None,
) -> tuple[str, str, List[str]]:
    persona_cfg = config.get("persona", {})
    output_sections = persona_cfg.get("output_sections", [])
    validation_cfg = persona_cfg.get("validation", {}) if isinstance(persona_cfg.get("validation", {}), dict) else {}

    validation_enabled = bool(validation_cfg.get("enabled", True))
    retries = int(validation_cfg.get("max_retries", 1))
    require_link_per_bullet = bool(validation_cfg.get("require_link_per_bullet", True))
    fallback_to_linked = bool(validation_cfg.get("fallback_to_linked_digest", True))
    max_linked_items = int(persona_cfg.get("max_linked_items", 6))

    if not validation_enabled:
        summary = summarize_with_ollama(items, config=config, persona=persona, model_override=model_override)
        return summary, "validation_disabled", []

    last_errors: List[str] = []
    summary = ""
    for attempt in range(retries + 1):
        additional = ""
        if attempt > 0:
            additional = (
                "Fix all prior format issues strictly. "
                "Use exact section headers, bullet-only content under each section, "
                "and include 'Link: <url>' within each bullet block."
            )

        summary = summarize_with_ollama(
            items,
            config=config,
            persona=persona,
            model_override=model_override,
            additional_instructions=additional,
        )
        errors = validate_summary_output(summary, output_sections, require_link_per_bullet=require_link_per_bullet)
        if not errors:
            return summary, "validated", []

        last_errors = errors
        LOGGER.warning("Summary validation failed (attempt %d/%d): %s", attempt + 1, retries + 1, "; ".join(errors[:4]))

    if fallback_to_linked:
        fallback = build_linked_digest(items, persona_cfg.get("name", persona.title()), max_items=max_linked_items)
        return fallback, "fallback_linked_digest", last_errors

    return summary, "validation_failed_no_fallback", last_errors


def prepare_single_message(text: str, max_len: int = 3400) -> str:
    lines = [line.rstrip() for line in as_text(text, "").splitlines()]
    normalized: List[str] = []
    blank_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_count += 1
            if blank_count <= 1:
                normalized.append("")
            continue
        blank_count = 0
        if line.startswith("    "):
            line = "  " + stripped
        normalized.append(line)

    msg = "\n".join(normalized).strip()
    if len(msg) <= max_len:
        return msg

    cutoff = msg.rfind("\n", 0, max_len - 20)
    if cutoff == -1:
        cutoff = max_len - 20
    return msg[:cutoff].rstrip() + "\n\n(Shortened for single-message delivery.)"


def send_telegram(text: str, token: str, chat_id: str, max_len: int = 3400) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": prepare_single_message(text, max_len=max_len),
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def clean_text(text: str) -> str:
    raw = html.unescape(as_text(text, ""))
    raw = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(raw.split())


def truncate_text(text: str, max_len: int) -> str:
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def build_linked_digest(items: List[Item], persona_name: str, max_items: int = 6) -> str:
    lines = [f"Top {persona_name} Updates", ""]
    count = 0
    for item in items:
        link = clean_text(item.link)
        if not link.startswith("http"):
            continue

        title = truncate_text(item.title, 110)
        source = truncate_text(item.source, 40)
        context = truncate_text(item.summary, 160)

        lines.append(f"- {title}")
        lines.append(f"  Source: {source}")
        if context:
            lines.append(f"  Why it matters: {context}")
        lines.append(f"  Link: {link}")
        lines.append("")

        count += 1
        if count >= max_items:
            break

    if count == 0:
        return "No qualifying items found in this run."

    lines.append("Note: Limited to high-signal items for quick scanning.")
    return "\n".join(lines)


def build_read_more_section(items: List[Item], max_links: int = 5) -> str:
    lines = ["Citations and Deep Dive", ""]
    count = 0
    for item in items:
        link = clean_text(item.link)
        if not link.startswith("http"):
            continue
        title = clean_text(item.title)[:100]
        source = clean_text(item.source)[:40]
        context = clean_text(item.summary)[:100]
        lines.append(f"[{count + 1}] {title}")
        lines.append(f"    Source: {source}")
        if context:
            lines.append(f"    Context: {context}")
        lines.append(f"    Link: {link}")
        lines.append("")
        count += 1
        if count >= max_links:
            break
    if count == 0:
        return ""
    return "\n".join(lines)


def run_digest(
    config_path: str,
    persona: str,
    model_override: Optional[str] = None,
    title_suffix: str = "",
) -> None:
    run_id = uuid.uuid4().hex[:12]
    append_run_record(
        {
            "timestamp_utc": iso_utc_now(),
            "run_id": run_id,
            "persona": persona,
            "status": "started",
            "config_path": config_path,
        }
    )

    try:
        config, persona_cfg, items, stats = load_items_for_run(config_path, persona=persona)

        linked_digest_only = bool(persona_cfg.get("linked_digest_only", False))
        quality_status = "linked_digest_only"
        quality_errors: List[str] = []
        if linked_digest_only:
            max_linked_items = int(persona_cfg.get("max_linked_items", 6))
            summary = build_linked_digest(items, persona_cfg.get("name", persona.title()), max_items=max_linked_items)
        else:
            LOGGER.info("Starting Ollama summarization...")
            summary, quality_status, quality_errors = summarize_with_quality_controls(
                items,
                config=config,
                persona=persona,
                model_override=model_override,
            )
            LOGGER.info("Ollama summarization complete")

        header_template = persona_cfg.get("header_template", "{persona} Daily Summary - {date}")
        header = header_template.format(
            persona=persona_cfg.get("name", persona.title()),
            date=dt.datetime.now().strftime("%Y-%m-%d"),
        )
        if title_suffix:
            header = f"{header} | {title_suffix}"
        message = f"{header}\n\n{summary}"
        # In LLM mode, links are expected inline with each relevant bullet.

        token = resolve_env("TELEGRAM_BOT_TOKEN", persona)
        chat_id = resolve_env("TELEGRAM_CHAT_ID", persona)
        if not token or not chat_id:
            raise RuntimeError(
                f"Missing Telegram credentials for persona '{persona}'. "
                f"Set {persona.upper()}_TELEGRAM_BOT_TOKEN/{persona.upper()}_TELEGRAM_CHAT_ID "
                "or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID."
            )

        LOGGER.info("Sending Telegram message...")
        max_message_chars = int(persona_cfg.get("single_message_max_chars", 3400))
        tg_response = send_telegram(message, token=token, chat_id=chat_id, max_len=max_message_chars)
        LOGGER.info("Telegram digest sent")

        tg_result = tg_response.get("result", {}) if isinstance(tg_response, dict) else {}
        append_run_record(
            {
                "timestamp_utc": iso_utc_now(),
                "run_id": run_id,
                "persona": persona,
                "status": "sent",
                "items_deduped": len(items),
                "rss_items": stats["rss_items"],
                "hn_items": stats["hn_items"],
                "raw_items": stats["raw_items"],
                "dedupe_skipped": stats["dedupe_skipped"],
                "persona_filtered": stats["persona_filtered"],
                "sent_item_keys": [item_dedupe_key(item) for item in items],
                "quality_status": quality_status,
                "quality_errors": quality_errors[:6],
                "model_override": model_override,
                "telegram_ok": bool(tg_response.get("ok", False)) if isinstance(tg_response, dict) else False,
                "telegram_message_id": tg_result.get("message_id"),
                "telegram_date": tg_result.get("date"),
            }
        )

        # Persist sent item keys so they are skipped in future runs.
        sent_keys = [item_dedupe_key(item) for item in items]
        save_seen_keys(persona, sent_keys)
        LOGGER.info("Saved %d item keys to seen history for persona '%s'", len(sent_keys), persona)
    except Exception as exc:
        append_run_record(
            {
                "timestamp_utc": iso_utc_now(),
                "run_id": run_id,
                "persona": persona,
                "status": "failed",
                "error": str(exc)[:500],
            }
        )
        raise


def run_digest_compare(config_path: str, persona: str, candidate_model: str, baseline_model: Optional[str] = None) -> None:
    config, persona_cfg, items, stats = load_items_for_run(config_path, persona=persona)

    if not items:
        raise RuntimeError("No qualifying items found in this run.")

    token = resolve_env("TELEGRAM_BOT_TOKEN", persona)
    chat_id = resolve_env("TELEGRAM_CHAT_ID", persona)
    if not token or not chat_id:
        raise RuntimeError(
            f"Missing Telegram credentials for persona '{persona}'. "
            f"Set {persona.upper()}_TELEGRAM_BOT_TOKEN/{persona.upper()}_TELEGRAM_CHAT_ID "
            "or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID."
        )

    max_message_chars = int(persona_cfg.get("single_message_max_chars", 3400))
    baseline_label = baseline_model or (resolve_env("OLLAMA_MODEL", persona) or "llama3.1:8b")
    header_template = persona_cfg.get("header_template", "{persona} Daily Summary - {date}")
    header = header_template.format(
        persona=persona_cfg.get("name", persona.title()),
        date=dt.datetime.now().strftime("%Y-%m-%d"),
    )

    for label, model_name in [("Baseline", baseline_label), ("Candidate", candidate_model)]:
        run_id = uuid.uuid4().hex[:12]
        append_run_record(
            {
                "timestamp_utc": iso_utc_now(),
                "run_id": run_id,
                "persona": persona,
                "status": "started_compare",
                "config_path": config_path,
                "compare_label": label,
                "model": model_name,
            }
        )

        summary, quality_status, quality_errors = summarize_with_quality_controls(
            items,
            config=config,
            persona=persona,
            model_override=model_name,
        )
        message = f"{header} | {label}: {model_name}\n\n{summary}"
        tg_response = send_telegram(message, token=token, chat_id=chat_id, max_len=max_message_chars)
        tg_result = tg_response.get("result", {}) if isinstance(tg_response, dict) else {}

        append_run_record(
            {
                "timestamp_utc": iso_utc_now(),
                "run_id": run_id,
                "persona": persona,
                "status": "sent_compare",
                "compare_label": label,
                "model": model_name,
                "items_deduped": len(items),
                "rss_items": stats["rss_items"],
                "hn_items": stats["hn_items"],
                "raw_items": stats["raw_items"],
                "dedupe_skipped": stats["dedupe_skipped"],
                "persona_filtered": stats["persona_filtered"],
                "sent_item_keys": [item_dedupe_key(item) for item in items],
                "quality_status": quality_status,
                "quality_errors": quality_errors[:6],
                "telegram_ok": bool(tg_response.get("ok", False)) if isinstance(tg_response, dict) else False,
                "telegram_message_id": tg_result.get("message_id"),
                "telegram_date": tg_result.get("date"),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily AI summary agent")
    parser.add_argument("--persona", default=DEFAULT_PERSONA, help="Persona name (ai, law, etc.)")
    parser.add_argument("--config", default=None, help="Path to YAML config")
    parser.add_argument("--env-file", default=None, help="Optional explicit .env file path")
    parser.add_argument("--once", action="store_true", help="Run once immediately")
    parser.add_argument("--model", default=None, help="Optional Ollama model override for this run")
    parser.add_argument(
        "--compare-model",
        default=None,
        help="Run one-shot A/B push: baseline model then this candidate model",
    )
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=True)
    else:
        load_dotenv()

    config_path = resolve_config_path(args.config, args.persona)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    setup_file_logging(os.environ.get("LOG_LEVEL", "INFO"))

    required_env = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [k for k in required_env if not resolve_env(k, args.persona)]
    if missing:
        prefixed = [f"{args.persona.upper()}_{name}" for name in missing]
        raise SystemExit(
            "Missing required env vars. Provide either persona-specific vars "
            f"({', '.join(prefixed)}) or global vars ({', '.join(missing)})."
        )

    if args.once:
        if args.compare_model:
            run_digest_compare(config_path, args.persona, candidate_model=args.compare_model, baseline_model=args.model)
        else:
            model_suffix = f"Model: {args.model}" if args.model else ""
            run_digest(config_path, args.persona, model_override=args.model, title_suffix=model_suffix)
        return

    interval_hours = int(resolve_env("INTERVAL_HOURS", args.persona) or "8")

    scheduler = BlockingScheduler(timezone=os.environ.get("TZ", "Asia/Kolkata"))
    scheduler.add_job(
        run_digest,
        "interval",
        args=[config_path, args.persona],
        hours=interval_hours,
        id=f"{args.persona}_digest_interval",
        next_run_time=dt.datetime.now(),
    )

    LOGGER.info(
        "Scheduler started for persona '%s'. Config=%s. Running every %d hour(s)",
        args.persona,
        config_path,
        interval_hours,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
