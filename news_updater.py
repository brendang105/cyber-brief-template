#!/usr/bin/env python3
"""Cybersecurity news updater.

Pulls RSS feeds, remembers what it has already sent (SQLite), and delivers a
digest of new items via a Discord webhook. Optionally scores each
digest for severity (config/severity_keywords.txt) and sends a short notification
ping to a separate Discord channel. Run it twice a day with GitHub Actions,
cron, or Task Scheduler.
"""

import html
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

import severity

BASE = Path(__file__).parent
CONFIG = BASE / "config"  # keyword lists and alert messages live here
DB_PATH = BASE / "news.db"

FEEDS = {
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "Dark Reading": "https://www.darkreading.com/rss.xml",
    "Schneier on Security": "https://www.schneier.com/feed/atom/",
    "The Record": "https://therecord.media/feed",
}

MAX_PER_FEED = 8  # newest items considered per feed per run
MAX_IN_DIGEST = 25  # cap on items per digest
FIRST_RUN_LIMIT = 3  # on first run, only send a few per feed (avoid a flood)
KEEP_SEEN_DAYS = 120  # forget links older than this so the state file stays small


def load_env(path: Path) -> None:
    """Tiny .env loader so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def local_now() -> datetime:
    """Current time in TIMEZONE (e.g. America/Denver), else the machine's clock."""
    tz = os.environ.get("TIMEZONE", "").strip()
    return datetime.now(ZoneInfo(tz)) if tz else datetime.now()


def db() -> sqlite3.Connection:
    # NEWS_DB_PATH lets a scheduler (e.g. GitHub Actions) keep the file elsewhere.
    path = Path(os.environ.get("NEWS_DB_PATH") or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        "url TEXT PRIMARY KEY, title TEXT, source TEXT, first_seen REAL)"
    )
    return conn


def prune_seen(conn: sqlite3.Connection) -> None:
    cutoff = time.time() - KEEP_SEEN_DAYS * 86400
    conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
    conn.commit()


def clean(text: str, limit: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def get_author(entry, source: str) -> str:
    """Author name(s) from a feed entry, or '' if the feed doesn't give one."""
    names = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]
    if not names and entry.get("author"):
        names = [entry["author"]]
    cleaned = []
    for n in names:
        n = clean(re.sub(r"\S+@\S+", "", n), 60).strip(
            " ()<>,"
        )  # drop e-mail addresses
        if n and n.lower() != source.lower() and n not in cleaned:
            cleaned.append(n)
    return shorten(", ".join(cleaned), 80)


def author_line(author: str) -> str:
    """'By Name', except when the feed already says who it is from, such as
    'Sponsored by Cape', which is shown as is (no doubled 'By')."""
    if re.match(r"(sponsored|promoted|partner)\b", author, re.I):
        return author
    return f"By {author}"


def fetch_new(conn: sqlite3.Connection) -> list[dict]:
    first_run = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    new_items = []
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url, agent="cyber-news-updater/1.0")
        except Exception as exc:  # network or parse failure
            print(f"[warn] {source}: {exc}", file=sys.stderr)
            continue
        if feed.bozo and not feed.entries:
            print(f"[warn] {source}: could not read feed", file=sys.stderr)
            continue
        entries = feed.entries[: FIRST_RUN_LIMIT if first_run else MAX_PER_FEED]
        for e in entries:
            link = e.get("link")
            if not link:
                continue
            if conn.execute("SELECT 1 FROM seen WHERE url = ?", (link,)).fetchone():
                continue
            new_items.append(
                {
                    "source": source,
                    "title": clean(e.get("title", "(untitled)"), 140),
                    "url": link,
                    "blurb": clean(e.get("summary", "")),
                    "author": get_author(e, source),
                    # Longer text used only for severity scoring.
                    "score_text": clean(e.get("title", ""), 300)
                    + " "
                    + clean(e.get("summary", ""), 1500),
                }
            )
    return new_items


def mark_seen(conn: sqlite3.Connection, items: list[dict]) -> None:
    now = time.time()
    conn.executemany(
        "INSERT OR IGNORE INTO seen (url, title, source, first_seen) VALUES (?, ?, ?, ?)",
        [(i["url"], i["title"], i["source"], now) for i in items],
    )
    conn.commit()


# ---------- formatting ----------


