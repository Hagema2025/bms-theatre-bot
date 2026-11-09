"""BookMyShow seat-availability alert scanner for Chennai theatres.

Each run scans every bookable date at the target theatres and records every seat class it
sees (even sold out) with its availability.

FILES (one writer each, so nothing ever overwrites anything else)

  docs/state/<VENUE>.json     WRITTEN BY THIS SCANNER, also the Mini App's data source
      { "20261107": { "28451": {
            "movie", "language", "dimension", "screen", "time", "start", "end", "url",
            "duration": 150,                       # movie length in minutes
            "tags": ["IMAX", "4K DOLBY ATMOS", "ENG subs"],  # BMS attributes + subtitle code
            "poster": "jailer-2-et00518674-1793799981",      # BMS EventImageCode
            "event_code": "ET00518674", "censor": "UA16+", "trailer": "https://...",
            "genre": "Action,Thriller",
            "cutoff": "2026-11-07T21:45:00+05:30", # when BMS stops selling this show
            "first_seen": "2026-11-07T12:09:58+05:30",   # only for shows this scanner added
            "classes": { "SILVER": {"price": 80, "status": "3", "soon_alerted": false,
                                    "first_seen_status": "0",
                                    "first_seen_time": "2026-11-07T12:09:58+05:30",
                                    "status_changed_at": "...", "sold_out_at": "...",
                                    "reopened_at": "...",
                                    "prev_price": 75, "price_changed_at": "..."} } } },
        "_updated": "<ISO time of the last real change>" }

  docs/tracking/<VENUE>.json  WRITTEN ONLY BY YOUR RUNNING BOT, read-only here
{ "20261107": { "28451": {
    "tracked": true,
    "thread": 4567,
    "categories": ["SILVER", "GOLD"]
} } }
  meta.json                   scanner-private: outage counters + Errors topic ids

TELEGRAM
  One tracking supergroup (Topics enabled) per theatre: TELEGRAM_CHAT_<VENUE>.
    - one topic per tracked show (created by the running bot when you press Track)
    - one "Errors" topic per theatre (created here)
  This scanner only SENDS (alerts, time-change notices, error warnings) and deletes the
  topic when the show ends. It never calls getUpdates, so it cannot clash with any
  other process that polls the same bot token.
  The bot must be admin in every tracking group with "Manage Topics".

ALERTS (tracked shows only, into that show's topic)
  - NEW           a class seen for the first time and bookable
  - NOW AVAILABLE a sold-out class becomes bookable (status 0 -> 1/2/3)
  - MORE SEATS    a bookable class gets more seats (1 -> 2, 2 -> 3, 1 -> 3)
  - SHOW SOON     the show is within REMINDER_MINUTES of start (once)
  Untracked shows are still scanned and stored silently, so tracking one later starts
  from accurate status.

A show is dropped SHOW_GRACE_MINUTES after its start; its topic is deleted then.
"""
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

from curl_cffi import requests as bms_req


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

IST = ZoneInfo("Asia/Kolkata")

class ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, IST).strftime("%Y-%m-%d %H:%M:%S")

_h = logging.StreamHandler()
_h.setFormatter(ISTFormatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_h])

# Venue codes from BMS's Chennai cinema list
TARGET_THEATRES = {
    "INTO": "INOX: The Marina Mall, OMR",
    "PVHR": "PVR: Heritage RSL ECR",
    "MCSK": "Miraj Cinemas: Sekaran Mall",
    "TVHP": "The Vijay Park Multiplex: Injambakkam",
    "CBMC": "Cinepolis: BSR Mall, OMR",
    "MAYJ": "MAYAJAAL Multiplex: ECR",
    "ACON": "AGS Cinemas OMR: Navalur (Vivira Mall)",
}

# Tracking group per theatre, e.g. TELEGRAM_CHAT_INTO=-1009876543210
TRACK_CHATS = {code: os.environ.get(f"TELEGRAM_CHAT_{code}", "").strip()
               for code in TARGET_THEATRES}

SHOW_GRACE         = timedelta(minutes=int(os.environ.get("SHOW_GRACE_MINUTES", "30")))  # state/alerts end this long after showtime
REMINDER_MINUTES = int(os.environ.get("REMINDER_MINUTES", "120"))
MAX_DATES          = int(os.environ.get("MAX_DATES", "7"))
FAIL_ALERT_AFTER   = 3
TG_RETRIES         = 4
CLEANUP_MAX_TRIES  = 5
ERRORS_THREAD_NAME = "⚠️ Errors"
STATE_DIR          = os.environ.get("STATE_DIR", "docs/state")
TRACKING_DIR       = os.environ.get("TRACKING_DIR", "docs/tracking")
META_FILE          = os.environ.get("META_FILE", "meta.json")
RULES_FILE        = os.environ.get("RULES_FILE", "docs/rules.json")
RULE_THREADS_FILE = os.environ.get("RULE_THREADS_FILE", "docs/rule_threads.json")
RULE_CUTOFF       = timedelta(minutes=int(os.environ.get("RULE_CUTOFF_MINUTES", "10")))  # no new rule-tracking this long after start
SHOW_PRICES       = {}   # (venue, date, session) -> prices seen this run
DAY_ABBR          = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DIVIDER            = "━━━━━━━━━━━━━━━━"
MINI_APP_URL = os.environ.get("MINI_APP_URL")

def _rule_markup(venue: str, rule_id: str, ws: datetime) -> dict:
    if not MINI_APP_URL:
        return None
        
    # Strip query params and index.html to get the base directory
    base = MINI_APP_URL.split("?")[0].replace("index.html", "").rstrip("/")
    url = f"{base}/tracked.html?view=rule&venue={venue}&rule_id={rule_id}&week={ws.strftime('%Y%m%d')}"
    
    return {
        "inline_keyboard": [[
            {"text": "🔍 View Matched Shows", "web_app": {"url": url}}
        ]]
    }

# Category AvailStatus, as checked against live seat maps:
# 3 = plenty of seats, 2 = filling fast, 1 = last few seats, 0 = sold out
AVAIL_LABELS = {"3": "Available", "2": "Filling fast", "1": "Almost full"}
STATUS_NAMES = {"0": "Sold out", **AVAIL_LABELS}

# Same request the BMS website's cinema page makes
SHOWTIMES_URL   = ("https://in.bookmyshow.com/api/v3/mobile/showtimes/byvenue"
                   "?dateCode={date}&venueCode={venue}&regionCode=CHEN&memberId=&appCode=WEBV2&lsId=")
SEAT_LAYOUT_URL = "https://in.bookmyshow.com/movies/chennai/seat-layout/{event}/{venue}/{session}/{date}"

