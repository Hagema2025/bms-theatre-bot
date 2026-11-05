"""BookMyShow seat-availability alert bot for Chennai theatres.

Each run scans every bookable date at the target theatres and stores every BUDGET
seat class it sees (price <= MAX_BUDGET_PRICE, even sold out) in state.json together
with its availability status. On the next runs it pings when:
  - a class is seen for the first time and is bookable        -> NEW
  - a sold-out class becomes bookable (status 0 -> 1/2/3)     -> NOW AVAILABLE
  - a bookable class gets more seats (1 -> 2, 2 -> 3, 1 -> 3) -> MORE SEATS
  - the show is within REMINDER_MINUTES of start (once)       -> SHOW SOON
There is no flicker damping: every increase pings.

Alerts for a show stop SHOW_GRACE_MINUTES (default 30) after its start time.
A show's stored state is dropped at that point. A theatre's date topic is closed
once every show of that date is past its window (so a date with a 12:30 AM show
stays open until ~1:00 AM, not until calendar midnight). When a topic is closed the
bot posts a "🗑 Delete this topic" button in it; the press is handled on the next run.

Only budget seat classes (<= MAX_BUDGET_PRICE, default 100) are tracked and alerted;
everything priced above that is ignored.

Telegram layout: one supergroup (Topics enabled) PER THEATRE. Inside each group:
  - one topic per show date (e.g. "Wed 04 Nov")
  - one "Errors" topic for that theatre's outage / crash warnings
The bot must be a group admin with "Manage Topics" (and "Delete Messages" for the delete button).

Chat IDs come from env vars, one per theatre: TELEGRAM_CHAT_<VENUE_CODE>
(e.g. TELEGRAM_CHAT_INTO). Keep them in GitHub secrets.
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

# One Telegram supergroup per theatre, e.g. TELEGRAM_CHAT_INTO=-1001234567890
THEATRE_CHATS = {code: os.environ.get(f"TELEGRAM_CHAT_{code}", "").strip()
                 for code in TARGET_THEATRES}

MAX_BUDGET_PRICE  = float(os.environ.get("MAX_BUDGET_PRICE", "100"))   # only seat classes priced <= this are tracked and alerted
REMINDER_MINUTES  = int(os.environ.get("REMINDER_MINUTES", "120"))
SHOW_GRACE        = timedelta(minutes=int(os.environ.get("SHOW_GRACE_MINUTES", "30")))  # alerts stop this long after showtime
MAX_DATES         = int(os.environ.get("MAX_DATES", "7"))
FAIL_ALERT_AFTER  = 3
TG_RETRIES        = 4
ERRORS_THREAD_NAME = "⚠️ Errors"
STATE_FILE        = "state.json"
DIVIDER           = "━━━━━━━━━━━━━━━━"

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


console_print("🤖 BookMyShow alert bot module loaded.")

console_print(f"⚙️ Budget limit: ₹{MAX_BUDGET_PRICE:.0f}")
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


def format_date(date_code):
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
    except (TypeError, ValueError):
        return date_code


def entry_end(entry):
    """When alerts for this stored show stop (showtime + grace). Older entries without it
    are kept until the end of their calendar date."""
    try:
        return datetime.fromisoformat(entry["end"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return datetime.strptime(entry.get("date", ""), "%Y%m%d").replace(tzinfo=IST) + timedelta(days=1)
    except (TypeError, ValueError):
        return datetime(2000, 1, 1, tzinfo=IST)


# ───────────────────────── state ─────────────────────────
# state["alerts"]:  every seat class seen -> {movie, theatre, date, time, category, price,
#                                             status, end, alerted_at?, soon_alerted}
# state["threads"]: "<VENUE>_errors" and "<VENUE>_date_<YYYYMMDD>" -> Telegram topic id
# state["tg_offset"]: next Telegram update id to read (for delete-button presses)

def load_state():
    console_print("📂 Loading saved state...")

    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            console_print("✅ Previous state loaded successfully.")

        except (OSError, ValueError) as e:
            console_print(f"❌ State loading failed: {e}")
    else:
        console_print("🆕 No previous state found. Starting fresh.")

    if not isinstance(state, dict):
        state = {}
    state.setdefault("alerts", {})
    state.setdefault("threads", {})
    state.setdefault("fail_streaks", {})      # venue -> consecutive failed runs (capped)
    state.setdefault("outage_alerted", [])    # venues whose outage warning was delivered
    state.pop("fail_streak", None)            # old single-streak format
    state.pop("last_heartbeat", None)         # changed every run and forced a commit per run

    console_print(
        f"📊 Previously tracked seat classes: {len(state['alerts'])}"
    )
    console_print(
        f"📌 Previously created Telegram topics: {len(state['threads'])}"
    )
    return state


def save_state(state):
    console_print("💾 Saving bot state...")

    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# ───────────────────────── telegram ─────────────────────────

def tg_api(method, payload):
    """Call a Telegram Bot API method via curl_cffi with retries. Returns (ok, body)."""
    console_print(f"📨 Telegram API request: {method}")

    if not TELEGRAM_BOT_TOKEN:
        console_print("❌ TELEGRAM_BOT_TOKEN is missing!")
        return False, {}
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    body = {}
    for attempt in range(1, TG_RETRIES + 1):
        console_print(
            f"🔄 Telegram attempt {attempt}/{TG_RETRIES}: {method}"
        )
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
            console_print(
                f"⚠️ Telegram error: HTTP {resp.status_code} - {desc}"
            )
            if resp.status_code == 429:
                wait = int(body.get("parameters", {}).get("retry_after", 5))
                console_print(
                    f"⏳ Telegram rate limit. Waiting {wait} seconds."
                )
                time.sleep(min(wait, 30))
                continue
            if 400 <= resp.status_code < 500:
                console_print("❌ Telegram client error. No retry.")
                return False, body  # bad token/chat/thread; retrying won't help
        except Exception as e:
            console_print(
                f"❌ Telegram connection error: {type(e).__name__}"
            )
            # Exception text can contain the URL (bot token), so log only the type
            logging.warning(f"Telegram {method} attempt {attempt}/{TG_RETRIES}: {type(e).__name__}")
        if attempt < TG_RETRIES:
            console_print(f"⏳ Retrying after {wait} seconds...")

            time.sleep(2 ** attempt)  # 2s, 4s, 8s backoff for network errors and 5xx
    console_print(f"❌ Telegram {method} failed after all retries.")

    return False, body


def get_thread(state, chat_id, key, name, force=False, note=None):
    """Topic ID for key in this chat, created on first use (with an intro note posted first).
    None if it can't be created."""
    if key in state["threads"] and not force:
        return state["threads"][key]
    ok, body = tg_api("createForumTopic", {"chat_id": chat_id, "name": name})
    if not ok:
        logging.warning(f"{key}: couldn't create topic '{name}' ({body.get('description', '')}); "
                        f"posting to the group's main chat instead")
        return None
    thread_id = body["result"]["message_thread_id"]
    state["threads"][key] = thread_id
    if note:
        # First message in the topic, before any alert; a failure here must not block alerts
        tg_api("sendMessage", {"chat_id": chat_id, "message_thread_id": thread_id, "text": note,
                               "disable_notification": True, "disable_web_page_preview": True})
    return thread_id


