#!/usr/bin/env python3
"""
Japan Post 小型印 (Kogata-in) Tracker
Scrapes the Japan Post website daily and notifies Discord of new commemorative postmarks.
"""

import json
import os
import re
import time
import hashlib
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
BASE_URL = "https://www.post.japanpost.jp/enjoy/culture/stamp/small/index.php"
IMAGE_BASE = "https://www.post.japanpost.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9",
}

# On first run, only scan this many pages to seed the state (avoids crawling years of history)
INIT_MAX_PAGES = 5
# On daily runs, check this many pages before stopping (catches bursts of new entries)
DAILY_MAX_PAGES = 3


# ── Scraping ──────────────────────────────────────────────────────────────────

def fetch_page(page: int) -> BeautifulSoup | None:
    url = f"{BASE_URL}?p={page}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch page {page}: {e}")
        return None


def get_text_after_label(text: str, label: str) -> str:
    """Extract value after a Japanese field label on the same or following line."""
    pattern = rf"{re.escape(label)}\s*[：:]\s*(.+?)(?=\n[^\s]|\Z)"
    m = re.search(pattern, text, re.DOTALL)
    if m:
        return m.group(1).strip().replace("\n", " ").replace("　", " ")
    return ""


def _extract_mail_address(raw: str) -> str:
    """多格式兜底提取郵頼送付先，搜索整块原始文本。"""
    # 格式1：～郵頼送付先～（波浪线包围）
    m = re.search(r"[～〜]郵頼送付先[～〜]\s*\n(.+?)(?=\s*※|\s*・「|\s*・引受|\s*【|\Z)", raw, re.DOTALL)
    if m:
        return m.group(1).strip().replace("\n", " ")
    # 格式2：郵頼送付先：（冒号格式）
    m = re.search(r"郵頼送付先[：:]\s*(.+?)(?=\s*※|\s*〇|\s*◎|\s*【|\Z)", raw, re.DOTALL)
    if m:
        return m.group(1).strip().replace("\n", " ")
    # 格式3：【郵頼について】区块内找〒邮编地址
    m = re.search(r"【郵頼について】.+?(〒\d{3}-\d{4}.+?)(?=\s*※|\s*・[^〒]|\Z)", raw, re.DOTALL)
    if m:
        return m.group(1).strip().replace("\n", " ")
    return "无"


def _extract_arrival(raw: str) -> str:
    """提取截止日期：到着分まで 或 必着 两种格式。"""
    # 格式1：※/〇 + 日期 + 到着分まで
    m = re.search(
        r"[※〇◎]\s*(\d{4}[/／年]\d{1,2}[月/／]\d{1,2}日?[（(]?[月火水木金土日]?[)）]?\s*到着分まで\S*)",
        raw
    )
    if m:
        return m.group(1).strip()
    # 格式2：日期 + まで + 必着
    m = re.search(
        r"(\d{4}年\d{1,2}月\d{1,2}日[（(][月火水木金土日][)）]まで[^\n。]{0,10}必着)",
        raw
    )
    if m:
        return m.group(1).strip()
    # 格式3：日期 + までに必着
    m = re.search(
        r"(\d{4}年\d{1,2}月\d{1,2}日[（(][月火水木金土日][)）]までに必着)",
        raw
    )
    if m:
        return m.group(1).strip()
    return "无"


def parse_entries(soup: BeautifulSoup) -> list[dict]:
    entries = []

    # Each entry contains an image with the kogata path
    images = soup.find_all("img", src=re.compile(r"kogata", re.I))

    for img in images:
        image_src = img.get("src", "")
        if not image_src:
            continue
        image_url = IMAGE_BASE + image_src if image_src.startswith("/") else image_src

        # Walk up to find an ancestor that contains all field labels
        container = img.parent
        for _ in range(8):
            if container is None:
                break
            t = container.get_text()
            if "使用期間" in t and "記念事項名称" in t:
                break
            container = container.parent

        if container is None:
            continue

        raw = container.get_text(separator="\n", strip=True)

        # Post office name: first line that ends with 郵便局 or contains it
        post_office = ""
        for line in raw.splitlines():
            line = line.strip()
            if "郵便局" in line and len(line) < 30:
                post_office = line
                break

        notes_raw = get_text_after_label(raw, "備考")

        mail_addr = _extract_mail_address(raw)
        arrival = _extract_arrival(raw)

        entry = {
            "post_office":  post_office,
            "event_name":   get_text_after_label(raw, "記念事項名称"),
            "usage_period": get_text_after_label(raw, "使用期間"),
            "location":     get_text_after_label(raw, "開設場所"),
            "notes":        notes_raw,
            "mail_address": mail_addr,
            "arrival":      arrival,
            "image_url":    image_url,
        }
        entries.append(entry)

    return entries