# Headers the BMS website sends with its API calls (User-Agent comes from curl_cffi's impersonation)
BMS_HEADERS = {
    "Referer":              "https://in.bookmyshow.com/",
    "Origin":               "https://in.bookmyshow.com",
    "Accept":               "application/json, text/plain, */*",
    "Accept-Language":      "en-IN,en;q=0.9",
    "x-app-code":           "WEB",
    "x-platform":           "WEB",
    "x-platform-code":      "WEB",
    "x-region-code":        "CHEN",
    "x-region-slug":        "chennai",
    "x-geohash":            "tf3",
    "x-latitude":           "13.056",
    "x-longitude":          "80.206",
    "x-location-selection": "manual",
}


def console_print(message):
    """Print timestamped messages in terminal and GitHub Actions."""
    print(
        f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True
    )


console_print("🤖 BookMyShow alert scanner module loaded.")
console_print(f"⚙️ Reminder: {REMINDER_MINUTES} minutes before show")
console_print(f"⚙️ Maximum dates to scan: {MAX_DATES}")
console_print(f"⚙️ Theatres configured: {len(TARGET_THEATRES)}")

# ───────────────────────── helpers ─────────────────────────


def parse_bms_time(value):
    try:
        return datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=IST)
    except (TypeError, ValueError):
        console_print(f"⚠️ Invalid BMS time: {value}")
        return None


def parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_date(date_code):
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
    except (TypeError, ValueError):
        return date_code


def entry_end(show, date_code):
    """When this stored show expires (showtime + grace). Falls back to the end of its date."""
    end = parse_iso(show.get("end"))
    if end:
        return end
    try:
        return datetime.strptime(date_code, "%Y%m%d").replace(tzinfo=IST) + timedelta(days=1)
    except (TypeError, ValueError):
        return datetime(2000, 1, 1, tzinfo=IST)


def has_subtitles(show):
    """BMS reuses SessionSubTitle(+Acronym) for things like '4K DOLBY 7.1' (acronym '4K '),
    so only treat it as subtitles when the text really says so."""
    return "SUBTITLE" in (show.get("SessionSubTitle") or "").upper()


def attr_tags(show):
    """Format/sound tags from BMS. IsAtmosEnabled is always 'N' in practice (Atmos shows when
    Attributes says so), so it is only a fallback."""
    attrs = (show.get("Attributes") or "").strip()
    tags = [attrs] if attrs else []
    if show.get("IsAtmosEnabled") == "Y" and "ATMOS" not in attrs.upper():
        tags.append("Dolby Atmos")
    return tags

def show_tags(show):
    return attr_tags(show)          # format/sound only: IMAX, 4K DOLBY ATMOS, ...


def show_subs(show):
    """Subtitle label kept out of tags, e.g. 'ENG subs'. Empty string if none."""
    if not has_subtitles(show):
        return ""
    code = (show.get("SessionSubTitleAcronym") or "").strip()
    return f"{code} subs" if code else "Subtitles"


def touch_class(c, status, price, now, label=""):
    iso = now.isoformat(timespec="seconds")
    old_price, old_status = c.get("price"), c.get("status")
    if old_price is not None and old_price != price:
        logging.info(f"💰 {label}: price {old_price:g} → {price:g}")
        c["prev_price"], c["price_changed_at"] = old_price, iso
    if old_status is not None and old_status != status:
        logging.info(f"📊 {label}: status {STATUS_NAMES.get(old_status, old_status)} → {STATUS_NAMES.get(status, status)}")
        c["status_changed_at"] = iso
        if status == "0":
            c["sold_out_at"] = iso
        elif old_status == "0":
            c["reopened_at"] = iso
    c["price"], c["status"] = price, status


# ───────────────────────── files ─────────────────────────

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_state():
    """state[VENUE] = {date: {session: show}} read from docs/state/<VENUE>.json."""
    console_print("📂 Loading saved state...")
    state = {}
    for venue in TARGET_THEATRES:
        data = read_json(os.path.join(STATE_DIR, f"{venue}.json"), {})
        if not isinstance(data, dict):
            data = {}
        data.pop("_updated", None)
        state[venue] = data
    shows = sum(len(s) for dates in state.values() for s in dates.values())
    console_print(f"🎬 Shows loaded: {shows}")
    return state


def flatten_tracking(data):
    """Combo records and legacy date entries -> {date: {session: entry}}.
    Each flattened entry carries the combo's topic id as message_thread_id."""
    out = {}
    for k, v in (data or {}).items():
        if re.fullmatch(r"\d{8}", str(k)) and isinstance(v, dict):          # legacy
            out.setdefault(k, {}).update(v)
        elif isinstance(v, dict) and isinstance(v.get("tracking"), dict) and v.get("date"):
            day = out.setdefault(v["date"], {})
            for sid, e in v["tracking"].items():
                day[sid] = {**e,
                            "message_thread_id": v.get("thread_id"),
                            "group_chat_id": v.get("group_chat_id")}
    return out


def load_tracking():
    """tracking[VENUE] = {date: {session: entry}}  (read-only here)."""
    tracking = {}
    for venue in TARGET_THEATRES:
        data = read_json(os.path.join(TRACKING_DIR, f"{venue}.json"), {})
        tracking[venue] = flatten_tracking(data) if isinstance(data, dict) else {}
    return tracking


THREAD_MAP = {}   # "<VENUE>_<original thread id>" -> replacement topic id (saved in meta.json)

def load_meta():
    global THREAD_MAP
    meta = read_json(META_FILE, {})
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("threads", {})
    meta.setdefault("fail_streaks", {})
    meta.setdefault("outage_alerted", [])
    THREAD_MAP = meta.setdefault("thread_map", {})
    return meta

def save_all(state, meta, now):
    """Write only files whose content really changed, so unchanged runs make no commits."""
    console_print("💾 Saving state...")
    for venue, data in state.items():
        path = os.path.join(STATE_DIR, f"{venue}.json")
        old = read_json(path, {})
        if isinstance(old, dict):
            old.pop("_updated", None)
        if old == data:
            continue
        out = dict(data)
        out["_updated"] = now.isoformat(timespec="seconds")
        write_json(path, out)
        logging.info(f"💾 Wrote {path} ({sum(len(d) for d in data.values())} dates)")
    if read_json(META_FILE, {}) != meta:
        write_json(META_FILE, meta)


def get_show(state, venue, date_code, session):
    return ((state.get(venue) or {}).get(date_code) or {}).get(session)


def tracking_entry(tracking, venue, date_code, session):
    return ((tracking.get(venue) or {}).get(date_code) or {}).get(session) or {}


def entry_thread(t):
    return t.get("message_thread_id") or t.get("thread")


def track_thread(state, tracking, venue, date_code, session):
    """Topic id for a TRACKED show, else None. A replacement wins if the original was deleted."""
    t = tracking_entry(tracking, venue, date_code, session)
    orig = entry_thread(t)
    if not t.get("tracked") or not orig:
        return None
    return THREAD_MAP.get(f"{venue}_{orig}", orig)


