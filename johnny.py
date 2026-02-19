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
from datetime import datetime
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
            name TEXT UNIQUE NOT NULL,
            rss_url TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            last_time REAL,
            interval_seconds INTEGER NOT NULL,
            last_checked REAL
        )
    """)

    conn.commit()
    conn.close()


def get_all_feeds():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT * FROM feeds")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_feed_by_name(name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT * FROM feeds WHERE name = ?", (name,))
    row = cur.fetchone()
    conn.close()
    return row


def add_feed(name, rss_url, channel_id, interval):
    interval = max(MIN_INTERVAL, min(interval, MAX_INTERVAL))
    now = time.time()

    parsed = feedparser.parse(rss_url)
    entries = parsed.entries if parsed.entries else []

    latest_timestamp = None

    for entry in entries:
        if entry.get("published_parsed"):
            dt = datetime(*entry["published_parsed"][:6])
            ts = dt.timestamp()
            if not latest_timestamp or ts > latest_timestamp:
                latest_timestamp = ts

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO feeds (
            name,
            rss_url,
            channel_id,
            last_time,
            interval_seconds,
            last_checked
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        name,
        rss_url,
        channel_id,
        latest_timestamp,
        interval,
        now
    ))

    conn.commit()
    conn.close()


def remove_feed(name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM feeds WHERE name = ?", (name,))
    conn.commit()
    conn.close()


def update_feed(name, rss_url=None, channel_id=None, interval=None):
    feed = get_feed_by_name(name)
    if not feed:
        return False

    _, _, old_url, old_channel, last_time, old_interval, last_checked = feed

    new_url = rss_url if rss_url else old_url
    new_channel = channel_id if channel_id else old_channel
    new_interval = old_interval

    if interval:
        new_interval = max(MIN_INTERVAL, min(interval, MAX_INTERVAL))

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        UPDATE feeds
        SET rss_url = ?, channel_id = ?, interval_seconds = ?
        WHERE name = ?
    """, (new_url, new_channel, new_interval, name))

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
        if parsed.bozo:
            return False, "Invalid RSS structure."
        if not parsed.entries:
            return False, "Feed contains no entries."
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
    time_struct = entry.get("published_parsed")

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
    if len(text) > 2000:
        text = text[:1997] + "..."

    return {
        "title": title,
        "link": link,
        "text": text,
        "time": time_struct
    }


def entry_to_datetime(entry):
    if entry["time"]:
        return datetime(*entry["time"][:6])
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
    feed_id, name, rss_url, channel_id, last_time, interval, last_checked = feed

    now = time.time()

    # Interval check
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

        # Clean + filter entries WITH timestamps only
        valid_entries = []

        for raw in entries:
            if not raw.get("published_parsed"):
                continue

            cleaned = clean_entry(raw)
            entry_dt = entry_to_datetime(cleaned)

            if entry_dt:
                valid_entries.append((entry_dt, cleaned))

        if not valid_entries:
            log.warning(f"Feed '{name}' has no valid timestamped entries")
            update_last_checked(feed_id)
            return

        # Sort oldest → newest
        valid_entries.sort(key=lambda x: x[0])

        # If last_time is None (should not happen after v1.0.1),
        # initialize it safely to newest timestamp and exit
        if not last_time:
            newest_dt = valid_entries[-1][0]
            update_last_time(feed_id, newest_dt)
            update_last_checked(feed_id)
            log.info(f"Feed '{name}' initialized with latest timestamp")
            return

        stored_dt = datetime.utcfromtimestamp(last_time)

        # Collect only entries newer than stored timestamp
        new_entries = [
            entry for dt, entry in valid_entries
            if dt > stored_dt
        ]

        # Anti-flood cap
        new_entries = new_entries[:MAX_POSTS_PER_CHECK]

        for entry in new_entries:
            await post_entry(channel, entry)

        # Update last_time ONLY if we posted something
        if new_entries:
            latest_dt = entry_to_datetime(new_entries[-1])
            if latest_dt:
                update_last_time(feed_id, latest_dt)
                log.info(f"Feed '{name}': posted {len(new_entries)} new entries")

        update_last_checked(feed_id)

    except Exception as e:
        log.error(f"Error checking feed '{name}': {e}"


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
async def addfeed(interaction: discord.Interaction, name: str,
                  rss_url: str,
                  channel: discord.TextChannel,
                  interval_seconds: int = 600):

    valid, error = validate_rss(rss_url)
    if not valid:
        await interaction.response.send_message(
            f"Invalid RSS feed: {error}",
            ephemeral=True
        )
        return

    try:
        add_feed(name, rss_url, channel.id, interval_seconds)
        log.info(f"Feed added: {name}")
        await interaction.response.send_message(
            f"Feed '{name}' added.",
            ephemeral=True
        )
    except sqlite3.IntegrityError:
        await interaction.response.send_message(
            "Feed name already exists.",
            ephemeral=True
        )


@tree.command(name="editfeed", description="Edit existing feed")
@app_commands.checks.has_permissions(administrator=True)
async def editfeed(interaction: discord.Interaction, name: str,
                   new_url: str = None,
                   new_channel: discord.TextChannel = None,
                   new_interval: int = None):

    if new_url:
        valid, error = validate_rss(new_url)
        if not valid:
            await interaction.response.send_message(
                f"Invalid RSS feed: {error}",
                ephemeral=True
            )
            return

    success = update_feed(
        name,
        rss_url=new_url,
        channel_id=new_channel.id if new_channel else None,
        interval=new_interval
    )

    if success:
        log.info(f"Feed updated: {name}")
        await interaction.response.send_message(
            f"Feed '{name}' updated.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "Feed not found.",
            ephemeral=True
        )


@tree.command(name="removefeed", description="Remove a feed")
@app_commands.checks.has_permissions(administrator=True)
async def removefeed(interaction: discord.Interaction, name: str):
    remove_feed(name)
    log.info(f"Feed removed: {name}")
    await interaction.response.send_message(
        f"Feed '{name}' removed.",
        ephemeral=True
    )


@tree.command(name="listfeeds", description="List configured feeds")
async def listfeeds(interaction: discord.Interaction):
    feeds = get_all_feeds()
    if not feeds:
        await interaction.response.send_message("No feeds configured.", ephemeral=True)
        return

    msg = ""
    for feed in feeds:
        _, name, _, channel_id, _, interval, _ = feed
        msg += f"{name} | <#{channel_id}> | every {interval}s\n"

    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="latest", description="Post latest items from a feed")
async def latest(interaction: discord.Interaction, name: str, count: int = 1):
    feed = get_feed_by_name(name)
    if not feed:
        await interaction.response.send_message("Feed not found.", ephemeral=True)
        return

    count = min(count, MAX_LATEST_COUNT)

    _, _, rss_url, _, _, _, _ = feed

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