"""
Economic Calendar Bot - Telegram
Sumber: investing.com/economic-calendar
Negara: USD, EUR, AUD, NZD, CNH, CHF, JPY, CAD, GBP, IDR
Impact: Medium + High only
Fitur: Bullish/Bearish/Neutral otomatis dengan logika terbalik
"""
 
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
 
import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
 
# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("EcoBot")
 
# ─── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = os.environ["CHAT_ID"]
TOPIC_ID   = int(os.environ.get("TOPIC_ID", "0")) or None
TIMEZONE   = ZoneInfo("Asia/Jakarta")
 
ALLOWED_CURRENCIES = {
    "USD", "EUR", "AUD", "NZD", "CNH", "CHF", "JPY", "CAD", "GBP", "IDR"
}
ALLOWED_IMPACT = {"medium", "high"}
CHECK_INTERVAL = 5
 
# ─── Emoji ──────────────────────────────────────────────────────────────────
IMPACT_EMOJI = {"high": "🔴", "medium": "🟡"}
FLAG = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "AUD": "🇦🇺", "CAD": "🇨🇦", "CHF": "🇨🇭", "NZD": "🇳🇿",
    "CNH": "🇨🇳", "IDR": "🇮🇩",
}
def flag(cur): return FLAG.get(cur.upper(), "🏳️")
 
# ─── Data "Terbalik" ─────────────────────────────────────────────────────────
# Jika actual > forecast = BEARISH (buruk untuk ekonomi)
# Jika actual < forecast = BULLISH (baik untuk ekonomi)
INVERTED_KEYWORDS = [
    # Pengangguran (lebih kecil = lebih baik untuk ekonomi)
    "unemployment rate", "unemployment change", "unemployment claims",
    "jobless claims", "initial jobless", "continuing jobless",
    "initial claims", "continuing claims",
    "claimant count",
]
 
# CATATAN logika FX:
# - Inflasi (CPI, PCE, PPI, dll) = NORMAL → actual > forecast = BULLISH
#   (inflasi naik → ekspektasi rate hike → mata uang menguat)
# - Pengangguran = TERBALIK → actual > forecast = BEARISH
#   (pengangguran naik = ekonomi melemah)
 
def is_inverted(event_name: str) -> bool:
    """Cek apakah data ini logikanya terbalik."""
    name_lower = event_name.lower()
    return any(kw in name_lower for kw in INVERTED_KEYWORDS)
 
 
def get_sentiment(ev: dict) -> dict:
    """
    Hitung sentimen: Bullish / Bearish / Neutral
    Returns dict: {label, emoji, icon}
    """
    actual   = ev.get("actual", "")
    forecast = ev.get("forecast", "")
 
    if not actual or not forecast:
        return {"label": "", "emoji": "", "icon": "📊"}
 
    # Bersihkan angka (hapus %, K, M, B, tanda +)
    def clean(s):
        s = s.strip().replace(",", ".")
        s = re.sub(r"[^0-9.\-]", "", s)
        return float(s) if s else None
 
    a = clean(actual)
    f = clean(forecast)
 
    if a is None or f is None:
        return {"label": "", "emoji": "", "icon": "📊"}
 
    inverted = is_inverted(ev.get("event", ""))
 
    if a > f:
        if inverted:
            return {"label": "BEARISH", "emoji": "🔻", "icon": "📉"}
        else:
            return {"label": "BULLISH", "emoji": "🟢", "icon": "📈"}
    elif a < f:
        if inverted:
            return {"label": "BULLISH", "emoji": "🟢", "icon": "📈"}
        else:
            return {"label": "BEARISH", "emoji": "🔻", "icon": "📉"}
    else:
        return {"label": "NETRAL", "emoji": "⚪", "icon": "➡️"}
 
 
# ─── Investing.com Scraper ───────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.investing.com/",
    "X-Requested-With": "XMLHttpRequest",
}
 