def send_to_thread(state, venue, thread_key, name, text, silent=False, html_mode=False, note=None):
    """Send text into a topic of the theatre's group; recreate the topic once if it was deleted.
    silent=True delivers without a notification sound; html_mode=True sends as Telegram HTML
    (links, bold); note is posted first when the topic is newly created. Returns True on success."""
    chat_id = THEATRE_CHATS.get(venue)
    if not chat_id:
        logging.error(f"{venue}: no TELEGRAM_CHAT_{venue} set, can't send")
        return False
    key = f"{venue}_{thread_key}"
    thread_id = get_thread(state, chat_id, key, name, note=note)
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True,
               "disable_notification": silent}
    if html_mode:
        payload["parse_mode"] = "HTML"
    if thread_id:
        payload["message_thread_id"] = thread_id
    ok, body = tg_api("sendMessage", payload)
    if not ok and thread_id and "thread" in body.get("description", "").lower():
        thread_id = get_thread(state, chat_id, key, name, force=True, note=note)
        payload.pop("message_thread_id", None)
        if thread_id:
            payload["message_thread_id"] = thread_id
        ok, body = tg_api("sendMessage", payload)
    return ok


def date_note(venue, date_code):
    """First message of a new date topic."""
    return (f"📌 NOTE\n"
            f"🎟️ {TARGET_THEATRES[venue]}\n"
            f"📅 {format_date(date_code)}\n"
            f"{DIVIDER}\n"
            f"Budget seats ≤ ₹{MAX_BUDGET_PRICE:.0f} only.\n"
            f"🔔 Now available = was sold out\n"
            f"📈 More seats = availability up\n"
            f"⏰ Show soon = starts within {REMINDER_MINUTES // 60}h\n"
            f"Alerts stop {int(SHOW_GRACE.total_seconds() // 60)} min after showtime.\n"
            f"Topic closes after the last show.")


