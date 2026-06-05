"""
╔══════════════════════════════════════════════════╗
║   🎬  ربات فیلم و سریال  —  نسخه ۳.۰ PRO        ║
║   بازنویسی کامل | رفع تمام باگ‌ها | قابلیت جدید ║
╚══════════════════════════════════════════════════╝
"""

import telebot
import requests
import sqlite3
import time
import json
import logging
import os
import io
import threading
from telebot import types
from datetime import datetime, timedelta
from collections import Counter

try:
    import jdatetime
    HAS_JDATETIME = True
except ImportError:
    HAS_JDATETIME = False

try:
    from deep_translator import GoogleTranslator
    HAS_TRANSLATOR = True
except ImportError:
    HAS_TRANSLATOR = False

# ─────────────────────────────────────────────────────────────
# تنظیمات Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# تنظیمات اصلی
# ─────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv('BOT_TOKEN',    '573020410:AAGZ4DAuhYSbZ6C2fNkkuE8PMi4zRN8oN8c')
TMDB_API_KEY = os.getenv('TMDB_API_KEY', '206311d64aeb1cb1a616fd84997a5c8d')
OMDB_API_KEY = os.getenv('OMDB_API_KEY', 'cde25ebc')
# کلید OpenSubtitles.com (رایگان - ثبت کنید روی opensubtitles.com/api)
OPENSUB_API_KEY = os.getenv('OPENSUB_API_KEY', '')
OPENSUB_APP_NAME = 'MovieBot_TG'

DB_NAME   = 'movie_bot_pro.db'
ADMIN_IDS: set[int] = {403618630}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

_BOT_USERNAME: str | None = None

def get_bot_username() -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        try:
            _BOT_USERNAME = bot.get_me().username
        except Exception:
            _BOT_USERNAME = "bot"
    return _BOT_USERNAME


# ─────────────────────────────────────────────────────────────
# Thread-safe state management
# ─────────────────────────────────────────────────────────────
_state_lock   = threading.Lock()
_cache_lock   = threading.Lock()
_ratelim_lock = threading.Lock()

user_states:   dict[int, str]   = {}
user_history:  dict[int, list]  = {}
temp_cache:    dict[str, dict]  = {}
user_last_req: dict[int, float] = {}
_stats_timer:  threading.Timer | None = None


def set_state(user_id: int, state: str | None):
    with _state_lock:
        if state is None:
            user_states.pop(user_id, None)
        else:
            user_states[user_id] = state


def get_state(user_id: int) -> str | None:
    with _state_lock:
        return user_states.get(user_id)


def push_history(user_id: int, item_type: str, item_id: str):
    with _state_lock:
        hist  = user_history.setdefault(user_id, [])
        entry = (item_type, str(item_id))
        if not hist or hist[-1] != entry:
            hist.append(entry)
            if len(hist) > 10:
                hist.pop(0)


def pop_history(user_id: int) -> tuple | None:
    with _state_lock:
        hist = user_history.get(user_id, [])
        if len(hist) > 1:
            hist.pop()
            return hist[-1]
        return None


def check_rate_limit(user_id: int, min_gap: float = 0.8) -> bool:
    with _ratelim_lock:
        now  = time.time()
        last = user_last_req.get(user_id, 0)
        if now - last < min_gap:
            return False
        user_last_req[user_id] = now
        return True


