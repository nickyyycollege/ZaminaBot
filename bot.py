import asyncio
import html
import logging
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import date, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup, NavigableString
from telegram import Bot

SOURCE_URL = os.getenv("SOURCE_URL", "https://rfc.nubip.edu.ua/to-a-student/changes-to-the-schedule/")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
TARGET_GROUP = os.getenv("TARGET_GROUP", "21-KI")
GROUP_LABEL = os.getenv("GROUP_LABEL", "21-КІ")  # how the group is shown in messages
DB_PATH = os.getenv("DB_PATH", "sent.sqlite3")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("schedule-bot")

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\d)")
DATE_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?")


def is_cancelled(row):
    for node in [row, *row.find_all(True)]:
        tag = node.name.lower() if node.name else ""
        style = (node.get("style") or "").lower().replace(" ", "")
        classes = " ".join(node.get("class", [])).lower()
        if tag in {"del", "s", "strike"} or "line-through" in style or "strike" in classes or "cancel" in classes:
            return True
    return False


def visible_text(cell):
    return " ".join(cell.stripped_strings).strip()


def _make_date(day, month, year_str, today):
    """Build a date; when the year is missing pick the year closest to today (handles Dec -> Jan)."""
    try:
        if year_str:
            year = int(year_str)
            year += 2000 if year < 100 else 0
            return date(year, month, day)
        options = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                options.append(date(year, month, day))
            except ValueError:
                pass
        return min(options, key=lambda d: abs((d - today).days)) if options else None
    except ValueError:
        return None


def find_date(text, today):
    low = text.lower()
    found = []
    for m in DATE_WORD.finditer(low):
        d = _make_date(int(m.group(1)), MONTHS[m.group(2)], m.group(3), today)
        if d:
            found.append((m.start(), d))
    for m in DATE_NUM.finditer(low):
        d = _make_date(int(m.group(1)), int(m.group(2)), m.group(3), today)
        if d:
            found.append((m.start(), d))
    return min(found, key=lambda x: x[0])[1] if found else None


def heading_date(text, today):
    """Date from a heading-like text ("Зміни до розкладу на 04.11.2026" or just "04.11.2026, середа")."""
    low = text.lower()
    if "оновл" in low or "опубл" in low:
        return None
    d = find_date(text, today)
    if not d:
        return None
    has_keyword = any(w in low for w in ("розклад", "зміни", "заняття"))
    short_date_only = len(text) < 60 and re.match(r"^\W*\d", text) is not None
    return d if (has_keyword or short_date_only) else None


def parse_schedule(html_text, today):
    """Return (set of dates found on the page, entries).
    entries: (date or None, section, group, lesson, subject) in document order."""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    dates, entries = set(), []
    current_date, section, last_group = None, "", ""

    for node in soup.descendants:
        if type(node) is NavigableString:
            if node.find_parent("table"):
                continue
            text = " ".join(str(node).split())
            if not text:
                continue
            d = heading_date(text, today)
            if d:
                current_date, section, last_group = d, "", ""
                dates.add(d)
        elif getattr(node, "name", None) == "table":
            if node.find_parent("table"):
                continue
            for row in node.find_all("tr"):
                cells = row.find_all(["td", "th"], recursive=False)
                if not cells or (row.find("th") and not row.find("td")):
                    continue
                values = [visible_text(cell) for cell in cells]
                if len(values) == 1 and values[0]:
                    d = heading_date(values[0], today)
                    if d:
                        current_date, section, last_group = d, "", ""
                        dates.add(d)
                    else:
                        section, last_group = values[0], ""
                    continue
                if len(values) < 3 or is_cancelled(row):
                    continue
                group, lesson, subject = values[0], values[1], " ".join(values[2:]).strip()
                if group:
                    last_group = group
                else:
                    group = last_group
                if not group or not lesson or not subject:
                    continue
                entries.append((current_date, section, group, lesson, subject))
    return dates, entries


