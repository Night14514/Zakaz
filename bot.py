import os, json, time, sqlite3, threading, queue, secrets, hmac, traceback
import http.client, random, re
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, Future

try:
    import fcntl
except ImportError:
    fcntl = None

BOT_TOKEN = os.getenv("BOT_TOKEN", "1780243378:m-J5FopJEBzXUHR2zOLopC7oaw73o-mbg2y")
API_URL = "https://api.staticgram.top"
DB_PATH = "bot.db"
LOCK_FILE = ".bot_instance.lock"

OWNER_USERNAME = "jumka"
AUTHORIZED_ADMIN_USERNAMES = {"jumka"}
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "CzML4O7xP5")

LOG_CMD = "/logu"
LOG_TITLE = "логи"

MIN_BET, MAX_BET = 10, 3000
WITHDRAW_FEE_PCT = 10
SUSPECT_STREAK = 12
SUSPECT_NET = 30000

POLL_TIMEOUT = 25
HTTP_TIMEOUT = 35
POLL_RETRIES = 4
CONFLICT_EXIT_AFTER = 3
SENDERS = 8

AUTO_PAY_PAID_MESSAGES = os.getenv("AUTO_PAY_PAID_MESSAGES", "0") == "1"

CUSTOM_EMOJI_IDS = {
    "star": "",
    "money": os.getenv("PE_MONEY_ID", "1026032102872848800"),
    "cup": "",
    "fire": "",
    "boom": "",
    "coin": "",
    "game": "",
    "bell": "",
}

GAMES = {
    "hl": "🔺 Больше / Меньше",
    "eo": "🔢 Чёт / Нечёт",
    "guess": "🎯 Угадай число",
    "bowl": "🎳 Боулинг",
    "dart": "🎯 Дартс",
    "bask": "🏀 Баскетбол",
}
NATIVE = {
    "bowl": ("🎳", 6, {6}, (23, 10), 4.0),
    "dart": ("🎯", 6, {6}, (2, 1), 3.5),
    "bask": ("🏀", 5, {4, 5}, (11, 5), 4.5),
}
SIMPLE_MULT = (17, 10)
GUESS_MULT = (2, 1)

GAME_CMDS = {
    "/hl": "hl", "/bm": "hl", "/бм": "hl",
    "/eo": "eo", "/cn": "eo", "/чн": "eo",
    "/guess": "guess", "/uch": "guess", "/уч": "guess",
    "/bowl": "bowl", "/боул": "bowl", "/боулинг": "bowl",
    "/dart": "dart", "/дартс": "dart",
    "/bask": "bask", "/баскет": "bask", "/баскетбол": "bask",
}

LINE = "━━━━━━━━━━━━━━"
FACES = "⚀⚁⚂⚃⚄⚅"
KEYCAPS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
DICE_WAIT = 3.5

_tl = threading.local()