COUNTRY_TO_CURRENCY = {
    "united states":  "USD",
    "euro zone":      "EUR",
    "european union": "EUR",
    "australia":      "AUD",
    "new zealand":    "NZD",
    "china":          "CNH",
    "switzerland":    "CHF",
    "japan":          "JPY",
    "canada":         "CAD",
    "united kingdom": "GBP",
    "indonesia":      "IDR",
}
 
async def fetch_investing(target_date: date) -> list[dict]:
    date_str = target_date.strftime("%Y-%m-%d")
    url = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
    payload = {
        "country[]":     ["5","17","29","25","37","43","35","27","4","31"],
        "importance[]":  ["2","3"],
        "dateFrom":      date_str,
        "dateTo":        date_str,
        "timeZone":      "55",
        "timeFilter":    "timeOnly",
        "currentTab":    "custom",
        "submitFilters": "1",
        "limit_from":    "0",
    }
    log.info("Fetching investing.com untuk %s", date_str)
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            data = resp.json()
            html = data.get("data", "")
        except Exception as e:
            log.warning("POST gagal (%s), coba GET...", e)
            resp = await client.get("https://www.investing.com/economic-calendar/")
            html = resp.text
    return parse_html(html, target_date)
 
 
def parse_html(html: str, target_date: date) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all("tr", class_=re.compile(r"js-event-item"))
    events = []
    for row in rows:
        try:
            time_td  = row.find("td", class_="time")
            time_str = time_td.get_text(strip=True) if time_td else ""
 
            country_td = row.find("td", class_="flagCur")
            currency   = ""
            if country_td:
                flag_span = country_td.find("span")
                if flag_span:
                    title    = flag_span.get("title", "").lower()
                    currency = COUNTRY_TO_CURRENCY.get(title, "")
 
            if not currency or currency not in ALLOWED_CURRENCIES:
                continue
 
            impact_td = row.find("td", class_="sentiment")
            impact    = ""
            if impact_td:
                bulls = impact_td.find_all("i", class_="grayFullBullishIcon")
                n = len(bulls)
                impact = "high" if n >= 3 else ("medium" if n == 2 else "low")
 
            if impact not in ALLOWED_IMPACT:
                continue
 
            event_td   = row.find("td", class_="event")
            event_name = ""
            if event_td:
                a = event_td.find("a")
                event_name = a.get_text(strip=True) if a else event_td.get_text(strip=True)
 
            if not event_name:
                continue
 
            actual   = _td_text(row, "actual")
            forecast = _td_text(row, "forecast")
            previous = _td_text(row, "prev")
 
            events.append({
                "time_str": time_str,
                "datetime": parse_time_wib(time_str, target_date),
                "currency": currency,
                "impact":   impact,
                "event":    event_name,
                "actual":   actual,
                "forecast": forecast,
                "previous": previous,
                "released": bool(actual),
                "row_id":   row.get("id", ""),
                "inverted": is_inverted(event_name),
            })
        except Exception as e:
            log.debug("Skip row: %s", e)
 
    log.info("Parsed %d events", len(events))
    return events
 
 
def _td_text(row, cls):
    td = row.find("td", class_=cls)
    if not td: return ""
    t = td.get_text(strip=True)
    return "" if t in ("&nbsp;", "\xa0") else t
 
 
def parse_time_wib(time_str, base_date):
    if not time_str or time_str.lower() in ("all day", "tentative", ""):
        return None
    try:
        ts = time_str.strip()
        fmt = "%H:%M"
        if "am" in ts.lower() or "pm" in ts.lower():
            fmt = "%I:%M%p" if ":" in ts else "%I%p"
        naive = datetime.strptime(ts, fmt).replace(
            year=base_date.year, month=base_date.month, day=base_date.day
        )
        return naive.replace(tzinfo=TIMEZONE)
    except Exception:
        return None
 
 
