# Johnny-RSS-bot

Multi-feed Discord RSS bot with per-feed intervals, admin controls and rotating logs.

## Features

- Multiple RSS feeds
- Custom feed names
- Per-feed check interval (min 60s, max 86400s)
- Rate limited (max 5 posts per check)
- Admin-only feed management
- Rotating logs (2MB × 5 files)
- SQLite database (johnny.db)

---

## Requirements

- Python 3.10+
- discord.py
- feedparser
- beautifulsoup4
- python-dotenv

Install dependencies:

```bash
pip install -r requirements.txt
```

## Setup

1. Clone repository
2. Create .env file:
```bash
DISCORD_TOKEN=your_bot_token_here
DB_FILE=johnny.db
```

3. Run the bot
```bash
python johnny.py
```

## Commands

Admin Only

/addfeed name rss_url channel interval_seconds

/editfeed name [new_url] [new_channel] [new_interval]

/removefeed name

Available to everyone

/listfeeds

/latest name count

/latest is limited to 5 items.

## Database

The bot uses SQLite (johnny.db) and creates the table automatically on first run.

## Logging

Logs are written to:
```bash
logs/johnny.log
```

Rotating:
- 2 MB per file
- 5 backups max
- ~10 MB total cap

## Production Notes

Minimum interval: 60 seconds
Maximum interval: 86400 seconds (24h)
Max 5 posts per feed check
Loop runs every 30 seconds