#!/usr/bin/env python3
"""
Economic Calendar Bot - Telegram
Sumber: ForexFactory (lebih reliable)
Fitur: Jadwal otomatis + Command interaktif
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("EcoBot")

# ─── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID   = os.environ["CHAT_ID"]
TOPIC_ID  = int(os.environ.get("TOPIC_ID", "0")) or None
TIMEZONE  = ZoneInfo("Asia/Jakarta")

ALLOWED_CURRENCIES = {
    "USD", "EUR", "AUD", "NZD", "CNY", "CHF", "JPY", "CAD", "GBP", "IDR"
}
ALLOWED_IMPACT = {"medium", "high"}
CHECK_INTERVAL = 5

# ─── Emoji ──────────────────────────────────────────────────────────────────
IMPACT_EMOJI = {"high": "🔴", "medium": "🟡"}
FLAG = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "AUD": "🇦🇺", "CAD": "🇨🇦", "CHF": "🇨🇭", "NZD": "🇳🇿",
    "CNY": "🇨🇳", "IDR": "🇮🇩",
}
def flag(cur): return FLAG.get(cur.upper(), "🏳️")

# ─── Data Terbalik (actual > forecast = BEARISH) ─────────────────────────────
INVERTED_KEYWORDS = [
    "unemployment rate", "unemployment change", "unemployment claims",
    "jobless claims", "initial jobless", "continuing jobless",
    "initial claims", "continuing claims", "claimant count",
]

def is_inverted(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in INVERTED_KEYWORDS)

def get_sentiment(ev: dict) -> dict:
    actual   = ev.get("actual", "")
    forecast = ev.get("forecast", "")
    if not actual or not forecast:
        return {"label": "", "emoji": "", "icon": "📊"}
    def clean(s):
        s = re.sub(r"[^0-9.\-]", "", s.replace(",", "."))
        return float(s) if s else None
    a, f = clean(actual), clean(forecast)
    if a is None or f is None:
        return {"label": "", "emoji": "", "icon": "📊"}
    inv = is_inverted(ev.get("event", ""))
    if a > f:
        return {"label": "BEARISH", "emoji": "🔻", "icon": "📉"} if inv else {"label": "BULLISH", "emoji": "🟢", "icon": "📈"}
    elif a < f:
        return {"label": "BULLISH", "emoji": "🟢", "icon": "📈"} if inv else {"label": "BEARISH", "emoji": "🔻", "icon": "📉"}
    return {"label": "NETRAL", "emoji": "⚪", "icon": "➡️"}

# ─── ForexFactory Scraper ────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

FF_CURRENCY_MAP = {
    "usd": "USD", "eur": "EUR", "gbp": "GBP", "jpy": "JPY",
    "aud": "AUD", "cad": "CAD", "chf": "CHF", "nzd": "NZD",
    "cny": "CNY", "idr": "IDR",
}

async def fetch_calendar(target_date: date) -> list[dict]:
    url = f"https://www.forexfactory.com/calendar?day={target_date.strftime('%b%d.%Y').lower()}"
    log.info("Fetching %s", url)
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return parse_ff(resp.text, target_date)
    except Exception as e:
        log.error("Fetch error: %s", e)
        return []

def parse_ff(html: str, target_date: date) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="calendar__table")
    if not table:
        return []

    events = []
    cur_time = ""

    for row in table.find_all("tr", class_=re.compile(r"calendar__row")):
        if "calendar__row--day-breaker" in row.get("class", []):
            continue

        # Waktu
        time_td = row.find("td", class_="calendar__time")
        if time_td:
            t = time_td.get_text(strip=True)
            if t: cur_time = t

        # Mata uang
        cur_td = row.find("td", class_="calendar__currency")
        currency = cur_td.get_text(strip=True).upper() if cur_td else ""
        if not currency or currency not in ALLOWED_CURRENCIES:
            continue

        # Impact
        impact_td = row.find("td", class_="calendar__impact")
        impact = ""
        if impact_td:
            span = impact_td.find("span")
            if span:
                cls = " ".join(span.get("class", []))
                if "high" in cls: impact = "high"
                elif "medium" in cls: impact = "medium"
                elif "low" in cls: impact = "low"
        if impact not in ALLOWED_IMPACT:
            continue

        # Nama event
        event_td = row.find("td", class_="calendar__event")
        event_name = ""
        if event_td:
            sp = event_td.find("span", class_="calendar__event-title")
            event_name = sp.get_text(strip=True) if sp else event_td.get_text(strip=True)
        if not event_name:
            continue

        actual   = _ff_td(row, "actual")
        forecast = _ff_td(row, "forecast")
        previous = _ff_td(row, "previous")

        events.append({
            "time_str": cur_time,
            "datetime": parse_ff_time(cur_time, target_date),
            "currency": currency,
            "impact":   impact,
            "event":    event_name,
            "actual":   actual,
            "forecast": forecast,
            "previous": previous,
            "released": bool(actual),
            "inverted": is_inverted(event_name),
            "row_id":   f"{currency}_{event_name}_{cur_time}",
        })

    log.info("Parsed %d events", len(events))
    return events

def _ff_td(row, cls):
    td = row.find("td", class_=f"calendar__{cls}")
    if not td: return ""
    t = td.get_text(strip=True)
    return "" if t in ("&nbsp;", "\xa0", "") else t

def parse_ff_time(time_str: str, base_date: date):
    if not time_str or time_str.lower() in ("all day", "tentative"):
        return None
    try:
        ts = time_str.strip().lower()
        fmt = "%I:%M%p" if ":" in ts else "%I%p"
        naive = datetime.strptime(ts, fmt).replace(
            year=base_date.year, month=base_date.month, day=base_date.day
        )
        # FF = ET (UTC-4 DST) → WIB (UTC+7) = +11 jam
        return (naive + timedelta(hours=11)).replace(tzinfo=TIMEZONE)
    except Exception:
        return None

# ─── State ──────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.events: list[dict] = []
        self.sent_ids: set = set()

    def reset(self, events):
        self.events = events
        self.sent_ids = set()

state = State()

# ─── Formatters ─────────────────────────────────────────────────────────────
HARI  = ["Senin","Selasa","Rabu","Kamis","Jumat","Sabtu","Minggu"]
BULAN = ["Jan","Feb","Mar","Apr","Mei","Jun","Jul","Agt","Sep","Okt","Nov","Des"]

def tgl_str(d: date):
    return f"{HARI[d.weekday()]}, {d.day} {BULAN[d.month-1]} {d.year}"

def fmt_schedule(events, d, title="JADWAL DATA EKONOMI"):
    lines = [f"📅 *{title}*", f"_{tgl_str(d)} • WIB_", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    if not events:
        lines.append("_Tidak ada data medium/high impact._")
        lines += ["", "━━━━━━━━━━━━━━━━━━━━━━", "🔴 High  🟡 Medium"]
        return "\n".join(lines)

    by_cur: dict[str, list] = {}
    for ev in sorted(events, key=lambda x: x["time_str"] or "99:99"):
        by_cur.setdefault(ev["currency"], []).append(ev)

    for cur in sorted(by_cur):
        lines.append(f"{flag(cur)} *{cur}*")
        for ev in by_cur[cur]:
            imp = IMPACT_EMOJI.get(ev["impact"], "⚪")
            t   = ev["time_str"] or "Tentative"
            # Konversi waktu ET ke WIB untuk display
            wib = ev.get("datetime")
            t_wib = wib.strftime("%H:%M") if wib else "?"
            inv = " _\\*_" if ev["inverted"] else ""
            lines.append(f"  {imp} `{t_wib} WIB` — {ev['event']}{inv}")
            if ev["forecast"]: lines.append(f"       📌 Forecast: `{ev['forecast']}`")
            if ev["previous"]: lines.append(f"       ⏮ Previous: `{ev['previous']}`")
        lines.append("")

    lines += ["━━━━━━━━━━━━━━━━━━━━━━", "🔴 High  🟡 Medium  _\\* = logika terbalik_", "🔔 Notif otomatis saat data rilis"]
    return "\n".join(lines)

def fmt_release(ev):
    imp  = IMPACT_EMOJI.get(ev["impact"], "⚪")
    sent = get_sentiment(ev)
    wib  = ev.get("datetime")
    t_wib = wib.strftime("%H:%M") if wib else ev["time_str"]
    inv_note = "\n⚠️ _Data terbalik: lebih kecil = lebih baik_" if ev.get("inverted") else ""
    parts = [
        f"{sent['icon']} *DATA RILIS!*", "",
        f"{flag(ev['currency'])} *{ev['currency']}* {imp}",
        f"📌 *{ev['event']}*", "",
        f"✅ Actual   : `{ev['actual'] or 'N/A'}`",
        f"🎯 Forecast : `{ev['forecast'] or 'N/A'}`",
        f"⏮️ Previous : `{ev['previous'] or 'N/A'}`", "",
    ]
    if sent["label"]:
        parts.append(f"{sent['emoji']} *{sent['label']}*")
    if inv_note:
        parts.append(inv_note)
    parts += ["", f"🕐 `{t_wib} WIB`"]
    return "\n".join(parts)

def fmt_recap(events, d):
    released = [e for e in events if e["released"]]
    pending  = [e for e in events if not e["released"]]
    lines = [f"📋 *REKAP DATA EKONOMI*", f"_{tgl_str(d)} • WIB_", "━━━━━━━━━━━━━━━━━━━━━━", ""]

    if released:
        lines.append("✅ *SUDAH RILIS*")
        for ev in released:
            imp  = IMPACT_EMOJI.get(ev["impact"], "⚪")
            sent = get_sentiment(ev)
            s    = f" {sent['emoji']} {sent['label']}" if sent["label"] else ""
            lines.append(f"  {flag(ev['currency'])} {imp} *{ev['event']}*\n    Actual: `{ev['actual']}` | Forecast: `{ev['forecast'] or '-'}`{s}")
        lines.append("")

    if pending:
        lines.append("⏳ *BELUM RILIS*")
        for ev in pending:
            imp = IMPACT_EMOJI.get(ev["impact"], "⚪")
            wib = ev.get("datetime")
            t   = wib.strftime("%H:%M WIB") if wib else (ev["time_str"] or "Tentative")
            lines.append(f"  {flag(ev['currency'])} {imp} {ev['event']} — `{t}`")
        lines.append("")

    if not released and not pending:
        lines.append("_Tidak ada data ekonomi hari ini._")

    lines += ["━━━━━━━━━━━━━━━━━━━━━━", f"Total: *{len(released)} rilis* • *{len(pending)} pending*", "🌙 Jadwal besok dikirim jam 00:00 WIB"]
    return "\n".join(lines)

# ─── Send Helper ────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)

async def send(text: str):
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

# ─── Scheduled Jobs ──────────────────────────────────────────────────────────
async def job_morning():
    today = datetime.now(TIMEZONE).date()
    log.info("[00:00] Jadwal %s", today)
    events = await fetch_calendar(today)
    state.reset(events)
    await send(fmt_schedule(events, today))

async def job_realtime():
    if not state.events: return
    today = datetime.now(TIMEZONE).date()
    fresh = await fetch_calendar(today)
    if not fresh: return
    state.events = fresh
    for ev in fresh:
        eid = ev["row_id"]
        if ev["released"] and eid not in state.sent_ids:
            state.sent_ids.add(eid)
            await send(fmt_release(ev))
            await asyncio.sleep(1)

async def job_recap():
    today = datetime.now(TIMEZONE).date()
    log.info("[23:00] Rekap %s", today)
    events = await fetch_calendar(today)
    state.events = events
    await send(fmt_recap(events, today))

# ─── Command Handlers ────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Economic Calendar Bot*\n\n"
        "*Command yang tersedia:*\n"
        "/hariini — Jadwal data ekonomi hari ini\n"
        "/besok — Jadwal data ekonomi besok\n"
        "/usd — Jadwal data USD hari ini\n"
        "/eur — Jadwal data EUR hari ini\n"
        "/gbp — Jadwal data GBP hari ini\n"
        "/jpy — Jadwal data JPY hari ini\n"
        "/aud — Jadwal data AUD hari ini\n"
        "/cad — Jadwal data CAD hari ini\n"
        "/chf — Jadwal data CHF hari ini\n"
        "/nzd — Jadwal data NZD hari ini\n"
        "/cny — Jadwal data CNY hari ini\n"
        "/cari \\[nama\\] — Cari event spesifik \\(contoh: /cari FOMC\\)\n"
        "/rekap — Rekap data yang sudah rilis hari ini",
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

async def cmd_hariini(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TIMEZONE).date()
    events = state.events or await fetch_calendar(today)
    if not state.events: state.reset(events)
    await update.message.reply_text(
        fmt_schedule(events, today),
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

async def cmd_besok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    besok = datetime.now(TIMEZONE).date() + timedelta(days=1)
    events = await fetch_calendar(besok)
    await update.message.reply_text(
        fmt_schedule(events, besok, "JADWAL BESOK"),
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

async def cmd_rekap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TIMEZONE).date()
    events = state.events or await fetch_calendar(today)
    await update.message.reply_text(
        fmt_recap(events, today),
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

async def cmd_negara(update: Update, ctx: ContextTypes.DEFAULT_TYPE, currency: str):
    today = datetime.now(TIMEZONE).date()
    events = state.events or await fetch_calendar(today)
    if not state.events: state.reset(events)
    filtered = [e for e in events if e["currency"] == currency]
    lines = [
        f"{flag(currency)} *DATA {currency} HARI INI*",
        f"_{tgl_str(today)}_",
        "━━━━━━━━━━━━━━━━━━━━━━", "",
    ]
    if not filtered:
        lines.append(f"_Tidak ada data {currency} medium/high impact hari ini._")
    else:
        for ev in filtered:
            imp   = IMPACT_EMOJI.get(ev["impact"], "⚪")
            wib   = ev.get("datetime")
            t_wib = wib.strftime("%H:%M") if wib else "?"
            status = ""
            if ev["released"]:
                sent = get_sentiment(ev)
                status = f"\n    ✅ `{ev['actual']}` vs `{ev['forecast'] or '-'}` {sent['emoji']} {sent['label']}"
            lines.append(f"  {imp} `{t_wib} WIB` — *{ev['event']}*")
            if not ev["released"] and ev["forecast"]:
                lines.append(f"    📌 Forecast: `{ev['forecast']}`")
            if status: lines.append(status)
    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━"]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

async def cmd_cari(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cari event spesifik misal /cari FOMC"""
    if not ctx.args:
        await update.message.reply_text("Contoh penggunaan: `/cari FOMC`", parse_mode=ParseMode.MARKDOWN)
        return

    keyword = " ".join(ctx.args).lower()
    today   = datetime.now(TIMEZONE).date()

    # Cari di hari ini, besok, minggu depan
    results = []
    for delta in range(8):
        d = today + timedelta(days=delta)
        evs = await fetch_calendar(d)
        found = [e for e in evs if keyword in e["event"].lower()]
        for ev in found:
            ev["_date"] = d
            results.append(ev)
        if results and delta > 0:
            break
        await asyncio.sleep(0.5)

    if not results:
        await update.message.reply_text(
            f"❌ Tidak ditemukan event mengandung *{keyword.upper()}* dalam 7 hari ke depan.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [
        f"🔍 *HASIL PENCARIAN: {keyword.upper()}*",
        "━━━━━━━━━━━━━━━━━━━━━━", "",
    ]
    for ev in results[:10]:
        imp   = IMPACT_EMOJI.get(ev["impact"], "⚪")
        d     = ev["_date"]
        wib   = ev.get("datetime")
        t_wib = wib.strftime("%H:%M") if wib else "?"
        hari  = "Hari ini" if d == today else ("Besok" if d == today + timedelta(1) else tgl_str(d))
        lines += [
            f"{flag(ev['currency'])} *{ev['currency']}* {imp}",
            f"📌 *{ev['event']}*",
            f"📅 {hari} — `{t_wib} WIB`",
        ]
        if ev["forecast"]: lines.append(f"📌 Forecast: `{ev['forecast']}`")
        if ev["previous"]: lines.append(f"⏮ Previous: `{ev['previous']}`")
        if ev["released"]:
            sent = get_sentiment(ev)
            lines.append(f"✅ Actual: `{ev['actual']}` {sent['emoji']} {sent['label']}")
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