# ─── State ──────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.today_events: list[dict] = []
        self.last_date: date | None = None
        self.sent_ids: set = set()
 
    def reset(self, events, today):
        self.today_events = events
        self.last_date    = today
        self.sent_ids     = set()
 
    def event_id(self, ev):
        return ev.get("row_id") or f"{ev['currency']}_{ev['event']}_{ev['time_str']}"
 
state = BotState()
 
# ─── Formatters ─────────────────────────────────────────────────────────────
HARI  = ["Senin","Selasa","Rabu","Kamis","Jumat","Sabtu","Minggu"]
BULAN = ["Jan","Feb","Mar","Apr","Mei","Jun","Jul","Agt","Sep","Okt","Nov","Des"]
 
def tgl_str(d: date):
    return f"{HARI[d.weekday()]}, {d.day} {BULAN[d.month-1]} {d.year}"
 
 
def fmt_schedule(events, d):
    lines = [
        "📅 *JADWAL DATA EKONOMI*",
        f"_{tgl_str(d)} • WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━", "",
    ]
    if not events:
        lines.append("_Tidak ada data medium/high impact hari ini._")
        return "\n".join(lines)
 
    by_cur: dict[str, list] = {}
    for ev in sorted(events, key=lambda x: x["time_str"] or "99:99"):
        by_cur.setdefault(ev["currency"], []).append(ev)
 
    for cur in sorted(by_cur):
        lines.append(f"{flag(cur)} *{cur}*")
        for ev in by_cur[cur]:
            imp = IMPACT_EMOJI.get(ev["impact"], "⚪")
            t   = ev["time_str"] or "Tentative"
            inv = " _(inv)_" if ev["inverted"] else ""
            lines.append(f"  {imp} `{t}` — {ev['event']}{inv}")
            if ev["forecast"]:
                lines.append(f"       📌 Forecast: `{ev['forecast']}`")
            if ev["previous"]:
                lines.append(f"       ⏮ Previous: `{ev['previous']}`")
        lines.append("")
 
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🔴 High  🟡 Medium  _(inv) = logika terbalik_",
        "🔔 Notif otomatis saat data rilis",
    ]
    return "\n".join(lines)
 
 
def fmt_release(ev):
    imp  = IMPACT_EMOJI.get(ev["impact"], "⚪")
    sent = get_sentiment(ev)
    inv_note = "\n⚠️ _Data terbalik: lebih kecil = lebih baik_" if ev.get("inverted") else ""
 
    return "\n".join(filter(None, [
        f"{sent['icon']} *DATA RILIS!*",
        "",
        f"{flag(ev['currency'])} *{ev['currency']}* {imp}",
        f"📌 *{ev['event']}*",
        "",
        f"✅ Actual   : `{ev['actual'] or 'N/A'}`",
        f"🎯 Forecast : `{ev['forecast'] or 'N/A'}`",
        f"⏮️ Previous : `{ev['previous'] or 'N/A'}`",
        "",
        f"{sent['emoji']} *{sent['label']}*" if sent['label'] else None,
        inv_note if inv_note else None,
        "",
        f"🕐 _{ev['time_str']} WIB_",
    ]))
 
 
def fmt_recap(events, d):
    released = [e for e in events if e["released"]]
    pending  = [e for e in events if not e["released"]]
 
    lines = [
        "📋 *REKAP DATA EKONOMI*",
        f"_{tgl_str(d)} • WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━", "",
    ]
 
    if released:
        lines.append("✅ *SUDAH RILIS*")
        for ev in released:
            imp  = IMPACT_EMOJI.get(ev["impact"], "⚪")
            sent = get_sentiment(ev)
            s_label = f" {sent['emoji']} {sent['label']}" if sent['label'] else ""
            lines.append(
                f"  {flag(ev['currency'])} {imp} *{ev['event']}*\n"
                f"    Actual: `{ev['actual']}` | Forecast: `{ev['forecast'] or '-'}`{s_label}"
            )
        lines.append("")
 
    if pending:
        lines.append("⏳ *BELUM RILIS*")
        for ev in pending:
            imp = IMPACT_EMOJI.get(ev["impact"], "⚪")
            lines.append(f"  {flag(ev['currency'])} {imp} {ev['event']} — `{ev['time_str'] or 'Tentative'}`")
        lines.append("")
 
    if not released and not pending:
        lines.append("_Tidak ada data ekonomi hari ini._")
 
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"Total: *{len(released)} rilis* • *{len(pending)} pending*",
        "🌙 Jadwal besok dikirim jam 00:00 WIB",
    ]
    return "\n".join(lines)
 
 