# ─────────────────────────────────────────────────────────────
# دیتابیس
# ─────────────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.executescript('''
        CREATE TABLE IF NOT EXISTS cache (
            id        TEXT PRIMARY KEY,
            data      TEXT NOT NULL,
            timestamp INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_bookmarks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            item_id    TEXT NOT NULL,
            item_type  TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, item_id, item_type)
        );
        CREATE TABLE IF NOT EXISTS user_ratings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            item_id    TEXT NOT NULL,
            item_type  TEXT NOT NULL,
            rating     INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, item_id, item_type)
        );
        CREATE TABLE IF NOT EXISTS user_activity_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            item_id     TEXT,
            item_type   TEXT,
            timestamp   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_notifications (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL,
            notification_type TEXT,
            title             TEXT,
            message           TEXT,
            item_id           TEXT,
            item_type         TEXT,
            is_read           INTEGER DEFAULT 0,
            created_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id            INTEGER PRIMARY KEY,
            favorite_genres    TEXT,
            favorite_directors TEXT,
            favorite_actors    TEXT,
            last_analysis      TEXT,
            created_at         TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            access_level TEXT DEFAULT "admin",
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_statistics (
            id              INTEGER PRIMARY KEY,
            total_users     INTEGER DEFAULT 0,
            active_users    INTEGER DEFAULT 0,
            total_searches  INTEGER DEFAULT 0,
            total_bookmarks INTEGER DEFAULT 0,
            last_updated    TEXT
        );
        CREATE TABLE IF NOT EXISTS user_detailed_stats (
            user_id         INTEGER PRIMARY KEY,
            total_searches  INTEGER DEFAULT 0,
            total_views     INTEGER DEFAULT 0,
            total_bookmarks INTEGER DEFAULT 0,
            total_ratings   INTEGER DEFAULT 0,
            favorite_genre  TEXT,
            last_active     TEXT,
            created_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bm_user  ON user_bookmarks(user_id);
        CREATE INDEX IF NOT EXISTS idx_rat_user ON user_ratings(user_id);
        CREATE INDEX IF NOT EXISTS idx_act_user ON user_activity_logs(user_id);
        CREATE INDEX IF NOT EXISTS idx_act_time ON user_activity_logs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_notif    ON user_notifications(user_id, is_read);
        ''')
        now = _now()
        for aid in ADMIN_IDS:
            c.execute(
                'INSERT OR IGNORE INTO admin_users (user_id, access_level, created_at) VALUES (?,?,?)',
                (aid, 'super_admin', now)
            )
        c.execute('INSERT OR IGNORE INTO bot_statistics (id, last_updated) VALUES (1,?)', (now,))
    logger.info("✅ Database initialized")


# ─────────────────────────────────────────────────────────────
# توابع کمکی عمومی
# ─────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_text(text) -> str:
    if not text:
        return ""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def fmt(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def to_shamsi(gregorian_date: str) -> str:
    if not gregorian_date or not HAS_JDATETIME:
        return gregorian_date[:10] if gregorian_date else ""
    try:
        dt = datetime.strptime(gregorian_date[:10], '%Y-%m-%d')
        jd = jdatetime.date.fromgregorian(date=dt)
        return jd.strftime('%Y/%m/%d')
    except Exception:
        return gregorian_date[:10]


def translate_text(text: str, target='fa') -> str:
    if not text or not HAS_TRANSLATOR:
        return text
    try:
        return GoogleTranslator(source='auto', target=target).translate(text) or text
    except Exception as e:
        logger.warning(f"Translation error: {e}")
        return text


def star_bar(vote: float, max_stars: int = 5) -> str:
    """تبدیل رتبه ۱۰ به ستاره"""
    filled = round(vote / 2)
    return '★' * filled + '☆' * (max_stars - filled)


# ─────────────────────────────────────────────────────────────
# Admin helpers
# ─────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    try:
        with get_conn() as conn:
            r = conn.execute('SELECT 1 FROM admin_users WHERE user_id=?', (user_id,)).fetchone()
        return r is not None
    except Exception:
        return False


def is_super_admin(user_id: int) -> bool:
    try:
        with get_conn() as conn:
            r = conn.execute(
                'SELECT 1 FROM admin_users WHERE user_id=? AND access_level="super_admin"',
                (user_id,)
            ).fetchone()
        return r is not None
    except Exception:
        return False


def add_admin(user_id: int, username: str, first_name: str, access_level='admin') -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO admin_users (user_id,username,first_name,access_level,created_at) VALUES (?,?,?,?,?)',
                (user_id, username, first_name, access_level, _now())
            )
        return True
    except Exception as e:
        logger.error(f"add_admin: {e}")
        return False


def remove_admin(user_id: int) -> bool:
    try:
        with get_conn() as conn:
            conn.execute('DELETE FROM admin_users WHERE user_id=?', (user_id,))
        return True
    except Exception as e:
        logger.error(f"remove_admin: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# کش دیتابیس
# ─────────────────────────────────────────────────────────────
def save_cache(key: str, data):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO cache (id,data,timestamp) VALUES (?,?,?)',
                (key, json.dumps(data, ensure_ascii=False), int(time.time()))
            )
    except Exception as e:
        logger.error(f"save_cache: {e}")


def get_cache(key: str, expiry=3600):
    try:
        with get_conn() as conn:
            row = conn.execute('SELECT data,timestamp FROM cache WHERE id=?', (key,)).fetchone()
        if row:
            data_str, ts = row
            if time.time() - ts < expiry:
                return json.loads(data_str)
    except Exception as e:
        logger.error(f"get_cache: {e}")
    return None


def clear_cache():
    try:
        with get_conn() as conn:
            conn.execute('DELETE FROM cache')
        with _cache_lock:
            temp_cache.clear()
    except Exception as e:
        logger.error(f"clear_cache: {e}")


# ─────────────────────────────────────────────────────────────
# Activity & Stats
# ─────────────────────────────────────────────────────────────
def log_user_activity(user_id: int, action_type: str, item_id=None, item_type=None):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT INTO user_activity_logs (user_id,action_type,item_id,item_type,timestamp) VALUES (?,?,?,?,?)',
                (user_id, action_type, str(item_id) if item_id else None, item_type, _now())
            )
            _update_detailed_stats(conn, user_id, action_type)
        _schedule_stats_update()
    except Exception as e:
        logger.error(f"log_activity: {e}")


def _update_detailed_stats(conn, user_id: int, action_type: str):
    now = _now()
    conn.execute(
        'INSERT OR IGNORE INTO user_detailed_stats (user_id,total_searches,total_views,total_bookmarks,total_ratings,last_active,created_at) VALUES (?,0,0,0,0,?,?)',
        (user_id, now, now)
    )
    col_map = {'search': 'total_searches', 'view': 'total_views', 'bookmark': 'total_bookmarks', 'rating': 'total_ratings'}
    col = col_map.get(action_type)
    if col:
        conn.execute(f'UPDATE user_detailed_stats SET {col}={col}+1, last_active=? WHERE user_id=?', (now, user_id))
    else:
        conn.execute('UPDATE user_detailed_stats SET last_active=? WHERE user_id=?', (now, user_id))


def _schedule_stats_update():
    global _stats_timer
    if _stats_timer and _stats_timer.is_alive():
        return
    _stats_timer = threading.Timer(30.0, _do_update_bot_statistics)
    _stats_timer.daemon = True
    _stats_timer.start()


def _do_update_bot_statistics():
    try:
        thirty_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            tu = conn.execute('SELECT COUNT(DISTINCT user_id) FROM user_activity_logs').fetchone()[0]
            au = conn.execute('SELECT COUNT(DISTINCT user_id) FROM user_activity_logs WHERE timestamp>?', (thirty_ago,)).fetchone()[0]
            ts = conn.execute('SELECT COUNT(*) FROM user_activity_logs WHERE action_type="search"').fetchone()[0]
            tb = conn.execute('SELECT COUNT(*) FROM user_bookmarks').fetchone()[0]
            conn.execute(
                'INSERT OR REPLACE INTO bot_statistics (id,total_users,active_users,total_searches,total_bookmarks,last_updated) VALUES (1,?,?,?,?,?)',
                (tu, au, ts, tb, _now())
            )
    except Exception as e:
        logger.error(f"update_stats: {e}")


# ─────────────────────────────────────────────────────────────
# TMDB & OMDB API
# ─────────────────────────────────────────────────────────────
def search_tmdb(query: str) -> list:
    if not query or len(query.strip()) < 2:
        return []
    key = f"search_{query.lower().strip()}"
    cached = get_cache(key, expiry=1800)
    if cached:
        return cached

    all_results, seen = [], set()
    for lang in ('fa-IR', 'en-US'):
        try:
            r = requests.get(
                'https://api.themoviedb.org/3/search/multi',
                params={'api_key': TMDB_API_KEY, 'query': query, 'language': lang, 'page': 1},
                timeout=10
            )
            if r.status_code == 200:
                for item in r.json().get('results', []):
                    if item.get('media_type') in ('movie', 'tv', 'person'):
                        uid = f"{item['media_type']}_{item['id']}"
                        if uid not in seen:
                            item['_lang'] = lang
                            all_results.append(item)
                            seen.add(uid)
        except Exception as e:
            logger.warning(f"search_tmdb lang={lang}: {e}")

    all_results.sort(key=lambda x: (
        {'fa-IR': 2, 'en-US': 1}.get(x.get('_lang', ''), 0),
        x.get('popularity', 0) or 0,
        x.get('vote_average', 0) or 0
    ), reverse=True)

    result = all_results[:15]
    save_cache(key, result)
    return result


def get_details(item_id, item_type: str) -> dict | None:
    if item_type not in ('movie', 'tv', 'person'):
        return None
    item_id = str(item_id)
    key     = f"details_{item_type}_{item_id}"
    cached  = get_cache(key, expiry=86400)
    if cached:
        # اطمینان از اینکه trailer در cache ذخیره بشه
        _ensure_trailer_cached(cached, item_id)
        return cached

    append = 'videos,credits,similar,external_ids,keywords'
    if item_type == 'person':
        append = 'external_ids,combined_credits'

    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/{item_type}/{item_id}',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'append_to_response': append},
            timeout=12
        )
        if r.status_code != 200:
            return None
        data = r.json()

        # اگه ویدیوها نداشت یا خالی بود، با زبان انگلیسی هم امتحان کن
        if item_type in ('movie', 'tv'):
            vids = data.get('videos', {}).get('results', [])
            if not vids:
                try:
                    r_en = requests.get(
                        f'https://api.themoviedb.org/3/{item_type}/{item_id}/videos',
                        params={'api_key': TMDB_API_KEY, 'language': 'en-US'},
                        timeout=8
                    )
                    if r_en.status_code == 200:
                        data['videos'] = r_en.json()
                except Exception:
                    pass

        # اگه overview فارسی نداشت
        if item_type in ('movie', 'tv') and not data.get('overview'):
            try:
                r_en = requests.get(
                    f'https://api.themoviedb.org/3/{item_type}/{item_id}',
                    params={'api_key': TMDB_API_KEY, 'language': 'en-US'},
                    timeout=5
                )
                if r_en.status_code == 200:
                    ov = r_en.json().get('overview', '')
                    if ov:
                        data['overview'] = translate_text(ov)
            except Exception:
                pass

        # اگه biography فارسی نداشت
        if item_type == 'person' and not data.get('biography'):
            try:
                r_en = requests.get(
                    f'https://api.themoviedb.org/3/person/{item_id}',
                    params={'api_key': TMDB_API_KEY, 'language': 'en-US'},
                    timeout=5
                )
                if r_en.status_code == 200:
                    bio = r_en.json().get('biography', '')
                    if bio:
                        data['biography'] = translate_text(bio)
            except Exception:
                pass

        # OMDB
        imdb_id = data.get('external_ids', {}).get('imdb_id')
        if imdb_id:
            omdb = get_omdb(imdb_id)
            if omdb:
                data['omdb'] = omdb

        save_cache(key, data)
        _ensure_trailer_cached(data, item_id)
        return data
    except Exception as e:
        logger.error(f"get_details {item_type}/{item_id}: {e}")
        return None


def _ensure_trailer_cached(data: dict, item_id: str):
    """تریلر رو همیشه در temp_cache ذخیره کن"""
    trailer_url = get_trailer_url(data.get('videos', {}))
    if trailer_url:
        title = data.get('title') or data.get('name', '')
        with _cache_lock:
            temp_cache[f"trailer_{item_id}"] = {
                'url':   trailer_url,
                'title': title,
                'ts':    time.time()
            }


def get_omdb(imdb_id: str) -> dict | None:
    if not imdb_id:
        return None
    key    = f"omdb_{imdb_id}"
    cached = get_cache(key, expiry=86400)
    if cached:
        return cached
    try:
        r = requests.get(
            'http://www.omdbapi.com/',
            params={'apikey': OMDB_API_KEY, 'i': imdb_id, 'plot': 'short'},
            timeout=5
        )
        if r.status_code == 200:
            d = r.json()
            if d.get('Response') == 'True':
                save_cache(key, d)
                return d
    except Exception as e:
        logger.warning(f"get_omdb {imdb_id}: {e}")
    return None


def get_trailer_url(videos: dict) -> str | None:
    results = (videos or {}).get('results', [])
    for vtype in ('Trailer', 'Teaser', 'Clip', 'Featurette'):
        for v in results:
            if v.get('site') == 'YouTube' and v.get('type') == vtype and v.get('key'):
                return f"https://www.youtube.com/watch?v={v['key']}"
    return None


def get_popular_movies() -> list:
    key    = "popular_movies_v3"
    cached = get_cache(key, expiry=43200)
    if cached:
        return cached
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/movie/popular',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            movies = r.json().get('results', [])[:20]
            save_cache(key, movies)
            return movies
    except Exception as e:
        logger.error(f"get_popular_movies: {e}")
    return []


def get_trending(period='week') -> list:
    key    = f"trending_{period}"
    cached = get_cache(key, expiry=3600)
    if cached:
        return cached
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/trending/all/{period}',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR'},
            timeout=10
        )
        if r.status_code == 200:
            items = r.json().get('results', [])[:15]
            save_cache(key, items)
            return items
    except Exception as e:
        logger.error(f"get_trending: {e}")
    return []


def get_top_rated(media_type='movie') -> list:
    key    = f"top_rated_{media_type}"
    cached = get_cache(key, expiry=21600)
    if cached:
        return cached
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/{media_type}/top_rated',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            items = r.json().get('results', [])[:20]
            save_cache(key, items)
            return items
    except Exception as e:
        logger.error(f"get_top_rated: {e}")
    return []


def get_top250_movies(page_num: int = 0) -> dict:
    """
    250 فیلم برتر IMDB از طریق TMDB
    هر صفحه 20 فیلم - 13 صفحه = 250+ فیلم
    """
    tmdb_page = (page_num // 20) + 1  # صفحه TMDB
    key       = f"top250_page_{tmdb_page}"
    cached    = get_cache(key, expiry=86400)
    if cached:
        return cached

    try:
        r = requests.get(
            'https://api.themoviedb.org/3/movie/top_rated',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'page': tmdb_page},
            timeout=12
        )
        if r.status_code == 200:
            data = r.json()
            result = {
                'items':       data.get('results', []),
                'total_pages': min(data.get('total_pages', 1), 13),
                'tmdb_page':   tmdb_page,
                'ts':          time.time()
            }
            save_cache(key, result)
            return result
    except Exception as e:
        logger.error(f"get_top250_movies: {e}")
    return {'items': [], 'total_pages': 1, 'tmdb_page': 1}


def get_similar_items(item_id, item_type: str) -> list:
    item_id = str(item_id)
    key     = f"similar_{item_type}_{item_id}"
    cached  = get_cache(key, expiry=86400)
    if cached:
        return cached
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/{item_type}/{item_id}/similar',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            items = [x for x in r.json().get('results', []) if x.get('id') and (x.get('title') or x.get('name'))]
            if items:
                save_cache(key, items[:20])
                return items[:20]
    except Exception as e:
        logger.error(f"get_similar_items: {e}")
    return get_popular_movies()


# ─────────────────────────────────────────────────────────────
# Bookmark & Rating
# ─────────────────────────────────────────────────────────────
def is_bookmarked(user_id: int, item_id, item_type: str) -> bool:
    try:
        with get_conn() as conn:
            r = conn.execute(
                'SELECT 1 FROM user_bookmarks WHERE user_id=? AND item_id=? AND item_type=?',
                (user_id, str(item_id), item_type)
            ).fetchone()
        return r is not None
    except Exception:
        return False


def add_bookmark(user_id: int, item_id, item_type: str) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO user_bookmarks (user_id,item_id,item_type,created_at) VALUES (?,?,?,?)',
                (user_id, str(item_id), item_type, _now())
            )
        log_user_activity(user_id, 'bookmark', item_id, item_type)
        return True
    except Exception as e:
        logger.error(f"add_bookmark: {e}")
        return False


def remove_bookmark(user_id: int, item_id, item_type: str) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                'DELETE FROM user_bookmarks WHERE user_id=? AND item_id=? AND item_type=?',
                (user_id, str(item_id), item_type)
            )
        return True
    except Exception as e:
        logger.error(f"remove_bookmark: {e}")
        return False


def get_user_rating(user_id: int, item_id, item_type: str) -> int | None:
    try:
        with get_conn() as conn:
            r = conn.execute(
                'SELECT rating FROM user_ratings WHERE user_id=? AND item_id=? AND item_type=?',
                (user_id, str(item_id), item_type)
            ).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def add_rating(user_id: int, item_id, item_type: str, rating: int) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO user_ratings (user_id,item_id,item_type,rating,created_at) VALUES (?,?,?,?,?)',
                (user_id, str(item_id), item_type, rating, _now())
            )
        log_user_activity(user_id, 'rating', item_id, item_type)
        return True
    except Exception as e:
        logger.error(f"add_rating: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Preferences
# ─────────────────────────────────────────────────────────────
_genre_map = {
    'علمی‌تخیلی': 878, 'اکشن': 28, 'ماجراجویی': 12, 'درام': 18,
    'کمدی': 35, 'ترسناک': 27, 'رمانتیک': 10749, 'فانتزی': 14,
    'انیمیشن': 16, 'معمایی': 9648, 'جنایی': 80, 'مستند': 99,
    'تاریخی': 36, 'جنگی': 10752, 'موسیقی': 10402, 'خانوادگی': 10751,
    'وسترن': 37, 'هیجان‌انگیز': 53,
}


def _movies_by_genre(genre_name: str) -> list:
    key = f"genre_{genre_name}"
    cached = get_cache(key, expiry=3600)
    if cached:
        return cached
    gid = _genre_map.get(genre_name, 28)
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/discover/movie',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR',
                    'with_genres': gid, 'sort_by': 'popularity.desc', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            items = r.json().get('results', [])[:15]
            save_cache(key, items)
            return items
    except Exception:
        pass
    return []


def _find_person_id(name: str) -> str | None:
    key = f"pid_{name}"
    cached = get_cache(key, expiry=86400)
    if cached:
        return cached
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/search/person',
            params={'api_key': TMDB_API_KEY, 'query': name, 'language': 'fa-IR'},
            timeout=10
        )
        if r.status_code == 200:
            results = r.json().get('results', [])
            if results:
                pid = str(results[0]['id'])
                save_cache(key, pid)
                return pid
    except Exception:
        pass
    return None


def analyze_preferences_async(user_id: int):
    t = threading.Thread(target=_do_analyze_preferences, args=(user_id,), daemon=True)
    t.start()


def _do_analyze_preferences(user_id: int):
    try:
        with get_conn() as conn:
            rows = conn.execute('''
                SELECT item_id, item_type FROM user_bookmarks WHERE user_id=?
                UNION
                SELECT item_id, item_type FROM user_ratings WHERE user_id=? AND rating>=4
            ''', (user_id, user_id)).fetchall()

        genres_c = Counter()
        directors_c = Counter()
        actors_c = Counter()

        for item_id, item_type in rows:
            d = get_details(item_id, item_type)
            if not d:
                continue
            for g in d.get('genres', []):
                genres_c[g['name']] += 1
            for p in d.get('credits', {}).get('crew', []):
                if p.get('job') == 'Director':
                    directors_c[p['name']] += 1
            for p in d.get('credits', {}).get('cast', [])[:10]:
                actors_c[p['name']] += 1

        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO user_preferences (user_id,favorite_genres,favorite_directors,favorite_actors,last_analysis,created_at) VALUES (?,?,?,?,?,?)',
                (user_id, json.dumps(dict(genres_c.most_common(10))),
                 json.dumps(dict(directors_c.most_common(10))),
                 json.dumps(dict(actors_c.most_common(15))),
                 _now(), _now())
            )
    except Exception as e:
        logger.error(f"_do_analyze_preferences: {e}")


def get_preferences(user_id: int) -> dict:
    try:
        with get_conn() as conn:
            r = conn.execute(
                'SELECT favorite_genres,favorite_directors,favorite_actors FROM user_preferences WHERE user_id=?',
                (user_id,)
            ).fetchone()
        if r:
            return {
                'genres':    json.loads(r[0]) if r[0] else {},
                'directors': json.loads(r[1]) if r[1] else {},
                'actors':    json.loads(r[2]) if r[2] else {},
            }
    except Exception:
        pass
    return {'genres': {}, 'directors': {}, 'actors': {}}


def get_user_stats(user_id: int) -> dict:
    try:
        thirty_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            bm    = conn.execute('SELECT COUNT(*) FROM user_bookmarks WHERE user_id=?', (user_id,)).fetchone()[0]
            rat   = conn.execute('SELECT COUNT(*) FROM user_ratings WHERE user_id=?', (user_id,)).fetchone()[0]
            total = conn.execute('SELECT COUNT(*) FROM user_activity_logs WHERE user_id=?', (user_id,)).fetchone()[0]
            mon   = conn.execute('SELECT COUNT(*) FROM user_activity_logs WHERE user_id=? AND timestamp>?', (user_id, thirty_ago)).fetchone()[0]
            avg_r = conn.execute('SELECT AVG(rating) FROM user_ratings WHERE user_id=?', (user_id,)).fetchone()[0]
        return {
            'bookmarks_count': bm, 'ratings_count': rat,
            'total_activities': total, 'monthly_activities': mon,
            'avg_rating': round(avg_r, 1) if avg_r else 0,
            'activity_per_day': round(mon / 30, 1) if mon > 0 else 0,
        }
    except Exception as e:
        logger.error(f"get_user_stats: {e}")
        return {}


def get_smart_recommendations(user_id: int) -> list:
    prefs = get_preferences(user_id)
    recs, seen = [], set()

    def add(items):
        for item in items:
            iid = item.get('id')
            if iid and iid not in seen:
                recs.append(item)
                seen.add(iid)

    for genre in list(prefs['genres'].keys())[:3]:
        add(_movies_by_genre(genre)[:3])
    for director in list(prefs['directors'].keys())[:2]:
        pid = _find_person_id(director)
        if pid:
            d = get_details(pid, 'person')
            if d:
                movies = sorted(d.get('combined_credits', {}).get('cast', []),
                                key=lambda x: x.get('popularity', 0), reverse=True)
                add(movies[:2])
    return recs[:10]


# ─────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────
def create_notification(user_id: int, ntype: str, title: str, message: str, item_id=None, item_type=None):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT INTO user_notifications (user_id,notification_type,title,message,item_id,item_type,created_at) VALUES (?,?,?,?,?,?,?)',
                (user_id, ntype, title, message, str(item_id) if item_id else None, item_type, _now())
            )
    except Exception as e:
        logger.error(f"create_notification: {e}")


def get_unread_notifications(user_id: int) -> list:
    try:
        with get_conn() as conn:
            rows = conn.execute(
                'SELECT id,notification_type,title,message,item_id,item_type,created_at FROM user_notifications WHERE user_id=? AND is_read=0 ORDER BY created_at DESC LIMIT 10',
                (user_id,)
            ).fetchall()
        return [{'id': r[0], 'type': r[1], 'title': r[2], 'message': r[3],
                 'item_id': r[4], 'item_type': r[5], 'created_at': r[6]} for r in rows]
    except Exception:
        return []


def mark_all_read(user_id: int):
    try:
        with get_conn() as conn:
            conn.execute('UPDATE user_notifications SET is_read=1 WHERE user_id=?', (user_id,))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# زیرنویس - OpenSubtitles REST API (واقعی)
# ─────────────────────────────────────────────────────────────
_opensub_token: str | None = None
_opensub_token_time: float = 0


def _get_opensub_token() -> str | None:
    """دریافت token از OpenSubtitles"""
    global _opensub_token, _opensub_token_time
    if not OPENSUB_API_KEY:
        return None
    # refresh هر 12 ساعت
    if _opensub_token and time.time() - _opensub_token_time < 43200:
        return _opensub_token
    try:
        r = requests.post(
            'https://api.opensubtitles.com/api/v1/login',
            json={'username': '', 'password': ''},
            headers={
                'Api-Key': OPENSUB_API_KEY,
                'Content-Type': 'application/json',
                'User-Agent': OPENSUB_APP_NAME,
            },
            timeout=10
        )
        if r.status_code == 200:
            _opensub_token     = r.json().get('token')
            _opensub_token_time = time.time()
            return _opensub_token
    except Exception as e:
        logger.warning(f"opensub login: {e}")
    return None


def search_subtitles_opensub(title: str, year: str = '', lang: str = 'fa',
                              item_type: str = 'movie', imdb_id: str = '') -> list:
    """جستجوی زیرنویس از OpenSubtitles.com API"""
    if not OPENSUB_API_KEY:
        return _subtitle_fallback_links(title, year)

    token = _get_opensub_token()
    headers = {
        'Api-Key':      OPENSUB_API_KEY,
        'User-Agent':   OPENSUB_APP_NAME,
        'Content-Type': 'application/json',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'

    params = {
        'query':     title,
        'languages': 'per' if lang == 'fa' else 'en',
        'type':      'movie' if item_type == 'movie' else 'episode',
    }
    if year:
        params['year'] = year
    if imdb_id:
        params['imdb_id'] = imdb_id.replace('tt', '')

    try:
        r = requests.get(
            'https://api.opensubtitles.com/api/v1/subtitles',
            params=params,
            headers=headers,
            timeout=12
        )
        if r.status_code == 200:
            data = r.json().get('data', [])
            subs = []
            for item in data[:8]:
                attrs     = item.get('attributes', {})
                files     = attrs.get('files', [{}])
                file_id   = files[0].get('file_id', 0) if files else 0
                lng       = attrs.get('language', 'en')
                is_fa     = (lng in ('fa', 'per'))
                subs.append({
                    'id':        str(item.get('id', '')),
                    'file_id':   file_id,
                    'lang':      'fa' if is_fa else 'en',
                    'lang_name': 'فارسی' if is_fa else 'انگلیسی',
                    'quality':   attrs.get('release', '').split('.')[0][:20] or 'نامشخص',
                    'source':    'OpenSubtitles',
                    'downloads': attrs.get('download_count', 0),
                    'uploader':  attrs.get('uploader', {}).get('name', ''),
                    'dl_url':    None,  # با API دانلود میشه
                    'url':       f"https://www.opensubtitles.com/fa/subtitles/{item.get('id','')}",
                })
            if subs:
                return subs
    except Exception as e:
        logger.warning(f"search_subtitles_opensub: {e}")

    return _subtitle_fallback_links(title, year)


def _subtitle_fallback_links(title: str, year: str = '') -> list:
    """لینک‌های جایگزین وقتی API نداشت"""
    encoded = requests.utils.quote(title)
    year_q  = f"+{year}" if year else ""
    return [
        {
            'id': 'fa_1', 'file_id': 0,
            'lang': 'fa', 'lang_name': 'فارسی',
            'quality': '1080p/720p', 'source': 'Subscene',
            'downloads': 0, 'uploader': '',
            'dl_url': None,
            'url': f'https://subscene.com/subtitles/searchbytitle?query={encoded}{year_q}',
        },
        {
            'id': 'fa_2', 'file_id': 0,
            'lang': 'fa', 'lang_name': 'فارسی',
            'quality': 'BluRay', 'source': 'Titr.tv',
            'downloads': 0, 'uploader': '',
            'dl_url': None,
            'url': f'https://titr.tv/?s={encoded}',
        },
        {
            'id': 'en_1', 'file_id': 0,
            'lang': 'en', 'lang_name': 'انگلیسی',
            'quality': '1080p/720p', 'source': 'OpenSubtitles',
            'downloads': 0, 'uploader': '',
            'dl_url': None,
            'url': f'https://www.opensubtitles.org/en/search/sublanguageid-eng/moviename-{encoded}',
        },
        {
            'id': 'fa_3', 'file_id': 0,
            'lang': 'fa', 'lang_name': 'فارسی',
            'quality': 'WEB-DL', 'source': 'Subtitle.ir',
            'downloads': 0, 'uploader': '',
            'dl_url': None,
            'url': f'https://subtitle.ir/?s={encoded}',
        },
    ]


def download_subtitle_via_api(file_id: int, title: str, lang: str) -> bytes | None:
    """دانلود فایل زیرنویس از OpenSubtitles API"""
    if not file_id or not OPENSUB_API_KEY:
        return None
    token = _get_opensub_token()
    headers = {
        'Api-Key':      OPENSUB_API_KEY,
        'User-Agent':   OPENSUB_APP_NAME,
        'Content-Type': 'application/json',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        r = requests.post(
            'https://api.opensubtitles.com/api/v1/download',
            json={'file_id': file_id},
            headers=headers,
            timeout=15
        )
        if r.status_code == 200:
            link = r.json().get('link')
            if link:
                dl_r = requests.get(link, timeout=20)
                if dl_r.status_code == 200:
                    return dl_r.content
    except Exception as e:
        logger.warning(f"download_subtitle_via_api: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# پیام‌ساز اصلی - طراحی جدید
# ─────────────────────────────────────────────────────────────


def get_now_playing() -> list:
    """فیلم‌هایی که الان در سینما هستند"""
    key = "now_playing_v1"
    cached = get_cache(key, expiry=10800)
    if cached:
        return cached
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/movie/now_playing',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            items = r.json().get('results', [])[:15]
            save_cache(key, items)
            return items
    except Exception as e:
        logger.error(f"get_now_playing: {e}")
    return []


def get_upcoming_movies() -> list:
    """فیلم‌های در راه"""
    key = "upcoming_v1"
    cached = get_cache(key, expiry=21600)
    if cached:
        return cached
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/movie/upcoming',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            items = r.json().get('results', [])[:15]
            save_cache(key, items)
            return items
    except Exception as e:
        logger.error(f"get_upcoming_movies: {e}")
    return []


def get_top_tv_shows() -> list:
    """برترین سریال‌ها"""
    key = "top_tv_v1"
    cached = get_cache(key, expiry=21600)
    if cached:
        return cached
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/tv/top_rated',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            items = r.json().get('results', [])[:15]
            save_cache(key, items)
            return items
    except Exception as e:
        logger.error(f"get_top_tv_shows: {e}")
    return []


def get_trending_daily() -> list:
    """ترند امروز"""
    key = "trending_day"
    cached = get_cache(key, expiry=1800)
    if cached:
        return cached
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/trending/all/day',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR'},
            timeout=10
        )
        if r.status_code == 200:
            items = r.json().get('results', [])[:15]
            save_cache(key, items)
            return items
    except Exception as e:
        logger.error(f"get_trending_daily: {e}")
    return []


def search_person_movies(person_name: str) -> tuple[dict | None, list]:
    """جستجوی فیلم‌های یک بازیگر/کارگردان"""
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/search/person',
            params={'api_key': TMDB_API_KEY, 'query': person_name, 'language': 'fa-IR'},
            timeout=10
        )
        if r.status_code == 200:
            results = r.json().get('results', [])
            if results:
                person = results[0]
                pid    = str(person['id'])
                d      = get_details(pid, 'person')
                movies = []
                if d:
                    all_credits = d.get('combined_credits', {}).get('cast', [])
                    movies = sorted(all_credits, key=lambda x: x.get('popularity', 0), reverse=True)[:15]
                return person, movies
    except Exception as e:
        logger.error(f"search_person_movies: {e}")
    return None, []


def get_collection_info(collection_id) -> dict | None:
    """اطلاعات مجموعه فیلم (مثلاً Marvel, Harry Potter)"""
    key = f"collection_{collection_id}"
    cached = get_cache(key, expiry=86400)
    if cached:
        return cached
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/collection/{collection_id}',
            params={'api_key': TMDB_API_KEY, 'language': 'fa-IR'},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            save_cache(key, data)
            return data
    except Exception as e:
        logger.error(f"get_collection_info: {e}")
    return None


def get_movie_reviews(item_id: str, item_type: str = 'movie') -> list:
    """نقدهای فیلم از TMDB"""
    key = f"reviews_{item_type}_{item_id}"
    cached = get_cache(key, expiry=86400)
    if cached:
        return cached
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/{item_type}/{item_id}/reviews',
            params={'api_key': TMDB_API_KEY, 'language': 'en-US', 'page': 1},
            timeout=10
        )
        if r.status_code == 200:
            reviews = r.json().get('results', [])[:5]
            save_cache(key, reviews)
            return reviews
    except Exception as e:
        logger.error(f"get_movie_reviews: {e}")
    return []


def build_collection_message(collection_id, user_id: int | None = None) -> tuple[str, types.InlineKeyboardMarkup]:
    """نمایش مجموعه فیلم"""
    data = get_collection_info(collection_id)
    if not data:
        return "❌ مجموعه یافت نشد", _nav_markup()

    uname   = get_bot_username()
    name    = data.get('name', '؟')
    overview= data.get('overview', '')
    poster  = data.get('poster_path')
    parts   = data.get('parts', [])
    parts   = sorted(parts, key=lambda x: x.get('release_date', ''), reverse=False)

    txt = ""
    if poster:
        txt += f"<a href='https://image.tmdb.org/t/p/w780{poster}'>&#8205;</a>"

    txt += f"🎬 <b>مجموعه: {safe_text(name)}</b>\n"
    txt += f"📦 تعداد فیلم‌ها: {len(parts)}\n\n"

    if overview:
        txt += f"<blockquote>{safe_text(overview[:400])}</blockquote>\n\n"

    txt += "<b>فیلم‌های مجموعه:</b>\n"
    for i, p in enumerate(parts, 1):
        ptitle = p.get('title', '؟')
        pyear  = (p.get('release_date', ''))[:4]
        pvote  = p.get('vote_average', 0) or 0
        pid    = str(p.get('id', ''))
        plink  = f"<a href='https://t.me/{uname}?start=movie_{pid}'>{safe_text(ptitle)}</a>"
        txt   += f"{i}. {plink}"
        if pyear:
            txt += f" ({pyear})"
        if pvote:
            txt += f" ⭐{pvote:.1f}"
        txt += "\n"
    return txt, _nav_markup()


def build_reviews_message(item_id: str, item_type: str, title: str) -> tuple[str, types.InlineKeyboardMarkup]:
    """نمایش نقدها"""
    reviews  = get_movie_reviews(item_id, item_type)
    back_btn = [types.InlineKeyboardButton("\U0001f519 برگشت به فیلم", callback_data=f"back_item:{item_type}:{item_id}")]
    if not reviews:
        return (
            f"\U0001f4dd <b>نقدها: {safe_text(title)}</b>\n\n\U0001f4ed هنوز نقدی ثبت نشده.",
            _nav_markup([back_btn])
        )
    txt = f"\U0001f4dd <b>نقدهای {safe_text(title)}</b>\n\n"
    for rv in reviews:
        author     = rv.get("author", "ناشناس")
        rv_content = rv.get("content", "")[:300]
        rating_d   = rv.get("author_details", {})
        rv_rating  = rating_d.get("rating")
        txt += "\u2500" * 25 + "\n"
        txt += f"\U0001f464 <b>{safe_text(author)}</b>"
        if rv_rating:
            txt += f"  \u2b50{rv_rating}/10"
        txt += f"\n<blockquote>{safe_text(rv_content)}...</blockquote>\n\n"
    mk = _nav_markup([back_btn])
    return txt, mk


def _item_type_from_data(data: dict) -> str:
    mt = data.get('media_type')
    if mt in ('movie', 'tv'):
        return mt
    if 'title' in data and 'release_date' in data:
        return 'movie'
    if 'name' in data and 'first_air_date' in data:
        return 'tv'
    return 'movie'


def build_movie_message(data: dict, user_id: int | None = None) -> tuple[str, types.InlineKeyboardMarkup, str | None]:
    if not data:
        return "❌ اطلاعات یافت نشد", types.InlineKeyboardMarkup(), None

    try:
        item_type = _item_type_from_data(data)
        item_id   = str(data.get('id', ''))

        if user_id:
            log_user_activity(user_id, 'view', item_id, item_type)
            push_history(user_id, item_type, item_id)

        title       = data.get('title') or data.get('name', 'بدون عنوان')
        orig_title  = data.get('original_title') or data.get('original_name', '')
        poster_path = data.get('poster_path')
        overview    = data.get('overview') or 'خلاصه‌ای موجود نیست.'
        rel_date    = data.get('release_date') or data.get('first_air_date', '')
        year        = rel_date[:4] if rel_date else 'نامشخص'
        vote_avg    = data.get('vote_average', 0) or 0
        vote_cnt    = data.get('vote_count', 0) or 0
        genres      = [g['name'] for g in data.get('genres', [])]
        budget      = data.get('budget')
        revenue     = data.get('revenue')
        runtime     = data.get('runtime')
        if item_type == 'tv' and not runtime:
            rts     = data.get('episode_run_time', [])
            runtime = rts[0] if rts else None
        status      = data.get('status', '')
        lang_orig   = data.get('original_language', '')
        seasons_n   = data.get('number_of_seasons')
        episodes_n  = data.get('number_of_episodes')
        keywords_l  = [k['name'] for k in data.get('keywords', {}).get('keywords', [])
                       if k.get('name')][:5]
        omdb        = data.get('omdb', {})
        imdb_rating = omdb.get('imdbRating', 'N/A')
        rt_score    = omdb.get('Ratings', [])
        rt_pct      = next((x['Value'] for x in rt_score if 'Rotten' in x.get('Source', '')), None)
        imdb_id     = data.get('external_ids', {}).get('imdb_id', '')
        cast        = data.get('credits', {}).get('cast', [])[:10]
        crew        = data.get('credits', {}).get('crew', [])
        directors   = [p for p in crew if p.get('job') == 'Director']
        writers_l   = [p for p in crew if p.get('job') in ('Writer', 'Screenplay')][:2]
        trailer_url = get_trailer_url(data.get('videos', {}))
        user_rating = get_user_rating(user_id, item_id, item_type) if user_id else None
        bookmarked  = is_bookmarked(user_id, item_id, item_type) if user_id else False

        uname = get_bot_username()

        # ─── متن پیام ────────────────────────────────────────
        txt = ""
        if poster_path:
            txt += f"<a href='https://image.tmdb.org/t/p/w780{poster_path}'>&#8205;</a>"

        type_icon = "🎬" if item_type == 'movie' else "📺"
        lang_icons = {'en': '🇺🇸', 'fr': '🇫🇷', 'ko': '🇰🇷', 'ja': '🇯🇵', 'es': '🇪🇸',
                      'de': '🇩🇪', 'it': '🇮🇹', 'zh': '🇨🇳', 'hi': '🇮🇳', 'fa': '🇮🇷',
                      'tr': '🇹🇷', 'pt': '🇧🇷', 'ru': '🇷🇺', 'ar': '🇸🇦'}
        lang_icon = lang_icons.get(lang_orig, '🌐')

        txt += (
            f"╔══════════════════════\n"
            f"║ {type_icon} <b>{safe_text(title)}</b>\n"
        )
        if orig_title and orig_title != title:
            txt += f"║ 〔{safe_text(orig_title[:40])}〕\n"
        txt += f"╚══════════════════════\n\n"

        # اطلاعات پایه
        info_lines = []
        info_lines.append(f"📅 <b>سال:</b> {year}")
        if rel_date and len(rel_date) >= 10:
            info_lines.append(f"📆 <b>انتشار:</b> {rel_date[:10]} ({to_shamsi(rel_date)})")
        if genres:
            info_lines.append(f"🎭 <b>ژانر:</b> {' | '.join(genres[:3])}")
        if runtime:
            h, m = divmod(int(runtime), 60)
            dur_txt = f"{h}ساعت {m}دقیقه" if h else f"{m} دقیقه"
            info_lines.append(f"⏱ <b>مدت:</b> {dur_txt}")
        if item_type == 'tv':
            if seasons_n:
                info_lines.append(f"📺 <b>فصل‌ها:</b> {seasons_n} فصل")
            if episodes_n:
                info_lines.append(f"🎞 <b>قسمت‌ها:</b> {episodes_n} قسمت")
        if status:
            status_fa = {
                'Released': '✅ منتشر شده', 'Post Production': '🔧 در تولید',
                'In Production': '🎬 در تولید', 'Planned': '📋 برنامه‌ریزی',
                'Canceled': '❌ لغو شده', 'Ended': '🏁 پایان یافته',
                'Returning Series': '🔄 در حال پخش', 'Pilot': '🚀 پایلوت',
            }.get(status, safe_text(status))
            info_lines.append(f"📌 <b>وضعیت:</b> {status_fa}")
        info_lines.append(f"{lang_icon} <b>زبان:</b> {lang_orig.upper() if lang_orig else '—'}")

        txt += "\n".join(info_lines) + "\n"

        # امتیازها
        txt += "\n"
        stars_tmdb = star_bar(vote_avg)
        txt += f"⭐ <b>TMDB:</b> {vote_avg:.1f}/10  {stars_tmdb}  ({fmt(vote_cnt)} رای)\n"
        if imdb_rating != 'N/A':
            stars_imdb = star_bar(float(imdb_rating))
            txt += f"🏆 <b>IMDB:</b> {imdb_rating}/10  {stars_imdb}"
            if imdb_id:
                txt += f"  <a href='https://www.imdb.com/title/{imdb_id}'>[IMDB]</a>"
            txt += "\n"
        if rt_pct:
            txt += f"🍅 <b>Rotten Tomatoes:</b> {rt_pct}\n"
        if user_rating:
            my_stars = '⭐' * user_rating
            txt += f"💫 <b>امتیاز من:</b> {my_stars} ({user_rating}/5)\n"

        # مالی
        if budget and budget > 0:
            txt += f"\n💰 <b>بودجه:</b> ${fmt(budget)}\n"
        if revenue and revenue > 0:
            txt += f"💵 <b>فروش:</b> ${fmt(revenue)}\n"

        # کارگردان و نویسنده
        if directors:
            dir_links = []
            for d in directors[:2]:
                dir_links.append(f"<a href='https://t.me/{uname}?start=person_{d['id']}'>{safe_text(d['name'])}</a>")
            txt += f"\n🎥 <b>کارگردان:</b> {' | '.join(dir_links)}\n"
        if writers_l:
            wr_links = []
            for w in writers_l:
                wr_links.append(f"<a href='https://t.me/{uname}?start=person_{w['id']}'>{safe_text(w['name'])}</a>")
            txt += f"✍️ <b>نویسنده:</b> {' | '.join(wr_links)}\n"

        # خلاصه داستان در بلوک کپی‌پذیر
        txt += f"\n📖 <b>خلاصه داستان</b>\n"
        txt += f"<blockquote>{safe_text(overview[:700])}"
        if len(overview) > 700:
            txt += "..."
        txt += "</blockquote>\n"

        # بازیگران
        if cast:
            txt += "\n🎭 <b>بازیگران</b>\n"
            actor_parts = []
            for actor in cast[:8]:
                char  = actor.get('character', '')
                alink = f"<a href='https://t.me/{uname}?start=person_{actor['id']}'>{safe_text(actor['name'])}</a>"
                if char:
                    actor_parts.append(f"{alink} <i>({safe_text(char[:25])})</i>")
                else:
                    actor_parts.append(alink)
            txt += " • ".join(actor_parts) + "\n"

        # کلیدواژه‌ها
        if keywords_l:
            txt += f"\n🏷 <b>برچسب:</b> {' '.join(['#' + k.replace(' ', '_') for k in keywords_l])}\n"

        # لینک ربات
        txt += f"\n🔗 <a href='https://t.me/{uname}?start={item_type}_{item_id}'>اشتراک‌گذاری</a>"

        # ─── keyboard ────────────────────────────────────────
        mk = types.InlineKeyboardMarkup(row_width=2)

        bm_text = "💔 حذف از لیست" if bookmarked else "❤️ ذخیره"
        mk.row(
            types.InlineKeyboardButton(bm_text, callback_data=f"bm:{item_type}:{item_id}"),
            types.InlineKeyboardButton("⭐ امتیاز دادن", callback_data=f"rate:{item_type}:{item_id}")
        )

        row2 = []
        if trailer_url:
            with _cache_lock:
                temp_cache[f"trailer_{item_id}"] = {'url': trailer_url, 'title': title, 'ts': time.time()}
            row2.append(types.InlineKeyboardButton("▶️ تریلر", callback_data=f"trailer:{item_id}"))
        row2.append(types.InlineKeyboardButton("🔄 مشابه", callback_data=f"similar:{item_type}:{item_id}:0"))
        mk.row(*row2)

        mk.row(
            types.InlineKeyboardButton("📥 زیرنویس", callback_data=f"sub:{item_type}:{item_id}"),
            types.InlineKeyboardButton("🔍 گوگل", url="https://www.google.com/search?q=" + requests.utils.quote(str(title) + " " + str(year)))
        )

        if imdb_id:
            mk.row(types.InlineKeyboardButton("🏆 صفحه IMDB", url=f"https://www.imdb.com/title/{imdb_id}"))

        # نقدها و مجموعه
        extra_row = []
        extra_row.append(types.InlineKeyboardButton("📝 نقدها", callback_data=f"reviews:{item_type}:{item_id}"))
        collection = data.get('belongs_to_collection')
        if collection:
            extra_row.append(types.InlineKeyboardButton("📦 مجموعه", callback_data=f"collection:{collection['id']}"))
        mk.row(*extra_row)

        mk.row(
            types.InlineKeyboardButton("🔙 برگشت", callback_data="go_back"),
            types.InlineKeyboardButton("🏠 خانه", callback_data="home")
        )

        thumb = f"https://image.tmdb.org/t/p/w300{poster_path}" if poster_path else None
        return txt, mk, thumb

    except Exception as e:
        logger.error(f"build_movie_message: {e}")
        return "❌ خطا در ساخت پیام", types.InlineKeyboardMarkup(), None


def build_person_message(data: dict, user_id: int | None = None) -> tuple[str, types.InlineKeyboardMarkup, str | None]:
    if not data:
        return "❌ اطلاعات یافت نشد", types.InlineKeyboardMarkup(), None

    try:
        person_id    = str(data.get('id', ''))
        if user_id:
            push_history(user_id, 'person', person_id)

        name         = data.get('name', 'بدون نام')
        profile_path = data.get('profile_path')
        biography    = data.get('biography') or 'بیوگرافی موجود نیست.'
        birthday     = data.get('birthday', '')
        deathday     = data.get('deathday', '')
        birthplace   = data.get('place_of_birth', '')
        dept         = data.get('known_for_department', '')
        popularity   = data.get('popularity', 0)
        known_for    = sorted(
            data.get('combined_credits', {}).get('cast', []),
            key=lambda x: x.get('popularity', 0), reverse=True
        )[:10]
        imdb_id      = data.get('external_ids', {}).get('imdb_id')
        uname        = get_bot_username()

        txt = ""
        if profile_path:
            txt += f"<a href='https://image.tmdb.org/t/p/w780{profile_path}'>&#8205;</a>"

        dept_fa = {
            'Acting': 'بازیگری', 'Directing': 'کارگردانی', 'Writing': 'نویسندگی',
            'Production': 'تهیه‌کنندگی', 'Camera': 'فیلمبرداری',
            'Art': 'هنر', 'Sound': 'صدا', 'Editing': 'تدوین',
        }.get(dept, dept)

        txt += (
            f"╔══════════════════════\n"
            f"║ 👤 <b>{safe_text(name)}</b>\n"
            f"╚══════════════════════\n\n"
        )

        info_lines = []
        if dept_fa:
            info_lines.append(f"🎭 <b>تخصص:</b> {safe_text(dept_fa)}")
        if birthday:
            age_txt = ""
            if not deathday:
                try:
                    bday = datetime.strptime(birthday[:10], "%Y-%m-%d")
                    age  = (datetime.now() - bday).days // 365
                    age_txt = f" ({age} ساله)"
                except Exception:
                    pass
            info_lines.append(f"🎂 <b>تولد:</b> {birthday} / {to_shamsi(birthday)}{age_txt}")
        if deathday:
            info_lines.append(f"✝️ <b>فوت:</b> {deathday} / {to_shamsi(deathday)}")
        if birthplace:
            info_lines.append(f"🌍 <b>محل تولد:</b> {safe_text(birthplace)}")
        if popularity:
            info_lines.append(f"📈 <b>محبوبیت:</b> {popularity:.1f}")

        txt += "\n".join(info_lines) + "\n"

        # بیوگرافی در بلوک کپی‌پذیر
        txt += f"\n📖 <b>بیوگرافی</b>\n"
        txt += f"<blockquote>{safe_text(biography[:800])}"
        if len(biography) > 800:
            txt += "..."
        txt += "</blockquote>\n"

        # آثار برجسته
        if known_for:
            txt += f"\n🎬 <b>آثار برجسته</b>\n"
            for w in known_for[:8]:
                wtitle = w.get('title') or w.get('name', 'بدون عنوان')
                wyear  = (w.get('release_date') or w.get('first_air_date', ''))[:4]
                wtype  = w.get('media_type', 'movie')
                wvote  = w.get('vote_average', 0) or 0
                wid = str(w["id"]); wlink  = f"<a href='https://t.me/{uname}?start={wtype}_{wid}'>{safe_text(wtitle)}</a>"
                role   = w.get('character', '')
                icon   = '🎬' if wtype == 'movie' else '📺'
                line   = f"{icon} {wlink}"
                if wyear:
                    line += f" ({wyear})"
                if wvote:
                    line += f" ⭐{wvote:.1f}"
                if role:
                    line += f"\n    <i>نقش: {safe_text(role[:30])}</i>"
                txt += line + "\n"

        mk = types.InlineKeyboardMarkup()
        if imdb_id:
            mk.row(types.InlineKeyboardButton("🏆 IMDB", url=f"https://www.imdb.com/name/{imdb_id}"))
        mk.row(
            types.InlineKeyboardButton("🔙 برگشت", callback_data="go_back"),
            types.InlineKeyboardButton("🏠 خانه", callback_data="home")
        )

        thumb = f"https://image.tmdb.org/t/p/w300{profile_path}" if profile_path else None
        return txt, mk, thumb

    except Exception as e:
        logger.error(f"build_person_message: {e}")
        return "❌ خطا در ساخت پیام", types.InlineKeyboardMarkup(), None


# ─────────────────────────────────────────────────────────────
# پیام‌سازهای صفحات
# ─────────────────────────────────────────────────────────────
def _nav_markup(extra_rows: list = None) -> types.InlineKeyboardMarkup:
    mk = types.InlineKeyboardMarkup()
    if extra_rows:
        for row in extra_rows:
            mk.row(*row)
    mk.row(
        types.InlineKeyboardButton("🔙 برگشت", callback_data="go_back"),
        types.InlineKeyboardButton("🏠 خانه",  callback_data="home")
    )
    return mk


def build_subtitle_message(item_id: str, item_type: str, title: str, year: str = '',
                            imdb_id: str = '') -> tuple[str, types.InlineKeyboardMarkup]:
    """ساخت پیام زیرنویس - جستجوی واقعی"""
    subs_fa = search_subtitles_opensub(title, year, 'fa', item_type, imdb_id)
    subs_en = search_subtitles_opensub(title, year, 'en', item_type, imdb_id)
    all_subs = subs_fa + [s for s in subs_en if s['lang'] == 'en']

    with _cache_lock:
        temp_cache[f"subs_{item_id}"] = {
            'subs':      all_subs,
            'title':     title,
            'year':      year,
            'item_type': item_type,
            'ts':        time.time()
        }

    txt  = f"📥 <b>زیرنویس: {safe_text(title)}</b>"
    if year:
        txt += f" ({year})"
    txt += "\n\n"
    txt += "┌─────────────────────────\n"
    txt += f"│ 🔍 یافت شده: {len(all_subs)} زیرنویس\n"
    txt += "└─────────────────────────\n\n"
    txt += "یک زیرنویس انتخاب کنید تا فایل برایتان ارسال شود:\n"

    mk = types.InlineKeyboardMarkup()
    for s in all_subs:
        lang_icon = '🇮🇷' if s['lang'] == 'fa' else '🇬🇧'
        dl_icon   = '📥' if (s['file_id'] or s['dl_url']) else '🔗'
        btn_text  = f"{lang_icon} {s['quality']} ‹ {s['source']} › {dl_icon}"
        if s.get('downloads') and int(s['downloads']) > 0:
            btn_text += f" {fmt(s['downloads'])}⬇"
        sub_cb = f"subdl:{item_id}:{s['id'][:40]}"
        if len(sub_cb.encode()) > 64:
            sub_cb = f"subdl:{item_id}:{s['id'][:20]}"
        mk.row(types.InlineKeyboardButton(btn_text, callback_data=sub_cb))

    mk.row(
        types.InlineKeyboardButton("🔙 برگشت", callback_data=f"back_item:{item_type}:{item_id}"),
        types.InlineKeyboardButton("🏠 خانه",  callback_data="home")
    )
    return txt, mk


def build_bookmarks_message(user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    uname = get_bot_username()
    try:
        with get_conn() as conn:
            rows = conn.execute(
                'SELECT item_id, item_type, created_at FROM user_bookmarks WHERE user_id=? ORDER BY created_at DESC LIMIT 20',
                (user_id,)
            ).fetchall()

        if not rows:
            return ("❤️ <b>علاقه‌مندی‌ها</b>\n\n"
                    "📭 هنوز چیزی ذخیره نکردید.\n\n"
                    "💡 برای ذخیره، در صفحه هر فیلم روی ❤️ بزنید."), _nav_markup()

        txt  = "❤️ <b>علاقه‌مندی‌های شما</b>\n"
        txt += f"({len(rows)} مورد)\n\n"
        for i, (iid, itype, cat) in enumerate(rows, 1):
            d = get_details(iid, itype)
            if d:
                ititle = d.get('title') or d.get('name', '؟')
                year   = (d.get('release_date') or d.get('first_air_date', ''))[:4]
                vote   = d.get('vote_average', 0) or 0
                iicon  = '🎬' if itype == 'movie' else '📺' if itype == 'tv' else '👤'
                link   = f"<a href='https://t.me/{uname}?start={itype}_{iid}'>{safe_text(ititle)}</a>"
                txt += f"{i}. {iicon} {link}"
                if year:
                    txt += f" ({year})"
                if vote:
                    txt += f" ⭐{vote:.1f}"
                txt += "\n"
        return txt, _nav_markup()
    except Exception as e:
        logger.error(f"build_bookmarks_message: {e}")
        return "❌ خطا", _nav_markup()


def build_ratings_message(user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    uname = get_bot_username()
    try:
        with get_conn() as conn:
            rows = conn.execute(
                'SELECT item_id, item_type, rating, created_at FROM user_ratings WHERE user_id=? ORDER BY created_at DESC LIMIT 20',
                (user_id,)
            ).fetchall()

        if not rows:
            return "⭐ <b>امتیازها</b>\n\n📭 هنوز امتیازی نداده‌اید.", _nav_markup()

        txt = "⭐ <b>امتیازهای شما</b>\n\n"
        for i, (iid, itype, rating, cat) in enumerate(rows, 1):
            d = get_details(iid, itype)
            if d:
                ititle = d.get('title') or d.get('name', '؟')
                year   = (d.get('release_date') or d.get('first_air_date', ''))[:4]
                link   = f"<a href='https://t.me/{uname}?start={itype}_{iid}'>{safe_text(ititle)}</a>"
                stars  = '★' * rating + '☆' * (5 - rating)
                txt   += f"{i}. {link}"
                if year:
                    txt += f" ({year})"
                txt += f"  {stars}\n"
        return txt, _nav_markup()
    except Exception as e:
        logger.error(f"build_ratings_message: {e}")
        return "❌ خطا", _nav_markup()


def build_stats_message(user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    stats = get_user_stats(user_id)
    prefs = get_preferences(user_id)

    txt  = "📊 <b>آمار و تحلیل شما</b>\n\n"
    txt += "┌─ فعالیت کلی ──────────────\n"
    txt += f"│ 🔍 جستجوها: {fmt(stats.get('total_activities', 0))}\n"
    txt += f"│ ❤️ علاقه‌مندی‌ها: {fmt(stats.get('bookmarks_count', 0))}\n"
    txt += f"│ ⭐ امتیازها: {fmt(stats.get('ratings_count', 0))}\n"
    txt += f"│ 📅 فعالیت ماه: {fmt(stats.get('monthly_activities', 0))}\n"
    txt += f"│ ⏱ میانگین روزانه: {stats.get('activity_per_day', 0)}\n"
    txt += f"│ 💫 میانگین امتیاز: {stats.get('avg_rating', 0)}/5\n"
    txt += "└────────────────────────────\n"

    if prefs['genres']:
        txt += "\n🎭 <b>ژانرهای محبوب</b>\n"
        total_g = sum(prefs['genres'].values()) or 1
        for i, (g, cnt) in enumerate(list(prefs['genres'].items())[:5], 1):
            pct    = cnt / total_g * 100
            bar    = '█' * int(pct / 10) + '░' * (10 - int(pct / 10))
            txt   += f"{i}. {safe_text(g)} {bar} {pct:.1f}٪\n"

    if prefs['directors']:
        txt += "\n🎬 <b>کارگردانان محبوب</b>\n"
        for i, (d, cnt) in enumerate(list(prefs['directors'].items())[:3], 1):
            txt += f"{i}. {safe_text(d)} ({cnt} اثر)\n"

    if prefs['actors']:
        txt += "\n👤 <b>بازیگران محبوب</b>\n"
        for i, (a, cnt) in enumerate(list(prefs['actors'].items())[:5], 1):
            txt += f"{i}. {safe_text(a)} ({cnt} اثر)\n"

    return txt, _nav_markup()


def build_recommendations_message(user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    uname = get_bot_username()
    recs  = get_smart_recommendations(user_id)
    prefs = get_preferences(user_id)

    if not recs:
        return (
            "🎯 <b>پیشنهادات هوشمند</b>\n\n"
            "📭 هنوز اطلاعات کافی نداریم.\n\n"
            "💡 برای فعال شدن پیشنهادات:\n"
            "• چند فیلم جستجو کنید\n"
            "• فیلم‌های دوست داشتنی را ذخیره کنید\n"
            "• به فیلم‌ها امتیاز بدهید",
            _nav_markup()
        )

    txt  = "🎯 <b>پیشنهادات هوشمند برای شما</b>\n"
    txt += "<i>بر اساس سلیقه و تاریخچه شما</i>\n\n"
    if prefs['genres']:
        top  = list(prefs['genres'].keys())[:3]
        txt += f"🎭 علایق: {' | '.join(top)}\n\n"

    for i, m in enumerate(recs, 1):
        ititle = m.get('title') or m.get('name', '؟')
        year   = (m.get('release_date') or m.get('first_air_date', ''))[:4]
        va     = m.get('vote_average', 0)
        mtype  = m.get('media_type', 'movie')
        iicon  = '🎬' if mtype == 'movie' else '📺'
        mid = str(m["id"]); link   = f"<a href='https://t.me/{uname}?start={mtype}_{mid}'>{safe_text(ititle)}</a>"
        txt   += f"{i}. {iicon} {link}"
        if year:
            txt += f" ({year})"
        if va:
            txt += f"  ⭐{va:.1f}"
        txt += "\n"

    return txt, _nav_markup()


def build_notifications_message(user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    uname  = get_bot_username()
    notifs = get_unread_notifications(user_id)

    if not notifs:
        return "🔔 <b>نوتیفیکیشن‌ها</b>\n\n📭 هیچ پیام خوانده‌نشده‌ای ندارید.", _nav_markup()

    txt = "🔔 <b>نوتیفیکیشن‌های جدید</b>\n\n"
    for n in notifs:
        txt += f"📌 <b>{safe_text(n['title'])}</b>\n"
        txt += f"   {safe_text(n['message'])}\n"
        if n['item_id'] and n['item_type']:
            ntype2 = n["item_type"]; nid2 = n["item_id"]; txt += f"   <a href='https://t.me/{uname}?start={ntype2}_{nid2}'>[مشاهده]</a>\n"
        txt += f"   <i>{n['created_at'][:16]}</i>\n\n"

    mark_all_read(user_id)
    return txt, _nav_markup()


# ─────────────────────────────────────────────────────────────
# لیست‌های پیمایش‌پذیر (Trending / Top Rated / Genre / Top250)
# ─────────────────────────────────────────────────────────────
def _build_list_message(items: list, title: str, list_id: str, index: int,
                         orig_cb: str) -> tuple[str, types.InlineKeyboardMarkup]:
    uname = get_bot_username()
    if not items:
        return "❌ نتیجه‌ای یافت نشد", _nav_markup()

    total = len(items)
    index = index % total
    item  = items[index]

    itype  = item.get('media_type', 'movie')
    iid    = str(item.get('id', ''))
    ititle = item.get('title') or item.get('name', '؟')
    year   = (item.get('release_date') or item.get('first_air_date', ''))[:4]
    vote   = item.get('vote_average') or 0
    desc   = (item.get('overview') or '')[:350]
    poster = item.get('poster_path')
    iicon  = {'movie': '🎬', 'tv': '📺', 'person': '👤'}.get(itype, '🎬')

    txt = ""
    if poster:
        txt += f"<a href='https://image.tmdb.org/t/p/w780{poster}'>&#8205;</a>"

    txt += f"<b>{safe_text(title)}</b>  •  {index+1}/{total}\n\n"
    txt += f"{iicon} <b>{safe_text(ititle)}</b>"
    if year:
        txt += f" ({year})"
    if vote:
        txt += f"\n{star_bar(vote)} ⭐{vote:.1f}/10"
    if desc:
        txt += f"\n\n<blockquote>{safe_text(desc)}</blockquote>"

    mk = types.InlineKeyboardMarkup()
    mk.row(types.InlineKeyboardButton("🔍 اطلاعات کامل", callback_data=f"open:{itype}:{iid}"))
    mk.row(
        types.InlineKeyboardButton("⬅️ قبلی",          callback_data=f"{orig_cb}:{(index-1)%total}"),
        types.InlineKeyboardButton(f"· {index+1}/{total} ·", callback_data="noop"),
        types.InlineKeyboardButton("بعدی ➡️",          callback_data=f"{orig_cb}:{(index+1)%total}"),
    )
    mk.row(
        types.InlineKeyboardButton("🔙 برگشت", callback_data="go_back"),
        types.InlineKeyboardButton("🏠 خانه",  callback_data="home")
    )
    return txt, mk


def _build_top250_message(global_index: int) -> tuple[str, types.InlineKeyboardMarkup]:
    """250 فیلم برتر - هر بار یک فیلم"""
    tmdb_page = (global_index // 20) + 1
    local_idx = global_index % 20
    page_data = get_top250_movies(global_index)
    items     = page_data.get('items', [])

    if not items or local_idx >= len(items):
        return "❌ فیلم یافت نشد", _nav_markup()

    # حداکثر 250 فیلم
    MAX_TOTAL = 250
    item  = items[local_idx]
    iid   = str(item.get('id', ''))
    itype = 'movie'

    ititle  = item.get('title') or item.get('name', '؟')
    orig_t  = item.get('original_title', '')
    year    = (item.get('release_date', ''))[:4]
    vote    = item.get('vote_average') or 0
    vote_n  = item.get('vote_count') or 0
    desc    = (item.get('overview') or '')[:400]
    poster  = item.get('poster_path')
    genres_l = item.get('genre_ids', [])

    txt = ""
    if poster:
        txt += f"<a href='https://image.tmdb.org/t/p/w780{poster}'>&#8205;</a>"

    rank = global_index + 1
    txt += f"🏆 <b>250 فیلم برتر IMDB</b>  •  #{rank}/{MAX_TOTAL}\n\n"
    txt += f"🎬 <b>{safe_text(ititle)}</b>"
    if orig_t and orig_t != ititle:
        txt += f"\n〔{safe_text(orig_t[:50])}〕"
    if year:
        txt += f"\n📅 {year}"
    if vote:
        txt += f"\n{star_bar(vote)} ⭐ {vote:.1f}/10  ({fmt(vote_n)} رای)"
    if desc:
        txt += f"\n\n<blockquote>{safe_text(desc)}</blockquote>"

    mk    = types.InlineKeyboardMarkup()
    mk.row(types.InlineKeyboardButton("🔍 اطلاعات کامل", callback_data=f"open:{itype}:{iid}"))

    prev_i = max(0, global_index - 1)
    next_i = min(MAX_TOTAL - 1, global_index + 1)
    nav_row = []
    if global_index > 0:
        nav_row.append(types.InlineKeyboardButton("⬅️ قبلی", callback_data=f"top250:{prev_i}"))
    nav_row.append(types.InlineKeyboardButton(f"#{rank}/{MAX_TOTAL}", callback_data="noop"))
    if global_index < MAX_TOTAL - 1:
        nav_row.append(types.InlineKeyboardButton("بعدی ➡️", callback_data=f"top250:{next_i}"))
    mk.row(*nav_row)

    # پرش سریع
    mk.row(
        types.InlineKeyboardButton("⏮ ابتدا", callback_data="top250:0"),
        types.InlineKeyboardButton("⏭ انتها", callback_data=f"top250:{MAX_TOTAL-1}")
    )
    mk.row(
        types.InlineKeyboardButton("🔙 برگشت", callback_data="go_back"),
        types.InlineKeyboardButton("🏠 خانه",  callback_data="home")
    )
    return txt, mk


# ─────────────────────────────────────────────────────────────
# پنل ادمین
# ─────────────────────────────────────────────────────────────
def build_admin_panel(user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    if not is_admin(user_id):
        return "❌ دسترسی ندارید.", types.InlineKeyboardMarkup()

    try:
        with get_conn() as conn:
            stats_row    = conn.execute('SELECT total_users,active_users,total_searches,total_bookmarks FROM bot_statistics WHERE id=1').fetchone()
            admin_count  = conn.execute('SELECT COUNT(*) FROM admin_users').fetchone()[0]
            daily_act    = conn.execute(
                'SELECT COUNT(*) FROM user_activity_logs WHERE timestamp>?',
                ((datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),)
            ).fetchone()[0]

        tu, au, ts, tb = stats_row if stats_row else (0, 0, 0, 0)

        txt  = "👑 <b>پنل مدیریت</b>\n\n"
        txt += "┌─ آمار کلی ────────────────\n"
        txt += f"│ 👥 کاربران کل: {fmt(tu)}\n"
        txt += f"│ 🔥 فعال (۳۰روز): {fmt(au)}\n"
        txt += f"│ 📈 فعالیت ۲۴ ساعت: {fmt(daily_act)}\n"
        txt += f"│ 🔍 جستجوها: {fmt(ts)}\n"
        txt += f"│ ❤️ علاقه‌مندی‌ها: {fmt(tb)}\n"
        txt += f"│ 👨‍💼 ادمین‌ها: {admin_count}\n"
        txt += "└────────────────────────────\n"

        mk = types.InlineKeyboardMarkup()
        mk.row(
            types.InlineKeyboardButton("📊 آمار دقیق",   callback_data="adm_stats"),
            types.InlineKeyboardButton("👥 کاربران",      callback_data="adm_users")
        )
        mk.row(
            types.InlineKeyboardButton("📢 همگانی",       callback_data="adm_broadcast"),
            types.InlineKeyboardButton("🎬 آمار فیلم‌ها", callback_data="adm_movies")
        )
        mk.row(
            types.InlineKeyboardButton("⭐ برترین‌ها",   callback_data="adm_top_users"),
            types.InlineKeyboardButton("⚙️ ادمین‌ها",    callback_data="adm_manage_admins")
        )
        if is_super_admin(user_id):
            mk.row(
                types.InlineKeyboardButton("🗑 پاک کش",   callback_data="adm_clear_cache"),
                types.InlineKeyboardButton("💾 بکاپ",     callback_data="adm_backup")
            )
        mk.row(types.InlineKeyboardButton("🏠 خانه", callback_data="home"))
        return txt, mk

    except Exception as e:
        logger.error(f"build_admin_panel: {e}")
        return "❌ خطا در پنل", types.InlineKeyboardMarkup()


def _adm_check(call, super_only=False) -> bool:
    uid = call.from_user.id
    ok  = is_super_admin(uid) if super_only else is_admin(uid)
    if not ok:
        bot.answer_callback_query(call.id, "❌ دسترسی ندارید!")
    return ok


def _show_admin_panel(call):
    uid     = call.from_user.id
    txt, mk = build_admin_panel(uid)
    try:
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception:
        pass


def handle_adm_stats(call):
    if not _adm_check(call):
        return
    try:
        with get_conn() as conn:
            tu   = conn.execute('SELECT COUNT(DISTINCT user_id) FROM user_activity_logs').fetchone()[0]
            bmu  = conn.execute('SELECT COUNT(DISTINCT user_id) FROM user_bookmarks').fetchone()[0]
            ratu = conn.execute('SELECT COUNT(DISTINCT user_id) FROM user_ratings').fetchone()[0]
            ts   = conn.execute('SELECT COUNT(*) FROM user_activity_logs WHERE action_type="search"').fetchone()[0]
            tb   = conn.execute('SELECT COUNT(*) FROM user_bookmarks').fetchone()[0]
            tr   = conn.execute('SELECT COUNT(*) FROM user_ratings').fetchone()[0]
            seven_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            wa   = conn.execute('SELECT COUNT(*) FROM user_activity_logs WHERE timestamp>?', (seven_ago,)).fetchone()[0]

        txt  = "📈 <b>آمار دقیق</b>\n\n"
        txt += f"👥 کاربران کل: {fmt(tu)}\n"
        txt += f"❤️ کاربران با علاقه‌مندی: {fmt(bmu)}\n"
        txt += f"⭐ کاربران با امتیاز: {fmt(ratu)}\n\n"
        txt += f"🔍 جستجوهای کل: {fmt(ts)}\n"
        txt += f"❤️ علاقه‌مندی‌ها: {fmt(tb)}\n"
        txt += f"⭐ امتیازها: {fmt(tr)}\n"
        txt += f"📅 فعالیت هفتگی: {fmt(wa)}\n"

        mk = types.InlineKeyboardMarkup()
        mk.row(types.InlineKeyboardButton("🔙 پنل", callback_data="adm_panel"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"handle_adm_stats: {e}")
        bot.answer_callback_query(call.id, "❌ خطا")


def handle_adm_users(call):
    if not _adm_check(call):
        return
    try:
        with get_conn() as conn:
            users = conn.execute('''
                SELECT u.user_id, COUNT(DISTINCT b.id) bm, COUNT(DISTINCT r.id) rt, MAX(u.timestamp) la
                FROM user_activity_logs u
                LEFT JOIN user_bookmarks b ON u.user_id=b.user_id
                LEFT JOIN user_ratings   r ON u.user_id=r.user_id
                GROUP BY u.user_id ORDER BY la DESC LIMIT 10
            ''').fetchall()

        txt = "👥 <b>کاربران اخیر</b>\n\n"
        for i, (uid, bm, rt, la) in enumerate(users, 1):
            try:
                ui    = bot.get_chat(uid)
                uname = f"@{ui.username}" if ui.username else (ui.first_name or str(uid))
            except Exception:
                uname = str(uid)
            txt += f"<b>{i}. {safe_text(uname)}</b> (<code>{uid}</code>)\n"
            txt += f"   ❤️{bm}  ⭐{rt}  ⏰{(la or '')[:16]}\n\n"

        mk = types.InlineKeyboardMarkup()
        mk.row(types.InlineKeyboardButton("🔙 پنل", callback_data="adm_panel"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"handle_adm_users: {e}")
        bot.answer_callback_query(call.id, "❌ خطا")


def handle_adm_movies(call):
    if not _adm_check(call):
        return
    try:
        with get_conn() as conn:
            popular = conn.execute(
                'SELECT item_id, item_type, COUNT(*) c FROM user_bookmarks GROUP BY item_id, item_type ORDER BY c DESC LIMIT 5'
            ).fetchall()
            top_rated = conn.execute(
                'SELECT item_id, item_type, AVG(rating) avg_r, COUNT(*) c FROM user_ratings GROUP BY item_id, item_type HAVING c>=2 ORDER BY avg_r DESC LIMIT 5'
            ).fetchall()

        txt = "🎬 <b>آمار فیلم‌ها</b>\n\n🏆 پرطرفدارترین‌ها:\n"
        for iid, itype, cnt in popular:
            d = get_details(iid, itype)
            t = (d.get('title') or d.get('name', '؟')) if d else iid
            txt += f"• {safe_text(t)} - {cnt} ❤️\n"

        txt += "\n⭐ بالاترین امتیاز:\n"
        for iid, itype, avg_r, cnt in top_rated:
            d = get_details(iid, itype)
            t = (d.get('title') or d.get('name', '؟')) if d else iid
            txt += f"• {safe_text(t)} - ⭐{avg_r:.1f} ({cnt} رای)\n"

        mk = types.InlineKeyboardMarkup()
        mk.row(types.InlineKeyboardButton("🔙 پنل", callback_data="adm_panel"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"handle_adm_movies: {e}")
        bot.answer_callback_query(call.id, "❌ خطا")


def handle_adm_top_users(call):
    if not _adm_check(call):
        return
    try:
        with get_conn() as conn:
            top_active = conn.execute(
                'SELECT user_id AS uid, COUNT(*) c FROM user_activity_logs GROUP BY uid ORDER BY c DESC LIMIT 5'
            ).fetchall()
            top_bm = conn.execute(
                'SELECT user_id AS uid, COUNT(*) c FROM user_bookmarks GROUP BY uid ORDER BY c DESC LIMIT 5'
            ).fetchall()

        txt = "🏆 <b>کاربران برتر</b>\n\n📈 فعال‌ترین:\n"
        for i, (uid, cnt) in enumerate(top_active, 1):
            try:
                ui    = bot.get_chat(uid)
                uname_str = f"@{ui.username}" if ui.username else (ui.first_name or str(uid))
            except Exception:
                uname_str = str(uid)
            txt += f"{i}. {safe_text(uname_str)} - {fmt(cnt)} فعالیت\n"

        txt += "\n❤️ بیشترین علاقه‌مندی:\n"
        for i, (uid, cnt) in enumerate(top_bm, 1):
            try:
                ui    = bot.get_chat(uid)
                uname_str = f"@{ui.username}" if ui.username else (ui.first_name or str(uid))
            except Exception:
                uname_str = str(uid)
            txt += f"{i}. {safe_text(uname_str)} - {fmt(cnt)} ❤️\n"

        mk = types.InlineKeyboardMarkup()
        mk.row(types.InlineKeyboardButton("🔙 پنل", callback_data="adm_panel"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"handle_adm_top_users: {e}")
        bot.answer_callback_query(call.id, "❌ خطا")


def handle_adm_manage_admins(call):
    if not _adm_check(call, super_only=True):
        return
    try:
        with get_conn() as conn:
            admins = conn.execute('SELECT user_id, username, first_name, access_level FROM admin_users').fetchall()

        txt = "👑 <b>مدیریت ادمین‌ها</b>\n\n"
        for aid, ausername, afname, alevel in admins:
            icon  = "🦸" if alevel == 'super_admin' else "👨‍💼"
            txt  += f"{icon} {safe_text(afname or 'بدون نام')}"
            if ausername:
                txt += f" (@{ausername})"
            txt += f" — {alevel}\n"

        txt += "\n💡 <i>/addadmin ID یا /removeadmin ID</i>"
        mk = types.InlineKeyboardMarkup()
        mk.row(types.InlineKeyboardButton("🔙 پنل", callback_data="adm_panel"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"handle_adm_manage_admins: {e}")
        bot.answer_callback_query(call.id, "❌ خطا")


def handle_adm_clear_cache(call):
    if not _adm_check(call, super_only=True):
        return
    clear_cache()
    bot.answer_callback_query(call.id, "✅ کش پاک شد")
    _show_admin_panel(call)


def handle_adm_backup(call):
    if not _adm_check(call, super_only=True):
        return
    try:
        import shutil
        bname = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(DB_NAME, bname)
        size  = os.path.getsize(bname) // 1024
        txt   = f"💾 <b>بکاپ</b>\n\n✅ فایل: <code>{bname}</code>\n📦 حجم: {size} KB"
        mk    = types.InlineKeyboardMarkup()
        mk.row(types.InlineKeyboardButton("🔙 پنل", callback_data="adm_panel"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"handle_adm_backup: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در بکاپ")


# ─────────────────────────────────────────────────────────────
# Broadcast
# ─────────────────────────────────────────────────────────────
def _do_broadcast(admin_id: int, message_text: str):
    try:
        with get_conn() as conn:
            users = conn.execute('SELECT DISTINCT user_id FROM user_activity_logs').fetchall()

        ok = fail = 0
        for (uid,) in users:
            try:
                bot.send_message(uid, message_text, parse_mode='HTML')
                ok += 1
                time.sleep(0.05)
            except Exception:
                fail += 1

        report = (f"📢 <b>گزارش ارسال همگانی</b>\n\n"
                  f"✅ موفق: {ok}\n❌ ناموفق: {fail}\n"
                  f"📊 مجموع: {ok+fail}\n⏰ {_now()}")
        bot.send_message(admin_id, report, parse_mode='HTML')
    except Exception as e:
        logger.error(f"_do_broadcast: {e}")


def send_broadcast_async(admin_id: int, message_text: str):
    t = threading.Thread(target=_do_broadcast, args=(admin_id, message_text), daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────
# Welcome
# ─────────────────────────────────────────────────────────────
def _welcome_text(user_id: int, user_name: str) -> str:
    name = safe_text(user_name)
    return (
        f"🎬 سلام <b>{name}</b> عزیز!\n\n"
        "╔══════════════════════════╗\n"
        "║   ربات پیشرفته فیلم و سریال   ║\n"
        "╚══════════════════════════╝\n\n"
        "✨ <b>قابلیت‌ها:</b>\n"
        "├ 🔍 جستجوی فیلم، سریال و بازیگر\n"
        "├ 🏆 250 فیلم برتر تاریخ سینما\n"
        "├ 🔥 ترندهای روز و هفته\n"
        "├ 🎭 فیلتر بر اساس ژانر\n"
        "├ ▶️ تریلر رسمی یوتیوب\n"
        "├ 📥 زیرنویس فارسی و انگلیسی\n"
        "├ ❤️ لیست علاقه‌مندی‌ها\n"
        "├ ⭐ امتیازدهی و آمار شخصی\n"
        "├ 🎯 پیشنهادات هوشمند\n"
        "├ 🔔 سیستم نوتیفیکیشن\n"
        "├ 🎬 فیلم‌های در حال پخش در سینما\n"
        "├ 🔜 پیش‌نمایش فیلم‌های جدید\n"
        "├ 📝 نقدها و بررسی‌ها\n"
        "└ 📦 مجموعه‌های فیلم (Marvel, DC, ...)\n\n"
        "💬 <i>کافیه نام فیلم یا سریال رو بنویسی!</i>"
    )


def _welcome_markup(user_id: int) -> types.InlineKeyboardMarkup:
    mk = types.InlineKeyboardMarkup()
    mk.row(types.InlineKeyboardButton("🔍 جستجوی اینلاین", switch_inline_query_current_chat=" "))
    mk.row(
        types.InlineKeyboardButton("🔥 ترند هفته",      callback_data="trending"),
        types.InlineKeyboardButton("🏆 250 فیلم برتر",  callback_data="top250:0")
    )
    mk.row(
        types.InlineKeyboardButton("⭐ برترین فیلم‌ها",  callback_data="top_rated"),
        types.InlineKeyboardButton("🎭 جستجو ژانر",     callback_data="genre_list:")
    )
    mk.row(
        types.InlineKeyboardButton("🎯 پیشنهادات",      callback_data="smart_recs"),
        types.InlineKeyboardButton("📊 آمار من",         callback_data="my_stats")
    )
    mk.row(
        types.InlineKeyboardButton("❤️ علاقه‌مندی‌ها",  callback_data="my_bookmarks"),
        types.InlineKeyboardButton("⭐ امتیازهای من",    callback_data="my_ratings")
    )
    mk.row(types.InlineKeyboardButton("🔔 نوتیفیکیشن‌ها", callback_data="my_notifs"))
    mk.row(
        types.InlineKeyboardButton("🎬 در سینما الان",   callback_data="now_playing"),
        types.InlineKeyboardButton("🔜 فیلم‌های جدید",  callback_data="upcoming")
    )
    mk.row(
        types.InlineKeyboardButton("📺 برترین سریال‌ها", callback_data="top_tv"),
        types.InlineKeyboardButton("📅 ترند امروز",      callback_data="trending_day")
    )
    if is_admin(user_id):
        mk.row(types.InlineKeyboardButton("👑 پنل ادمین", callback_data="adm_panel"))
    return mk


# ─────────────────────────────────────────────────────────────
# هندلرهای اصلی
# ─────────────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
def cmd_start(message):
    try:
        uid   = message.from_user.id
        uname = message.from_user.first_name or "کاربر"
        log_user_activity(uid, 'start')

        args = message.text.split(maxsplit=1)
        if len(args) > 1:
            param = args[1]
            parts = param.split('_', 1)
            if len(parts) == 2:
                ptype, pid = parts
                if ptype == 'person':
                    d = get_details(pid, 'person')
                    if d:
                        txt, mk, _ = build_person_message(d, uid)
                        _send_item(message.chat.id, txt, mk)
                        return
                elif ptype in ('movie', 'tv'):
                    d = get_details(pid, ptype)
                    if d:
                        txt, mk, _ = build_movie_message(d, uid)
                        _send_item(message.chat.id, txt, mk)
                        return

        bot.send_message(
            message.chat.id,
            _welcome_text(uid, uname),
            reply_markup=_welcome_markup(uid),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"cmd_start: {e}")


@bot.message_handler(commands=['help'])
def cmd_help(message):
    txt = (
        "📖 <b>راهنمای ربات</b>\n\n"
        "✏️ <b>جستجو:</b> فقط نام فیلم یا سریال بنویسید\n"
        "🔍 <b>اینلاین:</b> در هر چت @ربات + نام فیلم\n"
        "❤️ <b>ذخیره:</b> دکمه «ذخیره» در صفحه فیلم\n"
        "⭐ <b>امتیاز:</b> دکمه «امتیاز دادن» در صفحه فیلم\n"
        "📥 <b>زیرنویس:</b> دکمه «زیرنویس» — فایل برایتان ارسال میشه\n"
        "▶️ <b>تریلر:</b> دکمه «تریلر» — لینک یوتیوب\n\n"
        "📋 <b>دستورات:</b>\n"
        "/start — صفحه اصلی\n"
        "/help — این راهنما\n"
        "/bookmarks — علاقه‌مندی‌ها\n"
        "/stats — آمار من\n"
        "/recommendations — پیشنهادات\n"
    )
    if is_admin(message.from_user.id):
        txt += "/admin — پنل ادمین\n"
    bot.send_message(message.chat.id, txt, parse_mode='HTML')


@bot.message_handler(commands=['bookmarks'])
def cmd_bookmarks(message):
    txt, mk = build_bookmarks_message(message.from_user.id)
    bot.send_message(message.chat.id, txt, reply_markup=mk, parse_mode='HTML')


@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    txt, mk = build_stats_message(message.from_user.id)
    bot.send_message(message.chat.id, txt, reply_markup=mk, parse_mode='HTML')


@bot.message_handler(commands=['recommendations'])
def cmd_recs(message):
    txt, mk = build_recommendations_message(message.from_user.id)
    bot.send_message(message.chat.id, txt, reply_markup=mk, parse_mode='HTML')


@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    uid = message.from_user.id
    if not is_admin(uid):
        bot.send_message(message.chat.id, "❌ دسترسی ندارید.")
        return
    txt, mk = build_admin_panel(uid)
    bot.send_message(message.chat.id, txt, reply_markup=mk, parse_mode='HTML')


@bot.message_handler(commands=['addadmin'])
def cmd_addadmin(message):
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.send_message(message.chat.id, "❌ فقط سوپر ادمین می‌تواند ادمین اضافه کند.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "⚠️ استفاده: /addadmin <user_id>")
        return
    try:
        new_id = int(parts[1])
        add_admin(new_id, '', '', 'admin')
        bot.send_message(message.chat.id, f"✅ کاربر {new_id} به ادمین‌ها اضافه شد.")
    except ValueError:
        bot.send_message(message.chat.id, "❌ آیدی نامعتبر.")


@bot.message_handler(commands=['removeadmin'])
def cmd_removeadmin(message):
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.send_message(message.chat.id, "❌ فقط سوپر ادمین می‌تواند ادمین حذف کند.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "⚠️ استفاده: /removeadmin <user_id>")
        return
    try:
        rem_id = int(parts[1])
        if rem_id == uid:
            bot.send_message(message.chat.id, "❌ نمی‌توانید خودتان را حذف کنید.")
            return
        remove_admin(rem_id)
        bot.send_message(message.chat.id, f"✅ کاربر {rem_id} از ادمین‌ها حذف شد.")
    except ValueError:
        bot.send_message(message.chat.id, "❌ آیدی نامعتبر.")


@bot.message_handler(func=lambda m: True, content_types=['text'])
def handle_message(message):
    uid   = message.from_user.id
    state = get_state(uid)

    if not check_rate_limit(uid):
        return

    if state == 'awaiting_broadcast':
        set_state(uid, None)
        bot.send_message(uid, "⏳ در حال ارسال پیام همگانی...")
        send_broadcast_async(uid, message.text)
        return

    handle_search(message)


def handle_search(message):
    try:
        query = message.text.strip()
        if not query or len(query) < 2:
            return

        uid = message.from_user.id
        bot.send_chat_action(message.chat.id, 'typing')
        log_user_activity(uid, 'search')

        results = search_tmdb(query)
        if not results:
            bot.send_message(message.chat.id,
                             f"❌ نتیجه‌ای برای «{safe_text(query)}» پیدا نشد.\n\n"
                             "💡 سعی کنید نام انگلیسی را بنویسید.")
            return

        if len(results) > 1:
            _show_search_results(message.chat.id, results[:8], query)
        else:
            _open_item_send(message.chat.id, uid, results[0])

    except Exception as e:
        logger.error(f"handle_search: {e}")


def _show_search_results(chat_id: int, results: list, query: str):
    txt = f"🔍 <b>نتایج: «{safe_text(query)}»</b>\n\nیکی را انتخاب کنید:\n"
    mk  = types.InlineKeyboardMarkup()
    for item in results:
        ititle = item.get('title') or item.get('name', '؟')
        year   = (item.get('release_date') or item.get('first_air_date', ''))[:4]
        mtype  = item.get('media_type', 'movie')
        iid    = item.get('id')
        icon   = {'movie': '🎬', 'tv': '📺', 'person': '👤'}.get(mtype, '•')
        vote   = item.get('vote_average', 0) or 0
        label  = f"{icon} {ititle}"
        if year:
            label += f" ({year})"
        if vote:
            label += f" ⭐{vote:.1f}"
        mk.row(types.InlineKeyboardButton(label, callback_data=f"open:{mtype}:{iid}"))
    mk.row(types.InlineKeyboardButton("🏠 خانه", callback_data="home"))
    bot.send_message(chat_id, txt, reply_markup=mk, parse_mode='HTML')


def _open_item_send(chat_id: int, user_id: int, item: dict):
    itype = item.get('media_type', 'movie')
    iid   = item.get('id')
    d     = get_details(iid, itype)
    if not d:
        bot.send_message(chat_id, "❌ خطا در دریافت اطلاعات.")
        return
    if itype == 'person':
        txt, mk, _ = build_person_message(d, user_id)
    else:
        txt, mk, _ = build_movie_message(d, user_id)
    _send_item(chat_id, txt, mk)


def _send_item(chat_id: int, txt: str, mk: types.InlineKeyboardMarkup):
    """ارسال پیام جدید"""
    try:
        bot.send_message(chat_id, txt, reply_markup=mk, parse_mode='HTML',
                         disable_web_page_preview=False)
    except Exception as e:
        logger.error(f"_send_item: {e}")
        try:
            bot.send_message(chat_id, txt[:4000], reply_markup=mk, parse_mode='HTML',
                             disable_web_page_preview=True)
        except Exception as e2:
            logger.error(f"_send_item fallback: {e2}")


def _edit_item(call, txt: str, mk: types.InlineKeyboardMarkup):
    """ویرایش پیام موجود — همیشه edit کنه نه send"""
    try:
        bot.edit_message_text(
            txt,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=mk,
            parse_mode='HTML',
            disable_web_page_preview=False
        )
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            logger.warning(f"_edit_item: {e}")
    except Exception as e:
        logger.error(f"_edit_item: {e}")


# ─────────────────────────────────────────────────────────────
# Inline Query Handler
# ─────────────────────────────────────────────────────────────
@bot.inline_handler(func=lambda q: True)
def handle_inline_query(query):
    try:
        q_text = query.query.strip()
        if not q_text or len(q_text) < 2:
            results_data = get_popular_movies()[:8]
            for item in results_data:
                item.setdefault('media_type', 'movie')
        else:
            results_data = search_tmdb(q_text)[:8]

        inline_results = []
        uname = get_bot_username()

        for item in results_data:
            itype = item.get('media_type', 'movie')
            iid   = str(item.get('id', ''))
            if not iid:
                continue

            ititle = item.get('title') or item.get('name') or '؟'
            year   = (item.get('release_date') or item.get('first_air_date', ''))[:4]
            desc   = (item.get('overview') or '').strip()
            vote   = item.get('vote_average') or 0
            icon   = {'movie': '🎬', 'tv': '📺', 'person': '👤'}.get(itype, '🎬')

            poster = item.get('poster_path') or item.get('profile_path')
            thumb  = f"https://image.tmdb.org/t/p/w300{poster}" if poster else None

            title_label = f"{icon} {ititle}"
            if year:
                title_label += f" ({year})"

            desc_short = ""
            if vote:
                desc_short += f"⭐{vote:.1f}  "
            if year:
                desc_short += f"📅{year}  "
            if desc:
                desc_short += desc[:100]

            msg_txt  = f"{icon} <b>{safe_text(ititle)}</b>"
            if year:
                msg_txt += f" ({year})"
            if vote:
                msg_txt += f"\n{star_bar(vote)} ⭐ {vote:.1f}/10"
            if desc:
                msg_txt += f"\n\n{safe_text(desc[:400])}"
            msg_txt += f"\n\n📲 <a href='https://t.me/{uname}?start={itype}_{iid}'>مشاهده کامل در ربات</a>"

            try:
                kwargs = dict(
                    id=f"{itype}_{iid}",
                    title=title_label[:256],
                    description=desc_short[:256] if desc_short else " ",
                    input_message_content=types.InputTextMessageContent(
                        message_text=msg_txt[:4096],
                        parse_mode='HTML',
                        disable_web_page_preview=False
                    ),
                )
                if thumb:
                    kwargs['thumb_url']    = thumb
                    kwargs['thumb_width']  = 300
                    kwargs['thumb_height'] = 450

                inline_results.append(types.InlineQueryResultArticle(**kwargs))
            except Exception as e:
                logger.warning(f"inline item {iid}: {e}")

        if not inline_results:
            inline_results.append(types.InlineQueryResultArticle(
                id='no_result',
                title='❌ نتیجه‌ای یافت نشد',
                description='عبارت دیگری امتحان کنید',
                input_message_content=types.InputTextMessageContent(
                    message_text='❌ نتیجه‌ای یافت نشد.',
                    disable_web_page_preview=True
                )
            ))

        bot.answer_inline_query(query.id, inline_results, cache_time=30, is_personal=True)
    except Exception as e:
        logger.error(f"handle_inline_query: {e}")
        try:
            bot.answer_inline_query(query.id, [], cache_time=5)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Callback Handler — همه جا edit میکنه
# ─────────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    try:
        uid  = call.from_user.id
        data = call.data

        if not check_rate_limit(uid, min_gap=0.5):
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)  # همیشه جواب بده تا دکمه spin نکنه

        # ── ناوبری ──────────────────────────────────────────
        if data == 'home':
            _cb_home(call)

        elif data == 'go_back':
            _cb_go_back(call)

        # ── صفحات کاربر ──────────────────────────────────────
        elif data == 'my_stats':
            txt, mk = build_stats_message(uid)
            _edit_item(call, txt, mk)

        elif data == 'smart_recs':
            txt, mk = build_recommendations_message(uid)
            _edit_item(call, txt, mk)

        elif data == 'my_notifs':
            txt, mk = build_notifications_message(uid)
            _edit_item(call, txt, mk)

        elif data == 'my_bookmarks':
            txt, mk = build_bookmarks_message(uid)
            _edit_item(call, txt, mk)

        elif data == 'my_ratings':
            txt, mk = build_ratings_message(uid)
            _edit_item(call, txt, mk)

        # ── باز کردن آیتم ────────────────────────────────────
        elif data.startswith('open:'):
            _, itype, iid = data.split(':', 2)
            d = get_details(iid, itype)
            if not d:
                return
            if itype == 'person':
                txt, mk, _ = build_person_message(d, uid)
            else:
                txt, mk, _ = build_movie_message(d, uid)
            _edit_item(call, txt, mk)

        elif data.startswith('person:'):
            _, pid = data.split(':', 1)
            d = get_details(pid, 'person')
            if not d:
                return
            txt, mk, _ = build_person_message(d, uid)
            _edit_item(call, txt, mk)

        # ── bookmark ─────────────────────────────────────────
        elif data.startswith('bm:'):
            _, itype, iid = data.split(':', 2)
            if is_bookmarked(uid, iid, itype):
                remove_bookmark(uid, iid, itype)
            else:
                add_bookmark(uid, iid, itype)
                analyze_preferences_async(uid)
            d = get_details(iid, itype)
            if d:
                txt, mk, _ = build_movie_message(d, uid)
                _edit_item(call, txt, mk)

        # ── rate ─────────────────────────────────────────────
        elif data.startswith('rate:'):
            _, itype, iid = data.split(':', 2)
            mk = types.InlineKeyboardMarkup()
            stars_labels = ['⭐', '⭐⭐', '⭐⭐⭐', '⭐⭐⭐⭐', '⭐⭐⭐⭐⭐']
            for i in range(1, 6):
                mk.row(types.InlineKeyboardButton(
                    stars_labels[i-1],
                    callback_data=f"setrate:{itype}:{iid}:{i}"
                ))
            mk.row(
                types.InlineKeyboardButton("🔙 برگشت", callback_data=f"back_item:{itype}:{iid}"),
                types.InlineKeyboardButton("🏠 خانه",  callback_data="home")
            )
            bot.edit_message_text("⭐ <b>امتیاز خود را انتخاب کنید:</b>",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=mk, parse_mode='HTML')

        elif data.startswith('setrate:'):
            _, itype, iid, rating = data.split(':', 3)
            add_rating(uid, iid, itype, int(rating))
            analyze_preferences_async(uid)
            d = get_details(iid, itype)
            if d:
                txt, mk, _ = build_movie_message(d, uid)
                _edit_item(call, txt, mk)

        # ── back to item ──────────────────────────────────────
        elif data.startswith('back_item:'):
            _, itype, iid = data.split(':', 2)
            d = get_details(iid, itype)
            if d:
                txt, mk, _ = build_movie_message(d, uid)
                _edit_item(call, txt, mk)

        # ── similar ───────────────────────────────────────────
        elif data.startswith('similar:'):
            parts = data.split(':')
            itype, iid, idx_str = parts[1], parts[2], parts[3]
            items   = get_similar_items(iid, itype)
            list_id = f"sim_{iid}"
            with _cache_lock:
                temp_cache[list_id] = {'items': items, 'orig_type': itype, 'orig_id': iid, 'ts': time.time()}
            _show_similar(call, list_id, int(idx_str), uid)

        elif data.startswith('nav:'):
            _, list_id, idx_str = data.split(':', 2)
            _show_similar(call, list_id, int(idx_str), uid)

        # ── subtitle ─────────────────────────────────────────
        elif data.startswith('sub:'):
            _, itype, iid = data.split(':', 2)
            d     = get_details(iid, itype)
            title = (d.get('title') or d.get('name', '')) if d else ''
            year  = ((d.get('release_date') or d.get('first_air_date', ''))[:4]) if d else ''
            imdb_id = d.get('external_ids', {}).get('imdb_id', '') if d else ''
            txt, mk = build_subtitle_message(iid, itype, title, year, imdb_id)
            _edit_item(call, txt, mk)

        elif data.startswith('subdl:'):
            _cb_subtitle_download(call)

        # ── trailer ──────────────────────────────────────────
        elif data.startswith('trailer:'):
            _cb_trailer(call)

        # ── trending ─────────────────────────────────────────
        elif data == 'trending':
            items = get_trending('week')
            with _cache_lock:
                temp_cache['list_trending'] = {'items': items, 'ts': time.time()}
            txt, mk = _build_list_message(items, "🔥 ترندهای هفته", "list_trending", 0, "trendnav")
            _edit_item(call, txt, mk)

        elif data.startswith('trendnav:'):
            _, idx_s = data.split(':', 1)
            with _cache_lock:
                d = temp_cache.get('list_trending', {})
            items = d.get('items') or get_trending('week')
            txt, mk = _build_list_message(items, "🔥 ترندهای هفته", "list_trending", int(idx_s), "trendnav")
            _edit_item(call, txt, mk)

        # ── top_rated ────────────────────────────────────────
        elif data == 'top_rated':
            items = get_top_rated('movie')
            with _cache_lock:
                temp_cache['list_toprated'] = {'items': items, 'ts': time.time()}
            txt, mk = _build_list_message(items, "🏆 برترین فیلم‌ها", "list_toprated", 0, "toprnav")
            _edit_item(call, txt, mk)

        elif data.startswith('toprnav:'):
            _, idx_s = data.split(':', 1)
            with _cache_lock:
                d = temp_cache.get('list_toprated', {})
            items = d.get('items') or get_top_rated('movie')
            txt, mk = _build_list_message(items, "🏆 برترین فیلم‌ها", "list_toprated", int(idx_s), "toprnav")
            _edit_item(call, txt, mk)

        # ── top250 ───────────────────────────────────────────
        elif data.startswith('top250:'):
            _, idx_s = data.split(':', 1)
            txt, mk  = _build_top250_message(int(idx_s))
            _edit_item(call, txt, mk)

        # ── now_playing ──────────────────────────────────────
        elif data == 'now_playing':
            items = get_now_playing()
            with _cache_lock:
                temp_cache['list_nowplay'] = {'items': items, 'ts': time.time()}
            txt, mk = _build_list_message(items, "🎬 در حال پخش در سینما", "list_nowplay", 0, "nowplaynav")
            _edit_item(call, txt, mk)

        elif data.startswith('nowplaynav:'):
            _, idx_s = data.split(':', 1)
            with _cache_lock:
                d = temp_cache.get('list_nowplay', {})
            items = d.get('items') or get_now_playing()
            txt, mk = _build_list_message(items, "🎬 در حال پخش در سینما", "list_nowplay", int(idx_s), "nowplaynav")
            _edit_item(call, txt, mk)

        # ── upcoming ─────────────────────────────────────────
        elif data == 'upcoming':
            items = get_upcoming_movies()
            with _cache_lock:
                temp_cache['list_upcoming'] = {'items': items, 'ts': time.time()}
            txt, mk = _build_list_message(items, "🔜 فیلم‌های در راه", "list_upcoming", 0, "upcomingnav")
            _edit_item(call, txt, mk)

        elif data.startswith('upcomingnav:'):
            _, idx_s = data.split(':', 1)
            with _cache_lock:
                d = temp_cache.get('list_upcoming', {})
            items = d.get('items') or get_upcoming_movies()
            txt, mk = _build_list_message(items, "🔜 فیلم‌های در راه", "list_upcoming", int(idx_s), "upcomingnav")
            _edit_item(call, txt, mk)

        # ── top_tv ───────────────────────────────────────────
        elif data == 'top_tv':
            items = get_top_tv_shows()
            with _cache_lock:
                temp_cache['list_toptv'] = {'items': items, 'ts': time.time()}
            txt, mk = _build_list_message(items, "📺 برترین سریال‌ها", "list_toptv", 0, "toptvnav")
            _edit_item(call, txt, mk)

        elif data.startswith('toptvnav:'):
            _, idx_s = data.split(':', 1)
            with _cache_lock:
                d = temp_cache.get('list_toptv', {})
            items = d.get('items') or get_top_tv_shows()
            txt, mk = _build_list_message(items, "📺 برترین سریال‌ها", "list_toptv", int(idx_s), "toptvnav")
            _edit_item(call, txt, mk)

        # ── trending_day ─────────────────────────────────────
        elif data == 'trending_day':
            items = get_trending_daily()
            with _cache_lock:
                temp_cache['list_trendday'] = {'items': items, 'ts': time.time()}
            txt, mk = _build_list_message(items, "📅 ترند امروز", "list_trendday", 0, "trenddaynav")
            _edit_item(call, txt, mk)

        elif data.startswith('trenddaynav:'):
            _, idx_s = data.split(':', 1)
            with _cache_lock:
                d = temp_cache.get('list_trendday', {})
            items = d.get('items') or get_trending_daily()
            txt, mk = _build_list_message(items, "📅 ترند امروز", "list_trendday", int(idx_s), "trenddaynav")
            _edit_item(call, txt, mk)

        # ── reviews ──────────────────────────────────────────
        elif data.startswith('reviews:'):
            _, itype, iid = data.split(':', 2)
            d     = get_details(iid, itype)
            title = (d.get('title') or d.get('name', '')) if d else ''
            txt, mk = build_reviews_message(iid, itype, title)
            _edit_item(call, txt, mk)

        # ── collection ───────────────────────────────────────
        elif data.startswith('collection:'):
            _, cid = data.split(':', 1)
            txt, mk = build_collection_message(cid, uid)
            _edit_item(call, txt, mk)

        # ── genre ────────────────────────────────────────────
        elif data.startswith('genre_list:'):
            _cb_genre_list(call)

        elif data.startswith('genre_movies:'):
            _, gname, idx_s = data.split(':', 2)
            _cb_genre_movies(call, gname, int(idx_s))

        # ── admin ────────────────────────────────────────────
        elif data == 'adm_panel':
            _show_admin_panel(call)
        elif data == 'adm_stats':
            handle_adm_stats(call)
        elif data == 'adm_users':
            handle_adm_users(call)
        elif data == 'adm_movies':
            handle_adm_movies(call)
        elif data == 'adm_top_users':
            handle_adm_top_users(call)
        elif data == 'adm_manage_admins':
            handle_adm_manage_admins(call)
        elif data == 'adm_clear_cache':
            handle_adm_clear_cache(call)
        elif data == 'adm_backup':
            handle_adm_backup(call)
        elif data == 'adm_broadcast':
            _cb_adm_broadcast(call)

        elif data == 'noop':
            pass  # دکمه counter — نیازی به کار نیست

        else:
            pass  # دستور نامشخص — ignore

    except Exception as e:
        logger.error(f"handle_callback [{call.data}]: {e}")


# ─────────────────────────────────────────────────────────────
# توابع callback داخلی
# ─────────────────────────────────────────────────────────────
def _cb_home(call):
    uid   = call.from_user.id
    uname = call.from_user.first_name or "کاربر"
    try:
        bot.edit_message_text(
            _welcome_text(uid, uname),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_welcome_markup(uid),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.warning(f"_cb_home: {e}")


def _cb_go_back(call):
    uid  = call.from_user.id
    prev = pop_history(uid)
    if prev:
        ptype, pid = prev
        d = get_details(pid, ptype)
        if d:
            if ptype == 'person':
                txt, mk, _ = build_person_message(d, uid)
            else:
                txt, mk, _ = build_movie_message(d, uid)
            _edit_item(call, txt, mk)
            return
    _cb_home(call)


def _show_similar(call, list_id: str, index: int, user_id: int):
    with _cache_lock:
        list_data = temp_cache.get(list_id)

    if not list_data:
        return

    items = list_data['items']
    total = len(items)
    if total == 0:
        return

    index  = index % total
    item   = items[index]
    iid    = str(item.get('id', ''))
    itype  = item.get('media_type', list_data.get('orig_type', 'movie'))

    d = get_details(iid, itype)
    if not d:
        return

    txt, mk, _ = build_movie_message(d, user_id)

    # دکمه‌های ناوبری similar
    mk.row(
        types.InlineKeyboardButton("⬅️ قبلی",          callback_data=f"nav:{list_id}:{(index-1)%total}"),
        types.InlineKeyboardButton(f"· {index+1}/{total} ·", callback_data="noop"),
        types.InlineKeyboardButton("بعدی ➡️",          callback_data=f"nav:{list_id}:{(index+1)%total}"),
    )
    orig_type = list_data.get('orig_type', 'movie')
    orig_id   = list_data.get('orig_id', '')
    mk.row(types.InlineKeyboardButton("🔙 بازگشت به فیلم", callback_data=f"back_item:{orig_type}:{orig_id}"))

    _edit_item(call, txt, mk)


def _cb_trailer(call):
    """ارسال تریلر — یک پیام جدید می‌فرسته"""
    uid = call.from_user.id
    iid = call.data.split(':', 1)[1]

    with _cache_lock:
        data = temp_cache.get(f"trailer_{iid}")

    if not data:
        # اگه cache نداشت، از TMDB بگیر
        for itype in ('movie', 'tv'):
            d = get_details(iid, itype)
            if d:
                trailer_url = get_trailer_url(d.get('videos', {}))
                if trailer_url:
                    title = d.get('title') or d.get('name', '')
                    data  = {'url': trailer_url, 'title': title}
                    with _cache_lock:
                        temp_cache[f"trailer_{iid}"] = {'url': trailer_url, 'title': title, 'ts': time.time()}
                    break

    if not data or not data.get('url'):
        bot.send_message(call.message.chat.id, "❌ تریلری برای این فیلم یافت نشد.")
        return

    url   = data['url']
    title = data.get('title', '')

    txt = f"▶️ <b>تریلر رسمی</b>\n🎬 {safe_text(title)}\n\n{url}"
    mk  = types.InlineKeyboardMarkup()
    mk.row(types.InlineKeyboardButton("▶️ تماشای تریلر در یوتیوب", url=url))
    mk.row(
        types.InlineKeyboardButton("🔙 برگشت به فیلم", callback_data=f"back_item:movie:{iid}"),
        types.InlineKeyboardButton("🏠 خانه", callback_data="home")
    )
    try:
        # ارسال پیام جدید (نه edit) تا preview یوتیوب نمایش داده بشه
        # ابتدا پیام قبلی رو ویرایش کن که دکمه‌ها پاک بشن
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id,
                reply_markup=types.InlineKeyboardMarkup()
            )
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id, txt,
            reply_markup=mk,
            parse_mode='HTML',
            disable_web_page_preview=False
        )
    except Exception as e:
        logger.error(f"_cb_trailer: {e}")


def _cb_subtitle_download(call):
    """دریافت و ارسال فایل زیرنویس"""
    uid = call.from_user.id
    try:
        _, item_id, sub_id_partial = call.data.split(':', 2)
    except ValueError:
        return

    with _cache_lock:
        cache_data = temp_cache.get(f"subs_{item_id}")

    if not cache_data:
        bot.send_message(call.message.chat.id,
                         "⚠️ لطفاً دوباره روی دکمه زیرنویس در صفحه فیلم کلیک کنید.")
        return

    subs  = cache_data.get('subs', [])
    title = cache_data.get('title', '')
    year  = cache_data.get('year', '')
    # جستجوی زیرنویس با prefix match
    sub   = next((s for s in subs if s['id'][:len(sub_id_partial)] == sub_id_partial or
                  s['id'] == sub_id_partial), None)

    if not sub:
        sub = next((s for s in subs if sub_id_partial in s['id']), None)

    if not sub:
        bot.send_message(call.message.chat.id, "❌ زیرنویس یافت نشد.")
        return

    lang_icon = '🇮🇷' if sub['lang'] == 'fa' else '🇬🇧'
    quality   = sub.get('quality', 'نامشخص')
    source    = sub.get('source', '')
    file_id   = sub.get('file_id', 0)

    # تلاش برای دانلود مستقیم فایل
    file_bytes = None

    # روش ۱: OpenSubtitles API
    if file_id:
        bot.send_chat_action(call.message.chat.id, 'upload_document')
        file_bytes = download_subtitle_via_api(file_id, title, sub['lang'])

    if file_bytes:
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_'))[:30].strip()
        fname      = f"{safe_title}_{quality}_{sub['lang']}.srt"
        caption    = (
            f"📥 <b>زیرنویس ارسال شد!</b>\n\n"
            f"🎬 فیلم: {safe_text(title)}"
            + (f" ({year})" if year else "") + "\n"
            f"{lang_icon} زبان: {sub['lang_name']}\n"
            f"🎞 کیفیت: {quality}\n"
            f"🌐 منبع: {source}\n"
        )
        if sub.get('uploader'):
            caption += f"👤 آپلودر: {safe_text(sub['uploader'])}\n"
        try:
            bot.send_document(
                call.message.chat.id,
                io.BytesIO(file_bytes),
                visible_file_name=fname,
                caption=caption,
                parse_mode='HTML'
            )
            return
        except Exception as e:
            logger.error(f"send subtitle document: {e}")

    # روش ۲: لینک مستقیم دانلود
    sub_url = sub.get('url', '')
    info_txt = (
        f"📥 <b>زیرنویس: {safe_text(title)}</b>"
        + (f" ({year})" if year else "") + "\n\n"
        f"┌─ مشخصات ────────────────\n"
        f"│ {lang_icon} زبان: {sub['lang_name']}\n"
        f"│ 🎞 کیفیت: {quality}\n"
        f"│ 🌐 منبع: {source}\n"
        + (f"│ ⬇️ دانلود: {fmt(sub.get('downloads', 0))}\n" if sub.get('downloads') else "")
        + "└────────────────────────────\n\n"
        "🔗 برای دانلود روی دکمه زیر کلیک کنید:"
    )
    mk = types.InlineKeyboardMarkup()
    mk.row(types.InlineKeyboardButton(f"⬇️ دانلود از {source}", url=sub_url))
    mk.row(
        types.InlineKeyboardButton("🔙 برگشت", callback_data=f"sub:{cache_data['item_type']}:{item_id}"),
        types.InlineKeyboardButton("🏠 خانه",  callback_data="home")
    )
    # edit پیام موجود به جای ارسال جدید
    _edit_item(call, info_txt, mk)


def _cb_genre_list(call):
    genres = list(_genre_map.keys())
    txt    = "🎭 <b>انتخاب ژانر</b>\n\nژانر مورد نظر را انتخاب کنید:"
    mk     = types.InlineKeyboardMarkup()
    for i in range(0, len(genres), 2):
        row = [types.InlineKeyboardButton(genres[i], callback_data=f"genre_movies:{genres[i]}:0")]
        if i + 1 < len(genres):
            row.append(types.InlineKeyboardButton(genres[i+1], callback_data=f"genre_movies:{genres[i+1]}:0"))
        mk.row(*row)
    mk.row(types.InlineKeyboardButton("🏠 خانه", callback_data="home"))
    _edit_item(call, txt, mk)


def _cb_genre_movies(call, genre_name: str, index: int):
    items   = _movies_by_genre(genre_name)
    list_id = f"genre_{genre_name}"
    with _cache_lock:
        temp_cache[list_id] = {'items': items, 'ts': time.time()}
    txt, mk = _build_list_message(items, f"🎭 {genre_name}", list_id, index,
                                   f"genre_movies:{genre_name}")
    _edit_item(call, txt, mk)


def _cb_adm_broadcast(call):
    if not _adm_check(call):
        return
    uid = call.from_user.id
    set_state(uid, 'awaiting_broadcast')
    txt = ("📢 <b>ارسال همگانی</b>\n\n"
           "پیام خود را بنویسید (HTML پشتیبانی می‌شود):")
    mk  = types.InlineKeyboardMarkup()
    mk.row(types.InlineKeyboardButton("❌ لغو", callback_data="adm_panel"))
    bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                          reply_markup=mk, parse_mode='HTML')


# ─────────────────────────────────────────────────────────────
# پاکسازی temp_cache منقضی
# ─────────────────────────────────────────────────────────────
def _cleanup_temp_cache():
    while True:
        time.sleep(3600)
        try:
            now = time.time()
            with _cache_lock:
                expired = [k for k, v in temp_cache.items()
                           if now - v.get('ts', 0) > 7200]
                for k in expired:
                    del temp_cache[k]
            if expired:
                logger.info(f"temp_cache cleanup: {len(expired)} entries removed")
        except Exception as e:
            logger.error(f"temp_cache cleanup error: {e}")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    get_bot_username()

    cleanup_thread = threading.Thread(target=_cleanup_temp_cache, daemon=True)
    cleanup_thread.start()

    print("=" * 62)
    print("🎬  ربات فیلم و سریال — نسخه ۳.۰ PRO")
    print("=" * 62)
    print("✅ رفع مشکل سینتکس — f-string quote escaping برطرف شد")
    print("✅ رفع مشکل تریلر — videos همیشه از API مستقیم گرفته می‌شه")
    print("✅ فیلم‌های در حال پخش سینما — now_playing")
    print("✅ فیلم‌های در راه — upcoming")
    print("✅ برترین سریال‌ها — top tv")
    print("✅ ترند امروز — daily trending")
    print("✅ نقدها و بررسی‌ها — reviews")
    print("✅ مجموعه‌های فیلم — collections (Marvel, DC, ...)")
    print("✅ رفع مشکل Inline — cache_time کاهش یافت، sorting بهتر")
    print("✅ زیرنویس — API واقعی OpenSubtitles.com + ارسال فایل")
    print("✅ دکمه‌ها — همه جا edit می‌کنه (پیام قبلی پاک می‌شه)")
    print("✅ 250 فیلم برتر — با ناوبری کامل و پرش سریع")
    print("✅ طراحی کامل بازنویسی شد — blockquote، star_bar، border")
    print("✅ بیوگرافی و خلاصه در blockquote کپی‌پذیر")
    print("✅ اطلاعات کامل‌تر — RT score، نویسنده، کلیدواژه، فصل‌ها")
    print("=" * 62)

    try:
        logger.info("🔄 Starting bot polling...")
        bot.infinity_polling(timeout=60, long_polling_timeout=60, logger_level=logging.WARNING)
    except KeyboardInterrupt:
        print("\n👋 ربات متوقف شد.")
    except Exception as e:
        logger.error(f"Bot polling error: {e}")