# Factory untuk command per negara
def make_negara_handler(cur):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await cmd_negara(update, ctx, cur)
    return handler

# ─── Main ────────────────────────────────────────────────────────────────────
async def main():
    log.info("Bot starting...")

    # Pre-load hari ini
    today = datetime.now(TIMEZONE).date()
    events = await fetch_calendar(today)
    state.reset(events)
    log.info("Pre-load: %d events", len(events))

    # Setup application (untuk command handler)
    app = Application.builder().token(BOT_TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("hariini", cmd_hariini))
    app.add_handler(CommandHandler("besok",   cmd_besok))
    app.add_handler(CommandHandler("rekap",   cmd_rekap))
    app.add_handler(CommandHandler("cari",    cmd_cari))

    for cur in ["usd","eur","gbp","jpy","aud","cad","chf","nzd","cny","idr"]:
        app.add_handler(CommandHandler(cur, make_negara_handler(cur.upper())))

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(job_morning,  CronTrigger(hour=0,  minute=0,  timezone=TIMEZONE), id="morning")
    scheduler.add_job(job_realtime, "interval", minutes=CHECK_INTERVAL, id="realtime")
    scheduler.add_job(job_recap,    CronTrigger(hour=23, minute=0,  timezone=TIMEZONE), id="recap")
    scheduler.start()
    log.info("Scheduler aktif ✅")

    # Startup message
    try:
        await send(
            "🤖 *Economic Calendar Bot aktif!*\n\n"
            "📅 Jadwal → `00:00 WIB`\n"
            "🔔 Real-time update saat data rilis\n"
            "📋 Rekap → `23:00 WIB`\n\n"
            "*Command:*\n"
            "/hariini • /besok • /rekap\n"
            "/usd • /eur • /gbp • /jpy\n"
            "/aud • /cad • /chf • /nzd\n"
            "/cari FOMC • /cari NFP\n\n"
            "🔴 High  🟡 Medium\n"
            "🟢 Bullish  🔻 Bearish  ⚪ Netral"
        )
    except Exception as e:
        log.warning("Startup msg: %s", e)

    # Jalankan bot (polling)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Bot polling aktif ✅")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("Bot stopped.")

if __name__ == "__main__":
    asyncio.run(main())