def errors_note(venue):
    """First message of a new Errors topic."""
    return (f"📌 NOTE\n"
            f"🎟️ {TARGET_THEATRES[venue]}\n"
            f"{DIVIDER}\n"
            f"BookMyShow outages, recoveries and\n"
            f"bot crashes are posted here.")


def send_error(state, venue, text):
    return send_to_thread(state, venue, "errors", ERRORS_THREAD_NAME, text,
                          note=errors_note(venue))


def finish_date_topic(state, venue, date_code, key):
    """Post a delete button in a finished date's topic, then close the topic."""
    chat_id   = THEATRE_CHATS.get(venue)
    thread_id = state["threads"].get(key)
    if not chat_id or not thread_id:
        state["threads"].pop(key, None)
        return
    ok, body = tg_api("sendMessage", {
        "chat_id": chat_id, "message_thread_id": thread_id, "disable_notification": True,
        "text": f"📅 {format_date(date_code)} is over: every show has started. This topic is now closed.",
        "reply_markup": {"inline_keyboard": [[
            {"text": "🗑 Delete this topic", "callback_data": f"del:{thread_id}"}]]},
    })
    gone = "thread" in body.get("description", "").lower()   # topic was already deleted by hand
    if ok:
        tg_api("closeForumTopic", {"chat_id": chat_id, "message_thread_id": thread_id})
    if ok or gone:
        state["threads"].pop(key, None)
    # otherwise keep the key and retry on the next run


def process_callbacks(state):
    """Handle presses of the 'Delete this topic' button (only group admins may delete)."""
    ok, body = tg_api("getUpdates", {"offset": state.get("tg_offset", 0), "timeout": 0,
                                     "allowed_updates": ["callback_query"]})
    if not ok:
        return
    known_chats = {chat for chat in THEATRE_CHATS.values() if chat}
    for upd in body.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        cq   = upd.get("callback_query") or {}
        data = cq.get("data") or ""
        chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        thread  = data[4:]
        if not data.startswith("del:") or not thread.isdigit() or chat_id not in known_chats:
            continue
        ok_m, mb = tg_api("getChatMember", {"chat_id": chat_id, "user_id": (cq.get("from") or {}).get("id")})
        is_admin = ok_m and mb["result"].get("status") in ("creator", "administrator")
        if not is_admin:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"],
                                           "text": "Only group admins can delete topics.", "show_alert": True})
            continue
        deleted, _ = tg_api("deleteForumTopic", {"chat_id": chat_id, "message_thread_id": int(thread)})
        tg_api("answerCallbackQuery", {
            "callback_query_id": cq["id"], "show_alert": not deleted,
            "text": "Topic deleted." if deleted else "Couldn't delete it. The bot needs the Delete Messages right.",
        })


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
            clean_title = ev.get("EventTitle") or "Movie"  # <--- NEW: Extract clean title here
            for child in ev.get("ChildEvents") or []:
                for show in child.get("ShowTimes") or []:
                    for cat in show.get("Categories") or []:
                        yield day, child, show, cat, duration, clean_title


