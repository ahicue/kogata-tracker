#!/usr/bin/env python3
"""
Discord bot 指令：
  小型印      → 推送最新小型印
  换小型印    → 由新到旧浏览小型印
  风景印      → 推送最新风景印
  换风景印    → 由新到旧浏览风景印
  好          → 引用消息，收藏该条（小型印/风景印均可）
  发出        → 引用消息，标记已发出
  调出        → 显示所有收藏
  已发出      → 显示所有已发出

后台任务：每天 09:00 JST 自动检查新条目并推送 Webhook
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import discord
import requests
from dotenv import load_dotenv

from tracker import (
    fetch_page, parse_entries, build_embed, make_id,
    load_state, save_state, send_discord,
    INIT_MAX_PAGES, DAILY_MAX_PAGES,
)
from fuke_tracker import (
    get_detail_ids_from_list, parse_detail, build_fuke_embed,
    load_fuke_state, save_fuke_state, run_fuke_daily_check,
)

load_dotenv()

BOT_TOKEN           = os.environ.get("DISCORD_BOT_TOKEN", "")
POINTER_FILE        = os.path.join(os.path.dirname(__file__), "browse_pointer.json")
COLLECTION_FILE     = os.path.join(os.path.dirname(__file__), "collection.json")

JST = timezone(timedelta(hours=9))
DAILY_HOUR_JST = 9  # 毎朝 09:00 JST

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

_entries: list[dict] = []
_current_page = 1
_all_fetched = False
_last_fetch = 0.0
FETCH_COOLDOWN = 2.0

# 风景印缓存（按 detail_id 顺序，从新到旧）
_fuke_ids: list[str] = []        # detail_id 列表
_fuke_cache: dict[str, dict] = {}  # detail_id → entry
_fuke_list_page = 1
_fuke_list_done = False
FUKE_POINTER_FILE = os.path.join(os.path.dirname(__file__), "fuke_pointer.json")

_msg_entry: dict[int, dict] = {}


# ── 条目加载 ──────────────────────────────────────────────────────────────────

def _load_next_page() -> bool:
    global _current_page, _all_fetched, _last_fetch
    if _all_fetched:
        return False
    elapsed = time.time() - _last_fetch
    if elapsed < FETCH_COOLDOWN:
        time.sleep(FETCH_COOLDOWN - elapsed)
    soup = fetch_page(_current_page)
    _last_fetch = time.time()
    if not soup:
        _all_fetched = True
        return False
    entries = parse_entries(soup)
    if not entries:
        _all_fetched = True
        return False
    _entries.extend(entries)
    _current_page += 1
    return True


def get_entry(index: int) -> dict | None:
    while index >= len(_entries):
        if not _load_next_page():
            return None
    return _entries[index]


# ── 风景印加载 ────────────────────────────────────────────────────────────────

def _load_fuke_next_page() -> bool:
    global _fuke_list_page, _fuke_list_done
    if _fuke_list_done:
        return False
    ids = get_detail_ids_from_list(_fuke_list_page)
    if not ids:
        _fuke_list_done = True
        return False
    for did in ids:
        if did not in _fuke_cache:
            _fuke_ids.append(did)
    _fuke_list_page += 1
    return True


def get_fuke_entry(index: int) -> dict | None:
    while index >= len(_fuke_ids):
        if not _load_fuke_next_page():
            return None
    did = _fuke_ids[index]
    if did not in _fuke_cache:
        entry = parse_detail(did)
        if entry:
            _fuke_cache[did] = entry
        else:
            return None
    return _fuke_cache.get(did)


def load_fuke_pointer() -> int:
    if os.path.exists(FUKE_POINTER_FILE):
        with open(FUKE_POINTER_FILE) as f:
            return json.load(f).get("index", 0)
    return 0


def save_fuke_pointer(index: int):
    with open(FUKE_POINTER_FILE, "w") as f:
        json.dump({"index": index}, f)


# ── 小型印指针持久化 ──────────────────────────────────────────────────────────

def load_pointer() -> int:
    if os.path.exists(POINTER_FILE):
        with open(POINTER_FILE) as f:
            return json.load(f).get("index", 0)
    return 0


def save_pointer(index: int):
    with open(POINTER_FILE, "w") as f:
        json.dump({"index": index}, f)


# ── 收藏 / 已发出 ─────────────────────────────────────────────────────────────

def load_collection() -> dict:
    if os.path.exists(COLLECTION_FILE):
        with open(COLLECTION_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"favorites": [], "sent": []}


def save_collection(col: dict):
    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(col, f, ensure_ascii=False, indent=2)


def already_in(lst: list[dict], entry: dict) -> bool:
    target = make_id(entry)
    return any(make_id(e) == target for e in lst)


# ── 发送辅助 ──────────────────────────────────────────────────────────────────

async def push_entry(channel: discord.TextChannel, entry: dict, title: str) -> discord.Message:
    embed = discord.Embed.from_dict(build_embed(entry, title=title))
    msg = await channel.send(embed=embed)
    _msg_entry[msg.id] = entry
    return msg


# ── 每日检查后台任务 ──────────────────────────────────────────────────────────

async def daily_check_loop():
    """每天 09:00 JST 检查新小型印，有新的就通过 Webhook 推送。"""
    await client.wait_until_ready()
    print("[DAILY] Background checker started.")

    while not client.is_closed():
        now = datetime.now(JST)
        next_run = now.replace(hour=DAILY_HOUR_JST, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        print(f"[DAILY] Next check at {next_run.strftime('%Y-%m-%d %H:%M JST')} ({wait/3600:.1f}h)")
        await asyncio.sleep(wait)

        print("[DAILY] Running daily check...")
        try:
            state = load_state()
            known_ids: set[str] = set(state.get("known_ids", []))
            is_first_run = len(known_ids) == 0
            new_entries: list[dict] = []
            page = 1
            max_pages = INIT_MAX_PAGES if is_first_run else DAILY_MAX_PAGES

            while page <= max_pages:
                soup = fetch_page(page)
                if not soup:
                    break
                entries = parse_entries(soup)
                if not entries:
                    break
                page_has_new = False
                for entry in entries:
                    eid = make_id(entry)
                    if eid not in known_ids:
                        known_ids.add(eid)
                        if not is_first_run:
                            new_entries.append(entry)
                        page_has_new = True
                if not is_first_run and not page_has_new:
                    break
                page += 1
                await asyncio.sleep(1.5)

            if not is_first_run:
                for entry in new_entries:
                    send_discord(entry)
                print(f"[DAILY] Done. {len(new_entries)} new entry(s).")
            else:
                print(f"[DAILY] First run seed: {len(known_ids)} entries.")

            state["known_ids"] = list(known_ids)
            state["last_check"] = datetime.now().isoformat()
            save_state(state)

        except Exception as e:
            print(f"[DAILY] Error: {e}")

        # 同时运行风景印每日检查
        try:
            await asyncio.get_event_loop().run_in_executor(None, run_fuke_daily_check)
        except Exception as e:
            print(f"[DAILY][FUKE] Error: {e}")


# ── Discord 事件 ──────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"[BOT] Logged in as {client.user}")
    _load_next_page()
    _load_fuke_next_page()
    asyncio.ensure_future(daily_check_loop())


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    cmd = message.content.strip()

    if cmd == "小型印":
        async with message.channel.typing():
            entry = get_entry(0)
        if not entry:
            await message.channel.send("⚠️ 无法获取小型印信息。")
            return
        total = f"{len(_entries)}+" if not _all_fetched else str(len(_entries))
        await push_entry(message.channel, entry, f"🔖 最新小型印（共 {total} 条）")

    elif cmd == "换小型印":
        idx = load_pointer()
        async with message.channel.typing():
            entry = get_entry(idx)
        if not entry:
            await message.channel.send("📭 已到最旧一条，没有更多记录。")
            save_pointer(0)
            return
        total = f"{len(_entries)}+" if not _all_fetched else str(len(_entries))
        await push_entry(message.channel, entry, f"🔖 小型印 第 {idx + 1} 条（共 {total}）")
        save_pointer(idx + 1)

    # ── 风景印 ──
    elif cmd == "风景印":
        async with message.channel.typing():
            entry = get_fuke_entry(0)
        if not entry:
            await message.channel.send("⚠️ 无法获取风景印信息。")
            return
        total = f"{len(_fuke_ids)}+" if not _fuke_list_done else str(len(_fuke_ids))
        embed = discord.Embed.from_dict(build_fuke_embed(entry, f"🏞️ 最新風景印（共 {total} 条）"))
        msg = await message.channel.send(embed=embed)
        _msg_entry[msg.id] = entry

    elif cmd == "换风景印":
        idx = load_fuke_pointer()
        async with message.channel.typing():
            entry = get_fuke_entry(idx)
        if not entry:
            await message.channel.send("📭 已到最旧一条，没有更多记录。")
            save_fuke_pointer(0)
            return
        total = f"{len(_fuke_ids)}+" if not _fuke_list_done else str(len(_fuke_ids))
        embed = discord.Embed.from_dict(build_fuke_embed(entry, f"🏞️ 風景印 第 {idx + 1} 条（共 {total}）"))
        msg = await message.channel.send(embed=embed)
        _msg_entry[msg.id] = entry
        save_fuke_pointer(idx + 1)

    # ── 收藏 / 发出（小型印和风景印通用，通过引用自动识别类型）──
    elif cmd == "好":
        if not message.reference:
            await message.channel.send("请**引用**一条消息后再说「好」")
            return
        entry = _msg_entry.get(message.reference.message_id)
        if not entry:
            await message.channel.send("⚠️ 找不到对应记录（Bot 重启后旧消息需重新发送）")
            return
        col = load_collection()
        if already_in(col["favorites"], entry):
            await message.add_reaction("✅")
        else:
            col["favorites"].append(entry)
            save_collection(col)
            await message.add_reaction("⭐")

    elif cmd == "发出":
        if not message.reference:
            await message.channel.send("请**引用**一条消息后再说「发出」")
            return
        entry = _msg_entry.get(message.reference.message_id)
        if not entry:
            await message.channel.send("⚠️ 找不到对应记录（Bot 重启后旧消息需重新发送）")
            return
        col = load_collection()
        if already_in(col["sent"], entry):
            await message.add_reaction("✅")
        else:
            col["sent"].append(entry)
            save_collection(col)
            await message.add_reaction("📬")

    elif cmd == "调出":
        col = load_collection()
        favs = col["favorites"]
        if not favs:
            await message.channel.send("⭐ 收藏夹是空的。")
            return
        await message.channel.send(f"⭐ 共 {len(favs)} 条收藏：")
        for i, entry in enumerate(favs):
            is_fuke = entry.get("type") == "fuke"
            if is_fuke:
                embed = discord.Embed.from_dict(build_fuke_embed(entry, f"⭐ 收藏 {i+1}/{len(favs)}（風景印）"))
                msg = await message.channel.send(embed=embed)
            else:
                msg = await push_entry(message.channel, entry, f"⭐ 收藏 {i+1}/{len(favs)}（小型印）")
            _msg_entry[msg.id] = entry
            await asyncio.sleep(0.5)

    elif cmd == "已发出":
        col = load_collection()
        sent = col["sent"]
        if not sent:
            await message.channel.send("📬 发出列表是空的。")
            return
        await message.channel.send(f"📬 共 {len(sent)} 条已发出：")
        for i, entry in enumerate(sent):
            is_fuke = entry.get("type") == "fuke"
            if is_fuke:
                embed = discord.Embed.from_dict(build_fuke_embed(entry, f"📬 已发出 {i+1}/{len(sent)}（風景印）"))
                msg = await message.channel.send(embed=embed)
            else:
                msg = await push_entry(message.channel, entry, f"📬 已发出 {i+1}/{len(sent)}（小型印）")
            _msg_entry[msg.id] = entry
            await asyncio.sleep(0.5)


def main():
    if not BOT_TOKEN:
        print("[ERROR] .env 中未设置 DISCORD_BOT_TOKEN")
        return
    client.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
