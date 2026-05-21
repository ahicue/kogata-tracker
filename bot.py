#!/usr/bin/env python3
"""
風景印Bot 指令：
  风景印    → 推送最新风景印
  换        → 由新到旧浏览风景印（指针持久）
  好        → 引用消息，收藏该条
  发出      → 引用消息，标记已发出
  调出      → 显示所有收藏
  已发出    → 显示所有已发出

后台任务：每天 09:00 JST 自动检查新风景印并推送 Webhook
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import discord
from dotenv import load_dotenv

from fuke_tracker import (
    get_detail_ids_from_list, parse_detail, build_fuke_embed,
    make_fuke_id, run_fuke_daily_check,
)

load_dotenv()

BOT_TOKEN       = os.environ.get("DISCORD_BOT_TOKEN", "")
COLLECTION_FILE = os.path.join(os.path.dirname(__file__), "collection.json")
POINTER_FILE    = os.path.join(os.path.dirname(__file__), "fuke_pointer.json")

JST            = timezone(timedelta(hours=9))
DAILY_HOUR_JST = 9

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# 风景印缓存
_fuke_ids: list[str] = []
_fuke_cache: dict[str, dict] = {}
_fuke_list_page = 1
_fuke_list_done = False

# 消息ID → 条目映射
_msg_entry: dict[int, dict] = {}


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


# ── 指针持久化 ────────────────────────────────────────────────────────────────

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
    target = make_fuke_id(entry)
    return any(make_fuke_id(e) == target for e in lst)


# ── 发送辅助 ──────────────────────────────────────────────────────────────────

async def push_fuke(channel: discord.TextChannel, entry: dict, title: str) -> discord.Message:
    embed = discord.Embed.from_dict(build_fuke_embed(entry, title=title))
    msg = await channel.send(embed=embed)
    _msg_entry[msg.id] = entry
    return msg


# ── 每日检查后台任务 ──────────────────────────────────────────────────────────

async def daily_check_loop():
    await client.wait_until_ready()
    print("[DAILY] 風景印 checker started.")

    while not client.is_closed():
        now = datetime.now(JST)
        next_run = now.replace(hour=DAILY_HOUR_JST, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        print(f"[DAILY] Next check at {next_run.strftime('%Y-%m-%d %H:%M JST')} ({wait/3600:.1f}h)")
        await asyncio.sleep(wait)

        print("[DAILY] Running 風景印 check...")
        try:
            await asyncio.get_event_loop().run_in_executor(None, run_fuke_daily_check)
        except Exception as e:
            print(f"[DAILY] Error: {e}")


# ── Discord 事件 ──────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"[BOT] Logged in as {client.user}")
    _load_fuke_next_page()
    asyncio.ensure_future(daily_check_loop())


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    cmd = message.content.strip()

    if cmd == "风景印":
        async with message.channel.typing():
            entry = get_fuke_entry(0)
        if not entry:
            await message.channel.send("⚠️ 无法获取风景印信息。")
            return
        total = f"{len(_fuke_ids)}+" if not _fuke_list_done else str(len(_fuke_ids))
        await push_fuke(message.channel, entry, f"🏞️ 最新風景印（共 {total} 条）")

    elif cmd == "换":
        idx = load_pointer()
        async with message.channel.typing():
            entry = get_fuke_entry(idx)
        if not entry:
            await message.channel.send("📭 已到最旧一条，没有更多记录。")
            save_pointer(0)
            return
        total = f"{len(_fuke_ids)}+" if not _fuke_list_done else str(len(_fuke_ids))
        await push_fuke(message.channel, entry, f"🏞️ 風景印 第 {idx + 1} 条（共 {total}）")
        save_pointer(idx + 1)

    elif cmd == "好":
        if not message.reference:
            await message.channel.send("请**引用**一条风景印消息后再说「好」")
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
            await message.channel.send("请**引用**一条风景印消息后再说「发出」")
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
            await push_fuke(message.channel, entry, f"⭐ 收藏 {i+1}/{len(favs)}")
            await asyncio.sleep(0.5)

    elif cmd == "已发出":
        col = load_collection()
        sent = col["sent"]
        if not sent:
            await message.channel.send("📬 发出列表是空的。")
            return
        await message.channel.send(f"📬 共 {len(sent)} 条已发出：")
        for i, entry in enumerate(sent):
            await push_fuke(message.channel, entry, f"📬 已发出 {i+1}/{len(sent)}")
            await asyncio.sleep(0.5)


def main():
    if not BOT_TOKEN:
        print("[ERROR] .env 中未设置 DISCORD_BOT_TOKEN")
        return
    client.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
