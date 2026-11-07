"""BookMyShow seat-availability alert scanner for Chennai theatres.

Each run scans every bookable date at the target theatres and records every BUDGET
seat class it sees (price <= MAX_BUDGET_PRICE, even sold out) with its availability.

FILES (one writer each, so nothing ever overwrites anything else)

  docs/state/<VENUE>.json     WRITTEN BY THIS SCANNER, also the Mini App's data source
      { "20261107": { "28451": {
            "movie", "language", "dimension", "screen", "time", "start", "end", "url",
            "duration": 150,                       # movie length in minutes
            "tags": ["IMAX", "Dolby Atmos"],       # BMS attributes, Atmos, subtitle code
            "classes": { "SILVER": {"price": 80, "status": "3", "soon_alerted": false,
                                    "first_seen_status": "0",
                                    "first_seen_time": "2026-11-07T12:09:58+05:30"} } } },
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

from curl_cffi import requests as bms_req

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

IST = ZoneInfo("Asia/Kolkata")

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
META_FILE          = "meta.json"
DIVIDER            = "━━━━━━━━━━━━━━━━"

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


def load_tracking():
    """tracking[VENUE] = {date: {session: {"tracked", "thread"}}}  (read-only here)."""
    tracking = {}
    for venue in TARGET_THEATRES:
        data = read_json(os.path.join(TRACKING_DIR, f"{venue}.json"), {})
        tracking[venue] = data if isinstance(data, dict) else {}
    return tracking


def load_meta():
    meta = read_json(META_FILE, {})
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("threads", {})          # "<VENUE>_errors" -> topic id
    meta.setdefault("fail_streaks", {})     # venue -> consecutive failed runs (capped)
    meta.setdefault("outage_alerted", [])   # venues whose outage warning was delivered
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
    if read_json(META_FILE, {}) != meta:
        write_json(META_FILE, meta)


def get_show(state, venue, date_code, session):
    return ((state.get(venue) or {}).get(date_code) or {}).get(session)


def track_thread(state, tracking, venue, date_code, session):
    """Topic id for a TRACKED show, else None. If the original topic was deleted and this
    scanner made a replacement, the replacement is used while the file still lists the old id."""
    t = ((tracking.get(venue) or {}).get(date_code) or {}).get(session) or {}
    if not t.get("tracked") or not t.get("thread"):
        return None
    s = get_show(state, venue, date_code, session) or {}
    if s.get("thread") and s.get("thread_for") == t["thread"]:
        return s["thread"]
    return t["thread"]

def is_category_tracked(tracking, venue, date_code, session, category):
    """True only when this category is selected for tracking."""
    t = ((tracking.get(venue) or {}).get(date_code) or {}).get(session) or {}

    if not t.get("tracked"):
        return False

    selected = t.get("categories", [])

    if not isinstance(selected, list):
        return False

    return category.strip().upper() in {
        str(c).strip().upper() for c in selected
    }


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
    original = tracking[venue][date_code][session]["thread"]
    ok_t, tb = tg_api("createForumTopic", {"chat_id": tc, "name": track_name(date_code, s)})
    if not ok_t:
        return False
    new_thread = tb["result"]["message_thread_id"]
    s["thread"], s["thread_for"] = new_thread, original
    tg_api("sendMessage", {"chat_id": tc, "message_thread_id": new_thread, "text": status_text(s),
                           "parse_mode": "HTML", "disable_notification": True,
                           "disable_web_page_preview": True})
    ok, _ = tg_api("sendMessage", {"chat_id": tc, "message_thread_id": new_thread, "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": True})
    return ok


def announce_time_changes(state, tracking, changes):
    """Tell tracked shows' topics about a new showtime and rename the topic."""
    for c in changes:
        thread = track_thread(state, tracking, c["venue"], c["date"], c["session"])
        tc = TRACK_CHATS.get(c["venue"])
        s = get_show(state, c["venue"], c["date"], c["session"])
        if not (thread and tc and s):
            continue
        tg_api("sendMessage", {"chat_id": tc, "message_thread_id": thread,
                               "text": f"⏰ Time changed: {c['old']} → {c['new']}"})
        tg_api("editForumTopic", {"chat_id": tc, "message_thread_id": thread,
                                  "name": track_name(c["date"], s)})


# ───────────────────────── BMS fetching ─────────────────────────

def fetch_showtimes(session, venue, date_code):
    url = SHOWTIMES_URL.format(date=date_code, venue=venue)
    for _ in range(2):
        try:
            r = session.get(url, headers=BMS_HEADERS, timeout=12)
            if r.status_code == 200:
                return r.json()
            logging.warning(f"{venue} {date_code}: HTTP {r.status_code}")
            if 400 <= r.status_code < 500:
                return None  # blocked or bad request; retrying won't help
        except Exception as e:
            logging.warning(f"{venue} {date_code}: {type(e).__name__}: {e}")
        time.sleep(2)
    return None


def iter_categories(data):
    for day in data.get("ShowDetails") or []:
        for ev in day.get("Event") or []:
            duration = ev.get("EventDuration") or "0"
            clean_title = ev.get("EventTitle") or "Movie"
            for child in ev.get("ChildEvents") or []:
                for show in child.get("ShowTimes") or []:
                    for cat in show.get("Categories") or []:
                        yield day, child, show, cat, duration, clean_title


