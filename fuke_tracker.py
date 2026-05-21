#!/usr/bin/env python3
"""
Japan Post 風景印 (Fukeiin) Tracker
Scrapes detail pages for scenic postmarks and notifies Discord of new entries.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
FUKE_STATE_FILE = os.path.join(os.path.dirname(__file__), "fuke_state.json")
LIST_URL  = "https://www.post.japanpost.jp/enjoy/culture/stamp/fuke/item.php"
DETAIL_URL = "https://www.post.japanpost.jp/enjoy/culture/stamp/fuke/detail.php"
IMAGE_BASE = "https://www.post.japanpost.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9",
}

INIT_MAX_PAGES = 3
DAILY_MAX_PAGES = 2


# ── Scraping ──────────────────────────────────────────────────────────────────

def fetch_soup(url: str, params: dict = None) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        print(f"[FUKE][ERROR] {url}: {e}")
        return None


def get_detail_ids_from_list(page: int) -> list[str]:
    """Extract detail page IDs from a list page."""
    soup = fetch_soup(LIST_URL, params={"page": page})
    if not soup:
        return []
    ids = []
    for a in soup.find_all("a", href=re.compile(r"detail\.php\?id=\d+")):
        m = re.search(r"id=(\d+)", a["href"])
        if m:
            ids.append(m.group(1))
    return ids


def parse_detail(detail_id: str) -> dict | None:
    """Fetch and parse a 風景印 detail page."""
    soup = fetch_soup(DETAIL_URL, params={"id": detail_id})
    if not soup:
        return None

    raw = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in raw.splitlines()]

    def next_line_after(label: str) -> str:
        """Return the line immediately after the first line matching label."""
        for i, line in enumerate(lines):
            if line == label and i + 1 < len(lines):
                val = lines[i + 1].strip()
                return val if val else ""
        return ""

    # Image (use full-size, strip _thum suffix)
    img = soup.find("img", src=re.compile(r"/fuke/", re.I))
    image_url = ""
    if img:
        src = img.get("src", "")
        src_full = re.sub(r"_thum(\.[^.]+)$", r"\1", src)
        image_url = IMAGE_BASE + src_full if src_full.startswith("/") else src_full

    # Post office: standalone line containing 郵便局 (not page title)
    post_office = ""
    for line in lines[15:35]:  # skip header nav
        if "郵便局" in line and len(line) <= 20 and "風景印" not in line:
            post_office = line
            break

    # Prefecture: first occurrence of XX県/XX都/XX道/XX府
    prefecture = ""
    for line in lines[15:35]:
        if re.match(r".+[都道府県]$", line) and len(line) <= 10:
            prefecture = line
            break

    # 所在地: 〒 address under 開設場所
    address = "无"
    for i, line in enumerate(lines):
        if line == "開設場所" and i + 1 < len(lines):
            addr_parts = []
            for l in lines[i+1:i+4]:
                if l and not l.startswith("※"):
                    addr_parts.append(l)
                else:
                    break
            address = " ".join(addr_parts) if addr_parts else "无"
            break

    # 郵頼送付先: under 【郵頼送付先】
    mail_address = _extract_fuke_mail_address(lines, post_office, address)

    # 到着分 deadline
    arrival = _extract_fuke_arrival(raw)

    return {
        "type":         "fuke",
        "detail_id":    detail_id,
        "post_office":  post_office,
        "prefecture":   prefecture,
        "address":      address,
        "start_date":   next_line_after("使用開始日"),
        "end_date":     next_line_after("使用終了日"),
        "design":       next_line_after("意匠図案説明"),
        "designer":     next_line_after("図案作成者名"),
        "mail_address": mail_address,
        "arrival":      arrival,
        "image_url":    image_url,
    }


def _extract_address(raw: str) -> str:
    m = re.search(r"(〒\d{3}-\d{4}\s*.+?)(?=\n[^\s]|\Z)", raw, re.DOTALL)
    if m:
        return m.group(1).strip().replace("\n", " ")
    return "无"


def _extract_fuke_mail_address(lines: list[str], post_office: str, address: str) -> str:
    # 格式1：【郵頼送付先】 section（最常见）
    for i, line in enumerate(lines):
        if "郵頼送付先" in line:
            parts = []
            for l in lines[i+1:i+5]:
                if l and not l.startswith("(") and not l.startswith("・") and not l.startswith("※"):
                    parts.append(l)
                else:
                    break
            if parts:
                return " ".join(parts)
    # 格式2：address + 風景印担当あて
    raw_joined = "\n".join(lines)
    m = re.search(r"(〒\d{3}-\d{4}.+?風景印担当あて)", raw_joined, re.DOTALL)
    if m:
        return m.group(1).strip().replace("\n", " ")
    # 格式3：所在地 + 郵便局名 + 風景印担当あて
    if address != "无" and post_office:
        return f"{address} {post_office}　風景印担当あて"
    return "无"


def _extract_fuke_arrival(raw: str) -> str:
    # 初日押印締切 or 到着分
    m = re.search(
        r"(\d{1,2}月\d{1,2}日[（(][月火水木金土日][)）]到着分まで[^\n]*)",
        raw
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r"(\d{4}年\d{1,2}月\d{1,2}日[（(][月火水木金土日][)）]までに?必着[^\n]*)",
        raw
    )
    if m:
        return m.group(1).strip()
    return "无"


def make_fuke_id(entry: dict) -> str:
    key = entry.get("detail_id") or f"{entry['post_office']}|{entry['start_date']}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


# ── Embed ─────────────────────────────────────────────────────────────────────

def build_fuke_embed(entry: dict, title: str = "🏞️ 新しい風景印が登録されました") -> dict:
    fields = []

    if entry.get("post_office"):
        fields.append({"name": "🏣 郵便局", "value": entry["post_office"], "inline": True})
    if entry.get("prefecture"):
        fields.append({"name": "📍 都道府県", "value": entry["prefecture"], "inline": True})
    if entry.get("start_date"):
        label = "📅 使用開始日"
        val = entry["start_date"]
        if entry.get("end_date"):
            val += f" 〜 {entry['end_date']}"
        fields.append({"name": label, "value": val, "inline": False})
    if entry.get("design"):
        fields.append({"name": "🖼️ 意匠説明", "value": entry["design"][:200], "inline": False})
    if entry.get("mail_address"):
        fields.append({"name": "✉️ 郵頼送付先", "value": entry["mail_address"], "inline": False})
    if entry.get("arrival"):
        fields.append({"name": "⏰ 到着分締切", "value": entry["arrival"], "inline": True})

    embed = {
        "title": title,
        "color": 0x1E90FF,  # 风景印用蓝色区分小型印
        "fields": fields,
        "footer": {"text": "Japan Post 風景印トラッカー"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    if entry.get("image_url"):
        embed["image"] = {"url": entry["image_url"]}

    return embed


# ── Webhook ───────────────────────────────────────────────────────────────────

def send_fuke_discord(entry: dict, title: str = "🏞️ 新しい風景印が登録されました"):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"embeds": [build_fuke_embed(entry, title)]}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"[FUKE][OK] {entry.get('post_office')}")
    except requests.RequestException as e:
        print(f"[FUKE][ERROR] Discord: {e}")
    time.sleep(0.5)


# ── State ─────────────────────────────────────────────────────────────────────

def load_fuke_state() -> dict:
    if os.path.exists(FUKE_STATE_FILE):
        with open(FUKE_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"known_ids": [], "last_check": None}


def save_fuke_state(state: dict):
    with open(FUKE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── Main daily check ──────────────────────────────────────────────────────────

def run_fuke_daily_check() -> list[dict]:
    """Check for new 風景印 entries. Returns list of new entries found."""
    state = load_fuke_state()
    known_ids: set[str] = set(state.get("known_ids", []))
    is_first_run = len(known_ids) == 0
    new_entries: list[dict] = []
    max_pages = INIT_MAX_PAGES if is_first_run else DAILY_MAX_PAGES

    for page in range(1, max_pages + 1):
        detail_ids = get_detail_ids_from_list(page)
        if not detail_ids:
            break

        page_has_new = False
        for did in detail_ids:
            if did not in known_ids:
                known_ids.add(did)
                if not is_first_run:
                    entry = parse_detail(did)
                    if entry:
                        new_entries.append(entry)
                        print(f"[FUKE][NEW] {entry.get('post_office')} (id={did})")
                page_has_new = True
                time.sleep(0.8)

        if not is_first_run and not page_has_new:
            break

    if not is_first_run:
        for entry in new_entries:
            send_fuke_discord(entry)

    state["known_ids"] = list(known_ids)
    state["last_check"] = datetime.now().isoformat()
    save_fuke_state(state)

    if is_first_run:
        print(f"[FUKE] Seeded {len(known_ids)} entries.")
    else:
        print(f"[FUKE] Daily check done. {len(new_entries)} new.")

    return new_entries


if __name__ == "__main__":
    run_fuke_daily_check()