async def fetch_schedule(today):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ScheduleChangesBot/1.0)"}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
        response = await client.get(SOURCE_URL)
        response.raise_for_status()
        page = BeautifulSoup(response.text, "html.parser")
        urls = []
        for iframe in page.find_all("iframe", src=True):
            frame_url = urljoin(str(response.url), iframe["src"])
            if frame_url.startswith(("http://", "https://")):
                urls.append(frame_url)
        # The site currently embeds its schedule in an iframe; use the page itself as fallback.
        candidates = urls or [str(response.url)]
        last_error, parsed_empty = None, None
        for url in candidates:
            try:
                frame = await client.get(url)
                frame.raise_for_status()
                dates, entries = parse_schedule(frame.text, today)
                if entries:
                    return dates, entries
                parsed_empty = parsed_empty or (dates, entries)
            except Exception as exc:
                last_error = exc
        if parsed_empty is not None:
            return parsed_empty
        raise RuntimeError(f"Could not read schedule page: {last_error}")


def group_key(value):
    # Match Latin K/I typed by the user to visually equivalent Cyrillic К/І on the college site.
    return re.sub(r"[^0-9a-z]", "", value.lower().translate(str.maketrans({"к": "k", "і": "i"})))


def select_rows(dates, entries, target):
    """Pick the target group's rows for the target date. Returns (rows, warning)."""
    mine = [e for e in entries if group_key(e[2]) == group_key(TARGET_GROUP)]
    if not entries:
        return [], "На сторінці не знайдено жодного запису — перевір сайт вручну."
    if not dates:
        return [e[1:] for e in mine], "Дату на сторінці визначити не вдалося — показано всі зміни для групи, перевір вручну."
    rows = [e[1:] for e in mine if e[0] == target]
    warning = "" if target in dates else "На сайті ще немає записів на цю дату."
    return rows, warning


def make_messages(target, rows, warning=""):
    lines = ["<b>Зміни до розкладу занять</b>", f"Дата: {target.strftime('%d.%m.%Y')} (завтра)", ""]
    if rows:
        current_section = None
        for section, group, lesson, subject in rows:
            if section and section != current_section:
                if current_section is not None:
                    lines.append("")
                current_section = section
                lines.append(f"<b>{html.escape(section)}</b>")
            lines.append(f"• <b>{html.escape(group)}</b>, пара {html.escape(lesson)}: {html.escape(subject)}")
    else:
        lines.append(f"Для {html.escape(GROUP_LABEL)} замін на завтра немає.")
    if warning:
        lines += ["", f"⚠️ {html.escape(warning)}"]
    lines += ["", f'<a href="{html.escape(SOURCE_URL, quote=True)}">Джерело</a>']
    text = "\n".join(lines)
    # Keep comfortably below Telegram's message limit.
    if len(text) <= 3900:
        return [text]
    chunks, current = [], []
    for line in lines:
        if sum(len(x) + 1 for x in current) + len(line) > 3700 and current:
            chunks.append("\n".join(current))
            current = ["<b>Зміни до розкладу, продовження</b>"]
        current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def already_sent(day):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("CREATE TABLE IF NOT EXISTS sent (day TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
        return con.execute("SELECT 1 FROM sent WHERE day=?", (day,)).fetchone() is not None


def mark_sent(day):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("CREATE TABLE IF NOT EXISTS sent (day TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
        con.execute("INSERT OR IGNORE INTO sent(day, sent_at) VALUES (?, ?)", (day, datetime.now(TZ).isoformat()))


async def send_daily(force=False):
    today = datetime.now(TZ).date()
    target = today + timedelta(days=1)  # the bot reports changes for TOMORROW
    day = today.isoformat()
    if not force and already_sent(day):
        log.info("Already posted for %s", day)
        return
    dates, entries = await fetch_schedule(today)
    rows, warning = select_rows(dates, entries, target)
    log.info("Dates on page: %s; target %s; rows for %s: %d; warning: %s",
             sorted(d.isoformat() for d in dates), target, TARGET_GROUP, len(rows), warning or "-")
    messages = make_messages(target, rows, warning)
    async with Bot(BOT_TOKEN) as bot:
        for message in messages:
            await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML", disable_web_page_preview=True)
    if not force:  # a RUN_NOW test must not block the real 18:00 post
        mark_sent(day)
    log.info("Posted for %s (%d changes)", target, len(rows))


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def start_health_server():
    """Tiny HTTP endpoint so a free web host (Render) + uptime pinger keeps the process awake."""
    port = os.getenv("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health endpoint listening on port %s", port)


async def main():
    start_health_server()
    if os.getenv("RUN_NOW", "0") == "1":
        await send_daily(force=True)
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(send_daily, "cron", hour=18, minute=0, id="daily-schedule", misfire_grace_time=3600)
    scheduler.start()
    log.info("Running; scheduled daily post at 18:00 %s", TZ.key)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