# ─── Jobs ────────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
 
async def send(text: str, parse_mode=ParseMode.MARKDOWN):
    """Kirim pesan ke group, khusus ke topik jika TOPIC_ID diset."""
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode=parse_mode,
        message_thread_id=TOPIC_ID,
    )
 
async def job_morning():
    today = datetime.now(TIMEZONE).date()
    log.info("[00:00] Jadwal %s", today)
    try:
        events = await fetch_investing(today)
        state.reset(events, today)
        await send(fmt_schedule(events, today))
        log.info("[00:00] Terkirim %d events", len(events))
    except Exception as e:
        log.error("[00:00] %s", e)
 
async def job_realtime():
    if not state.today_events: return
    today = datetime.now(TIMEZONE).date()
    try:
        fresh = await fetch_investing(today)
        state.today_events = fresh
        for ev in fresh:
            eid = state.event_id(ev)
            if ev["released"] and eid not in state.sent_ids:
                state.sent_ids.add(eid)
                await send(fmt_release(ev))
                log.info("[RT] Rilis: %s %s", ev["currency"], ev["event"])
                await asyncio.sleep(1)
    except Exception as e:
        log.warning("[RT] %s", e)
 
async def job_recap():
    today = datetime.now(TIMEZONE).date()
    log.info("[23:00] Rekap %s", today)
    try:
        events = await fetch_investing(today)
        state.today_events = events
        await send(fmt_recap(events, today))
        log.info("[23:00] Rekap terkirim")
    except Exception as e:
        log.error("[23:00] %s", e)
 
 
# ─── Main ────────────────────────────────────────────────────────────────────
async def main():
    log.info("Bot starting...")
    me = await bot.get_me()
    log.info("Login: @%s", me.username)
 
    today = datetime.now(TIMEZONE).date()
    try:
        events = await fetch_investing(today)
        state.reset(events, today)
        log.info("Pre-load: %d events", len(events))
    except Exception as e:
        log.warning("Pre-load gagal: %s", e)
 
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(job_morning,  CronTrigger(hour=0,  minute=0,  timezone=TIMEZONE), id="morning")
    scheduler.add_job(job_realtime, "interval", minutes=CHECK_INTERVAL, id="realtime")
    scheduler.add_job(job_recap,    CronTrigger(hour=23, minute=0,  timezone=TIMEZONE), id="recap")
    scheduler.start()
    log.info("Scheduler aktif ✅")
 
    negara = " ".join(f"{flag(c)}{c}" for c in sorted(ALLOWED_CURRENCIES))
    try:
        await send(
            "🤖 *Economic Calendar Bot aktif!*\n\n"
            "📅 Jadwal harian → `00:00 WIB`\n"
            "🔔 Update real-time saat data rilis\n"
            "📋 Rekap harian → `23:00 WIB`\n\n"
            f"*Negara filter:*\n{negara}\n\n"
            "🔴 High  🟡 Medium\n"
            "🟢 Bullish  🔻 Bearish  ⚪ Netral\n"
            "📡 Sumber: investing.com"
        )
    except Exception as e:
        log.warning("Startup msg: %s", e)
 
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("Bot stopped.")
 
if __name__ == "__main__":
    asyncio.run(main())