def is_category_tracked(tracking, venue, date_code, session, category, price):
    t = tracking_entry(tracking, venue, date_code, session)
    if not t.get("tracked"):
        return False
    mode = t.get("mode")
    if mode == "all":
        return True
    if mode == "budget":
        try:
            return price <= float(t.get("max_price"))
        except (TypeError, ValueError):
            return False
    cats = t.get("categories")
    if not isinstance(cats, list):
        return False
    return category.strip().upper() in {str(c).strip().upper() for c in cats}

# ───────────────────────── telegram ─────────────────────────

_last_send = {}
SEND_METHODS = {"sendMessage", "editMessageText", "editMessageReplyMarkup",
                "createForumTopic", "editForumTopic"}


def throttle(chat_id, gap=3.2):
    """Keep to about 18 messages/min per group (Telegram allows 20)."""
    wait = _last_send.get(chat_id, 0) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_send[chat_id] = time.monotonic()


def tg_api(method, payload):
    """Call a Telegram Bot API method via curl_cffi with retries. Returns (ok, body)."""
    console_print(f"📨 Telegram API request: {method}")

    if not TELEGRAM_BOT_TOKEN:
        console_print("❌ TELEGRAM_BOT_TOKEN is missing!")
        return False, {}
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    body = {}
    if method in SEND_METHODS and payload.get("chat_id"):
        throttle(payload["chat_id"])
    for attempt in range(1, TG_RETRIES + 1):
        console_print(f"🔄 Telegram attempt {attempt}/{TG_RETRIES}: {method}")
        try:
            resp = bms_req.post(url, json=payload, timeout=15)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and body.get("ok"):
                console_print(f"✅ Telegram {method} successful.")
                return True, body
            desc = body.get("description", "")
            console_print(f"⚠️ Telegram error: HTTP {resp.status_code} - {desc}")
            if resp.status_code == 429:
                wait = int(body.get("parameters", {}).get("retry_after", 5))
                console_print(f"⏳ Telegram rate limit. Waiting {wait} seconds.")
                time.sleep(min(wait, 60))
                continue
            if 400 <= resp.status_code < 500:
                console_print("❌ Telegram client error. No retry.")
                return False, body  # bad token/chat/thread; retrying won't help
        except Exception as e:
            console_print(f"❌ Telegram connection error: {type(e).__name__}")
            # Exception text can contain the URL (bot token), so log only the type
            logging.warning(f"Telegram {method} attempt {attempt}/{TG_RETRIES}: {type(e).__name__}")
        if attempt < TG_RETRIES:
            delay = 2 ** attempt  # 2s, 4s, 8s backoff for network errors and 5xx
            console_print(f"⏳ Retrying after {delay} seconds...")
            time.sleep(delay)
    console_print(f"❌ Telegram {method} failed after all retries.")

    return False, body


def errors_note(venue):
    """First message of a new Errors topic."""
    return (f"📌 NOTE\n"
            f"🎟️ {TARGET_THEATRES[venue]}\n"
            f"{DIVIDER}\n"
            f"BookMyShow outages, recoveries and\n"
            f"bot crashes are posted here.")


def get_errors_thread(meta, venue, force=False):
    """Errors topic of this theatre's tracking group, created on first use."""
    chat_id = TRACK_CHATS.get(venue)
    key = f"{venue}_errors"
    if key in meta["threads"] and not force:
        return meta["threads"][key]
    ok, body = tg_api("createForumTopic", {"chat_id": chat_id, "name": ERRORS_THREAD_NAME})
    if not ok:
        logging.warning(f"{venue}: couldn't create the Errors topic ({body.get('description', '')})")
        return None
    thread_id = body["result"]["message_thread_id"]
    meta["threads"][key] = thread_id
    tg_api("sendMessage", {"chat_id": chat_id, "message_thread_id": thread_id, "text": errors_note(venue),
                           "disable_notification": True, "disable_web_page_preview": True})
    return thread_id


def send_error(meta, venue, text):
    """Post into the theatre's Errors topic; recreate the topic once if it was deleted."""
    chat_id = TRACK_CHATS.get(venue)
    if not chat_id:
        logging.error(f"{venue}: no TELEGRAM_CHAT_{venue} set, can't send")
        return False
    thread_id = get_errors_thread(meta, venue)
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if thread_id:
        payload["message_thread_id"] = thread_id
    ok, body = tg_api("sendMessage", payload)
    if not ok and thread_id and "thread" in body.get("description", "").lower():
        thread_id = get_errors_thread(meta, venue, force=True)
        payload.pop("message_thread_id", None)
        if thread_id:
            payload["message_thread_id"] = thread_id
        ok, body = tg_api("sendMessage", payload)
    return ok


# ───────────────────────── tracking topics ─────────────────────────

def track_name(date_code, s):
    return f"{format_date(date_code)} · {s.get('movie', 'Movie')} · {s.get('time', '')}"[:120]


def status_text(s):
    """Seat status summary, used when a deleted topic has to be recreated."""
    rows = "\n".join(f"💺 {html.escape(d)} · ₹{c['price']:g} · {STATUS_NAMES.get(c['status'], c['status'])}"
                     for d, c in s.get("classes", {}).items())
    return (f"🎬 <b>{html.escape(s.get('movie', 'Movie'))}</b>\n🕘 {html.escape(s.get('time', ''))}\n{rows}\n"
            f'🔗 <a href="{html.escape(s.get("url", ""), quote=True)}">Book seats</a>')


def send_track(state, tracking, venue, date_code, session, text):
    """Send an alert into the tracked show's topic. If the topic was deleted by hand, make a
    replacement (remembered in state, since tracking/ is not ours to edit) and resend once."""
    tc = TRACK_CHATS.get(venue)
    s = get_show(state, venue, date_code, session)
    thread = track_thread(state, tracking, venue, date_code, session)
    if not tc or not thread or not s:
        return False
    ok, body = tg_api("sendMessage", {"chat_id": tc, "message_thread_id": thread, "text": text,
                                      "parse_mode": "HTML", "disable_web_page_preview": True})
    if ok:
        return True
    if "thread" not in body.get("description", "").lower():
        return False
    original = entry_thread(tracking_entry(tracking, venue, date_code, session))
    ok_t, tb = tg_api("createForumTopic", {"chat_id": tc, "name": track_name(date_code, s)})
    if not ok_t:
        return False
    new_thread = tb["result"]["message_thread_id"]
    THREAD_MAP[f"{venue}_{original}"] = new_thread
    tg_api("sendMessage", {"chat_id": tc, "message_thread_id": new_thread, "text": status_text(s),
                           "parse_mode": "HTML", "disable_notification": True,
                           "disable_web_page_preview": True})
    ok, _ = tg_api("sendMessage", {"chat_id": tc, "message_thread_id": new_thread, "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": True})
    return ok