def make_id(entry: dict) -> str:
    """Stable unique ID based on post office + usage period."""
    key = f"{entry['post_office']}|{entry['usage_period']}|{entry['event_name']}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"known_ids": [], "last_check": None}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── Discord ───────────────────────────────────────────────────────────────────

def build_embed(entry: dict, title: str = "🔖 新しい小型印が登録されました") -> dict:
    """Build a Discord embed for a kogata-in entry."""
    fields = []
    if entry.get("event_name"):
        fields.append({"name": "📮 記念事項", "value": entry["event_name"], "inline": False})
    if entry.get("usage_period"):
        fields.append({"name": "📅 使用期間", "value": entry["usage_period"], "inline": True})
    if entry.get("post_office"):
        fields.append({"name": "🏣 郵便局", "value": entry["post_office"], "inline": True})
    if entry.get("location"):
        fields.append({"name": "📍 開設場所", "value": entry["location"], "inline": False})
    if entry.get("mail_address"):
        fields.append({"name": "✉️ 郵頼送付先", "value": entry["mail_address"], "inline": False})
    if entry.get("arrival"):
        fields.append({"name": "⏰ 到着分締切", "value": entry["arrival"], "inline": True})

    # 備考：去掉已单独显示的郵頼送付先/到着分部分，避免重复
    notes = entry.get("notes", "")
    notes = re.sub(r"郵頼送付先[：:].+", "", notes, flags=re.DOTALL).strip()
    notes = re.sub(r"※?\d{4}[/／年]\d{1,2}[月/／]\d{1,2}.{0,20}到着分まで\S*", "", notes).strip()
    if notes:
        fields.append({"name": "📝 備考", "value": notes[:400] + ("…" if len(notes) > 400 else ""), "inline": False})

    embed = {
        "title": title,
        "color": 0xE60012,
        "fields": fields,
        "footer": {"text": "Japan Post 小型印トラッカー"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    # Full-size image (not thumbnail)
    if entry.get("image_url"):
        embed["image"] = {"url": entry["image_url"]}

    return embed


def send_discord(entry: dict, title: str = "🔖 新しい小型印が登録されました"):
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL not set — skipping notification.")
        return

    payload = {"embeds": [build_embed(entry, title)]}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"[OK] Notified Discord: {entry.get('event_name') or entry.get('post_office')}")
    except requests.RequestException as e:
        print(f"[ERROR] Discord notification failed: {e}")
    time.sleep(0.5)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting kogata-in check...")

    state = load_state()
    known_ids: set[str] = set(state.get("known_ids", []))
    is_first_run = len(known_ids) == 0

    if is_first_run:
        print(f"[INFO] First run — seeding database from first {INIT_MAX_PAGES} pages (no notifications sent).")

    new_entries: list[dict] = []
    page = 1
    max_pages = INIT_MAX_PAGES if is_first_run else DAILY_MAX_PAGES

    while page <= max_pages:
        print(f"[INFO] Fetching page {page}...")
        soup = fetch_page(page)
        if soup is None:
            break

        entries = parse_entries(soup)
        if not entries:
            print(f"[INFO] No entries on page {page} — stopping.")
            break

        page_has_new = False
        for entry in entries:
            eid = make_id(entry)
            if eid not in known_ids:
                known_ids.add(eid)
                if not is_first_run:
                    new_entries.append(entry)
                    print(f"[NEW] {entry.get('event_name') or entry.get('post_office')}")
                page_has_new = True

        # On daily runs, stop once we hit a page with no new entries
        if not is_first_run and not page_has_new:
            print(f"[INFO] Page {page} has no new entries — stopping early.")
            break

        page += 1
        time.sleep(1.5)  # polite crawl delay

    if is_first_run:
        print(f"[INFO] Initial database seeded with {len(known_ids)} entries.")
    elif new_entries:
        print(f"[INFO] Found {len(new_entries)} new entry(s). Sending to Discord...")
        for entry in new_entries:
            send_discord(entry)
    else:
        print("[INFO] No new entries found.")

    state["known_ids"] = list(known_ids)
    state["last_check"] = datetime.now().isoformat()
    save_state(state)
    print("[INFO] Done.")


if __name__ == "__main__":
    main()
