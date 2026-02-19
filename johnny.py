print(">>> RSS Johnny starting")

import os
import sqlite3
import random
import time
import logging
from logging.handlers import RotatingFileHandler
import discord
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, UTC
from zoneinfo import ZoneInfo
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# ----------------------
# CONFIG
# ----------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_FILE = os.getenv("DB_FILE", "johnny.db")

MIN_INTERVAL = 60
MAX_INTERVAL = 86400
MAX_POSTS_PER_CHECK = 5
MAX_LATEST_COUNT = 5
MAX_DESCRIPTION_LENGTH = 3500

if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing in .env")

# ----------------------
# LOGGING (Rotating)
# ----------------------
os.makedirs("logs", exist_ok=True)

handler = RotatingFileHandler(
    "logs/johnny.log",
    maxBytes=2_000_000,
    backupCount=5
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler],
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("johnny")

# ----------------------
# DISCORD
# ----------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ----------------------
# DATABASE
# ----------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            rss_url TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            last_time REAL,
            interval_seconds INTEGER NOT NULL,
            last_checked REAL,
            last_entry_id TEXT,
            UNIQUE(guild_id, name)
        )
    """)

    conn.commit()
    conn.close()


def update_last_entry_id(feed_id, entry_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "UPDATE feeds SET last_entry_id = ? WHERE id = ?",
        (entry_id, feed_id)
    )
    conn.commit()
    conn.close()


def get_all_feeds():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT * FROM feeds")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_feed_by_name(guild_id, name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM feeds WHERE guild_id = ? AND name = ?",
        (guild_id, name)
    )
    row = cur.fetchone()
    conn.close()
    return row


def add_feed(guild_id, name, rss_url, channel_id, interval):
    interval = max(MIN_INTERVAL, min(interval, MAX_INTERVAL))
    now = time.time()

    parsed = feedparser.parse(rss_url)
    entries = parsed.entries if parsed.entries else []

    latest_timestamp = None

    for entry in entries:
        time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if time_struct:
            dt = datetime(*time_struct[:6])
            ts = dt.timestamp()
            if not latest_timestamp or ts > latest_timestamp:
                latest_timestamp = ts


    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO feeds (
            guild_id,
            name,
            rss_url,
            channel_id,
            last_time,
            interval_seconds,
            last_checked
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        guild_id,
        name,
        rss_url,
        channel_id,
        latest_timestamp,
        interval,
        now
    ))

    conn.commit()
    conn.close()


def remove_feed(guild_id, name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM feeds WHERE guild_id = ? AND name = ?",
        (guild_id, name)
    )
    conn.commit()
    conn.close()


def update_feed(guild_id, name, rss_url=None, channel_id=None, interval=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM feeds WHERE guild_id = ? AND name = ?",
        (guild_id, name)
    )
    feed = cur.fetchone()

    if not feed:
        conn.close()
        return False

    feed_id = feed[0]
    old_url = feed[3]
    old_channel = feed[4]
    old_interval = feed[6]

    new_url = rss_url if rss_url else old_url
    new_channel = channel_id if channel_id else old_channel
    new_interval = max(MIN_INTERVAL, min(interval, MAX_INTERVAL)) if interval else old_interval

    cur.execute("""
        UPDATE feeds
        SET rss_url = ?, channel_id = ?, interval_seconds = ?
        WHERE id = ?
    """, (new_url, new_channel, new_interval, feed_id))

    conn.commit()
    conn.close()
    return True

def update_last_time(feed_id, dt):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "UPDATE feeds SET last_time = ? WHERE id = ?",
        (dt.timestamp(), feed_id)
    )
    conn.commit()
    conn.close()


def update_last_checked(feed_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "UPDATE feeds SET last_checked = ? WHERE id = ?",
        (time.time(), feed_id)
    )
    conn.commit()
    conn.close()


# ----------------------
# RSS VALIDATION
# ----------------------
def validate_rss(url):
    try:
        parsed = feedparser.parse(url)

        if not parsed.entries:
            return False, "Feed contains no entries."

        # Kolla att minst en entry har timestamp
        has_timestamp = any(
            entry.get("published_parsed") or entry.get("updated_parsed")
            for entry in parsed.entries
        )

        if not has_timestamp:
            return False, "Feed contains no valid timestamps."

        return True, None

    except Exception as e:
        return False, str(e)


# ----------------------
# RSS PARSING
# ----------------------
def clean_entry(entry):
    title = entry.get("title", "No title")
    link = entry.get("link")
    description = entry.get("summary") or entry.get("description") or ""
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")

    soup = BeautifulSoup(description, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    for a in soup.find_all("a"):
        href = a.get("href")
        text = a.get_text(strip=True)
        if href:
            a.replace_with(f"[{text}]({href})")
        else:
            a.replace_with(text)

    text = soup.get_text().strip()
    if len(text) > MAX_DESCRIPTION_LENGTH:
        text = text[:MAX_DESCRIPTION_LENGTH - 3] + "..."

    return {
        "title": title,
        "link": link,
        "text": text,
        "time": time_struct
    }


def entry_to_datetime(entry):
    if entry["time"]:
        return datetime(*entry["time"][:6], tzinfo=UTC)
    return None
    

def format_time(dt_struct):
    if not dt_struct:
        return "Unknown date"

    dt = datetime(*dt_struct[:6], tzinfo=ZoneInfo("UTC"))
    local_dt = dt.astimezone(ZoneInfo("Europe/Stockholm"))
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


async def post_entry(channel, entry):
    embed = discord.Embed(
        title=entry["title"],
        url=entry["link"],
        description=entry["text"],
        color=discord.Color(random.randint(0, 0xFFFFFF))
    )

    embed.set_footer(text=f"Published: {format_time(entry['time'])}")
    await channel.send(embed=embed)


async def check_feed(feed):
    feed_id, guild_id, name, rss_url, channel_id, last_time, interval, last_checked, last_entry_id = feed
    now = time.time()

    # Respect interval
    if last_checked and now - last_checked < interval:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        parsed = feedparser.parse(rss_url)
        entries = parsed.entries if parsed.entries else []

        if not entries:
            update_last_checked(feed_id)
            return

        valid_entries = []

        for raw in entries:
            time_struct = raw.get("published_parsed") or raw.get("updated_parsed")
            if not time_struct:
                continue

            cleaned = clean_entry(raw)
            entry_dt = entry_to_datetime(cleaned)

            if not entry_dt:
                continue

            entry_id = raw.get("id") or raw.get("link")
            if not entry_id:
                continue

            valid_entries.append((entry_dt, cleaned, entry_id))

        if not valid_entries:
            update_last_checked(feed_id)
            return

        # Sort oldest → newest
        valid_entries.sort(key=lambda x: x[0])

        # First-time init: never post backlog
        if last_time is None:
            latest_dt, _, latest_id = valid_entries[-1]
            update_last_time(feed_id, latest_dt)
            update_last_entry_id(feed_id, latest_id)
            update_last_checked(feed_id)
            log.info(f"Feed '{name}' initialized without backlog posting")
            return

        stored_dt = datetime.fromtimestamp(last_time, UTC)
        new_entries = []

        for dt, entry, entry_id in valid_entries:
            # Skip already posted ID
            if last_entry_id and entry_id == last_entry_id:
                continue

            # Skip older timestamps
            if dt <= stored_dt:
                continue

            new_entries.append((dt, entry, entry_id))

        # Anti-flood
        new_entries = new_entries[:MAX_POSTS_PER_CHECK]

        for dt, entry, entry_id in new_entries:
            await post_entry(channel, entry)

        if new_entries:
            last_dt, _, last_id = new_entries[-1]
            update_last_time(feed_id, last_dt)
            update_last_entry_id(feed_id, last_id)
            log.info(f"Feed '{name}': posted {len(new_entries)} new entries")

        update_last_checked(feed_id)

    except Exception as e:
        log.error(f"Error checking feed '{name}': {e}")


# ----------------------
# LOOP
# ----------------------
@tasks.loop(seconds=30)
async def rss_loop():
    feeds = get_all_feeds()
    for feed in feeds:
        await check_feed(feed)


# ----------------------
# EVENTS
# ----------------------
@bot.event
async def on_ready():
    await tree.sync()
    print("Slash commands synced")
    log.info(f"Logged in as {bot.user}")
    init_db()
    rss_loop.start()


# ----------------------
# SLASH COMMANDS
# ----------------------
@tree.command(name="addfeed", description="Add a new RSS feed")
@app_commands.checks.has_permissions(administrator=True)
async def addfeed(interaction: discord.Interaction,
                  name: str,
                  rss_url: str,
                  channel: discord.TextChannel,
                  interval_seconds: int = 600):

    guild_id = interaction.guild.id

    valid, error = validate_rss(rss_url)
    if not valid:
        await interaction.response.send_message(
            f"Invalid RSS feed: {error}",
            ephemeral=True
        )
        return

    try:
        add_feed(guild_id, name, rss_url, channel.id, interval_seconds)
        log.info(f"Feed added: {name} (guild {guild_id})")
        await interaction.response.send_message(
            f"Feed '{name}' added.",
            ephemeral=True
        )
    except sqlite3.IntegrityError:
        await interaction.response.send_message(
            "Feed name already exists in this server.",
            ephemeral=True
        )


@tree.command(name="editfeed", description="Edit existing feed")
@app_commands.checks.has_permissions(administrator=True)
async def editfeed(interaction: discord.Interaction,
                   name: str,
                   new_url: str = None,
                   new_channel: discord.TextChannel = None,
                   new_interval: int = None):

    guild_id = interaction.guild.id

    if new_url:
        valid, error = validate_rss(new_url)
        if not valid:
            await interaction.response.send_message(
                f"Invalid RSS feed: {error}",
                ephemeral=True
            )
            return

    success = update_feed(
        guild_id,
        name,
        rss_url=new_url,
        channel_id=new_channel.id if new_channel else None,
        interval=new_interval
    )

    if success:
        log.info(f"Feed updated: {name} (guild {guild_id})")
        await interaction.response.send_message(
            f"Feed '{name}' updated.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "Feed not found in this server.",
            ephemeral=True
        )


@tree.command(name="removefeed", description="Remove a feed")
@app_commands.checks.has_permissions(administrator=True)
async def removefeed(interaction: discord.Interaction, name: str):

    guild_id = interaction.guild.id
    remove_feed(guild_id, name)

    log.info(f"Feed removed: {name} (guild {guild_id})")
    await interaction.response.send_message(
        f"Feed '{name}' removed.",
        ephemeral=True
    )


@tree.command(name="listfeeds", description="List configured feeds")
async def listfeeds(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT name, channel_id, interval_seconds FROM feeds WHERE guild_id = ?",
        (guild_id,)
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No feeds configured.", ephemeral=True)
        return

    msg = ""
    for name, channel_id, interval in rows:
        msg += f"{name} | <#{channel_id}> | every {interval}s\n"

    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="latest", description="Post latest items from a feed")
async def latest(interaction: discord.Interaction,
                 name: str,
                 count: int = 1):

    guild_id = interaction.guild.id

    feed = get_feed_by_name(guild_id, name)
    if not feed:
        await interaction.response.send_message(
            "Feed not found in this server.",
            ephemeral=True
        )
        return

    count = min(count, MAX_LATEST_COUNT)

    feed_id, guild_id, name, rss_url, channel_id, last_time, interval, last_checked, last_entry_id = feed

    parsed = feedparser.parse(rss_url)
    entries = parsed.entries if parsed.entries else []

    cleaned = [clean_entry(e) for e in entries[:count]]

    for entry in reversed(cleaned):
        await post_entry(interaction.channel, entry)

    await interaction.response.send_message(
        f"Posted {len(cleaned)} item(s).",
        ephemeral=True
    )


bot.run(TOKEN)