def _conn():
    c = getattr(_tl, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=15000")
        _tl.c = c
    return c


def _upsert_balance(c, u, amount, now):
    c.execute(
        "INSERT INTO balances(username,amount,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET amount=excluded.amount, updated_at=excluded.updated_at",
        (u, int(amount), int(now)),
    )


def init_db():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS balances (username TEXT PRIMARY KEY, amount INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chats (username TEXT PRIMARY KEY, chat_id INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, game TEXT NOT NULL, bet INTEGER NOT NULL, win INTEGER NOT NULL, ts INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_history_user ON history(username, id DESC);
    CREATE TABLE IF NOT EXISTS payments (charge_id TEXT PRIMARY KEY, username TEXT NOT NULL, amount INTEGER NOT NULL, ts INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS withdrawals (id TEXT PRIMARY KEY, username TEXT NOT NULL, chat_id INTEGER, amount INTEGER NOT NULL, fee INTEGER NOT NULL DEFAULT 0, payout INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, result TEXT, created_at INTEGER NOT NULL, closed_at INTEGER, closed_by TEXT, comment TEXT);
    CREATE INDEX IF NOT EXISTS idx_wd_status ON withdrawals(status, created_at DESC);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS moderation (username TEXT PRIMARY KEY, banned INTEGER NOT NULL DEFAULT 0, warns INTEGER NOT NULL DEFAULT 0, mute_until INTEGER NOT NULL DEFAULT 0, last_action TEXT);
    CREATE TABLE IF NOT EXISTS inflight (username TEXT PRIMARY KEY, game TEXT NOT NULL, bet INTEGER NOT NULL, chat_id INTEGER, ts INTEGER NOT NULL);
    """)
    for k in ("games", "bets", "payouts"):
        c.execute("INSERT OR IGNORE INTO stats(key,value) VALUES(?,0)", (k,))


def get_balances():
    rows = _conn().execute("SELECT username, amount FROM balances").fetchall()
    return {r["username"]: int(r["amount"]) for r in rows}


def get_balance(u):
    r = _conn().execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
    return int(r["amount"]) if r else 0


def ensure_user(u, chat_id=None):
    _conn().execute(
        "INSERT OR IGNORE INTO balances(username,amount,updated_at) VALUES(?,0,?)",
        (u, int(time.time())),
    )
    if chat_id is not None:
        set_chat(u, chat_id)
    return get_balance(u)


def add_balance(u, delta):
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
        new = (int(row["amount"]) if row else 0) + int(delta)
        _upsert_balance(c, u, new, time.time())
        c.execute("COMMIT")
        return new
    except Exception:
        c.execute("ROLLBACK")
        raise


def set_balance(u, v):
    _conn().execute(
        "INSERT INTO balances(username,amount,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET amount=excluded.amount, updated_at=excluded.updated_at",
        (u, int(v), int(time.time())),
    )


def get_chats():
    rows = _conn().execute("SELECT username, chat_id FROM chats").fetchall()
    return {r["username"]: int(r["chat_id"]) for r in rows}


def get_chat(u):
    r = _conn().execute("SELECT chat_id FROM chats WHERE username=?", (u,)).fetchone()
    return int(r["chat_id"]) if r else None


def set_chat(u, chat_id):
    _conn().execute(
        "INSERT INTO chats(username,chat_id) VALUES(?,?) "
        "ON CONFLICT(username) DO UPDATE SET chat_id=excluded.chat_id",
        (u, int(chat_id)),
    )


def get_stats():
    rows = _conn().execute("SELECT key,value FROM stats").fetchall()
    d = {r["key"]: int(r["value"]) for r in rows}
    return {"games": d.get("games", 0), "bets": d.get("bets", 0), "payouts": d.get("payouts", 0)}


def inc_stats(games=0, bets=0, payouts=0):
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        for k, v in (("games", games), ("bets", bets), ("payouts", payouts)):
            if v:
                c.execute("UPDATE stats SET value=value+? WHERE key=?", (int(v), k))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def add_history(u, g, bet, win):
    _conn().execute(
        "INSERT INTO history(username,game,bet,win,ts) VALUES(?,?,?,?,?)",
        (u, g, int(bet), int(win), int(time.time())),
    )


def get_history(limit=500):
    rows = _conn().execute(
        "SELECT username,game,bet,win,ts FROM history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"username": r["username"], "game": r["game"], "bet": int(r["bet"]), "win": int(r["win"]), "time": int(r["ts"])}
        for r in rows
    ]


def get_user_history(u, limit=30):
    rows = _conn().execute(
        "SELECT username,game,bet,win,ts FROM history WHERE username=? ORDER BY id DESC LIMIT ?",
        (u, limit),
    ).fetchall()
    return [
        {"username": r["username"], "game": r["game"], "bet": int(r["bet"]), "win": int(r["win"]), "time": int(r["ts"])}
        for r in rows
    ]


def count_games_per_user(limit=10):
    rows = _conn().execute(
        "SELECT username, COUNT(*) c FROM history GROUP BY username ORDER BY c DESC LIMIT ?", (limit,)
    ).fetchall()
    return [(r["username"], int(r["c"])) for r in rows]


def has_payment(cid):
    return _conn().execute("SELECT 1 FROM payments WHERE charge_id=?", (cid,)).fetchone() is not None


def add_payment(cid, u, amount):
    _conn().execute(
        "INSERT OR IGNORE INTO payments(charge_id,username,amount,ts) VALUES(?,?,?,?)",
        (cid, u, int(amount), int(time.time())),
    )


def apply_payment(cid, u, amount):
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT 1 FROM payments WHERE charge_id=?", (cid,)).fetchone():
            c.execute("ROLLBACK")
            return None
        row = c.execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
        new = (int(row["amount"]) if row else 0) + int(amount)
        _upsert_balance(c, u, new, time.time())
        c.execute(
            "INSERT INTO payments(charge_id,username,amount,ts) VALUES(?,?,?,?)",
            (cid, u, int(amount), int(time.time())),
        )
        c.execute("COMMIT")
        return new
    except Exception:
        c.execute("ROLLBACK")
        raise


def get_donors(limit=10):
    rows = _conn().execute(
        "SELECT username, SUM(amount) s FROM payments GROUP BY username ORDER BY s DESC LIMIT ?", (limit,)
    ).fetchall()
    return [(r["username"], int(r["s"])) for r in rows]


def get_payments_log(limit=50):
    rows = _conn().execute(
        "SELECT username, amount, ts, charge_id FROM payments ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"username": r["username"], "amount": int(r["amount"]), "time": int(r["ts"]), "charge_id": r["charge_id"]}
        for r in rows
    ]


def get_payments_total():
    r = _conn().execute("SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM payments").fetchone()
    return int(r["s"]), int(r["c"])


def _wd(r):
    return {
        "id": r["id"], "username": r["username"], "chat_id": r["chat_id"],
        "amount": int(r["amount"]), "fee": int(r["fee"]), "payout": int(r["payout"]),
        "reserved": bool(r["reserved"]), "status": r["status"], "result": r["result"],
        "created_at": int(r["created_at"]), "closed_at": r["closed_at"],
        "closed_by": r["closed_by"], "comment": r["comment"],
    }


def get_withdrawals(limit=1000):
    rows = _conn().execute("SELECT * FROM withdrawals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_wd(r) for r in rows]


def get_withdrawal(rid):
    r = _conn().execute("SELECT * FROM withdrawals WHERE id=?", (rid,)).fetchone()
    return _wd(r) if r else None


def find_open_withdrawal(u):
    r = _conn().execute(
        "SELECT * FROM withdrawals WHERE username=? AND status='open' ORDER BY created_at DESC LIMIT 1", (u,)
    ).fetchone()
    return _wd(r) if r else None


def add_withdrawal(d):
    _conn().execute(
        "INSERT INTO withdrawals(id,username,chat_id,amount,fee,payout,reserved,status,result,created_at,closed_at,closed_by,comment) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            d["id"], d["username"], d.get("chat_id"), int(d["amount"]), int(d.get("fee", 0)),
            int(d.get("payout", d["amount"])), 1 if d.get("reserved") else 0,
            d.get("status", "open"), d.get("result"), int(d.get("created_at", time.time())),
            d.get("closed_at"), d.get("closed_by"), d.get("comment"),
        ),
    )


def close_withdrawal_db(rid, result, closed_by, comment):
    _conn().execute(
        "UPDATE withdrawals SET status='closed', result=?, closed_at=?, closed_by=?, comment=? WHERE id=? AND status='open'",
        (result, int(time.time()), closed_by, (comment or "")[:1000], rid),
    )


def delete_closed_withdrawals_db():
    cur = _conn().execute("DELETE FROM withdrawals WHERE status='closed'")
    return cur.rowcount


def open_withdrawal_tx(u, chat_id, amount):
    amount = int(amount)
    fee = wd_fee(amount)
    payout_amt = amount - fee
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute(
            "SELECT 1 FROM withdrawals WHERE username=? AND status='open' LIMIT 1", (u,)
        ).fetchone():
            c.execute("ROLLBACK")
            return None, "open"
        row = c.execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
        bal = int(row["amount"]) if row else 0
        if amount <= 0 or amount > bal:
            c.execute("ROLLBACK")
            return None, "balance"
        now = int(time.time())
        _upsert_balance(c, u, bal - amount, now)
        rid = f"WD-{int(time.time() * 1000)}-{secrets.randbelow(900) + 100}"
        c.execute(
            "INSERT INTO withdrawals(id,username,chat_id,amount,fee,payout,reserved,status,result,created_at,closed_at,closed_by,comment) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, u, chat_id, amount, fee, payout_amt, 1, "open", None, now, None, None, None),
        )
        c.execute("COMMIT")
        return {"id": rid, "amount": amount, "fee": fee, "payout": payout_amt, "left": bal - amount}, None
    except Exception:
        c.execute("ROLLBACK")
        raise


def undo_open_withdrawal(rid):
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        r = c.execute("SELECT * FROM withdrawals WHERE id=? AND status='open'", (rid,)).fetchone()
        if not r:
            c.execute("ROLLBACK")
            return False
        if r["reserved"]:
            row = c.execute("SELECT amount FROM balances WHERE username=?", (r["username"],)).fetchone()
            bal = int(row["amount"]) if row else 0
            _upsert_balance(c, r["username"], bal + int(r["amount"]), time.time())
        c.execute("DELETE FROM withdrawals WHERE id=? AND status='open'", (rid,))
        c.execute("COMMIT")
        return True
    except Exception:
        c.execute("ROLLBACK")
        raise


def finalize_withdrawal_tx(rid, result, closed_by, comment, refund=False, deduct=False):
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        r = c.execute("SELECT * FROM withdrawals WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "open":
            c.execute("ROLLBACK")
            return None, "closed"
        u = r["username"]
        amount = int(r["amount"])
        row = c.execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
        bal = int(row["amount"]) if row else 0
        now = int(time.time())
        note = (comment or "")[:1000]
        if deduct:
            if bal < amount:
                c.execute("ROLLBACK")
                return None, "balance"
            _upsert_balance(c, u, bal - amount, now)
        elif refund:
            _upsert_balance(c, u, bal + amount, now)
        c.execute(
            "UPDATE withdrawals SET status='closed', result=?, closed_at=?, closed_by=?, comment=? WHERE id=? AND status='open'",
            (result, now, closed_by, note, rid),
        )
        closed = _wd(r)
        closed["status"] = "closed"
        closed["result"] = result
        closed["closed_at"] = now
        closed["closed_by"] = closed_by
        closed["comment"] = note
        c.execute("COMMIT")
        return closed, None
    except Exception:
        c.execute("ROLLBACK")
        raise


def get_setting(k, default=None):
    r = _conn().execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]


def set_setting(k, v):
    _conn().execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (k, json.dumps(v, ensure_ascii=False)),
    )


def get_moderation(u):
    r = _conn().execute("SELECT * FROM moderation WHERE username=?", (u,)).fetchone()
    if not r:
        return {"banned": False, "warns": 0, "mute_until": 0, "last_action": None}
    last = None
    if r["last_action"]:
        try:
            last = json.loads(r["last_action"])
        except Exception:
            last = None
    return {
        "banned": bool(r["banned"]), "warns": int(r["warns"]),
        "mute_until": int(r["mute_until"]), "last_action": last,
    }


def set_moderation(u, d):
    _conn().execute(
        "INSERT INTO moderation(username,banned,warns,mute_until,last_action) VALUES(?,?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET banned=excluded.banned, warns=excluded.warns, "
        "mute_until=excluded.mute_until, last_action=excluded.last_action",
        (
            u, 1 if d.get("banned") else 0, int(d.get("warns", 0)),
            int(d.get("mute_until", 0)),
            json.dumps(d.get("last_action"), ensure_ascii=False) if d.get("last_action") else None,
        ),
    )


def set_inflight(u, g, bet, chat_id):
    _conn().execute(
        "INSERT INTO inflight(username,game,bet,chat_id,ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET game=excluded.game, bet=excluded.bet, "
        "chat_id=excluded.chat_id, ts=excluded.ts",
        (u, g, int(bet), chat_id, int(time.time())),
    )


def clear_inflight(u):
    _conn().execute("DELETE FROM inflight WHERE username=?", (u,))


def get_inflight():
    rows = _conn().execute("SELECT * FROM inflight").fetchall()
    return {
        r["username"]: {"game": r["game"], "bet": int(r["bet"]), "chat_id": r["chat_id"], "time": int(r["ts"])}
        for r in rows
    }


def reserve_bet(u, g, bet, chat_id):
    bet = int(bet)
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT 1 FROM inflight WHERE username=?", (u,)).fetchone():
            c.execute("ROLLBACK")
            return False
        row = c.execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
        cur = int(row["amount"]) if row else 0
        if bet < MIN_BET or bet > MAX_BET or bet > cur:
            c.execute("ROLLBACK")
            return False
        now = int(time.time())
        _upsert_balance(c, u, cur - bet, now)
        c.execute(
            "INSERT INTO inflight(username,game,bet,chat_id,ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET game=excluded.game, bet=excluded.bet, "
            "chat_id=excluded.chat_id, ts=excluded.ts",
            (u, g, bet, chat_id, now),
        )
        c.execute("COMMIT")
        return True
    except Exception:
        c.execute("ROLLBACK")
        raise


def credit_win(u, win):
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        inflight = c.execute("SELECT bet FROM inflight WHERE username=?", (u,)).fetchone()
        if not inflight:
            c.execute("ROLLBACK")
            return None
        row = c.execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
        new = (int(row["amount"]) if row else 0) + int(win)
        _upsert_balance(c, u, new, time.time())
        c.execute("DELETE FROM inflight WHERE username=?", (u,))
        c.execute("COMMIT")
        return new
    except Exception:
        c.execute("ROLLBACK")
        raise


def release_bet(u):
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        inflight = c.execute("SELECT bet FROM inflight WHERE username=?", (u,)).fetchone()
        if not inflight:
            c.execute("ROLLBACK")
            return False
        row = c.execute("SELECT amount FROM balances WHERE username=?", (u,)).fetchone()
        new = (int(row["amount"]) if row else 0) + int(inflight["bet"])
        _upsert_balance(c, u, new, time.time())
        c.execute("DELETE FROM inflight WHERE username=?", (u,))
        c.execute("COMMIT")
        return True
    except Exception:
        c.execute("ROLLBACK")
        raise


def get_log_chat():
    return get_setting("log_chat", None)


def set_log_chat(cid, title, by):
    set_setting("log_chat", {"chat_id": cid, "title": title, "by": by, "time": int(time.time())})


def get_test_mode():
    return bool(get_setting("test_mode", False))


def set_test_mode(v):
    set_setting("test_mode", bool(v))


LOCK = threading.RLock()
game_pool = ThreadPoolExecutor(max_workers=32)
admins = set()
pending = {}
used_callbacks = {}
playing = set()
streaks = {}
session = {}
pw_fail = {}
suspect_seen = {}


def norm(x):
    return (x or "").lstrip("@").lower()


def pe(name, fallback="⭐"):
    eid = str(CUSTOM_EMOJI_IDS.get(name, "") or "").strip()
    if eid.isdigit():
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback


def render_text(text):
    return text, ("HTML" if "<tg-emoji " in text else None)


def is_admin(uid, u):
    return norm(u) in AUTHORIZED_ADMIN_USERNAMES or uid in admins


_u = urlparse(API_URL)
_tl_api = threading.local()
_HDR = {"Content-Type": "application/json"}
_paid_warned = {}


def _http(method, data, timeout):
    for fresh in (False, True):
        c = getattr(_tl_api, "c", None)
        reused = c is not None
        try:
            if c is None:
                cls = http.client.HTTPSConnection if _u.scheme == "https" else http.client.HTTPConnection
                c = cls(_u.netloc, timeout=timeout)
                _tl_api.c = c
            else:
                c.timeout = timeout
                if c.sock is not None:
                    c.sock.settimeout(timeout)
            c.request("POST", f"{_u.path.rstrip('/')}/bot{BOT_TOKEN}/{method}", body=data, headers=_HDR)
            r = c.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        except Exception as e:
            try:
                c.close()
            except Exception:
                pass
            _tl_api.c = None
            if reused and not fresh and isinstance(e, (OSError, http.client.HTTPException)):
                continue
            raise


def api(method, retries=2, timeout=10, poll=None, **params):
    if poll is not None:
        params["timeout"] = poll
    data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    for attempt in range(retries + 1):
        try:
            code, body = _http(method, data, timeout)
        except (OSError, http.client.HTTPException) as e:
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
                continue
            print(f"⚠️ Временная ошибка ({method}): {e}")
            return {"ok": False, "temporary": True}
        except Exception as e:
            print(f"⚠️ Ошибка API ({method}): {e}")
            return {"ok": False}
        if code < 400:
            try:
                return json.loads(body)
            except Exception:
                print(f"⚠️ Плохой ответ ({method}): {body[:200]}")
                return {"ok": False}
        if code == 409:
            return {"ok": False, "error_code": 409, "conflict": True, "description": body}
        if "ALLOW_PAYMENT_REQUIRED" in body:
            k = params.get("chat_id")
            now = time.time()
            m = re.search(r"ALLOW_PAYMENT_REQUIRED_(\d+)", body)
            required = int(m.group(1)) if m else 1
            if AUTO_PAY_PAID_MESSAGES and method == "sendMessage":
                rp = dict(params)
                rp["allow_paid_stars"] = required
                rd = json.dumps({kk: v for kk, v in rp.items() if v is not None}).encode()
                try:
                    pc, pb = _http(method, rd, timeout)
                    if pc < 400:
                        try:
                            return json.loads(pb)
                        except Exception:
                            pass
                except Exception:
                    pass
            if now - _paid_warned.get(k, 0) > 600:
                _paid_warned[k] = now
                print(f"⚠️ Чат {k}: платные сообщения ({required} ⭐)")
            return {"ok": False, "error_code": 403, "description": body, "paid_required": required}
        if code == 429 and attempt < retries:
            try:
                wait = float(json.loads(body).get("parameters", {}).get("retry_after", 1))
            except Exception:
                wait = 1
            time.sleep(min(wait, 10))
            continue
        if code in (502, 503, 504) and attempt < retries:
            time.sleep(min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.25))
            continue
        if not (code == 400 and ("query is too old" in body or "query ID is invalid" in body)):
            print(f"⚠️ HTTP {method}: {code} — {body[:300]}")
        return {"ok": False, "error_code": code, "description": body}
    return {"ok": False}


_sq = [queue.Queue() for _ in range(SENDERS)]
_sq_lock = threading.Lock()
_sq_started = [False]


def _sender(q):
    while True:
        fut, method, kw = q.get()
        try:
            fut.set_result(api(method, **kw))
        except Exception as e:
            fut.set_result({"ok": False, "error": str(e)})


def api_async(method, key, **kw):
    with _sq_lock:
        if not _sq_started[0]:
            _sq_started[0] = True
            for q in _sq:
                threading.Thread(target=_sender, args=(q,), daemon=True).start()
    fut = Future()
    _sq[hash(key) % SENDERS].put((fut, method, kw))
    return fut


def send(chat_id, text, markup=None, wait=False):
    text, parse_mode = render_text(text)
    fut = api_async(
        "sendMessage", chat_id, chat_id=chat_id, text=text,
        reply_markup=markup, parse_mode=parse_mode,
        retries=3, timeout=HTTP_TIMEOUT,
    )
    if not wait:
        return {"ok": True, "queued": True}
    try:
        return fut.result(timeout=max(40, HTTP_TIMEOUT + 5))
    except Exception:
        return {"ok": False}


def dice(chat_id, emoji="🎲"):
    return api("sendDice", chat_id=chat_id, emoji=emoji, retries=1, timeout=10)


def dice_value(chat_id, emoji="🎲", maxv=6):
    a = dice(chat_id, emoji)
    v = (a.get("result") or {}).get("dice", {}).get("value") if a.get("ok") else None
    return v if isinstance(v, int) and 1 <= v <= maxv else None


def kb(rows):
    def _clean(btn):
        if isinstance(btn, dict) and "text" in btn:
            btn = dict(btn)
            btn["text"] = re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", btn["text"])
        return btn
    return {"inline_keyboard": [[_clean(x) for x in row] for row in rows]}


def b(text, data):
    return {"text": text, "callback_data": data}


_log_q = queue.Queue()
_log_lock = threading.Lock()
_log_started = [False]


def _log_worker():
    while True:
        cid, text = _log_q.get()
        parts = [text]
        while len(parts) < 10:
            try:
                c2, t2 = _log_q.get_nowait()
            except queue.Empty:
                break
            if c2 != cid:
                _log_q.put((c2, t2))
                break
            parts.append(t2)
        try:
            send(cid, ("\n\n" + "─" * 10 + "\n\n").join(parts))
        except Exception:
            traceback.print_exc()
        time.sleep(1.5)


def log_group(text):
    info = get_log_chat()
    if not info or not info.get("chat_id"):
        return
    cid = int(info["chat_id"])
    with _log_lock:
        if not _log_started[0]:
            _log_started[0] = True
            threading.Thread(target=_log_worker, daemon=True).start()
    _log_q.put((cid, text))


def suspect(u, reason):
    u = norm(u) or "неизвестный"
    key = (u, reason)
    now = time.time()
    if now - suspect_seen.get(key, 0) < 600:
        return
    suspect_seen[key] = now
    msg = f"Подозрение на @{u}, причина: {reason}"
    print("🚨", msg)
    log_group(msg)


def audit_balances():
    try:
        cur = get_balances()
    except Exception:
        return
    for u, v in cur.items():
        if not isinstance(v, int) or v < 0:
            suspect(u, f"некорректный баланс: {v}")


def menu():
    return kb([
        [b("🔺 Больше / Меньше", "game:hl"), b("🔢 Чёт / Нечёт", "game:eo")],
        [b("🎯 Угадай число", "game:guess"), b("🎳 Боулинг", "game:bowl")],
        [b("🎯 Дартс", "game:dart"), b("🏀 Баскетбол", "game:bask")],
        [b(f"{pe('money', '💰')} Баланс", "bal"), b(f"{pe('star', '⭐')} Пополнить", "topup")],
        [b("📜 Мои игры", "my"), b("📤 Вывод", "withdraw")],
    ])


def bets(game):
    return kb([
        [b("10 ⭐", f"bet:{game}:10"), b("50 ⭐", f"bet:{game}:50"), b("100 ⭐", f"bet:{game}:100")],
        [b("500 ⭐", f"bet:{game}:500"), b("1000 ⭐", f"bet:{game}:1000")],
        [b("💯 Ва-банк", f"bet:{game}:all")],
        [b("✍️ Своя ставка", f"betcustom:{game}")],
        [b("⬅️ Меню", "menu")],
    ])


def admins_kb():
    tm = "🟢 ВКЛ" if get_test_mode() else "⚫ ВЫКЛ"
    return kb([
        [b("👥 Игроки", "adm:list"), b("📊 Статистика", "adm:stats")],
        [b("🕘 История", "adm:history"), b("🎮 История игрока", "adm:playerhistory")],
        [b("🏆 Донатеры", "adm:donors"), b("🔥 Активные", "adm:active")],
        [b("💳 Лог донатов", "adm:donatelog")],
        [b("🛡 Модерация", "adm:moderation")],
        [b(f"🧪 Тестеровка: {tm}", "adm:testing")],
        [b("📤 Заявки на вывод", "adm:withdrawals")],
        [b("📢 Рассылка всем", "adm:broadcast")],
        [b("➕ Начислить", "adm:add"), b("➖ Списать", "adm:take")],
        [b("⭐ Stars бота", "sadm:stars")],
        [b("💸 Вывести Stars владельцу", "sadm:withdraw")],
        [b("🚪 Выйти", "adm:logout")],
    ])


def topup_menu_kb():
    return kb([
        [b("⭐ 100", "topup_amount:100"), b("⭐ 500", "topup_amount:500")],
        [b("⭐ 1000", "topup_amount:1000"), b("⭐ 5000", "topup_amount:5000")],
        [b("✍️ Своя сумма", "topup_custom")],
        [b("⬅️ Меню", "menu")],
    ])


def wd_fee(a):
    return (int(a) * WITHDRAW_FEE_PCT + 50) // 100


def wd_payout(a):
    return int(a) - wd_fee(a)


def withdrawal_amount_kb():
    return kb([
        [b(f"50 ⭐ → {wd_payout(50)}", "wd_amount:50"), b(f"100 ⭐ → {wd_payout(100)}", "wd_amount:100")],
        [b(f"500 ⭐ → {wd_payout(500)}", "wd_amount:500"), b(f"1000 ⭐ → {wd_payout(1000)}", "wd_amount:1000")],
        [b("✍️ Своя сумма", "wd_custom")],
        [b("⬅️ Меню", "menu")],
    ])


def _mult(g, choice=None):
    if g in ("hl", "eo"):
        return SIMPLE_MULT
    if g == "guess":
        return GUESS_MULT
    return NATIVE[g][3]


def payout(bet, g, choice=None):
    n, d = _mult(g, choice)
    return (bet * n * 2 + d) // (2 * d)


def mult_text(g, choice=None):
    n, d = _mult(g, choice)
    return "x" + str(n / d).rstrip("0").rstrip(".")


def face(v):
    return FACES[v - 1]


def sign(n):
    return f"+{n}" if n > 0 else str(n)


def strip(v, win_set, maxv=6):
    nums = "".join(KEYCAPS[:maxv])
    zone = "".join("🟩" if i in win_set else "🟥" for i in range(1, maxv + 1))
    ptr = "".join("🔺" if i == v else "⬜" for i in range(1, maxv + 1))
    return f"{nums}\n{zone}\n{ptr}\n🟩 — твоя зона   🟥 — не твоя"


def bet_error(u, bet):
    bal = get_balance(u)
    if bet < MIN_BET:
        return f"⚠️ Минимальная ставка: {MIN_BET} ⭐"
    if bet > MAX_BET:
        return f"⚠️ Максимальная ставка: {MAX_BET} ⭐"
    if bet > bal:
        return f"⚠️ Недостаточно ⭐. Баланс: {bal}"
    return None


def resolve_bet(u, bet):
    if isinstance(bet, str) and bet.lower() == "all":
        return min(get_balance(u), MAX_BET)
    return int(bet)


def show_menu(chat_id, u):
    bal = ensure_user(u, chat_id)
    send(
        chat_id,
        f"🎰 ИГРОВОЙ БОТ\n{LINE}\n👤 @{u}\n"
        f"{pe('money', '💰')} Баланс: {bal} {pe('star', '⭐')}\n{LINE}\n"
        f"🔺 Больше / Меньше — x1.7\n🔢 Чёт / Нечёт — x1.7\n"
        f"🎯 Угадай число — x2\n🎳 Боулинг — x2.3\n🎯 Дартс (яблочко) — x2\n"
        f"🏀 Баскетбол — x2.2\n{LINE}\nВыбери игру 👇",
        menu(),
    )


def choose_game(chat_id, u, g):
    if g not in GAMES:
        return send(chat_id, "⚠️ Эта игра больше недоступна. Выбери из меню 👇", menu())
    rules = {
        "hl": f"🔻 Меньше = 1–3\n🔺 Больше = 4–6\n🎯 Шанс: 50%\n💎 Победа: {mult_text('hl')}",
        "eo": f"2️⃣ Чёт = 2, 4, 6\n1️⃣ Нечёт = 1, 3, 5\n🎯 Шанс: 50%\n💎 Победа: {mult_text('eo')}",
        "guess": "Выбери число от 1 до 6.\n🎯 Шанс: 1 из 6\n💎 Победа: x2",
        "bowl": f"🎳 Бросок кеглем Telegram.\n🏆 Выигрыш при СТРАЙКЕ (6)\n🎯 Шанс: 1 из 6\n💎 Победа: {mult_text('bowl')}",
        "dart": f"🎯 Бросок дротика Telegram.\n🏆 Выигрыш при ЯБЛОЧКЕ (6)\n🎯 Шанс: 1 из 6\n💎 Победа: {mult_text('dart')}",
        "bask": f"🏀 Бросок мяча Telegram.\n🏆 Попадание в кольцо (4–5)\n🎯 Шанс: 40%\n💎 Победа: {mult_text('bask')}",
    }
    send(
        chat_id,
        f"{GAMES[g]}\n{LINE}\n{rules[g]}\n{LINE}\n"
        f"{pe('money', '💰')} Баланс: {get_balance(u)} {pe('star', '⭐')}\nВыбери ставку 👇",
        bets(g),
    )


def choice_kb(g, bet):
    if g == "hl":
        rows = [[
            b(f"⬆️ Больше 4–6 · {mult_text('hl')}", f"play:hl:{bet}:high"),
            b(f"⬇️ Меньше 1–3 · {mult_text('hl')}", f"play:hl:{bet}:low"),
        ]]
    elif g == "eo":
        rows = [[
            b(f"2️⃣ Чёт · {mult_text('eo')}", f"play:eo:{bet}:even"),
            b(f"1️⃣ Нечёт · {mult_text('eo')}", f"play:eo:{bet}:odd"),
        ]]
    elif g == "guess":
        n = [b(str(i), f"play:guess:{bet}:{i}") for i in range(1, 7)]
        rows = [n[:3], n[3:]]
    else:
        rows = [[b("🎲 Бросить", f"play:{g}:{bet}:roll")]]
    rows.append([b("⬅️ Меню", "menu")])
    return kb(rows)


def testing_blocked(chat_id, u, uid=None):
    if not get_test_mode():
        return False
    if norm(u) in AUTHORIZED_ADMIN_USERNAMES or (uid is not None and uid in admins):
        return False
    send(chat_id, "🧪 Бот в режиме тестеровки. Игры временно недоступны.")
    return True


def start_game(chat_id, u, g, bet, uid=None):
    if testing_blocked(chat_id, u, uid) or blocked(chat_id, u, for_game=True):
        return
    try:
        bet = resolve_bet(u, bet)
    except (TypeError, ValueError):
        return send(chat_id, "⚠️ Неверная ставка.")
    e = bet_error(u, bet)
    if e:
        return send(chat_id, e)
    if g in ("hl", "eo", "guess"):
        send(chat_id, f"{GAMES[g]}\nСтавка: {bet} ⭐\n\nВыбери:", choice_kb(g, bet))
    else:
        play(chat_id, u, g, bet, None, uid=uid)


def play(chat_id, u, g, bet, choice, uid=None):
    if g not in GAMES:
        return send(chat_id, "⚠️ Эта игра отключена.")
    if testing_blocked(chat_id, u, uid):
        return
    if g == "hl" and choice not in ("high", "low"):
        return send(chat_id, "⚠️ Выбери: б (больше) или м (меньше).")
    if g == "eo" and choice not in ("even", "odd"):
        return send(chat_id, "⚠️ Выбери: ч (чёт) или н (нечёт).")
    if g == "guess":
        try:
            choice = int(choice)
        except (TypeError, ValueError):
            choice = 0
        if not 1 <= choice <= 6:
            return send(chat_id, "⚠️ Число должно быть от 1 до 6.")
    if blocked(chat_id, u, for_game=True):
        return
    try:
        bet = resolve_bet(u, bet)
    except (TypeError, ValueError):
        return send(chat_id, "⚠️ Неверная ставка.")
    e = bet_error(u, bet)
    if e:
        return send(chat_id, e)
    with LOCK:
        if u in playing:
            return send(chat_id, "⏳ Подожди — твой кубик ещё в полёте!")
        playing.add(u)
    try:
        game_pool.submit(run_game, chat_id, u, g, bet, choice)
    except Exception:
        playing.discard(u)
        raise


def pick_text(g, choice):
    if g == "hl":
        return "🔺 Больше (4–6)" if choice == "high" else "🔻 Меньше (1–3)"
    if g == "eo":
        return "2️⃣ Чёт" if choice == "even" else "1️⃣ Нечёт"
    if g == "guess":
        return f"🎯 Число {choice}"
    return ""


def evaluate(g, bet, choice, vals):
    v = vals[0]
    if g in NATIVE:
        em, mx, wins, _, _ = NATIVE[g]
        hit = v in wins
        pic = f"{em} Выпало: {v} из {mx}\n{strip(v, wins, mx)}"
        if g == "bowl":
            why = "🎳 СТРАЙК!" if hit else f"Не страйк — выпало {v} из 6"
        elif g == "dart":
            why = "🎯 В ЯБЛОЧКО!" if hit else f"Не яблочко — выпало {v} из 6"
        else:
            why = "🏀 Мяч в кольце!" if hit else "Мимо кольца"
        return (payout(bet, g) if hit else 0), pic, why, mult_text(g)
    if g == "hl":
        zone = {4, 5, 6} if choice == "high" else {1, 2, 3}
        why = f"Выпало {v} — это {'больше' if v >= 4 else 'меньше'}"
        return (
            payout(bet, "hl") if v in zone else 0,
            f"🎲 Выпало: {face(v)} {v}\n{strip(v, zone)}",
            why,
            mult_text("hl"),
        )
    if g == "eo":
        zone = {2, 4, 6} if choice == "even" else {1, 3, 5}
        why = f"Выпало {v} — это {'чёт' if v % 2 == 0 else 'нечёт'}"
        return (
            payout(bet, "eo") if v in zone else 0,
            f"🎲 Выпало: {face(v)} {v}\n{strip(v, zone)}",
            why,
            mult_text("eo"),
        )
    why = f"Ты загадал {choice}, выпало {v}"
    return (
        payout(bet, "guess") if v == choice else 0,
        f"🎲 Выпало: {face(v)} {v}\n{strip(v, {choice})}",
        why,
        "x2",
    )


def run_game(chat_id, u, g, bet, choice):
    deducted = False
    settled = False
    refund_note = None
    try:
        head = f"{GAMES[g]}\n{LINE}\n💵 Ставка: {bet} ⭐"
        if g == "bowl":
            intro = f"{head}\n🏆 Нужен страйк (6) · {mult_text(g)}\n\n🎳 Бросок! Смотри анимацию 👇"
        elif g == "dart":
            intro = f"{head}\n🏆 Нужно яблочко (6) · {mult_text(g)}\n\n🎯 Бросок! Смотри анимацию 👇"
        elif g == "bask":
            intro = f"{head}\n🏆 Нужно попасть в кольцо · {mult_text(g)}\n\n🏀 Бросок! Смотри анимацию 👇"
        else:
            intro = f"{head}\n🎯 Твой выбор: {pick_text(g, choice)}\n\n🎲 Кубик летит… 👇"
        if not send(chat_id, intro, wait=True).get("ok"):
            return
        with LOCK:
            e = bet_error(u, bet)
            if e:
                send(chat_id, e)
                return
            if not reserve_bet(u, g, bet, chat_id):
                send(chat_id, bet_error(u, bet) or "⚠️ Недостаточно ⭐.")
                return
            deducted = True
        em, mx = (NATIVE[g][0], NATIVE[g][1]) if g in NATIVE else ("🎲", 6)
        vals = [dice_value(chat_id, em, mx)]
        if None in vals:
            refund_note = "⚠️ Кубик не ответил. Ставка возвращена 💸"
            return
        time.sleep(NATIVE[g][4] if g in NATIVE else DICE_WAIT)
        win, pic, why, mult = evaluate(g, bet, choice, vals)
        net = win - bet
        with LOCK:
            after = credit_win(u, win)
            if after is None:
                return
            settled = True
            before = after - win + bet
            add_history(u, g, bet, win)
            inc_stats(games=1, bets=bet, payouts=win)
            ss = session.setdefault(u, {"games": 0, "wins": 0, "net": 0})
            ss["games"] += 1
            ss["net"] += net
            if net > 0:
                ss["wins"] += 1
                streaks[u] = streaks.get(u, 0) + 1
            elif net < 0:
                streaks[u] = 0
            st = streaks.get(u, 0)
        cup = pe("cup", "🏆")
        fire = pe("fire", "🔥")
        boom = pe("boom", "💥")
        if net > 0 and g == "guess":
            banner = f"🎯 {boom} ТОЧНОЕ ПОПАДАНИЕ! {boom} 🎯\n{cup} ПОБЕДА " + mult
        elif net > 0 and g in ("bowl", "dart") and vals[0] == 6:
            gi = "🎳" if g == "bowl" else "🎯"
            banner = f"{gi} {boom} ТОЧНО! {boom} {gi}\n{cup} ПОБЕДА " + mult
        elif net > 0:
            banner = f"{cup}{cup} ПОБЕДА! {cup}{cup}"
        elif net == 0:
            banner = "🤝 НИЧЬЯ — ставка возвращена"
        else:
            banner = f"{boom} Не повезло…"
        money = (
            f"💵 Ставка: {bet} ⭐\n🏦 Выплата: {win} ⭐"
            + (f" ({mult})" if win else "")
            + f"\n{'📈' if net > 0 else '📉' if net < 0 else '➖'} Итог: {sign(net)} ⭐"
        )
        extra = []
        if st >= 2:
            extra.append(f"{fire} Серия побед: {st}")
        extra.append(f"📊 Сессия: {ss['games']} игр · {ss['wins']} побед · {sign(ss['net'])} ⭐")
        text = (
            f"{GAMES[g]} — РЕЗУЛЬТАТ\n{LINE}\n{pic}\n{LINE}\n{why}\n\n{banner}\n\n"
            f"{money}\n{LINE}\n{pe('money', '💰')} Баланс: {before} → {after} {pe('star', '⭐')}\n"
            + "\n".join(extra)
        )
        rows = [[b(f"🔁 Ещё раз ({bet})", f"bet:{g}:{bet}")]]
        if bet * 2 <= MAX_BET and after >= bet * 2:
            rows[0].append(b(f"⏫ x2 ({bet * 2})", f"bet:{g}:{bet * 2}"))
        rows.append([b("🎮 Меню", "menu"), b("⭐ Пополнить", "topup")])
        send(chat_id, text, kb(rows))
        if net > 0:
            res = "✅ Выиграл: +" + str(net) + " ⭐"
        elif net == 0:
            res = "🤝 Ничья (ставка возвращена)"
        else:
            res = f"❌ Проиграл: {bet} ⭐"
        log_group(f"🎮 Игра\n👤 @{u}\n🕹 {GAMES[g]}\n💵 Ставка: {bet} ⭐\n{res}")
        if after < 0:
            suspect(u, f"отрицательный баланс ({after} ⭐) после игры")
        if net > 0 and st >= SUSPECT_STREAK and (st - SUSPECT_STREAK) % 5 == 0:
            suspect(u, f"{st} побед подряд")
        if ss["net"] >= SUSPECT_NET:
            suspect(u, f"выигрыш за сессию {ss['net']} ⭐")
    except Exception as e:
        traceback.print_exc()
        suspect(u, f"ошибка бота при игре ({type(e).__name__}: {e})")
    finally:
        if deducted and not settled:
            try:
                if release_bet(u):
                    send(chat_id, refund_note or "↩️ Ставка возвращена.")
            except Exception:
                traceback.print_exc()
        playing.discard(u)


def recover_inflight():
    d = get_inflight()
    if not d:
        return
    for u, i in d.items():
        bet = int(i.get("bet", 0))
        if not release_bet(u):
            continue
        if i.get("chat_id"):
            send(i["chat_id"], f"⚠️ Бот перезапускался во время игры. Ставка {bet} ⭐ возвращена 💸")
        print(f"♻️ Возвращена ставка {bet} ⭐ игроку @{u}")


def my_games(chat_id, u):
    h = get_user_history(u, 10)
    if not h:
        return send(
            chat_id,
            f"📜 МОИ ИГРЫ\n{LINE}\nПока пусто — самое время сыграть! 🎲",
            kb([[b("🎮 Играть", "menu")]]),
        )
    lines, total = [], 0
    for i in h:
        net = i["win"] - i["bet"]
        total += net
        icon = "✅" if net > 0 else "🤝" if net == 0 else "❌"
        lines.append(f"{icon} {GAMES.get(i['game'], '?')} · {i['bet']} ⭐ → {sign(net)}")
    send(
        chat_id,
        f"📜 МОИ ПОСЛЕДНИЕ ИГРЫ\n{LINE}\n" + "\n".join(lines)
        + f"\n{LINE}\n{'📈' if total > 0 else '📉' if total < 0 else '➖'} Итог за {len(h)} игр: {sign(total)} ⭐",
        kb([[b("🎮 Играть", "menu"), b("💰 Баланс", "bal")]]),
    )


def topup_menu(chat_id):
    send(chat_id, "💳 Выбери сумму:", topup_menu_kb())


def invoice(chat_id, u, amount):
    if blocked(chat_id, u):
        return
    try:
        amount = int(amount)
    except Exception:
        return send(chat_id, "⚠️ Неверная сумма.")
    if amount <= 0:
        return send(chat_id, "⚠️ Сумма должна быть больше 0.")
    payload = f"topup:{u}:{amount}:{int(time.time() * 1000)}"
    r = api(
        "sendInvoice", chat_id=chat_id, title=f"Пополнение {amount} ⭐",
        description=f"Игровой баланс: {amount} Telegram Stars",
        payload=payload, provider_token="", currency="XTR",
        prices=[{"label": "Пополнение", "amount": amount}], timeout=10,
    )
    if not r.get("ok"):
        send(chat_id, "⚠️ Не удалось создать счёт. Попробуй ещё раз.")
    return r


def precheckout(q):
    p = str(q.get("invoice_payload", ""))
    try:
        amount_ok = int(q.get("total_amount", 0) or 0) > 0
    except (TypeError, ValueError):
        amount_ok = False
    ok = p.startswith("topup:") and q.get("currency") == "XTR" and amount_ok
    return api(
        "answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=ok,
        error_message=None if ok else "Некорректный платёж.", retries=0, timeout=3,
    )


def successful(m):
    p = m.get("successful_payment") or {}
    payload = str(p.get("invoice_payload", ""))
    parts = payload.split(":")
    if len(parts) != 4 or parts[0] != "topup" or p.get("currency") != "XTR":
        return
    try:
        u = norm(parts[1])
        expected = int(parts[2])
        amount = int(p.get("total_amount", 0))
    except Exception:
        return
    if not u or amount != expected:
        suspect(u, f"сумма оплаты не совпала ({expected} vs {amount})")
        return
    cid = p.get("telegram_payment_charge_id") or payload
    new_balance = apply_payment(cid, u, amount)
    if new_balance is None:
        suspect(u, f"повторная оплата {cid}")
        return
    send(m["chat"]["id"], f"✅ Оплата получена!\n⭐ Начислено: {amount}\n💰 Баланс: {new_balance}")


def withdrawal_menu(chat_id, u):
    bal = get_balance(u)
    cur = find_open_withdrawal(u)
    if cur:
        return send(
            chat_id,
            f"📤 ВЫВОД STARS\n{LINE}\n🟢 У тебя уже есть открытая заявка\n"
            f"🧾 #{cur['id']}\n⭐ Списано: {cur['amount']}\n"
            f"💸 К выплате: {cur['payout']} ⭐ (комиссия {WITHDRAW_FEE_PCT}%)\n{LINE}\n"
            f"⏳ Ожидай обработки — до 24 часов.\n📩 Вопросы: @jumka",
            kb([[b("⬅️ Меню", "menu")]]),
        )
    send(
        chat_id,
        f"📤 ВЫВОД STARS\n{LINE}\n💰 Доступно: {bal} ⭐\n"
        f"💸 Комиссия бота: {WITHDRAW_FEE_PCT}%\n⏳ Обработка: до 24 часов\n"
        f"👤 Выплачивает: @jumka\n{LINE}\nВыбери сумму 👇",
        withdrawal_amount_kb(),
    )


def create_withdrawal(chat_id, u, amount):
    if blocked(chat_id, u):
        return
    try:
        amount = int(amount)
    except Exception:
        return send(chat_id, "⚠️ Сумма должна быть числом.")
    if amount <= 0:
        return send(chat_id, "⚠️ Сумма должна быть больше 0.")
    bal = get_balance(u)
    if amount > bal:
        return send(chat_id, f"⚠️ Недостаточно ⭐. Баланс: {bal}")
    if find_open_withdrawal(u):
        return send(chat_id, "⚠️ У тебя уже есть открытая заявка. Дождись обработки.")
    try:
        info, err = open_withdrawal_tx(u, chat_id, amount)
    except Exception:
        traceback.print_exc()
        return send(chat_id, "⚠️ Не удалось создать заявку. Баланс не изменён.")
    if err == "open":
        return send(chat_id, "⚠️ У тебя уже есть открытая заявка. Дождись обработки.")
    if err or not info:
        return send(chat_id, f"⚠️ Недостаточно ⭐. Баланс: {get_balance(u)}")
    rid, fee, payout_amt = info["id"], info["fee"], info["payout"]
    text = (
        f"📤 ЗАЯВКА СОЗДАНА\n{LINE}\n🧾 Номер: #{rid}\n"
        f"⭐ Списано с баланса: {amount} (зарезервировано)\n"
        f"💸 Комиссия бота {WITHDRAW_FEE_PCT}%: {fee} ⭐\n"
        f"✅ К выплате: {payout_amt} ⭐\n💰 Остаток: {info['left']} ⭐\n"
        f"📌 Статус: 🟢 открыта\n{LINE}\n"
        f"📩 Напиши @jumka — он обработает заявку.\n⏳ Ожидание: до 24 часов."
    )
    if not send(chat_id, text, kb([[b("⬅️ Меню", "menu")]]), wait=True).get("ok"):
        undo_open_withdrawal(rid)
        return
    for adm in AUTHORIZED_ADMIN_USERNAMES:
        cid = get_chat(adm)
        if cid:
            try:
                send(
                    cid,
                    f"🔔 Новая заявка на вывод\n🧾 #{rid}\n👤 @{u}\n"
                    f"⭐ Списано: {amount}\n💸 К выплате: {payout_amt} ⭐ (комиссия {fee})",
                )
            except Exception:
                pass


def admin_withdrawals(chat_id, status=None):
    reqs = get_withdrawals(limit=200)
    if status == "open":
        arr, title, back = [r for r in reqs if r["status"] == "open"], "🟢 Открытые заявки", "adm:withdrawals:all"
    elif status == "closed":
        arr, title, back = [r for r in reqs if r["status"] == "closed"], "⚫ Закрытые заявки", "adm:withdrawals:all"
    else:
        arr, title, back = reqs, "📋 Все заявки", "adm:back"
    rows = []
    for r in arr[:50]:
        icon = "🟢" if r["status"] == "open" else "⚫"
        label = f"{icon} #{r['id']} — @{r['username']} — {r['amount']} ⭐"
        rows.append([b(label[:60], f"wd:view:{r['id']}")])
    text = f"{title}\n\n" + ("👇 Нажми на заявку:" if rows else "Пусто.")
    rows.append([b("🟢 Открытые", "adm:withdrawals:open"), b("⚫ Закрытые", "adm:withdrawals:closed")])
    rows.append([b("📋 Все", "adm:withdrawals:all"), b("⬅️ Назад", back)])
    if status == "closed":
        rows.insert(-1, [b("🗑 Удалить все закрытые", "adm:withdrawals:delete_closed")])
    send(chat_id, text, kb(rows))


def admin_view_withdrawal(chat_id, rid):
    r = get_withdrawal(rid)
    if not r:
        return send(chat_id, "❌ Заявка не найдена.")
    status = (
        "🟢 открыта" if r["status"] == "open"
        else "❌ отклонена" if r["result"] == "rejected" else "✅ выплачена"
    )
    text = (
        f"📤 Заявка #{r['id']}\n\n👤 @{r['username']}\n"
        f"⭐ Списано с игрока: {r['amount']}\n💸 Комиссия бота: {r['fee']} ⭐\n"
        f"✅ К выплате: {r['payout']} ⭐\n📌 Статус: {status}\n"
        f"🕒 Создана: {time.strftime('%d.%m.%Y %H:%M', time.localtime(r['created_at']))}"
    )
    if r["closed_by"]:
        text += f"\n👮 Закрыл: @{r['closed_by']}"
    if r["comment"]:
        text += f"\n💬 Комментарий: {r['comment']}"
    if r["status"] == "open":
        rows = [
            [b("✅ Выплачено", f"wd:close:{rid}"), b("❌ Отклонить", f"wd:reject:{rid}")],
            [b("⬅️ Открытые", "adm:withdrawals:open")],
        ]
    else:
        rows = [[b("⬅️ Закрытые", "adm:withdrawals:closed")]]
    send(chat_id, text, kb(rows))


def start_close_withdrawal(chat_id, uid, rid, reject=False):
    r = get_withdrawal(rid)
    if not r or r["status"] != "open":
        return send(chat_id, "⚠️ Эта заявка уже закрыта.")
    pending[uid] = {"type": "withdraw_close_comment", "rid": rid, "reject": reject}
    if reject:
        send(chat_id, "❌ Отклонение заявки: звёзды вернутся игроку.\n\n💬 Напиши причину отказа:")
    else:
        send(chat_id, "✅ Выплата заявки.\n\n💬 Напиши комментарий:")


def close_withdrawal(chat_id, uid, admin_u, rid, comment, reject=False):
    r = get_withdrawal(rid)
    if not r or r["status"] != "open":
        return send(chat_id, "⚠️ Заявка уже закрыта.")
    u = r["username"]
    amount = r["amount"]
    reserved = r["reserved"]
    refund = bool(reject and reserved)
    deduct = bool((not reject) and (not reserved))
    try:
        closed, err = finalize_withdrawal_tx(
            rid, "rejected" if reject else "paid", admin_u, comment, refund=refund, deduct=deduct,
        )
    except Exception:
        traceback.print_exc()
        return send(chat_id, "⚠️ Не удалось закрыть заявку.")
    if err == "balance":
        return send(chat_id, f"❌ Нельзя закрыть: у @{u} {get_balance(u)} ⭐, нужно {amount} ⭐.")
    if err == "closed" or not closed:
        return send(chat_id, "⚠️ Заявка уже закрыта.")
    if reject:
        send(chat_id, f"❌ Заявка #{rid} отклонена.\n⭐ Возвращено @{u}: {amount if reserved else 0}")
    else:
        send(chat_id, f"✅ Заявка #{rid} выплачена.\n💰 Баланс @{u}: {get_balance(u)}")
    try:
        if r["chat_id"]:
            if reject:
                send(r["chat_id"], f"❌ Заявка #{rid} отклонена.\n⭐ {amount} вернулись на баланс.\n💬 Причина: {comment}")
            else:
                send(r["chat_id"], f"✅ Заявка #{rid} выплачена.\n⭐ К выплате: {r['payout']}\n💬 Комментарий: {comment}")
    except Exception:
        pass
    log_group(
        f"📤 Заявка #{rid} — {'❌ отклонена' if reject else '✅ выплачена'}\n"
        f"👤 @{u}\n⭐ {amount}\n👮 @{admin_u}"
    )


def delete_closed_withdrawals(chat_id):
    n = delete_closed_withdrawals_db()
    send(chat_id, f"🗑 Удалено закрытых заявок: {n}")
    admin_withdrawals(chat_id, "closed")


def mod_status(u):
    d = get_moderation(norm(u))
    if d["mute_until"] and d["mute_until"] <= int(time.time()):
        d["mute_until"] = 0
        set_moderation(norm(u), d)
    return d


def blocked(chat_id, u, for_game=False):
    if not u:
        return False
    st = mod_status(u)
    if st["banned"]:
        send(chat_id, "⛔ Ты заблокирован в боте. Обратись к администрации.")
        return True
    if for_game and st["mute_until"] > int(time.time()):
        left = st["mute_until"] - int(time.time())
        mins = max(1, (left + 59) // 60)
        send(chat_id, f"🔇 Ты замьючен и не можешь играть ещё ~{mins} мин.")
        return True
    return False


def moderation_panel(chat_id):
    send(
        chat_id, "🛡 Модерация\n\nВыбери действие. Сначала введи @username:",
        kb([
            [b("🔇 Мут", "mod:ask:mute"), b("🔨 Бан", "mod:ask:ban")],
            [b("⚠️ Варн", "mod:ask:warn"), b("🔊 Размут", "mod:ask:unmute")],
            [b("🔓 Разбан", "mod:ask:unban"), b("♻️ Разварн", "mod:ask:unwarn")],
            [b("⬅️ Админ-панель", "adm:back")],
        ]),
    )


def moderation_target_panel(chat_id, target):
    target = norm(target)
    st = mod_status(target)
    mute = (
        "нет" if st["mute_until"] <= int(time.time())
        else time.strftime("до %d.%m %H:%M", time.localtime(st["mute_until"]))
    )
    send(
        chat_id,
        f"🛡 @{target}\n\n⚠️ Варны: {st['warns']}/3\n🔇 Мут: {mute}\n"
        f"🔨 Бан: {'да' if st['banned'] else 'нет'}\n\nВыбери действие:",
        kb([
            [b("🔇 Мут 10 мин", f"mod:do:mute:{target}:600"), b("🔇 Мут 1 час", f"mod:do:mute:{target}:3600")],
            [b("🔇 Мут 24 часа", f"mod:do:mute:{target}:86400"), b("🔨 Бан", f"mod:do:ban:{target}")],
            [b("⚠️ Варн", f"mod:do:warn:{target}"), b("🔊 Размут", f"mod:do:unmute:{target}")],
            [b("🔓 Разбан", f"mod:do:unban:{target}"), b("♻️ Разварн", f"mod:do:unwarn:{target}")],
            [b("⬅️ Модерация", "adm:moderation")],
        ]),
    )


def moderate(chat_id, admin_u, target, action, duration=0):
    target = norm(target)
    if not target:
        return send(chat_id, "⚠️ Укажи username.")
    st = get_moderation(target)
    now = int(time.time())
    if action == "warn":
        st["warns"] += 1
        if st["warns"] >= 3:
            st["banned"] = True
            result = f"⚠️ @{target}: 3-й варн — автоматический бан."
        else:
            result = f"⚠️ @{target}: варн {st['warns']}/3."
    elif action == "unwarn":
        st["warns"] = max(0, st["warns"] - 1)
        result = f"♻️ @{target}: варн снят. Сейчас {st['warns']}/3."
    elif action == "mute":
        st["mute_until"] = now + int(duration)
        result = f"🔇 @{target} замьючен до {time.strftime('%d.%m.%Y %H:%M', time.localtime(st['mute_until']))}."
    elif action == "unmute":
        st["mute_until"] = 0
        result = f"🔊 @{target} размьючен."
    elif action == "ban":
        st["banned"] = True
        result = f"🔨 @{target} забанен."
    elif action == "unban":
        st["banned"] = False
        result = f"🔓 @{target} разбанен."
    else:
        return send(chat_id, "⚠️ Неизвестное действие.")
    st["last_action"] = {"action": action, "admin": norm(admin_u), "time": now}
    set_moderation(target, st)
    send(chat_id, "✅ " + result)
    log_group(f"🛡 Модерация\n👤 @{target}\n🎯 {action}\n👮 @{norm(admin_u)}")
    cid = get_chat(target)
    if cid:
        notice = {
            "warn": "⚠️ Тебе выдано предупреждение.",
            "unwarn": "♻️ С тебя сняли предупреждение.",
            "mute": f"🔇 Ты замьючен до {time.strftime('%d.%m.%Y %H:%M', time.localtime(st['mute_until']))}.",
            "unmute": "🔊 Мут снят.",
            "ban": "🔨 Ты забанен в боте.",
            "unban": "🔓 Бан снят.",
        }.get(action)
        if notice:
            try:
                send(cid, notice)
            except Exception:
                pass
    moderation_target_panel(chat_id, target)


def panel(chat_id):
    send(chat_id, "🛠 Админ-панель", admins_kb())


def testing_panel(chat_id):
    st = get_test_mode()
    text = (
        f"🧪 РЕЖИМ ТЕСТЕРОВКИ\n{LINE}\n"
        f"Статус: {'🟢 ВКЛЮЧЁН' if st else '⚫ ВЫКЛЮЧЕН'}\n\n"
        "Служит для тестирования новых обновлений."
    )
    btn = "⚫ Выключить" if st else "🟢 Включить"
    send(chat_id, text, kb([[b(btn, "adm:testing_toggle")], [b("⬅️ Админ-панель", "adm:back")]]))


def toggle_testing(chat_id, admin_u):
    st = not get_test_mode()
    set_test_mode(st)
    log_group(f"🧪 Тестеровка {'ВКЛЮЧЕНА' if st else 'ВЫКЛЮЧЕНА'}\n👤 @{admin_u}")
    testing_panel(chat_id)


def players(chat_id):
    x = get_balances()
    if not x:
        return send(chat_id, "👥 Игроков пока нет.")
    text = "👥 Игроки:\n\n" + "\n".join(f"@{u}: {v} ⭐" for u, v in sorted(x.items(), key=lambda z: -z[1]))
    send(chat_id, text[:3900])


def admin_stats(chat_id):
    x = get_balances()
    s = get_stats()
    send(
        chat_id,
        f"📊 Статистика\n\n👥 Игроков: {len(x)}\n"
        f"💰 На балансах: {sum(x.values())} ⭐\n"
        f"🎮 Игр: {s['games']}\n🎰 Ставок: {s['bets']} ⭐\n💸 Выплат: {s['payouts']} ⭐",
    )


def history(chat_id):
    h = get_history(20)
    if not h:
        return send(chat_id, "🕘 История пуста.")
    lines = ["🕘 История:"] + [
        f"@{i['username']} — {GAMES.get(i['game'], '?')} — {i['bet']} → {i['win']} ⭐" for i in h
    ]
    send(chat_id, "\n".join(lines)[:3900])


def player_history(chat_id, username):
    target = norm(username)
    if not target:
        return send(chat_id, "⚠️ Укажи @username игрока.")
    rows = get_user_history(target, 30)
    if not rows:
        return send(chat_id, f"🎮 У @{target} пока пусто.")
    lines = [f"🎮 История игр @{target}:", ""]
    for i in rows:
        game = GAMES.get(i["game"], i["game"])
        net = i["win"] - i["bet"]
        s = "+" if net > 0 else ""
        lines.append(f"• {game} — ставка {i['bet']} ⭐ → {i['win']} ⭐ ({s}{net})")
    send(chat_id, "\n".join(lines)[:3900])


def donors(chat_id):
    rows = get_donors(10)
    if not rows:
        return send(chat_id, "🏆 Донатеров пока нет.")
    send(chat_id, "🏆 Донатеры:\n\n" + "\n".join(f"{n}. @{u} — {a} ⭐" for n, (u, a) in enumerate(rows, 1)))


def active(chat_id):
    rows = count_games_per_user(10)
    if not rows:
        return send(chat_id, "🔥 Активных пока нет.")
    send(chat_id, "🔥 Активные:\n\n" + "\n".join(f"{n}. @{u} — {c} игр" for n, (u, c) in enumerate(rows, 1)))


def donatelog(chat_id):
    total, count = get_payments_total()
    top = get_donors(5)
    recent = get_payments_log(20)
    parts = [
        f"💳 Лог донатов\n{LINE}",
        f"💰 Всего: {total} ⭐",
        f"🧾 Платежей: {count}",
    ]
    if top:
        parts.append("🏆 Топ:")
        parts.extend(f"{n}. @{u} — {a} ⭐" for n, (u, a) in enumerate(top, 1))
    parts.append("🕘 Последние:")
    if recent:
        for p in recent:
            ts = time.strftime("%d.%m %H:%M", time.localtime(p["time"]))
            parts.append(f"• {ts} @{p['username']} — {p['amount']} ⭐")
    else:
        parts.append("Пока пусто.")
    send(chat_id, "\n".join(parts)[:3900])


_lock_fh = None
_callback_seen = {}


def acquire_instance_lock():
    global _lock_fh
    if fcntl is None:
        return
    _lock_fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Уже запущен другой экземпляр бота.")
        raise SystemExit(1)
    _lock_fh.seek(0)
    _lock_fh.truncate()
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()


def who(obj):
    user = (obj or {}).get("from") or {}
    uid = user.get("id")
    name = norm(user.get("username")) or (f"id{uid}" if uid else "")
    return uid, name


def cmd_of(text):
    head = (text or "").strip().split()
    if not head or not head[0].startswith("/"):
        return "", []
    return head[0].split("@", 1)[0].lower(), head[1:]


def require_admin(chat_id, uid, u):
    if is_admin(uid, u):
        return True
    send(chat_id, "⚠️ Только для админа. Команда: /admin")
    return False


def show_balance(chat_id, u):
    bal = ensure_user(u, chat_id)
    send(
        chat_id,
        f"💰 Баланс @{u}: {bal} ⭐",
        kb([[b("🎮 Меню", "menu"), b("⭐ Пополнить", "topup")]]),
    )


def bot_stars(chat_id):
    r = api("getMyStarBalance", timeout=15)
    if not r.get("ok"):
        return send(chat_id, "⚠️ Не удалось получить Stars бота.")
    res = r.get("result") or {}
    amount = res.get("amount", res) if isinstance(res, dict) else res
    send(chat_id, f"⭐ Stars бота: {amount}", kb([[b("⬅️ Админ-панель", "adm:back")]]))


def admin_adjust(chat_id, admin_u, text, take=False):
    parts = text.replace("@", " ").split()
    if len(parts) != 2:
        return send(chat_id, "⚠️ Формат: @username 100")
    target = norm(parts[0])
    try:
        amount = int(parts[1])
    except ValueError:
        return send(chat_id, "⚠️ Сумма должна быть числом.")
    if not target or amount <= 0:
        return send(chat_id, "⚠️ Укажи username и сумму больше 0.")
    if take and get_balance(target) < amount:
        return send(chat_id, f"⚠️ У @{target} только {get_balance(target)} ⭐.")
    new = add_balance(target, -amount if take else amount)
    verb = "списано" if take else "начислено"
    send(chat_id, f"✅ @{target}: {verb} {amount} ⭐\n💰 Баланс: {new}")
    cid = get_chat(target)
    if cid:
        send(cid, f"{'➖' if take else '➕'} {verb.capitalize()} {amount} ⭐\n💰 Баланс: {new}")
    log_group(f"{'➖ Списание' if take else '➕ Начисление'}\n👤 @{target}\n⭐ {amount}\n👮 @{admin_u}")


def run_broadcast(chat_id, text):
    chats = get_chats()
    if not chats:
        return send(chat_id, "📢 Получателей пока нет.")
    for cid in chats.values():
        send(cid, text)
    send(chat_id, f"📢 Поставлено в очередь: {len(chats)}")


def handle_pending(chat_id, uid, u, text):
    state = pending.get(uid) or {}
    kind = state.get("type")
    if kind == "admin_password":
        fails = pw_fail.get(uid) or {"n": 0, "until": 0}
        if fails["until"] > time.time():
            return send(chat_id, "⚠️ Слишком много попыток. Подожди и попробуй снова.")
        if ADMIN_PASSWORD and hmac.compare_digest(text.encode(), ADMIN_PASSWORD.encode()):
            admins.add(uid)
            pw_fail.pop(uid, None)
            pending.pop(uid, None)
            return panel(chat_id)
        fails["n"] += 1
        if fails["n"] >= 5:
            fails["until"] = time.time() + 600
            fails["n"] = 0
        pw_fail[uid] = fails
        return send(chat_id, "⚠️ Неверный пароль.")
    if kind == "bet":
        pending.pop(uid, None)
        return start_game(chat_id, u, state.get("game"), text.strip(), uid=uid)
    if kind == "topup":
        pending.pop(uid, None)
        return invoice(chat_id, u, text.strip())
    if kind == "wd":
        pending.pop(uid, None)
        return create_withdrawal(chat_id, u, text.strip())
    if kind == "broadcast":
        pending.pop(uid, None)
        return run_broadcast(chat_id, text)
    if kind in ("add", "take"):
        pending.pop(uid, None)
        return admin_adjust(chat_id, u, text, take=(kind == "take"))
    if kind == "playerhistory":
        pending.pop(uid, None)
        return player_history(chat_id, text)
    if kind == "mod_user":
        pending.pop(uid, None)
        return moderation_target_panel(chat_id, text)
    if kind == "withdraw_close_comment":
        pending.pop(uid, None)
        return close_withdrawal(chat_id, uid, u, state.get("rid"), text, reject=bool(state.get("reject")))
    pending.pop(uid, None)
    send(chat_id, "⚠️ Это действие устарело. Открой меню: /start")


def handle_command(chat_id, uid, u, text, chat):
    cmd, args = cmd_of(text)
    if cmd in ("/start", "/menu", "/help"):
        return show_menu(chat_id, u)
    if cmd in ("/balance", "/bal", "/баланс"):
        return show_balance(chat_id, u)
    if cmd in ("/topup", "/пополнить"):
        return topup_menu(chat_id)
    if cmd in ("/withdraw", "/вывод"):
        return withdrawal_menu(chat_id, u)
    if cmd in ("/my", "/игры"):
        return my_games(chat_id, u)
    if cmd in GAME_CMDS:
        g = GAME_CMDS[cmd]
        if args:
            return start_game(chat_id, u, g, args[0], uid=uid)
        return choose_game(chat_id, u, g)
    if cmd == "/cancel":
        pending.pop(uid, None)
        return show_menu(chat_id, u)
    if cmd == "/admin":
        if is_admin(uid, u):
            return panel(chat_id)
        pending[uid] = {"type": "admin_password"}
        return send(chat_id, "🔐 Введи пароль админа:")
    if cmd == LOG_CMD and is_admin(uid, u):
        title = (chat or {}).get("title") or LOG_TITLE
        set_log_chat(chat_id, title, u)
        return send(chat_id, f"✅ Лог-чат установлен: {title}")
    send(chat_id, "Напиши /start, чтобы открыть меню.")


def route_callback(chat_id, uid, u, data):
    admin_data = data.startswith(("adm:", "sadm:", "wd:", "mod:"))
    if admin_data and not require_admin(chat_id, uid, u):
        return
    if data == "menu":
        return show_menu(chat_id, u)
    if data == "bal":
        return show_balance(chat_id, u)
    if data == "my":
        return my_games(chat_id, u)
    if data == "topup":
        return topup_menu(chat_id)
    if data == "topup_custom":
        pending[uid] = {"type": "topup"}
        return send(chat_id, "✍️ Введи сумму пополнения в ⭐:")
    if data.startswith("topup_amount:"):
        return invoice(chat_id, u, data.split(":", 1)[1])
    if data == "withdraw":
        return withdrawal_menu(chat_id, u)
    if data == "wd_custom":
        pending[uid] = {"type": "wd"}
        return send(chat_id, "✍️ Введи сумму вывода в ⭐:")
    if data.startswith("wd_amount:"):
        return create_withdrawal(chat_id, u, data.split(":", 1)[1])
    if data.startswith("game:"):
        return choose_game(chat_id, u, data.split(":", 1)[1])
    if data.startswith("betcustom:"):
        g = data.split(":", 1)[1]
        pending[uid] = {"type": "bet", "game": g}
        return send(chat_id, f"✍️ Введи ставку от {MIN_BET} до {MAX_BET} ⭐:")
    if data.startswith("bet:"):
        _, g, bet = data.split(":", 2)
        return start_game(chat_id, u, g, bet, uid=uid)
    if data.startswith("play:"):
        _, g, bet, choice = data.split(":", 3)
        return play(chat_id, u, g, bet, choice, uid=uid)
    if data == "adm:back":
        return panel(chat_id)
    if data == "adm:list":
        return players(chat_id)
    if data == "adm:stats":
        return admin_stats(chat_id)
    if data == "adm:history":
        return history(chat_id)
    if data == "adm:playerhistory":
        pending[uid] = {"type": "playerhistory"}
        return send(chat_id, "🎮 Напиши @username игрока:")
    if data == "adm:donors":
        return donors(chat_id)
    if data == "adm:active":
        return active(chat_id)
    if data == "adm:donatelog":
        return donatelog(chat_id)
    if data == "adm:moderation":
        return moderation_panel(chat_id)
    if data == "adm:testing":
        return testing_panel(chat_id)
    if data == "adm:testing_toggle":
        return toggle_testing(chat_id, u)
    if data == "adm:withdrawals":
        return admin_withdrawals(chat_id)
    if data.startswith("adm:withdrawals:"):
        kind = data.split(":", 2)[2]
        if kind == "delete_closed":
            return delete_closed_withdrawals(chat_id)
        return admin_withdrawals(chat_id, None if kind == "all" else kind)
    if data == "adm:broadcast":
        pending[uid] = {"type": "broadcast"}
        return send(chat_id, "📢 Напиши текст рассылки. /cancel — отмена.")
    if data == "adm:add":
        pending[uid] = {"type": "add"}
        return send(chat_id, "➕ Напиши: @username 100")
    if data == "adm:take":
        pending[uid] = {"type": "take"}
        return send(chat_id, "➖ Напиши: @username 100")
    if data == "adm:logout":
        admins.discard(uid)
        return send(chat_id, "🚪 Ты вышел из админки.")
    if data == "sadm:stars":
        return bot_stars(chat_id)
    if data == "sadm:withdraw":
        return send(
            chat_id,
            "💸 Stars самого бота выводятся через кабинет StaticGram, не через баланс игрока.",
            kb([[b("⭐ Stars бота", "sadm:stars")], [b("⬅️ Админ-панель", "adm:back")]]),
        )
    if data.startswith("wd:view:"):
        return admin_view_withdrawal(chat_id, data.split(":", 2)[2])
    if data.startswith("wd:close:"):
        return start_close_withdrawal(chat_id, uid, data.split(":", 2)[2], reject=False)
    if data.startswith("wd:reject:"):
        return start_close_withdrawal(chat_id, uid, data.split(":", 2)[2], reject=True)
    if data.startswith("mod:ask:"):
        pending[uid] = {"type": "mod_user", "action": data.split(":", 2)[2]}
        return send(chat_id, "🛡 Напиши @username:")
    if data.startswith("mod:do:"):
        parts = data.split(":")
        action = parts[2] if len(parts) > 2 else ""
        target = parts[3] if len(parts) > 3 else ""
        duration = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        return moderate(chat_id, u, target, action, duration)
    send(chat_id, "⚠️ Кнопка устарела. Открой меню: /start", menu())


def handle_message(m):
    if m.get("successful_payment"):
        return successful(m)
    chat = m.get("chat") or {}
    chat_id = chat.get("id")
    uid, u = who(m)
    text = (m.get("text") or "").strip()
    if not chat_id or not u or not text:
        return
    ensure_user(u, chat_id)
    cmd, _ = cmd_of(text)
    if chat.get("type") != "private":
        if cmd == LOG_CMD and is_admin(uid, u):
            set_log_chat(chat_id, chat.get("title") or LOG_TITLE, u)
            send(chat_id, f"✅ Лог-чат установлен: {chat.get('title') or chat_id}")
        return
    if norm(u) not in AUTHORIZED_ADMIN_USERNAMES and blocked(chat_id, u):
        return
    if cmd:
        pending.pop(uid, None)
        return handle_command(chat_id, uid, u, text, chat)
    if uid in pending:
        return handle_pending(chat_id, uid, u, text)
    send(chat_id, "Напиши /start, чтобы открыть меню.")


def handle_callback(cq):
    qid = cq.get("id")
    now = time.time()
    if qid in _callback_seen and now - _callback_seen[qid] < 3600:
        api("answerCallbackQuery", callback_query_id=qid, retries=0, timeout=5)
        return
    _callback_seen[qid] = now
    if len(_callback_seen) > 4000:
        old = [k for k, t in _callback_seen.items() if now - t > 3600]
        for k in old:
            _callback_seen.pop(k, None)
    api("answerCallbackQuery", callback_query_id=qid, retries=1, timeout=8)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    uid, u = who(cq)
    data = cq.get("data") or ""
    if not chat_id or not u:
        return
    ensure_user(u, chat_id)
    route_callback(chat_id, uid, u, data)


def handle_update(upd):
    if "message" in upd:
        handle_message(upd["message"])
    elif "callback_query" in upd:
        handle_callback(upd["callback_query"])
    elif "pre_checkout_query" in upd:
        precheckout(upd["pre_checkout_query"])


def main():
    if not BOT_TOKEN:
        print("Задай BOT_TOKEN.")
        raise SystemExit(1)
    acquire_instance_lock()
    init_db()
    me = api("getMe", retries=2, timeout=20)
    if not me.get("ok"):
        print("Бот не авторизован:", str(me.get("description") or me)[:300])
        raise SystemExit(1)
    name = (me.get("result") or {}).get("username")
    print(f"Бот @{name} запущен")
    api("deleteWebhook", drop_pending_updates=False, retries=1, timeout=15)
    recover_inflight()
    offset = None
    conflicts = 0
    while True:
        upd = api(
            "getUpdates",
            retries=POLL_RETRIES,
            timeout=HTTP_TIMEOUT + 5,
            poll=POLL_TIMEOUT,
            offset=offset,
            allowed_updates=["message", "callback_query", "pre_checkout_query"],
        )
        if not upd.get("ok"):
            if upd.get("conflict") or upd.get("error_code") == 409:
                conflicts += 1
                print(f"Конфликт getUpdates ({conflicts})")
                if conflicts >= CONFLICT_EXIT_AFTER:
                    print("Обновления уже забирает другой процесс.")
                    raise SystemExit(1)
            else:
                conflicts = 0
            time.sleep(1)
            continue
        conflicts = 0
        for item in upd.get("result") or []:
            offset = item["update_id"] + 1
            try:
                handle_update(item)
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