def check_category(venue, day, child, show, cat, duration, clean_title,alerts, now, seen):
    """Track one seat class in alerts and return an alert dict if it should ping, else None.
    Adds every class it processes to seen."""
    try:
        price = float(cat.get("CurPrice") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price > MAX_BUDGET_PRICE:
        return None   # only budget seats are tracked
    
    session_id = show.get("SessionId")
    date_code  = show.get("ShowDateCode") or day.get("Date")
    if not session_id or not date_code:
        return None
        
    desc   = (cat.get("PriceDesc") or "").strip().upper()
    key    = f"{venue}_{session_id}_{date_code}_{desc}"
    status = str(cat.get("AvailStatus", "0"))
    if status not in STATUS_NAMES:
        return None

    start = parse_bms_time(show.get("ShowDateTime"))
    # Alerts stop SHOW_GRACE after the show starts (BMS's own cutoff time isn't used)
    end = start + SHOW_GRACE if start else parse_bms_time(show.get("CutOffDateTime"))
    if end and end < now:
        return None
        
    seen.add(key)
    end_iso = end.isoformat(timespec="seconds") if end else ""
    mins_to_start = (start - now).total_seconds() / 60 if start else None
    soon = mins_to_start is not None and 0 <= mins_to_start <= REMINDER_MINUTES

    # --- NEW EXTRA TAGS EXTRACTED HERE ---
    sub_acronym = show.get("SessionSubTitleAcronym") or "" 
    attributes = show.get("Attributes") or ""
    atmos = "Dolby Atmos" if show.get("IsAtmosEnabled") == "Y" else ""
    dimension = child.get("EventDimension") or ""
    censor = child.get("EventCensor") or ""
    trailer = child.get("EventTrailer") or "" # <--- NEW: GET TRAILER
    # NEW: Grab the clean language and parent Title
    language = child.get("EventLanguage") or ""
    
    # Add language to the front of the tags list
    tags = [t for t in (attributes, atmos) if t]
    extra_tags = " · ".join(tags)
    # Format the subtitle bracket if it exists (e.g., "[ENG SUB]")
    sub_tag = f"[{sub_acronym}💬]" if sub_acronym else ""

    # --- CALCULATE SHOW END TIME ---
    time_str = show.get("ShowTime") or ""
    if start and duration.isdigit() and int(duration) > 0:
        total_mins = int(duration) + 25
        end_time = start + timedelta(minutes=total_mins)
        time_str = f"{time_str} - {end_time.strftime('%I:%M %p')}"

    prev = alerts.get(key)
    if prev is None:
        if status == "0":
            # Sold out right now: store it so a later run can ping when it opens
            alerts[key] = {
                "movie": child.get("EventName") or "Movie", "theatre": TARGET_THEATRES[venue],
                "date": date_code, "time": time_str,
                "category": desc, "price": price,
                "status": "0", "end": end_iso, "soon_alerted": False,
            }
            return None
        kind, prev_status = "new", None
    else:
        # Entries from older versions have no status: assume sold out if flagged, else full
        prev_status = prev.get("status") or ("0" if prev.get("sold_out") else "3")
        if int(status) > int(prev_status):
            kind = "opened" if prev_status == "0" else "more"
        elif soon and status != "0" and not prev.get("soon_alerted"):
            kind = "soon"
        else:
            prev["status"] = status   # no ping, but keep the stored availability current
            if end_iso:
                prev["end"] = end_iso
            return None

    return {
        "key": key, "kind": kind, "soon": soon, "mins": mins_to_start,
        "venue": venue, "theatre": TARGET_THEATRES[venue],
"movie": clean_title,  # <--- USE CLEAN TITLE HERE
        "date": date_code,
                "time": time_str, "screen": show.get("ScreenName") or "",
        "category": desc, "price": price, "status": status, "prev_status": prev_status,
        "start": start, "end": end_iso,
        "censor": censor, "extra_tags": extra_tags, # <--- NEW DATA ADDED TO HIT
        "trailer": trailer, # <--- ADD IT HERE
        "url": SEAT_LAYOUT_URL.format(event=child.get("EventCode"), venue=venue,
                                      session=session_id, date=date_code),
                                      "sub_tag":sub_tag,
                                      "dimension":dimension
    }

def scan(session, state, now):
    """Check every bookable date at every theatre.
    Returns (hits, stats) where stats is {venue: {"ok": n, "failed": n}}."""
    hits, keys = [], set()
    stats = {venue: {"ok": 0, "failed": 0} for venue in TARGET_THEATRES}
    seen, scanned = set(), set()
    
    for venue in TARGET_THEATRES:
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
            scanned.add((venue, date_code))
            scanned.update((venue, d.get("Date")) for d in data.get("ShowDetails") or [])
            
            if len(done) == 1:
                dates += [d.get("DateCode") for d in data.get("ShowDatesArray") or []
                          if d.get("DateCode") and not d.get("isDisabled")]
                          
            for day, child, show, cat, duration, clean_title in iter_categories(data): # <-- Added duration here
                try:
                    hit = check_category(venue, day, child, show, cat, duration, clean_title, state["alerts"], now, seen) # <-- Added duration here
                except Exception as e:
                    logging.warning(f"{venue} {date_code}: skipped a seat class ({type(e).__name__}: {e})")
                    continue
                if hit and hit["key"] not in keys:
                    keys.add(hit["key"])
                    hits.append(hit)

    for key, entry in state["alerts"].items():
        if ((key.split("_")[0], entry.get("date")) in scanned
                and key not in seen and entry.get("status") != "0"):
            entry["status"] = "0"
            
    return hits, stats

def cleanup_ended(state, now):
    """Drop stored shows past their alert window. A theatre date whose shows have ALL just
    expired is queued in state["closing"] and its topic is closed (retried every run until it
    works). A topic is never closed merely because no shows are stored for it."""
    kept, expired = {}, set()
    for k, v in state["alerts"].items():
        if not isinstance(v, dict):
            continue
        if entry_end(v) >= now:
            kept[k] = v
        else:
            expired.add((k.split("_")[0], v.get("date")))
    state["alerts"] = kept
    live = {(k.split("_")[0], v.get("date")) for k, v in kept.items()}

    closing = state.setdefault("closing", [])
    for venue, date_code in expired - live:
        key = f"{venue}_date_{date_code}"
        if key in state["threads"] and key not in closing:
            closing.append(key)
    for key in list(closing):
        venue, _, date_code = key.split("_")
        if (venue, date_code) in live or key not in state["threads"]:
            closing.remove(key)                        # shows came back, or topic already gone
            continue
        finish_date_topic(state, venue, date_code, key)
        if key not in state["threads"]:
            closing.remove(key)                        # closed; otherwise retry next run


# ───────────────────────── alerts ─────────────────────────

def format_hit(h):
    lines = []
    if h["kind"] == "new":
      lines.append("🆕 NEW BUDGET SEATS")
    elif h["kind"] == "opened":
        lines.append("🔔 NOW AVAILABLE (was sold out)")
    elif h["kind"] == "more":
        lines.append(f"📈 MORE SEATS (was {STATUS_NAMES[h['prev_status']].lower()})")
        
    if h["soon"]:
        hours, mins = divmod(max(int(h["mins"]), 0), 60)
        lines.append(f"⏰ SHOW IN {hours}h {mins}m" if hours else f"⏰ SHOW IN {mins} MIN")
    elif h["mins"] is not None and h["mins"] < 0:
        lines.append(f"⏰ STARTED {int(-h['mins'])} MIN AGO")
        
    # --- MOVIE TITLE LINE (Title, 3D, Censor, Subtitles) ---
    censor_tag = f" ({html.escape(h['censor'])})" if h.get("censor") else ""
    dim_tag = f" {html.escape(h['dimension'])}" if h.get("dimension")  else ""
    sub_tag = f" {html.escape(h['sub_tag'])}" if h.get("sub_tag") else ""
    lang_text = f"{html.escape(h['language'])}" if h.get("language") else ""

    title_text = f"{html.escape(h['movie'])} {lang_text}-{dim_tag}"
    
    if h.get("trailer"):
        lines.append(f'🎬 <b><a href="{html.escape(h["trailer"], quote=True)}">{title_text}</a></b>{censor_tag}{sub_tag}')
    else:
        lines.append(f"🎬 <b>{title_text}</b>{censor_tag}{sub_tag}")

    lines.append(f"🕘 {html.escape(h['time'])}")
    if h["screen"]:
              lines.append(f"🖥️ {html.escape(h['screen'])}")
        
    
    if h.get("extra_tags"):
        lines.append(f"✨ <i>{html.escape(h['extra_tags'])}</i>")
        
        
    

          
    # --- PRICE (No decimals) & STATUS ---
    lines.append(f"💺 {html.escape(h['category'])} · ₹{h['price']}")
    lines.append(f"📊 {AVAIL_LABELS[h['status']]}")
    lines.append(f'🔗 <a href="{html.escape(h["url"], quote=True)}">Book seats</a>')
    
    return "\n".join(lines)

def record_alert(state, h, now):
    prev = state["alerts"].get(h["key"], {})
    state["alerts"][h["key"]] = {
        "movie": h["movie"], "theatre": h["theatre"], "date": h["date"], "time": h["time"],
        "category": h["category"], "price": h["price"], "status": h["status"], "end": h["end"],
        "alerted_at": now.isoformat(timespec="seconds"),
        "soon_alerted": h["soon"] or prev.get("soon_alerted", False),
    }


def send_alerts(hits, state, now):
    """One message per (theatre, date), split if long, posted in that theatre group's date topic.
    Returns alerts delivered."""
    groups = {}
    for h in sorted(hits, key=lambda h: h["start"] or now):
        groups.setdefault((h["venue"], h["date"]), []).append(h)
    sent = 0
    for (venue, date_code), group in groups.items():
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
            part   = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
            header = (f"🎟️ <b>{html.escape(TARGET_THEATRES[venue])}</b>\n"
                      f"📅 {format_date(date_code)} · {len(group)} budget alert(s){part}")
            text = f"{header}\n{DIVIDER}\n" + f"\n{DIVIDER}\n".join(b for _, b in chunk)
            if send_to_thread(state, venue, f"date_{date_code}", format_date(date_code), text,
                              html_mode=True, note=date_note(venue, date_code)):
                for h, _ in chunk:
                    record_alert(state, h, now)
                sent += len(chunk)
            time.sleep(1)
    return sent


def check_health(state, stats):
    """Per theatre: warn in that group's Errors topic when BMS has failed for several runs in a row,
    and again when it recovers. Returns False if no theatre could be reached at all."""
    streaks, alerted = state["fail_streaks"], state["outage_alerted"]
    for venue, s in stats.items():
        name = TARGET_THEATRES[venue]
        if s["ok"] == 0:
            # Capped so a long outage doesn't change state.json (and commit) every run
            streaks[venue] = min(streaks.get(venue, 0) + 1, FAIL_ALERT_AFTER)
            if streaks[venue] == FAIL_ALERT_AFTER and venue not in alerted:
                if send_error(state, venue,
                              f"⚠️ BMS bot: couldn't fetch showtimes for {name} on {FAIL_ALERT_AFTER} "
                              f"checks in a row ({s['failed']} failed requests). "
                              f"No alerts for this theatre until it recovers."):
                    alerted.append(venue)
        else:
            if venue in alerted:
                alerted.remove(venue)
                send_error(state, venue, f"✅ BMS bot: {name} is reachable again, alerts resumed.")
            streaks.pop(venue, None)
    return any(s["ok"] for s in stats.values())


# ───────────────────────── main ─────────────────────────

def main():
    now     = datetime.now(IST)
    state   = load_state()
    healthy = False
    # If this always says 0 shows / 0 topics, state.json isn't being saved between runs
    logging.info(f"State loaded: {len(state['alerts'])} shows, {len(state['threads'])} topics")

    missing = [code for code, chat in THEATRE_CHATS.items() if not chat]
    if missing:
        logging.error("No Telegram chat configured for: "
                      + ", ".join(f"TELEGRAM_CHAT_{c}" for c in missing))

    try:
        process_callbacks(state)          # delete-button presses since the last run
    except Exception:
        logging.exception("Callback handling failed")

    try:
        session = bms_req.Session(impersonate="chrome")
        try:
            hits, stats = scan(session, state, now)
        finally:
            session.close()
        ok     = sum(s["ok"] for s in stats.values())
        failed = sum(s["failed"] for s in stats.values())
        logging.info(f"BMS requests: {ok} ok, {failed} failed. Alerts to send: {len(hits)}")
        if hits:
            logging.info(f"Telegram alerts delivered: {send_alerts(hits, state, now)}/{len(hits)}")
        healthy = check_health(state, stats)
    except Exception as e:
        logging.exception("Run crashed")
        for venue in TARGET_THEATRES:
            send_error(state, venue, f"🔥 BMS bot crashed: {type(e).__name__}: {str(e)[:300]}")

    try:
        cleanup_ended(state, now)         # expire old shows, close finished date topics
    except Exception:
        logging.exception("Cleanup failed")
    save_state(state)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
