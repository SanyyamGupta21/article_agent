#!/usr/bin/env python3
"""Test individual components of the AI digest agent."""

import os
import sys
from dotenv import load_dotenv

# Add src to path to import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from ai_digest_agent import (
    load_config,
    fetch_rss_items,
    Item,
    send_telegram,
    summarize_with_ollama,
)

load_dotenv()

print("=" * 60)
print("Component Test Suite")
print("=" * 60)

# Test 1: Config loading
print("\n[1] Testing config loading...")
try:
    config = load_config("config/feeds.yaml")
    print(f"✓ Config loaded. Found {len(config.get('rss_feeds', []))} RSS feeds")
except Exception as e:
    print(f"✗ Config loading failed: {e}")
    sys.exit(1)

# Test 2: RSS Feed fetching
print("\n[2] Testing RSS feed fetching...")
try:
    rss_urls = config.get("rss_feeds", [])[:2]  # Test with first 2 feeds
    items = fetch_rss_items(rss_urls, limit_per_feed=2)
    print(f"✓ Fetched {len(items)} items from RSS feeds")
    if items:
        print(f"  Sample: {items[0].title[:50]}...")
except Exception as e:
    print(f"✗ RSS fetching failed: {e}")

# Test 3: Ollama connectivity
print("\n[3] Testing Ollama...")
try:
    test_items = [Item(
        source="Test",
        title="Test Article",
        link="http://test.com",
        published="2025-01-01",
        summary="Test summary"
    )]
    summary = summarize_with_ollama(test_items)
    print(f"✓ Ollama response received ({len(summary)} chars)")
except Exception as e:
    print(f"✗ Ollama failed: {e}")
    print("  Make sure Ollama is running on http://localhost:11434")

# Test 4: Telegram connectivity
print("\n[4] Testing Telegram...")
try:
    test_msg = "✓ AI Digest Agent Test - All systems operational!"
    send_telegram(test_msg)
    print("✓ Telegram message sent successfully")
except Exception as e:
    print(f"✗ Telegram failed: {e}")

print("\n" + "=" * 60)
print("Test suite complete!")
print("=" * 60)