def announce_time_changes(state, tracking, changes):
    """Tell the combo topic about a new showtime."""
    for c in changes:
        thread = track_thread(state, tracking, c["venue"], c["date"], c["session"])
        tc = TRACK_CHATS.get(c["venue"])
        s = get_show(state, c["venue"], c["date"], c["session"])
        if not (thread and tc and s):
            continue
        tg_api("sendMessage", {"chat_id": tc, "message_thread_id": thread,
                               "text": f"⏰ {s.get('movie', 'Show')}: time changed {c['old']} → {c['new']}"})
        
# ───────────────────────── BMS fetching ─────────────────────────

def fetch_showtimes(session, venue, date_code):
    url = SHOWTIMES_URL.format(date=date_code, venue=venue)
    for attempt in (1, 2):
        t0 = time.monotonic()
        try:
            r = session.get(url, headers=BMS_HEADERS, timeout=12)
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                logging.info(f"{venue} {date_code}: HTTP 200 in {ms}ms (try {attempt})")
                return r.json()
            server = r.headers.get("server", "?")
            snippet = (r.text or "")[:120].replace("\n", " ")
            logging.warning(f"{venue} {date_code}: HTTP {r.status_code} in {ms}ms (try {attempt}) "
                            f"server={server} body='{snippet}'")
            if 400 <= r.status_code < 500:
                return None
        except Exception as e:
            logging.warning(f"{venue} {date_code}: {type(e).__name__}: {e} (try {attempt})")
        time.sleep(2)
    return None


def iter_categories(data):
    """Yields (day, child, show, cat, duration, title, event) for every seat category."""
    for day in data.get("ShowDetails") or []:
        for ev in day.get("Event") or []:
            duration = ev.get("EventDuration") or "0"
            clean_title = ev.get("EventTitle") or "Movie"
            for child in ev.get("ChildEvents") or []:
                for show in child.get("ShowTimes") or []:
                    for cat in show.get("Categories") or []:
                        yield day, child, show, cat, duration, clean_title, ev


def register_shows(vstate, venue, data, now):
    """Keep one entry per show under vstate[date][session]. Returns showtime changes found."""
    changes = []
    for day, child, show, cat, duration, title, ev in iter_categories(data):
        try:
            price = float(cat.get("CurPrice") or 0)
        except (TypeError, ValueError):
            continue
        session_id = show.get("SessionId")
        date_code  = show.get("ShowDateCode") or day.get("Date")
        start      = parse_bms_time(show.get("ShowDateTime"))
        if not (price > 0 and session_id and date_code and start):
            continue
        if start + SHOW_GRACE < now:
            continue
        sid = str(session_id)
        day_shows = vstate.setdefault(date_code, {})
        is_new = sid not in day_shows
        entry = day_shows.setdefault(sid, {"classes": {}, "time": ""})
        if is_new:
            entry["first_seen"] = now.isoformat(timespec="seconds")   # set once
            logging.info(f"🆕 Show added: {venue} {date_code} {sid} · {title} · "
                 f"{child.get('EventLanguage')} {child.get('EventDimension')} · {show.get('ShowTime')}")
        new_time = show.get("ShowTime") or ""
        if entry.get("time") and new_time and new_time != entry["time"]:
            logging.info(f"⏰ Time change: {venue} {date_code}/{sid} {entry['time']} → {new_time}")
            changes.append({"venue": venue, "date": date_code, "session": sid,
                            "old": entry["time"], "new": new_time})
        cutoff = parse_bms_time(show.get("CutOffDateTime"))
        entry["time"]       = new_time or entry.get("time", "")
        entry["movie"]      = title
        entry["language"]   = child.get("EventLanguage") or ""
        entry["dimension"]  = child.get("EventDimension") or ""
        entry["screen"]     = show.get("ScreenName") or ""
        entry["url"]        = SEAT_LAYOUT_URL.format(event=child.get("EventCode"), venue=venue,
                                                     session=session_id, date=date_code)
        entry["start"]      = start.isoformat(timespec="seconds")
        entry["end"]        = (start + SHOW_GRACE).isoformat(timespec="seconds")
        entry["cutoff"]     = cutoff.isoformat(timespec="seconds") if cutoff else ""
        entry["duration"]   = int(duration) if str(duration).isdigit() else 0   # movie length in minutes
        entry["tags"]       = show_tags(show)
        entry["subs"]       = show_subs(show)
        entry["poster"]     = child.get("EventImageCode") or ""
        entry["event_code"] = child.get("EventCode") or ""
        entry["censor"]     = child.get("EventCensor") or ""
        entry["trailer"]    = child.get("EventTrailer") or ""
        entry["genre"]      = ev.get("EventGenre") or ""
    return changes