def register_shows(vstate, venue, data, now):
    """Keep one entry per show under vstate[date][session]. Returns showtime changes found."""
    changes = []
    for day, child, show, cat, duration, title in iter_categories(data):
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
        entry = vstate.setdefault(date_code, {}).setdefault(sid, {"classes": {}, "time": ""})
        new_time = show.get("ShowTime") or ""
        if entry.get("time") and new_time and new_time != entry["time"]:
            changes.append({"venue": venue, "date": date_code, "session": sid,
                            "old": entry["time"], "new": new_time})
        entry["time"]      = new_time or entry.get("time", "")
        entry["movie"]     = title
        entry["language"]  = child.get("EventLanguage") or ""
        entry["dimension"] = child.get("EventDimension") or ""
        entry["screen"]    = show.get("ScreenName") or ""
        entry["url"]       = SEAT_LAYOUT_URL.format(event=child.get("EventCode"), venue=venue,
                                                    session=session_id, date=date_code)
        entry["start"]     = start.isoformat(timespec="seconds")
        entry["end"]       = (start + SHOW_GRACE).isoformat(timespec="seconds")
        entry["duration"]  = int(duration) if str(duration).isdigit() else 0   # movie length in minutes
        entry["tags"]      = [t for t in (show.get("Attributes"),
                                          "Dolby Atmos" if show.get("IsAtmosEnabled") == "Y" else "",
                                          show.get("SessionSubTitleAcronym")) if t]
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
    sub_acronym = show.get("SessionSubTitleAcronym") or ""
    attributes = show.get("Attributes") or ""
    atmos = "Dolby Atmos" if show.get("IsAtmosEnabled") == "Y" else ""
    tags = [t for t in (attributes, atmos) if t]
    extra_tags = " · ".join(tags)
    sub_tag = f"[{sub_acronym}💬]" if sub_acronym else ""

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
            prev["status"] = status   # no ping, but keep the stored availability current
            prev["price"] = price
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
            try:
                changes += register_shows(vstate, venue, data, now)
            except Exception as e:
                logging.warning(f"{venue} {date_code}: register_shows failed ({type(e).__name__}: {e})")
            scanned.add((venue, date_code))
            scanned.update((venue, d.get("Date")) for d in data.get("ShowDetails") or [])

            if len(done) == 1:
                dates += [d.get("DateCode") for d in data.get("ShowDatesArray") or []
                          if d.get("DateCode") and not d.get("isDisabled")]

            for day, child, show, cat, duration, clean_title in iter_categories(data):
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
                        c["status"] = "0"

    return hits, stats, changes


def cleanup_ended(state, tracking, now):
    """Drop shows past their window and delete their tracking topics. If a topic can't be
    deleted, keep the show and retry on later runs (up to CLEANUP_MAX_TRIES)."""
    for venue, dates in state.items():
        for date_code in list(dates):
            shows = dates[date_code]
            for sid in list(shows):
                s = shows[sid]
                if entry_end(s, date_code) >= now:
                    continue
                thread = track_thread(state, tracking, venue, date_code, sid)
                tc = TRACK_CHATS.get(venue)
                if thread and tc:
                    ok, body = tg_api("deleteForumTopic", {"chat_id": tc, "message_thread_id": thread})
                    gone = "thread" in body.get("description", "").lower()
                    if not ok and not gone:
                        s["cleanup_tries"] = s.get("cleanup_tries", 0) + 1
                        if s["cleanup_tries"] < CLEANUP_MAX_TRIES:
                            continue                      # retry next run
                shows.pop(sid)
            if not shows:
                dates.pop(date_code)


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
    prev = entry["classes"].get(h["category"], {})
    entry["classes"][h["category"]] = {
        "price": h["price"], "status": h["status"],
        "soon_alerted": h["soon"] or prev.get("soon_alerted", False),
        "first_seen_status": prev.get("first_seen_status") or prev.get("status") or h["status"],
        "first_seen_time": prev.get("first_seen_time") or now.isoformat(timespec="seconds"),
    }


def send_alerts(hits, state, tracking, now):
    """Send alerts only for selected categories in tracked shows.
    All other category statuses are still recorded silently."""
    groups = {}

    for h in sorted(hits, key=lambda h: h["start"] or now):

        thread = track_thread(
            state, tracking,
            h["venue"], h["date"], h["session"]
        )

        category_selected = is_category_tracked(
            tracking,
            h["venue"],
            h["date"],
            h["session"],
            h["category"]
        )

        if thread and category_selected:
            groups.setdefault(
                (h["venue"], h["date"], h["session"]), []
            ).append(h)
        else:
            # Keep the category's status current without sending an alert.
            record_alert(state, h, now)

    sent = 0
    for (venue, date_code, session), group in groups.items():
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
        for n, chunk in enumerate(chunks, 1):
            part = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
            header = (f"🎟️ <b>{html.escape(TARGET_THEATRES[venue])}</b>\n"
                      f"📅 {format_date(date_code)} · {len(group)} alert(s){part}")
            text = f"{header}\n{DIVIDER}\n" + f"\n{DIVIDER}\n".join(b for _, b in chunk)
            if send_track(state, tracking, venue, date_code, session, text):
                for h, _ in chunk:
                    record_alert(state, h, now)
                sent += len(chunk)
            time.sleep(1)
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
        logging.info(f"BMS requests: {ok} ok, {failed} failed. Alerts to send: {len(hits)}")
        if hits:
            logging.info(f"Telegram alerts delivered: {send_alerts(hits, state, tracking, now)}/{len(hits)}")
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
    save_all(state, meta, now)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