def group_by_source(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for i in items:
        grouped.setdefault(i["source"], []).append(i)
    return grouped


def format_text(items: list[dict]) -> str:
    lines = []
    for source, group in group_by_source(items).items():
        lines.append(f"== {source} ==")
        for i in group:
            lines.append(f"- {i['title']}\n  {i['url']}")
            if i["blurb"]:
                lines.append(f"  {i['blurb']}")
            if i.get("author"):
                lines.append(f"  {author_line(i['author'])}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------- delivery ----------


def digest_title(now: datetime | None = None) -> str:
    """e.g. 'October 26 Morning Digest' / 'October 26 Evening Digest'."""
    now = now or local_now()
    if now.hour < 12:
        part = "Morning"
    elif now.hour < 15:
        part = "Afternoon"
    else:
        part = "Evening"
    return f"{now:%B} {now.day} {part} Digest"


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def discord_chunks(
    items: list[dict], blurbs: bool = False, limit: int = 1900
) -> list[str]:
    """Split the digest into messages under Discord's 2000-character cap.

    Each source's stories are kept together in one message, so a source
    heading appears only once. A new message starts when the next source
    won't fit. Only a single source that is too long for one message on its
    own is split, and its heading is repeated in that case.
    """
    # Build one block of text per source (heading + its stories).
    blocks = []
    for source, group in group_by_source(items).items():
        heading = f"__**{source}**__\n"
        entries = []
        for i in group:
            entry = f"• [{i['title']}](<{i['url']}>)\n"
            if blurbs and i["blurb"]:
                entry += f"> {shorten(i['blurb'], 160)}\n"
            if i.get("author"):
                entry += f"-# {author_line(i['author'])}\n"  # "-#" is Discord's small gray text
            entries.append(entry)
        blocks.append((heading, entries))

    chunks, current = [], ""
    for heading, entries in blocks:
        block = heading + "".join(entries) + "\n"
        if len(block) <= limit:
            # Whole source fits in a message: start a new message if it
            # won't fit alongside what we already have.
            if current and len(current) + len(block) > limit:
                chunks.append(current.rstrip())
                current = ""
            current += block
            continue
        # Rare: one source alone is longer than a message. Split it by story.
        if current:
            chunks.append(current.rstrip())
            current = ""
        part = heading
        for entry in entries:
            if len(part) + len(entry) > limit:
                chunks.append(part.rstrip())
                part = heading
            part += entry
        current = part + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def ping_settings() -> tuple[str, dict]:
    """Optional @mention so Discord sends a notification for each digest.

    DISCORD_PING_USER_IDS is a comma-separated list of Discord user IDs.
    Returns (text to put at the top of the first message, allowed_mentions).
    """
    ids = [
        i.strip()
        for i in os.environ.get("DISCORD_PING_USER_IDS", "").split(",")
        if i.strip().isdigit()
    ]
    if not ids:
        # No pings wanted: block every mention, including any "@everyone"
        # that might appear in a headline.
        return "", {"allowed_mentions": {"parse": []}}
    text = " ".join(f"<@{i}>" for i in ids) + "\n"
    # Only the listed users can be pinged; roles, @everyone and @here never are.
    return text, {"allowed_mentions": {"users": ids}}


def discord_guild_id(webhook: str) -> str:
    """Server ID, used to build a link to the digest post.

    Taken from DISCORD_GUILD_ID if set, otherwise asked from Discord using the
    webhook. Returns '' when it can't be found (the link is then left out).
    """
    guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if guild:
        return guild
    try:
        r = requests.get(webhook, timeout=10)
        r.raise_for_status()
        return str(r.json().get("guild_id") or "")
    except Exception:
        return ""


def send_discord(items: list[dict]) -> str | None:
    """Posts the digest. Returns None if Discord isn't configured, otherwise a
    link to the post ('' when the link can't be worked out)."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return None

    forum = os.environ.get("DISCORD_FORUM", "").strip().lower() in ("1", "true", "yes")
    ping_text, mentions = ping_settings()
    post_url = ""

    if forum:
        # Forum channel: each digest becomes its own post. The first message
        # creates the post (title + body); any extra messages go into the
        # same post using the thread id Discord returns.
        chunks = discord_chunks(items, blurbs=True)
        payload = {
            "content": ping_text + chunks[0],
            "thread_name": digest_title()[:100],
            **mentions,
        }
        tags = [
            t.strip()
            for t in os.environ.get("DISCORD_TAG_IDS", "").split(",")
            if t.strip()
        ]
        if tags:
            payload["applied_tags"] = tags
        r = requests.post(webhook, params={"wait": "true"}, json=payload, timeout=15)
        r.raise_for_status()
        thread_id = r.json()["channel_id"]
        for chunk in chunks[1:]:
            time.sleep(1)
            r = requests.post(
                webhook,
                params={"thread_id": thread_id},
                json={"content": chunk, **mentions},
                timeout=15,
            )
            r.raise_for_status()
        print(f"Posted to Discord forum: {payload['thread_name']}")
        guild = discord_guild_id(webhook)
        post_url = f"https://discord.com/channels/{guild}/{thread_id}" if guild else ""
    else:
        # Regular text channel: plain messages with a short header.
        chunks = discord_chunks(items)
        chunks[0] = ping_text + "**Cyber news digest**\n" + chunks[0]
        for n, chunk in enumerate(chunks):
            if n:
                time.sleep(1)
            body = {"content": chunk, **mentions}
            r = requests.post(webhook, json=body, timeout=15)
            r.raise_for_status()
        print("Sent to Discord.")
    return post_url


def load_messages(path: Path) -> dict[str, list[str]]:
    """Reads notify_messages.txt: [severe] / [notable] / [usual] sections with
    one message per line."""
    out: dict[str, list[str]] = {}
    cur = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            cur = m.group(1).lower()
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return out


def build_notification(
    level: str, top: dict | None, post_url: str, note: str = ""
) -> tuple[str, dict]:
    """Builds the alert text and its allowed_mentions. Only the one role in
    DISCORD_NOTIFY_ROLE_ID can ever be pinged (never @everyone, @here or other roles).
    """
    role = os.environ.get("DISCORD_NOTIFY_ROLE_ID", "").strip()
    role = role if role.isdigit() else ""
    options = load_messages(CONFIG / "notify_messages.txt").get(level) or [
        "Fresh digest is ready."
    ]
    text = random.choice(options)
    if top:
        for key, value in (
            ("{headline}", top["title"]),
            ("{source}", top["source"]),
            ("{why}", ", ".join(top["why"])),
        ):
            text = text.replace(key, value)
    lines = [(f"<@&{role}> " if role else "") + f"**NEW DIGEST:** {text}"]
    if top and level != "usual":
        lines.append(
            f"**{top['title']}** ({top['source']}) flagged for: {', '.join(top['why'])}"
        )
    if note:
        lines.append(note)
    if post_url:
        lines.append(post_url)
    mentions = {"roles": [role]} if role else {"parse": []}
    return "\n".join(lines)[:1900], mentions


def send_notification(
    level: str, top: dict | None, post_url: str, note: str = ""
) -> bool:
    """Posts the alert to the notification channel. Returns False if not configured."""
    webhook = os.environ.get("DISCORD_NOTIFY_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    content, mentions = build_notification(level, top, post_url, note)
    r = requests.post(
        webhook, json={"content": content, "allowed_mentions": mentions}, timeout=15
    )
    r.raise_for_status()
    print(f"Sent {level} alert to the notification channel.")
    return True


def main() -> int:
    load_env(BASE / ".env")
    dry_run = "--dry-run" in sys.argv

    conn = db()
    prune_seen(conn)
    items = fetch_new(conn)[:MAX_IN_DIGEST]
    if not items:
        print("No new items.")
        # Test aid: a manual run with nothing new still sends a "usual" alert, so
        # you can check the notification bot. Scheduled runs stay silent unless
        # DISCORD_NOTIFY_ON_EMPTY=true is set.
        wanted = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or (
            os.environ.get("DISCORD_NOTIFY_ON_EMPTY", "").strip().lower()
            in ("1", "true", "yes")
        )
        if wanted and not dry_run:
            try:
                send_notification("usual", None, "", note="No new stories this run.")
            except Exception as exc:
                print(f"[error] notification: {exc}", file=sys.stderr)
        return 0

    # Rate the digest. A problem with the keyword file must never stop the digest.
    try:
        level, top = severity.score_items(severity.load(CONFIG), items)
    except Exception as exc:
        print(f"[error] severity: {exc}", file=sys.stderr)
        level, top = "usual", None

    if dry_run:
        print(f"Post title: {digest_title()}")
        print(f"Severity: {level}")
        if top:
            print(
                f"Top story: {top['title']} ({top['source']}) matched: {', '.join(top['why'])}"
            )
        alert, _ = build_notification(level, top, "")
        print(f"Alert preview (not sent):\n{alert}")
        print()
        print(format_text(items))
        return 0

    post_url = None  # None = not sent; '' = sent, but no link to the post
    try:
        result = send_discord(items)
        if isinstance(result, str):
            post_url = result
    except Exception as exc:
        print(f"[error] send_discord: {exc}", file=sys.stderr)

    if post_url is not None:
        mark_seen(conn, items)  # only mark seen if something was actually sent
        try:
            send_notification(level, top, post_url)
        except Exception as exc:  # a failed alert must never undo the digest
            print(f"[error] notification: {exc}", file=sys.stderr)
    else:
        print("Discord is not configured or the post failed; items not marked seen.")
        print(format_text(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
