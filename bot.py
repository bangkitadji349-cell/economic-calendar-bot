#!/usr/bin/env python3
"""
Economic Calendar Bot - Telegram
Sumber: Finnhub API (reliable, tidak diblokir)
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import httpx
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("EcoBot")

# ─── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
CHAT_ID      = os.environ["CHAT_ID"]
TOPIC_ID     = int(os.environ.get("TOPIC_ID", "0")) or None
FINNHUB_KEY  = os.environ["FINNHUB_KEY"]
TIMEZONE     = ZoneInfo("Asia/Jakarta")
CHECK_INTERVAL = 5

ALLOWED_CURRENCIES = {
    "USD", "EUR", "AUD", "NZD", "CNY", "CHF", "JPY", "CAD", "GBP", "IDR"
}

# ─── Emoji ──────────────────────────────────────────────────────────────────
IMPACT_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
FLAG = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "AUD": "🇦🇺", "CAD": "🇨🇦", "CHF": "🇨🇭", "NZD": "🇳🇿",
    "CNY": "🇨🇳", "IDR": "🇮🇩",
}
def flag(cur): return FLAG.get(cur.upper(), "🏳️")

# Mapping country code Finnhub → currency
COUNTRY_CURRENCY = {
    "US": "USD", "EU": "EUR", "GB": "GBP", "JP": "JPY",
    "AU": "AUD", "CA": "CAD", "CH": "CHF", "NZ": "NZD",
    "CN": "CNY", "ID": "IDR",
}

# ─── Data Terbalik ───────────────────────────────────────────────────────────
INVERTED_KEYWORDS = [
    "unemployment rate", "unemployment change", "unemployment claims",
    "jobless claims", "initial jobless", "continuing jobless",
    "initial claims", "continuing claims", "claimant count",
]

def is_inverted(name: str) -> bool:
    return any(k in name.lower() for k in INVERTED_KEYWORDS)

def get_sentiment(ev: dict) -> dict:
    actual   = str(ev.get("actual") or "")
    forecast = str(ev.get("estimate") or "")
    if not actual or not forecast or actual in ("None", "") or forecast in ("None", ""):
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

# ─── Finnhub Fetcher ─────────────────────────────────────────────────────────
async def fetch_calendar(from_date: date, to_date: date = None) -> list[dict]:
    if to_date is None:
        to_date = from_date
    url = "https://finnhub.io/api/v1/calendar/economic"
    params = {
        "from": from_date.strftime("%Y-%m-%d"),
        "to":   to_date.strftime("%Y-%m-%d"),
        "token": FINNHUB_KEY,
    }
    log.info("Fetching Finnhub %s → %s", from_date, to_date)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.error("Finnhub fetch error: %s", e)
        return []

    raw = data.get("economicCalendar", [])
    events = []
    for item in raw:
        # Currency dari country code
        country = (item.get("country") or "").upper()
        currency = COUNTRY_CURRENCY.get(country, "")
        if not currency or currency not in ALLOWED_CURRENCIES:
            continue

        # Parse waktu → WIB
        time_raw = item.get("time") or ""
        event_dt = parse_finnhub_time(time_raw)

        # Impact
        impact_raw = (item.get("impact") or "").lower()
        impact = impact_raw if impact_raw in ("high", "medium", "low") else "low"

        event_name = item.get("event") or ""
        actual     = item.get("actual")
        estimate   = item.get("estimate")
        prev       = item.get("prev")

        # Format angka
        def fmt_num(v):
            if v is None or str(v) in ("None", ""):
                return ""
            return str(v)

        actual_str   = fmt_num(actual)
        estimate_str = fmt_num(estimate)
        prev_str     = fmt_num(prev)

        ev = {
            "time_str": event_dt.strftime("%H:%M") if event_dt else "?",
            "datetime": event_dt,
            "currency": currency,
            "country":  country,
            "impact":   impact,
            "event":    event_name,
            "actual":   actual_str,
            "estimate": estimate_str,
            "forecast": estimate_str,
            "previous": prev_str,
            "released": bool(actual_str),
            "inverted": is_inverted(event_name),
            "row_id":   f"{currency}_{event_name}_{time_raw}",
            "date":     event_dt.date() if event_dt else from_date,
        }
        events.append(ev)

    log.info("Finnhub: %d events", len(events))
    return events


def parse_finnhub_time(time_str: str):
    """Parse waktu Finnhub (UTC) ke WIB (UTC+7)."""
    if not time_str:
        return None
    try:
        # Format: "2026-05-11T14:30:00+00:00" atau "2026-05-11 14:30:00"
        from datetime import timezone
        ts = time_str.replace("Z", "+00:00")
        if "T" in ts:
            dt_utc = datetime.fromisoformat(ts)
        else:
            dt_utc = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        # Pastikan UTC
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        # Konversi ke WIB
        return dt_utc.astimezone(TIMEZONE)
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

    day_events = [e for e in events if e.get("date") == d]

    if not day_events:
        lines += ["_Tidak ada data ekonomi untuk negara filter hari ini._", "", "━━━━━━━━━━━━━━━━━━━━━━"]
        return "\n".join(lines)

    by_cur: dict[str, list] = {}
    for ev in sorted(day_events, key=lambda x: x["time_str"] or "99:99"):
        by_cur.setdefault(ev["currency"], []).append(ev)

    for cur in sorted(by_cur):
        lines.append(f"{flag(cur)} *{cur}*")
        for ev in by_cur[cur]:
            imp   = IMPACT_EMOJI.get(ev["impact"], "🟢")
            t_wib = ev["time_str"]
            inv   = " _(inv)_" if ev["inverted"] else ""
            status = " ✅" if ev["released"] else ""
            lines.append(f"  {imp} `{t_wib} WIB` — {ev['event']}{inv}{status}")
            if ev["forecast"] and not ev["released"]:
                lines.append(f"       📌 Forecast: `{ev['forecast']}`")
            if ev["previous"]:
                lines.append(f"       ⏮ Previous: `{ev['previous']}`")
            if ev["released"] and ev["actual"]:
                sent = get_sentiment(ev)
                lines.append(f"       ✅ Actual: `{ev['actual']}` {sent['emoji']} {sent['label']}")
        lines.append("")

    lines += ["━━━━━━━━━━━━━━━━━━━━━━", "🔴 High  🟡 Medium  🟢 Low", "🔔 Notif otomatis saat data rilis"]
    return "\n".join(lines)

def fmt_release(ev):
    imp  = IMPACT_EMOJI.get(ev["impact"], "🟢")
    sent = get_sentiment(ev)
    t_wib = ev["time_str"]
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
    day_events = [e for e in events if e.get("date") == d]
    released = [e for e in day_events if e["released"]]
    pending  = [e for e in day_events if not e["released"]]
    lines = [f"📋 *REKAP DATA EKONOMI*", f"_{tgl_str(d)} • WIB_", "━━━━━━━━━━━━━━━━━━━━━━", ""]

    if released:
        lines.append("✅ *SUDAH RILIS*")
        for ev in released:
            imp  = IMPACT_EMOJI.get(ev["impact"], "🟢")
            sent = get_sentiment(ev)
            s    = f" {sent['emoji']} {sent['label']}" if sent["label"] else ""
            lines.append(f"  {flag(ev['currency'])} {imp} *{ev['event']}*\n    Actual: `{ev['actual']}` | Forecast: `{ev['forecast'] or '-'}`{s}")
        lines.append("")

    if pending:
        lines.append("⏳ *BELUM RILIS*")
        for ev in pending:
            imp = IMPACT_EMOJI.get(ev["impact"], "🟢")
            lines.append(f"  {flag(ev['currency'])} {imp} {ev['event']} — `{ev['time_str']} WIB`")
        lines.append("")

    if not released and not pending:
        lines.append("_Tidak ada data ekonomi hari ini._")

    lines += ["━━━━━━━━━━━━━━━━━━━━━━", f"Total: *{len(released)} rilis* • *{len(pending)} pending*", "🌙 Jadwal besok dikirim jam 00:00 WIB"]
    return "\n".join(lines)

# ─── Send Helper ────────────────────────────────────────────────────────────
bot_client = Bot(token=BOT_TOKEN)

async def send(text: str):
    await bot_client.send_message(
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
        "*Command:*\n"
        "/hariini — Jadwal hari ini\n"
        "/besok — Jadwal besok\n"
        "/rekap — Rekap yang sudah rilis\n"
        "/usd /eur /gbp /jpy /aud\n"
        "/cad /chf /nzd /cny /idr\n"
        "/cari FOMC — Cari event spesifik",
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

async def cmd_hariini(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TIMEZONE).date()
    if not state.events:
        events = await fetch_calendar(today)
        state.reset(events)
    await update.message.reply_text(
        fmt_schedule(state.events, today),
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
    if not state.events:
        events = await fetch_calendar(today)
        state.reset(events)
    await update.message.reply_text(
        fmt_recap(state.events, today),
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=TOPIC_ID,
    )

async def cmd_negara(update: Update, ctx: ContextTypes.DEFAULT_TYPE, currency: str):
    today = datetime.now(TIMEZONE).date()
    if not state.events:
        events = await fetch_calendar(today)
        state.reset(events)
    filtered = [e for e in state.events if e["currency"] == currency and e.get("date") == today]
    lines = [f"{flag(currency)} *DATA {currency} HARI INI*", f"_{tgl_str(today)}_", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    if not filtered:
        lines.append(f"_Tidak ada data {currency} hari ini._")
    else:
        for ev in sorted(filtered, key=lambda x: x["time_str"]):
            imp   = IMPACT_EMOJI.get(ev["impact"], "🟢")
            lines.append(f"  {imp} `{ev['time_str']} WIB` — *{ev['event']}*")
            if ev["released"]:
                sent = get_sentiment(ev)
                lines.append(f"    ✅ Actual: `{ev['actual']}` vs Forecast: `{ev['forecast'] or '-'}` {sent['emoji']} {sent['label']}")
            else:
                if ev["forecast"]: lines.append(f"    📌 Forecast: `{ev['forecast']}`")
                if ev["previous"]: lines.append(f"    ⏮ Previous: `{ev['previous']}`")
    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━"]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, message_thread_id=TOPIC_ID)

async def cmd_cari(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Contoh: `/cari FOMC`", parse_mode=ParseMode.MARKDOWN)
        return
    keyword = " ".join(ctx.args).lower()
    today   = datetime.now(TIMEZONE).date()
    # Cari dalam 14 hari ke depan sekaligus
    to_date = today + timedelta(days=14)
    all_events = await fetch_calendar(today, to_date)
    found = [e for e in all_events if keyword in e["event"].lower()]

    if not found:
        await update.message.reply_text(
            f"❌ Tidak ditemukan event *{keyword.upper()}* dalam 14 hari ke depan.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"🔍 *HASIL: {keyword.upper()}*", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    for ev in found[:10]:
        imp   = IMPACT_EMOJI.get(ev["impact"], "🟢")
        d     = ev.get("date", today)
        hari  = "Hari ini" if d == today else ("Besok" if d == today + timedelta(1) else tgl_str(d))
        lines += [
            f"{flag(ev['currency'])} *{ev['currency']}* {imp}",
            f"📌 *{ev['event']}*",
            f"📅 {hari} — `{ev['time_str']} WIB`",
        ]
        if ev["forecast"]: lines.append(f"📌 Forecast: `{ev['forecast']}`")
        if ev["previous"]: lines.append(f"⏮ Previous: `{ev['previous']}`")
        if ev["released"]:
            sent = get_sentiment(ev)
            lines.append(f"✅ Actual: `{ev['actual']}` {sent['emoji']} {sent['label']}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, message_thread_id=TOPIC_ID)

def make_negara_handler(cur):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await cmd_negara(update, ctx, cur)
    return handler

# ─── Main ────────────────────────────────────────────────────────────────────
async def main():
    log.info("Bot starting...")

    today = datetime.now(TIMEZONE).date()
    events = await fetch_calendar(today)
    state.reset(events)
    log.info("Pre-load: %d events", len(events))

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("hariini", cmd_hariini))
    app.add_handler(CommandHandler("besok",   cmd_besok))
    app.add_handler(CommandHandler("rekap",   cmd_rekap))
    app.add_handler(CommandHandler("cari",    cmd_cari))
    for cur in ["usd","eur","gbp","jpy","aud","cad","chf","nzd","cny","idr"]:
        app.add_handler(CommandHandler(cur, make_negara_handler(cur.upper())))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(job_morning,  CronTrigger(hour=0,  minute=0,  timezone=TIMEZONE), id="morning")
    scheduler.add_job(job_realtime, "interval", minutes=CHECK_INTERVAL, id="realtime")
    scheduler.add_job(job_recap,    CronTrigger(hour=23, minute=0,  timezone=TIMEZONE), id="recap")
    scheduler.start()
    log.info("Scheduler aktif ✅")

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
            "🔴 High  🟡 Medium  🟢 Low\n"
            "🟢 Bullish  🔻 Bearish  ⚪ Netral\n"
            "📡 Sumber: Finnhub"
        )
    except Exception as e:
        log.warning("Startup msg: %s", e)

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