def check_category(venue, day, child, show, cat, duration, clean_title, vstate, now, seen):
    """Track one seat class and return an alert dict if it should ping, else None.
    Adds every class it processes to seen."""
    try:
        price = float(cat.get("CurPrice") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
       return None

    session_id = show.get("SessionId")
    date_code  = show.get("ShowDateCode") or day.get("Date")
    if not session_id or not date_code:
        return None
    sid = str(session_id)
    entry = (vstate.get(date_code) or {}).get(sid)
    if entry is None:
        return None   # register_shows skipped it (expired or no start time)

    desc   = (cat.get("PriceDesc") or "").strip().upper()
    status = str(cat.get("AvailStatus", "0"))
    if status not in STATUS_NAMES:
        return None

    start = parse_bms_time(show.get("ShowDateTime"))
    end = start + SHOW_GRACE if start else parse_bms_time(show.get("CutOffDateTime"))
    if end and end < now:
        return None

    seen.add((venue, date_code, sid, desc))
    mins_to_start = (start - now).total_seconds() / 60 if start else None
    soon = mins_to_start is not None and 0 <= mins_to_start <= REMINDER_MINUTES

    # Extra tags for the alert text
    extra_tags = " · ".join(attr_tags(show))
    sub_acronym = (show.get("SessionSubTitleAcronym") or "").strip()
    sub_tag = f"[{sub_acronym}💬]" if sub_acronym and has_subtitles(show) else ""

    # Show start - end time (end = start + duration + 25 min of ads/trailers)
    time_str = show.get("ShowTime") or ""
    if start and str(duration).isdigit() and int(duration) > 0:
        end_time = start + timedelta(minutes=int(duration) + 25)
        time_str = f"{time_str} - {end_time.strftime('%I:%M %p')}"

    prev = entry["classes"].get(desc)
    if prev is None:
        if status == "0":
            # Sold out right now: store it so a later run can ping when it opens
            entry["classes"][desc] = {"price": price, "status": "0", "soon_alerted": False,
                                      "first_seen_status": "0",
                                      "first_seen_time": now.isoformat(timespec="seconds")}
            return None
        kind, prev_status = "new", None
    else:
        prev_status = prev.get("status") or "3"
        if int(status) > int(prev_status):
            kind = "opened" if prev_status == "0" else "more"
        elif soon and status != "0" and not prev.get("soon_alerted"):
            kind = "soon"
        else:
            # Classes stored before first_seen existed get their first_seen values from now on
            prev.setdefault("first_seen_status", prev_status)
            prev.setdefault("first_seen_time", now.isoformat(timespec="seconds"))
            touch_class(prev, status, price, now, f"{venue} {date_code}/{sid} {desc}")
            return None

    return {
        "key": f"{venue}_{date_code}_{sid}_{desc}", "session": sid,
        "kind": kind, "soon": soon, "mins": mins_to_start,
        "venue": venue, "date": date_code, "movie": clean_title,
        "time": time_str, "screen": show.get("ScreenName") or "",
        "category": desc, "price": price, "status": status, "prev_status": prev_status,
        "start": start,
        "censor": child.get("EventCensor") or "", "extra_tags": extra_tags,
        "trailer": child.get("EventTrailer") or "",
        "url": SEAT_LAYOUT_URL.format(event=child.get("EventCode"), venue=venue,
                                      session=session_id, date=date_code),
        "sub_tag": sub_tag,
        "dimension": child.get("EventDimension") or "",
        "language": child.get("EventLanguage") or "",
    }


def scan(session, state, now):
    """Check every bookable date at every theatre.
    Returns (hits, stats, changes). stats is {venue: {"ok": n, "failed": n}}."""
    hits, keys, changes = [], set(), []
    stats = {venue: {"ok": 0, "failed": 0} for venue in TARGET_THEATRES}
    seen, scanned = set(), set()

    for venue in TARGET_THEATRES:
        vstate = state[venue]
        dates, done = [now.strftime("%Y%m%d")], set()
        while dates and len(done) < MAX_DATES:
            date_code = dates.pop(0)
            if date_code in done:
                continue
            done.add(date_code)

            data = fetch_showtimes(session, venue, date_code)
            time.sleep(0.5)

            if data is None:
                stats[venue]["failed"] += 1
                continue

            stats[venue]["ok"] += 1
            n_shows = sum(len(day.get("Event") or []) for day in data.get("ShowDetails") or [])

            logging.info(f"{venue} {date_code}: {n_shows} movies, {len(vstate.get(date_code, {}))} shows stored")
            try:
                changes += register_shows(vstate, venue, data, now)
            except Exception as e:
                logging.warning(f"{venue} {date_code}: register_shows failed ({type(e).__name__}: {e})")
            scanned.add((venue, date_code))
            scanned.update((venue, d.get("Date")) for d in data.get("ShowDetails") or [])
            collect_prices(venue, data)

            if len(done) == 1:
                dates += [d.get("DateCode") for d in data.get("ShowDatesArray") or []
                          if d.get("DateCode") and not d.get("isDisabled")]

            for day, child, show, cat, duration, clean_title, _ev in iter_categories(data):
                try:
                    hit = check_category(venue, day, child, show, cat, duration, clean_title,
                                         vstate, now, seen)
                except Exception as e:
                    logging.warning(f"{venue} {date_code}: skipped a seat class ({type(e).__name__}: {e})")
                    continue
                if hit and hit["key"] not in keys:
                    keys.add(hit["key"])
                    hits.append(hit)

    # A class that vanished from a date we did scan is treated as sold out
    for venue, dates in state.items():
        for date_code, shows in dates.items():
            if (venue, date_code) not in scanned:
                continue
            for sid, s in shows.items():
                for desc, c in s.get("classes", {}).items():
                    if (venue, date_code, sid, desc) not in seen and c.get("status") != "0":
                        touch_class(c, "0", c.get("price"), now, f"{venue} {date_code}/{sid} {desc} (vanished)")

    logging.info(f"📍 {venue} done: {stats[venue]['ok']} ok / {stats[venue]['failed']} failed of {len(done)} dates")


    return hits, stats, changes


def cleanup_ended(state, tracking, now):
    """Drop shows past their window. Delete a combo topic only when none of its
    tracked shows is still running. Failed deletes are retried (CLEANUP_MAX_TRIES)."""
    alive = set()
    for venue, dates in state.items():
        for date_code, shows in dates.items():
            for sid, s in shows.items():
                if entry_end(s, date_code) >= now:
                    t = track_thread(state, tracking, venue, date_code, sid)
                    if t:
                        alive.add((venue, t))

    deleted = set()
    for venue, dates in state.items():
        tc = TRACK_CHATS.get(venue)
        for date_code in list(dates):
            shows = dates[date_code]
            for sid in list(shows):
                s = shows[sid]
                if entry_end(s, date_code) >= now:
                    continue
                thread = track_thread(state, tracking, venue, date_code, sid)
                if thread and tc and (venue, thread) not in alive and (venue, thread) not in deleted:
                    ok, body = tg_api("deleteForumTopic", {"chat_id": tc, "message_thread_id": thread})
                    gone = "thread" in body.get("description", "").lower()
                    logging.info(f"Topic {thread} delete: ok={ok} gone={gone}")
                    if ok or gone:
                        deleted.add((venue, thread))
                        orig = entry_thread(tracking_entry(tracking, venue, date_code, sid))
                        THREAD_MAP.pop(f"{venue}_{orig}", None)
                    else:
                        s["cleanup_tries"] = s.get("cleanup_tries", 0) + 1
                        if s["cleanup_tries"] < CLEANUP_MAX_TRIES:
                            continue 
                logging.info(f"🧹 Expired: {venue} {date_code}/{sid} {s.get('movie')} {s.get('time')}")                 # retry next run
                shows.pop(sid)
            if not shows:
                dates.pop(date_code)

# ───────────────────────── auto-track rules ─────────────────────────

def load_rules():
    cfg = read_json(RULES_FILE, {})
    rules = cfg.get("rules") if isinstance(cfg, dict) else []
    return [r for r in (rules or []) if isinstance(r, dict) and r.get("id") and r.get("name")]


def load_rule_threads():
    data = read_json(RULE_THREADS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_rule_threads(rule_threads):
    if read_json(RULE_THREADS_FILE, {}) != rule_threads:
        write_json(RULE_THREADS_FILE, rule_threads)


def collect_prices(venue, data):
    """Remember every seat price seen for each show this run (used for budget rules)."""
    for day, child, show, cat, *_ in iter_categories(data):
        try:
            price = float(cat.get("CurPrice") or 0)
        except (TypeError, ValueError):
            continue
        sid = show.get("SessionId")
        date_code = show.get("ShowDateCode") or day.get("Date")
        if price > 0 and sid and date_code:
            SHOW_PRICES.setdefault((venue, date_code, str(sid)), []).append(price)


def _min_price(venue, date_code, sid, show):
    prices = SHOW_PRICES.get((venue, date_code, sid)) or \
             [c.get("price") for c in (show.get("classes") or {}).values()]
    prices = [float(p) for p in prices if p]
    return min(prices) if prices else None


def _hm(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _in_window(w, minutes):
    if not isinstance(w, dict):
        return True
    if w.get("from") and minutes < _hm(w["from"]):
        return False
    if w.get("to") and minutes > _hm(w["to"]):
        return False
    return True


def rule_matches(rule, venue, date_code, show):
    """Same logic as ruleMatches() in rules.html."""
    venues = [str(v).upper() for v in (rule.get("venues") or [])]
    if venues and venue not in venues:
        return False

    movie = str(rule.get("movie") or "").strip().lower()
    if movie and movie not in str(show.get("movie") or "").lower():
        return False

    langs = [str(x).lower() for x in (rule.get("languages") or [])]
    if langs and str(show.get("language") or "").lower() not in langs:
        return False

    dow = (datetime.strptime(date_code, "%Y%m%d").weekday() + 1) % 7      # Sunday = 0, like the page
    days = rule.get("days") or []
    if days and dow not in days:
        return False

    d_from = str(rule.get("from") or "").replace("-", "")
    d_to = str(rule.get("to") or "").replace("-", "")
    if d_from and date_code < d_from:
        return False
    if d_to and date_code > d_to:
        return False

    start = parse_iso(show.get("start"))
    if not start:
        return False
    local = start.astimezone(IST)
    window = (rule.get("day_times") or {}).get(str(dow)) if rule.get("per_day") else rule.get("time")
    return _in_window(window, local.hour * 60 + local.minute)


def week_bounds(date_code):
    d = datetime.strptime(date_code, "%Y%m%d").date()
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _fmt_day(d):
    return f"{d.strftime('%a')}, {d.day} {d.strftime('%b')}"          # Mon, 9 Nov


def rule_thread_name(rule_name, ws, we):
    suffix = f" | {_fmt_day(ws)} to {_fmt_day(we)}"
    return (rule_name[:128 - len(suffix)] + suffix)[:128]


def _win_text(w):
    w = w or {}
    if not (w.get("from") or w.get("to")):
        return "any time"
    return f"{w.get('from') or '00:00'}–{w.get('to') or '23:59'}"


def describe_rule(rule):
    days = [d for d in (1, 2, 3, 4, 5, 6, 0) if d in (rule.get("days") or [])]
    if rule.get("per_day") and days:
        when = ", ".join(f"{DAY_ABBR[d]} {_win_text((rule.get('day_times') or {}).get(str(d)))}" for d in days)
    else:
        when = (" ".join(DAY_ABBR[d] for d in days) or "Every day") + " · " + _win_text(rule.get("time"))
    seats = f"Seats up to ₹{rule.get('max_price')}" if rule.get("mode") == "budget" else "All seat types"
    return " · ".join([rule.get("movie") or "Any movie",
                       ", ".join(rule.get("languages") or []) or "Any language", when, seats])


def ensure_rule_thread(rule_threads, venue, rule, ws, we):
    """Find or create the topic for (rule, theatre, week). Returns its record or None."""
    chat = TRACK_CHATS.get(venue)
    if not chat:
        return None
    key = f"{venue}_{rule['id']}_{ws:%Y%m%d}"
    name = rule_thread_name(rule["name"], ws, we)
    ent = rule_threads.get(key)

    if ent and ent.get("thread_id"):
        if ent.get("rule_name") != rule["name"]:                      # rule was renamed
            tg_api("editForumTopic", {"chat_id": chat, "message_thread_id": ent["thread_id"], "name": name})
            ent["rule_name"] = rule["name"]
        return ent

    ok, body = tg_api("createForumTopic", {"chat_id": chat, "name": name})
    if not ok:
        return None
    thread_id = body["result"]["message_thread_id"]
    ent = ent or {}
    ent.update(venue=venue, rule_id=rule["id"], rule_name=rule["name"],
               week_start=f"{ws:%Y%m%d}", week_end=f"{we:%Y%m%d}",
               chat_id=str(chat), thread_id=thread_id,
               created=datetime.now(IST).isoformat(timespec="seconds"))
    rule_threads[key] = ent
    markup = _rule_markup(venue, rule["id"], ws)
    tg_api("sendMessage", {
        "chat_id": chat, "message_thread_id": thread_id, "parse_mode": "HTML",
        "disable_notification": True, "disable_web_page_preview": True,
        "text": (f"📌 <b>{html.escape(rule['name'])}</b>\n"
                 f"🎟️ {html.escape(TARGET_THEATRES[venue])}\n"
                 f"📅 {_fmt_day(ws)} to {_fmt_day(we)}\n"
                 f"⚙️ {html.escape(describe_rule(rule))}\n\n"
                 f"🔔 Alerts for shows matching this rule appear in this topic."),**({"reply_markup": markup} if markup else {})})
    return ent


def _post_rule(rule_threads, rule, venue, ws, we, text):
    """Send into the rule's weekly topic. If the topic was deleted by hand, make a new one once."""
    for _ in range(2):
        ent = ensure_rule_thread(rule_threads, venue, rule, ws, we)
        if not ent:
            return False
        ok, body = tg_api("sendMessage", {
            "chat_id": ent["chat_id"], "message_thread_id": ent["thread_id"], "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True})
        if ok:
            return True
        if "thread" not in body.get("description", "").lower():
            return False
        ent["thread_id"] = None                                       # forces a new topic next loop
    return False


def send_rule(rule_threads, rule, venue, date_code, text):
    ws, we = week_bounds(date_code)
    return _post_rule(rule_threads, rule, venue, ws, we, text)


def apply_rules(state, rules, rule_threads, now):
    """Mark every new matching show as tracked by the rule, creating weekly topics as needed.
    Returns how many (show, rule) pairs were added."""
    by_id = {r["id"]: r for r in rules}
    added, count = {}, 0

    for venue, dates in state.items():
        if not TRACK_CHATS.get(venue):
            continue
        for date_code, shows in dates.items():
            for sid, show in shows.items():
                for t in show.get("tracked_by_rules") or []:          # keep names in sync after a rename
                    r = by_id.get(t.get("rule_id"))
                    if r and t.get("rule") != r["name"]:
                        t["rule"] = r["name"]

                start = parse_iso(show.get("start"))
                if not start or now > start + RULE_CUTOFF:
                    continue
                done = show.get("tracked_by_rules") or []
                have = {t.get("rule_id") for t in done}

                for rule in rules:
                    if rule["id"] in have or not rule_matches(rule, venue, date_code, show):
                        continue
                    if rule.get("mode") == "budget":
                        low = _min_price(venue, date_code, sid, show)
                        if low is None or low > float(rule.get("max_price") or 0):
                            continue
                    ws, we = week_bounds(date_code)
                    if not ensure_rule_thread(rule_threads, venue, rule, ws, we):
                        continue                                       # try again next run
                    done = done + [{"rule": rule["name"], "rule_id": rule["id"],
                                    "tracked_at": now.isoformat(timespec="seconds")}]
                    show["tracked_by_rules"] = done
                    count += 1
                    logging.info(f"✅ Rule '{rule['name']}' matched {venue} {date_code}/{sid} {show.get('movie')} {show.get('time')}")
                    line = (f"• {html.escape(format_date(date_code))} · {html.escape(show.get('time', ''))} · "
                            f"{html.escape(show.get('movie', ''))} "
                            f"({html.escape(' '.join(x for x in (show.get('language'), show.get('dimension')) if x))})")
                    added.setdefault((venue, rule["id"], ws), (rule, we, []))[2].append(line)

    # one message per topic listing what was just tracked
    for (venue, _rid, ws), (rule, we, lines) in added.items():
        for i in range(0, len(lines), 25):
            part = lines[i:i + 25]
            _post_rule(rule_threads, rule, venue, ws, we,
                       f"✅ Now tracking {len(lines)} show(s)\n" + "\n".join(part))
            time.sleep(1)
    return count


def rules_on_show(show, by_id):
    return [by_id[t["rule_id"]] for t in (show or {}).get("tracked_by_rules") or []
            if t.get("rule_id") in by_id]


def rule_category_ok(rule, price):
    if rule.get("mode") == "budget":
        try:
            return price <= float(rule.get("max_price"))
        except (TypeError, ValueError):
            return False
    return True

def stamp_rule_categories(state, rules_by_id, now):
    """Per rule and show, record when each seat category first qualified (category-level tracked time).
    Shows tracked before this existed are backfilled once from their tracked_at / first_seen_time."""
    iso = now.isoformat(timespec="seconds")
    for dates in state.values():
        for shows in dates.values():
            for show in shows.values():
                for t in show.get("tracked_by_rules") or []:
                    rule = rules_by_id.get(t.get("rule_id"))
                    if not rule:
                        continue
                    legacy = "cats" not in t
                    cats = t.setdefault("cats", {})
                    for name, c in (show.get("classes") or {}).items():
                        price = c.get("price")
                        if name in cats or not price or not rule_category_ok(rule, price):
                            continue
                        stamp = iso
                        if legacy:
                            cand = [x for x in (parse_iso(t.get("tracked_at")),
                                                parse_iso(c.get("first_seen_time"))) if x]
                            if cand:
                                stamp = max(cand).isoformat(timespec="seconds")
                        cats[name] = stamp


def cleanup_rule_threads(rule_threads, now):
    """Delete a weekly topic once its week is over (Monday 03:00 IST)."""
    for key, ent in list(rule_threads.items()):
        try:
            end = datetime.strptime(ent["week_end"], "%Y%m%d").replace(tzinfo=IST) + timedelta(days=1, hours=3)
        except (KeyError, ValueError):
            rule_threads.pop(key, None)
            continue
        if now < end:
            continue
        gone = True
        if ent.get("thread_id") and ent.get("chat_id"):
            ok, body = tg_api("deleteForumTopic", {"chat_id": ent["chat_id"], "message_thread_id": ent["thread_id"]})
            gone = ok or "thread" in body.get("description", "").lower()
        tries = ent.get("tries", 0) + 1
        if gone or tries >= CLEANUP_MAX_TRIES:
            rule_threads.pop(key, None)
        else:
            ent["tries"] = tries
# ───────────────────────── alerts ─────────────────────────

def format_hit(h):
    lines = []
    if h["kind"] == "new":
        lines.append("🆕 NEW SEAT CATEGORY")
    elif h["kind"] == "opened":
        lines.append("🔔 NOW AVAILABLE (was sold out)")
    elif h["kind"] == "more":
        lines.append(f"📈 MORE SEATS (was {STATUS_NAMES[h['prev_status']].lower()})")

    if h["soon"]:
        hours, mins = divmod(max(int(h["mins"]), 0), 60)
        lines.append(f"⏰ SHOW IN {hours}h {mins}m" if hours else f"⏰ SHOW IN {mins} MIN")
    elif h["mins"] is not None and h["mins"] < 0:
        lines.append(f"⏰ STARTED {int(-h['mins'])} MIN AGO")

    # Movie title line (title, language, dimension, censor, subtitles)
    censor_tag = f" ({html.escape(h['censor'])})" if h.get("censor") else ""
    sub_tag = f" {html.escape(h['sub_tag'])}" if h.get("sub_tag") else ""
    lang_dim = " · ".join(html.escape(x) for x in (h.get("language"), h.get("dimension")) if x)
    title_text = html.escape(h["movie"]) + (f" · {lang_dim}" if lang_dim else "")

    if h.get("trailer"):
        lines.append(f'🎬 <b><a href="{html.escape(h["trailer"], quote=True)}">{title_text}</a></b>{censor_tag}{sub_tag}')
    else:
        lines.append(f"🎬 <b>{title_text}</b>{censor_tag}{sub_tag}")

    lines.append(f"🕘 {html.escape(h['time'])}")
    if h["screen"]:
        lines.append(f"🖥️ {html.escape(h['screen'])}")
    if h.get("extra_tags"):
        lines.append(f"✨ <i>{html.escape(h['extra_tags'])}</i>")

    lines.append(f"💺 {html.escape(h['category'])} · ₹{h['price']:g}")
    lines.append(f"📊 {AVAIL_LABELS[h['status']]}")
    lines.append(f'🔗 <a href="{html.escape(h["url"], quote=True)}">Book seats</a>')

    return "\n".join(lines)


def record_alert(state, h, now):
    """Store the class's latest status (and the soon flag) after it was alerted or silently seen.
    first_seen_status / first_seen_time are set once, the first time the class is stored."""
    entry = get_show(state, h["venue"], h["date"], h["session"])
    if entry is None:
        return
    iso = now.isoformat(timespec="seconds")
    c = entry["classes"].get(h["category"])
    if c is None:
        c = entry["classes"][h["category"]] = {
            "soon_alerted": False, "first_seen_status": h["status"], "first_seen_time": iso}
    else:
        c.setdefault("first_seen_status", c.get("status") or h["status"])
        c.setdefault("first_seen_time", iso)
    touch_class(c, h["status"], h["price"], now, f"{h['venue']} {h['date']}/{h['session']} {h['category']}")
    c["soon_alerted"] = h["soon"] or c.get("soon_alerted", False)


def _deliver(group, venue, date_code, send_fn):
    """Send one show's alerts in messages under 3600 characters. Returns the hits delivered."""
    chunks, current, size = [], [], 0
    for h in group:
        block = format_hit(h)
        if current and size + len(block) + len(DIVIDER) + 2 > 3600:
            chunks.append(current)
            current, size = [], 0
        current.append((h, block))
        size += len(block) + len(DIVIDER) + 2
    if current:
        chunks.append(current)

    delivered = []
    for n, chunk in enumerate(chunks, 1):
        part = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
        header = (f"🎟️ <b>{html.escape(TARGET_THEATRES[venue])}</b>\n"
                  f"📅 {format_date(date_code)} · {len(group)} alert(s){part}")
        text = f"{header}\n{DIVIDER}\n" + f"\n{DIVIDER}\n".join(b for _, b in chunk)
        if send_fn(text):
            delivered += [h for h, _ in chunk]
        time.sleep(1)
    return delivered


def send_alerts(hits, state, tracking, now, rules_by_id=None, rule_threads=None):
    """Alerts go to the show's manual topic and/or the topic of each rule tracking it.
    Everything else is recorded silently."""
    rules_by_id, rule_threads = rules_by_id or {}, rule_threads if rule_threads is not None else {}
    manual, by_rule, dest = {}, {}, {}          # dest[hit key] = [destinations, delivered]

    for h in sorted(hits, key=lambda h: h["start"] or now):
        dest[h["key"]] = [0, 0]
        v, d, s = h["venue"], h["date"], h["session"]

        if track_thread(state, tracking, v, d, s) and \
           is_category_tracked(tracking, v, d, s, h["category"], h["price"]):
            manual.setdefault((v, d, s), []).append(h)
            dest[h["key"]][0] += 1

        for rule in rules_on_show(get_show(state, v, d, s), rules_by_id):
            if rule_category_ok(rule, h["price"]):
                by_rule.setdefault((v, d, s, rule["id"]), (rule, []))[1].append(h)
                dest[h["key"]][0] += 1

    for (v, d, s), group in manual.items():
        for h in _deliver(group, v, d, lambda t, v=v, d=d, s=s: send_track(state, tracking, v, d, s, t)):
            dest[h["key"]][1] += 1

    for (v, d, s, _rid), (rule, group) in by_rule.items():
        for h in _deliver(group, v, d, lambda t, v=v, d=d, rule=rule: send_rule(rule_threads, rule, v, d, t)):
            dest[h["key"]][1] += 1
    logging.info(f"Alert plan: {len(hits)} hits → {len(manual)} manual groups, {len(by_rule)} rule groups")
    for h in hits:
      logging.info(f"  {h['kind'].upper():6} {h['venue']} {h['date']}/{h['session']} "
                 f"{h['category']} ₹{h['price']:g} · {h['movie']} · dest={dest[h['key']][0]}")

    sent = 0
    for h in hits:
        n, ok = dest[h["key"]]
        if n == 0 or ok:                         # nothing to send, or at least one copy delivered
            record_alert(state, h, now)
        sent += ok
    return sent

def check_health(meta, stats):
    """Per theatre: warn in its Errors topic when BMS has failed for several runs in a row,
    and again when it recovers. Returns False if no theatre could be reached at all."""
    streaks, alerted = meta["fail_streaks"], meta["outage_alerted"]
    for venue, s in stats.items():
        name = TARGET_THEATRES[venue]
        if s["ok"] == 0:
            # Capped so a long outage doesn't change meta.json (and commit) every run
            streaks[venue] = min(streaks.get(venue, 0) + 1, FAIL_ALERT_AFTER)
            if streaks[venue] == FAIL_ALERT_AFTER and venue not in alerted:
                if send_error(meta, venue,
                              f"⚠️ BMS bot: couldn't fetch showtimes for {name} on {FAIL_ALERT_AFTER} "
                              f"checks in a row ({s['failed']} failed requests). "
                              f"No alerts for this theatre until it recovers."):
                    alerted.append(venue)
        else:
            if venue in alerted:
                alerted.remove(venue)
                send_error(meta, venue, f"✅ BMS bot: {name} is reachable again, alerts resumed.")
            streaks.pop(venue, None)
    return any(s["ok"] for s in stats.values())


# ───────────────────────── main ─────────────────────────

def main():
    now      = datetime.now(IST)
    meta     = load_meta()
    state    = load_state()
    tracking = load_tracking()
    rules_by_id  = {r["id"]: r for r in load_rules() if r.get("enabled", True)}
    rule_threads = load_rule_threads()
    healthy  = False

    tracked = sum(1 for v in tracking.values() for d in v.values() if isinstance(d, dict)
                  for t in d.values() if isinstance(t, dict) and t.get("tracked"))
    logging.info(f"Tracked shows in tracking files: {tracked}")

    missing = [code for code, chat in TRACK_CHATS.items() if not chat]
    if missing:
        logging.error("No tracking group configured for: "
                      + ", ".join(f"TELEGRAM_CHAT_{c}" for c in missing)
                      + " (no alerts or error warnings for these theatres)")

    try:
        session = bms_req.Session(impersonate="chrome")
        try:
            hits, stats, changes = scan(session, state, now)
        finally:
            session.close()
        ok     = sum(s["ok"] for s in stats.values())
        failed = sum(s["failed"] for s in stats.values())
        newly = apply_rules(state, list(rules_by_id.values()), rule_threads, now)
        if newly:
            logging.info(f"Rules: {newly} show(s) newly tracked")
        if hits or rules_by_id:
            logging.info(f"Telegram alerts delivered: "
                         f"{send_alerts(hits, state, tracking, now, rules_by_id, rule_threads)}/{len(hits)}")
        stamp_rule_categories(state, rules_by_id, now)

        if changes:
            announce_time_changes(state, tracking, changes)
        healthy = check_health(meta, stats)
    except Exception as e:
        logging.exception("Run crashed")
        for venue in TARGET_THEATRES:
            send_error(meta, venue, f"🔥 BMS bot crashed: {type(e).__name__}: {str(e)[:300]}")

    try:
        cleanup_ended(state, tracking, now)   # drop finished shows, delete their topics
    except Exception:
        logging.exception("Cleanup failed")
    try:
        cleanup_rule_threads(rule_threads, now)
    except Exception:
        logging.exception("Rule thread cleanup failed")
    save_rule_threads(rule_threads)
    total_shows = sum(len(s) for d in state.values() for s in d.values())
    logging.info(f"━━ RUN SUMMARY ━━ healthy={healthy} · shows={total_shows} · "
             f"hits={len(hits) if 'hits' in locals() else 0} · "
             f"failed_requests={failed if 'failed' in locals() else '?'}")
    save_all(state, meta, now)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
