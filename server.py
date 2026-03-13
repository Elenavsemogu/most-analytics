"""
MOST Analytics Dashboard — Backend
FastAPI + PostgreSQL (Supabase) / SQLite fallback, TGStat, GPT-анализ.
Запуск: uvicorn server:app --reload --port 8090
"""

import os
import json
import time
import asyncio
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

import secrets
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, Depends, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

load_dotenv()

TGSTAT_TOKEN = os.getenv("TGSTAT_TOKEN", "")
CHANNEL_ID = os.getenv("MOST_CHANNEL_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
CRON_SECRET = os.getenv("CRON_SECRET", "mostsecret2026")
DATABASE_URL = os.getenv("DATABASE_URL", "")
TGSTAT_BASE = "https://api.tgstat.ru"
DB_PATH = Path(__file__).parent / "analytics.db"

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "")
TGSTAT_CACHE_TTL = 6 * 3600  # 6 hours

app = FastAPI(title="MOST Analytics")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

VALID_TOKENS: set[str] = set()

_tgstat_cache: dict = {"stat": None, "info": None, "ts": 0}

LOGIN_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MOST Analytics — Вход</title><style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,system-ui,sans-serif;background:#0f1117;color:#e1e4ed;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#1a1d27;border:1px solid #2e3348;border-radius:16px;padding:40px;width:340px;text-align:center}
h1{font-size:22px;margin-bottom:8px;background:linear-gradient(135deg,#6c5ce7,#a29bfe);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
p{color:#8b90a5;font-size:14px;margin-bottom:24px}
input{width:100%;padding:12px;background:#232736;border:1px solid #2e3348;border-radius:8px;color:#e1e4ed;font-size:15px;margin-bottom:12px;text-align:center}
input:focus{outline:none;border-color:#6c5ce7}
button{width:100%;padding:12px;background:#6c5ce7;border:none;border-radius:8px;color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#5a4bd4}.err{color:#e17055;font-size:13px;margin-bottom:12px}
</style></head><body><div class="card"><h1>MOST Analytics</h1><p>Введите пароль для доступа</p>
<form method="POST" action="/login">ERR_PLACEHOLDER<input type="password" name="password" placeholder="Пароль" autofocus>
<button type="submit">Войти</button></form></div></body></html>"""

@app.post("/login")
async def login(request: Request):
    form = await request.form()
    pw = form.get("password", "")
    if pw == DASHBOARD_PASSWORD:
        token = secrets.token_hex(32)
        VALID_TOKENS.add(token)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("session", token, httponly=True, max_age=86400 * 7)
        return resp
    html = LOGIN_HTML.replace("ERR_PLACEHOLDER", '<div class="err">Неверный пароль</div>')
    return HTMLResponse(html, status_code=401)

def check_auth(request: Request):
    if not DASHBOARD_PASSWORD:
        return
    token = request.cookies.get("session", "")
    if token not in VALID_TOKENS:
        raise HTTPException(status_code=401, detail="unauthorized")

@app.get("/login")
def login_page():
    if not DASHBOARD_PASSWORD:
        return RedirectResponse("/")
    return HTMLResponse(LOGIN_HTML.replace("ERR_PLACEHOLDER", ""))


# ── Database ──────────────────────────────────────────────────────────

class _DbConn:
    """Unified wrapper: psycopg2 (Supabase) or sqlite3. Converts ? → %s for PG."""

    def __init__(self, conn, is_pg=False):
        self._conn = conn
        self._pg = is_pg

    def execute(self, query, params=None):
        if self._pg:
            query = query.replace("?", "%s")
            cur = self._conn.cursor()
            cur.execute(query, params or ())
            return cur
        return self._conn.execute(query, params or ())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.commit()
            else:
                self._conn.rollback()
        finally:
            self.close()
        return False


def get_db():
    if DATABASE_URL and psycopg2:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return _DbConn(conn, is_pg=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return _DbConn(conn, is_pg=False)


def _init_sqlite():
    if DATABASE_URL and psycopg2:
        return
    c = sqlite3.connect(str(DB_PATH))
    c.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at TEXT NOT NULL,
            participants INTEGER, avg_reach INTEGER, err_percent REAL,
            daily_reach INTEGER, ci_index REAL, posts_count INTEGER,
            channel_title TEXT, raw_json TEXT);
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY, post_id TEXT UNIQUE, snapshot_id INTEGER,
            date TEXT, text TEXT, views INTEGER DEFAULT 0, forwards INTEGER DEFAULT 0,
            reactions INTEGER DEFAULT 0, shares INTEGER DEFAULT 0, link TEXT);
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            period_start TEXT, period_end TEXT, analysis_type TEXT DEFAULT 'weekly',
            gpt_response TEXT, snapshots_used TEXT);
        CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(collected_at);
        CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date);
        CREATE INDEX IF NOT EXISTS idx_posts_views ON posts(views);
    """)
    c.close()

_init_sqlite()

UPSERT_POST = """INSERT INTO posts (post_id, snapshot_id, date, text, views, forwards, reactions, shares, link)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (post_id) DO UPDATE SET
    snapshot_id=EXCLUDED.snapshot_id, date=EXCLUDED.date, text=EXCLUDED.text,
    views=EXCLUDED.views, forwards=EXCLUDED.forwards, reactions=EXCLUDED.reactions,
    shares=EXCLUDED.shares, link=EXCLUDED.link"""

IGNORE_POST = """INSERT INTO posts (post_id, snapshot_id, date, text, views, forwards, reactions, shares, link)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (post_id) DO NOTHING"""

INSERT_SNAPSHOT = """INSERT INTO snapshots (collected_at, participants, avg_reach, err_percent,
    daily_reach, ci_index, posts_count, channel_title, raw_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"""


# ── Post classifier ──────────────────────────────────────────────────

def classify_post(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ['ищем', 'вакансия', 'откликайся', 'откликайтесь', 'отклик:',
                             'актуальных вакансий', '@daria_hrg', 'задачи:', 'что важно:',
                             'будет плюсом', 'зарплатный оффер', 'открыта позиция']):
        return 'vacancy'
    if any(k in t for k in ['как описать', 'как написать', 'резюме', 'собеседов', 'зарплат',
                             'карьер', 'gap year', 'рекрутер', 'навык', 'мотивац',
                             'кто такой', 'сколько платят', 'переговор']):
        return 'career'
    if any(k in t for k in ['статья', 'вышла наша', 'читайте', 'подборк', 'дайджест',
                             'анонс', 'партнёр', 'конференц', 'событи']):
        return 'announce'
    return 'story'


# ── Marketing strategy context for GPT ───────────────────────────────

STRATEGY_FILE = Path(__file__).parent / "marketing-strategy.md"
AUDIT_FILE = Path(__file__).parent / "channel-audit.md"

def load_strategy() -> str:
    if STRATEGY_FILE.exists():
        return STRATEGY_FILE.read_text(encoding="utf-8")
    return ""

def load_audit() -> str:
    if AUDIT_FILE.exists():
        return AUDIT_FILE.read_text(encoding="utf-8")
    return ""


# ── TGStat API ────────────────────────────────────────────────────────

TGSTAT_ERRORS = {
    "quota_foreign_channel": "Канал не привязан к аккаунту TGStat. Откройте tgstat.ru, найдите канал и нажмите «Это мой канал».",
    "no_active_subscription": "Нет активной подписки на этот API. Проверьте тариф на tgstat.ru/my/profile.",
    "invalid_token": "Невалидный токен TGStat. Проверьте TGSTAT_TOKEN в .env.",
    "channel_not_found": "Канал не найден. Проверьте MOST_CHANNEL_ID в .env.",
}

async def tgstat_request(endpoint: str, params: dict = None) -> dict:
    if not TGSTAT_TOKEN:
        raise HTTPException(400, "TGSTAT_TOKEN не настроен в .env")
    params = params or {}
    params["token"] = TGSTAT_TOKEN
    if "channelId" not in params:
        params["channelId"] = CHANNEL_ID
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{TGSTAT_BASE}/{endpoint}", params=params)
        data = resp.json()
        if data.get("status") == "error":
            err_code = data.get("error", "unknown")
            human_msg = TGSTAT_ERRORS.get(err_code, f"TGStat API ошибка: {err_code}")
            raise HTTPException(502, human_msg)
        return data.get("response", data)


async def tgstat_cached() -> tuple[dict, str]:
    """Return (channel_stat, channel_title) from cache or fresh API call.
    Uses only 2 API requests, caches for TGSTAT_CACHE_TTL seconds."""
    now = time.time()
    if _tgstat_cache["ts"] and (now - _tgstat_cache["ts"]) < TGSTAT_CACHE_TTL:
        return _tgstat_cache["stat"] or {}, _tgstat_cache["info"].get("title", "MOST") if _tgstat_cache["info"] else "MOST"
    try:
        stat = await tgstat_request("channels/stat")
        info = await tgstat_request("channels/get")
        _tgstat_cache.update({"stat": stat, "info": info, "ts": now})
        return stat, info.get("title", "MOST")
    except Exception:
        if _tgstat_cache["stat"]:
            return _tgstat_cache["stat"], (_tgstat_cache["info"] or {}).get("title", "MOST")
        return {}, "MOST"


# ── Data Collection ───────────────────────────────────────────────────

@app.post("/api/collect", dependencies=[Depends(check_auth)])
async def collect_data():
    """Собрать свежие данные с TGStat и сохранить в SQLite."""
    channel_info = await tgstat_request("channels/get")
    channel_stat = await tgstat_request("channels/stat")

    now = int(time.time())
    week_ago = now - 7 * 86400
    posts_data = await tgstat_request("channels/posts", {
        "startTime": str(week_ago),
        "endTime": str(now),
        "limit": "50"
    })

    posts_list = posts_data if isinstance(posts_data, list) else posts_data.get("items", [])

    collected_at = datetime.utcnow().isoformat()
    raw = {
        "channel_info": channel_info,
        "channel_stat": channel_stat,
        "posts": posts_list
    }

    snap_params = (
        collected_at, channel_stat.get("participants_count", 0),
        channel_stat.get("avg_post_reach", 0), channel_stat.get("err_percent", 0),
        channel_stat.get("daily_reach", 0), channel_stat.get("ci_index", 0),
        len(posts_list), channel_info.get("title", "MOST"),
        json.dumps(raw, ensure_ascii=False))

    with get_db() as conn:
        row = conn.execute(INSERT_SNAPSHOT, snap_params).fetchone()
        snapshot_id = row["id"] if isinstance(row, dict) else row[0]

        for p in posts_list:
            post_id = p.get("id") or hashlib.md5(
                (p.get("link", "") + str(p.get("date", ""))).encode()
            ).hexdigest()
            date_val = p.get("date", "")
            if isinstance(date_val, (int, float)):
                date_val = datetime.utcfromtimestamp(date_val).isoformat()
            conn.execute(UPSERT_POST, (
                str(post_id), snapshot_id, date_val,
                (p.get("text") or "")[:500], p.get("views", 0),
                p.get("forwards_count", p.get("forwards", 0)),
                p.get("reactions_count", 0),
                p.get("shares_count", p.get("shares", 0)),
                p.get("link", "")))

    return {
        "status": "ok", "snapshot_id": snapshot_id,
        "participants": channel_stat.get("participants_count", 0),
        "posts_collected": len(posts_list), "collected_at": collected_at
    }


import re as _re

def _parse_views(text: str) -> int:
    text = text.strip().replace("\xa0", "").replace(" ", "")
    if text.endswith("K"):
        return int(float(text[:-1]) * 1000)
    if text.endswith("M"):
        return int(float(text[:-1]) * 1_000_000)
    try:
        return int(text)
    except ValueError:
        return 0


async def _scrape_telegram_channel(username: str, max_pages: int = 30):
    """Scrape public Telegram channel page for all posts with views/dates."""
    clean = username.lstrip("@")
    all_posts = []
    before = None

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
        for _ in range(max_pages):
            url = f"https://t.me/s/{clean}"
            if before:
                url += f"?before={before}"
            resp = await client.get(url)
            html = resp.text

            post_ids = _re.findall(rf'data-post="{clean}/(\d+)"', html)
            if not post_ids:
                if not all_posts:
                    print(f"[scraper] No posts found on page. Status={resp.status_code}, len={len(html)}, url={url}")
                break

            dates = _re.findall(r'datetime="([^"]+)"', html)
            views_raw = _re.findall(
                r'class="tgme_widget_message_views"[^>]*>([^<]+)', html
            )
            texts_raw = _re.findall(
                r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                html, _re.DOTALL
            )

            for i, pid in enumerate(post_ids):
                view_val = _parse_views(views_raw[i]) if i < len(views_raw) else 0
                date_val = dates[i] if i < len(dates) else ""
                raw_text = texts_raw[i] if i < len(texts_raw) else ""
                clean_text = _re.sub(r"<[^>]+>", " ", raw_text).strip()[:500]
                all_posts.append({
                    "post_id": pid,
                    "link": f"https://t.me/{clean}/{pid}",
                    "date": date_val,
                    "views": view_val,
                    "text": clean_text,
                })

            before = min(int(p) for p in post_ids)
            if before <= 1:
                break
            await asyncio.sleep(0.5)

    return all_posts


@app.post("/api/collect-history", dependencies=[Depends(check_auth)])
async def collect_history(days: int = Query(0)):
    """Загрузить все посты из публичной страницы Telegram-канала.
    days=0 означает «все посты», иначе — за последние N дней.
    TGStat не обязателен — если API недоступен, посты всё равно загрузятся."""

    channel_stat, channel_title = await tgstat_cached()

    all_posts = await _scrape_telegram_channel(CHANNEL_ID)

    if days > 0:
        cutoff = datetime.utcnow() - timedelta(days=days)
        filtered = []
        for p in all_posts:
            try:
                dt = datetime.fromisoformat(p["date"].replace("+00:00", "+00:00").replace("Z", "+00:00"))
                dt = dt.replace(tzinfo=None)
                if dt >= cutoff:
                    filtered.append(p)
            except Exception:
                filtered.append(p)
        all_posts = filtered

    collected_at = datetime.utcnow().isoformat()
    snap_params = (
        collected_at, channel_stat.get("participants_count", 0),
        channel_stat.get("avg_post_reach", 0), channel_stat.get("err_percent", 0),
        channel_stat.get("daily_reach", 0), channel_stat.get("ci_index", 0),
        len(all_posts), channel_title, "{}")

    with get_db() as conn:
        row = conn.execute(INSERT_SNAPSHOT, snap_params).fetchone()
        snapshot_id = row["id"] if isinstance(row, dict) else row[0]

        for p in all_posts:
            conn.execute(UPSERT_POST, (
                str(p["post_id"]), snapshot_id, p["date"],
                p["text"], p["views"], 0, 0, 0, p["link"]))

    return {
        "status": "ok", "days_collected": days if days > 0 else "all",
        "posts_collected": len(all_posts), "snapshot_id": snapshot_id,
        "tgstat_available": bool(channel_stat)
    }


@app.post("/api/upload-posts", dependencies=[Depends(check_auth)])
async def upload_posts(request: Request):
    """Принять список постов JSON (fallback если скрейпер не работает с сервера)."""
    body = await request.json()
    posts_data = body.get("posts", [])
    if not posts_data:
        raise HTTPException(400, "Нет постов в запросе")

    channel_stat, channel_title = await tgstat_cached()
    collected_at = datetime.utcnow().isoformat()

    snap_params = (
        collected_at, channel_stat.get("participants_count", 0),
        channel_stat.get("avg_post_reach", 0), channel_stat.get("err_percent", 0),
        channel_stat.get("daily_reach", 0), channel_stat.get("ci_index", 0),
        len(posts_data), channel_title, "{}")

    with get_db() as conn:
        row = conn.execute(INSERT_SNAPSHOT, snap_params).fetchone()
        snapshot_id = row["id"] if isinstance(row, dict) else row[0]
        for p in posts_data:
            conn.execute(UPSERT_POST, (
                str(p.get("post_id", "")), snapshot_id, p.get("date", ""),
                (p.get("text", ""))[:500], p.get("views", 0),
                0, 0, 0, p.get("link", "")))

    return {"status": "ok", "posts_uploaded": len(posts_data), "snapshot_id": snapshot_id}


# ── TGStat Premium Historical Data ────────────────────────────────────

@app.get("/api/tgstat-history", dependencies=[Depends(check_auth)])
async def get_tgstat_history():
    """Fetch historical data from TGStat Premium: subscribers, views, avg reach, ERR."""
    if not TGSTAT_TOKEN:
        raise HTTPException(400, "TGSTAT_TOKEN не настроен")

    end_date = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    params_base = {"startDate": start_date, "endDate": end_date, "group": "day"}

    results = {}
    endpoints = {
        "subscribers": "channels/subscribers",
        "views": "channels/views",
        "avg_reach": "channels/avg-posts-reach",
        "err": "channels/err",
    }

    for key, endpoint in endpoints.items():
        try:
            data = await tgstat_request(endpoint, params_base.copy())
            if isinstance(data, list):
                results[key] = data
            elif isinstance(data, dict) and "items" in data:
                results[key] = data["items"]
            else:
                results[key] = data if isinstance(data, list) else []
        except Exception as e:
            results[key] = {"error": str(e)}

    return results


# ── Data Retrieval ────────────────────────────────────────────────────

@app.get("/api/snapshots", dependencies=[Depends(check_auth)])
def get_snapshots(limit: int = 100):
    """Снэпшоты с реальными данными (participants > 0)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, collected_at, participants, avg_reach, err_percent, "
            "daily_reach, ci_index, posts_count, channel_title "
            "FROM snapshots WHERE participants > 0 ORDER BY collected_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/posts", dependencies=[Depends(check_auth)])
def get_posts(
    days: int = Query(30, description="За сколько дней"),
    sort: str = Query("views", description="Сортировка: views, date, forwards, reactions"),
    limit: int = Query(100)
):
    """Посты за период, с сортировкой."""
    valid_sorts = {"views", "date", "forwards", "reactions"}
    sort_col = sort if sort in valid_sorts else "views"
    order = "DESC" if sort_col != "date" else "DESC"

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT DISTINCT post_id, date, text, views, forwards, reactions, shares, link
            FROM posts WHERE date >= ?
            ORDER BY {sort_col} {order} LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/metrics", dependencies=[Depends(check_auth)])
def get_metrics_history():
    """Временной ряд ключевых метрик (для графиков). Пропускаем снэпшоты с нулевыми данными."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT collected_at, participants, avg_reach, err_percent, daily_reach, posts_count "
            "FROM snapshots WHERE participants > 0 ORDER BY collected_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/top-posts", dependencies=[Depends(check_auth)])
def get_top_posts(days: int = 30, limit: int = 10):
    """Топ постов по просмотрам за период."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT post_id, date, text, views, forwards, reactions, shares, link
            FROM posts WHERE date >= ?
            ORDER BY views DESC LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


# ── GPT Analysis ──────────────────────────────────────────────────────

def _build_post_summary(posts: list) -> str:
    from collections import defaultdict
    weeks = defaultdict(list)
    for p in posts:
        try:
            dt = datetime.fromisoformat(p["date"].replace("+00:00", "").replace("Z", ""))
            wk = dt.strftime("%Y-W%W")
        except Exception:
            wk = "unknown"
        ptype = classify_post(p.get("text", ""))
        weeks[wk].append({**p, "type": ptype, "week": wk})

    lines = []
    all_views = [p["views"] for p in posts if p.get("views")]
    avg_all = sum(all_views) / len(all_views) if all_views else 0
    lines.append(f"Всего постов: {len(posts)}, средний охват: {avg_all:.0f}")
    type_counts = defaultdict(lambda: {"count": 0, "views": 0})
    for p in posts:
        t = classify_post(p.get("text", ""))
        type_counts[t]["count"] += 1
        type_counts[t]["views"] += p.get("views", 0)
    lines.append("\nРаспределение по типам:")
    for t, d in sorted(type_counts.items(), key=lambda x: -x[1]["count"]):
        avg = d["views"] / d["count"] if d["count"] else 0
        lines.append(f"  {t}: {d['count']} постов, ср.охват {avg:.0f}")

    lines.append("\nПонедельная динамика:")
    for wk in sorted(weeks):
        wp = weeks[wk]
        avg = sum(p.get("views", 0) for p in wp) / len(wp) if wp else 0
        types = ", ".join(sorted(set(p["type"] for p in wp)))
        lines.append(f"  {wk}: {len(wp)} постов, ср.охват {avg:.0f} ({types})")

    lines.append("\nТоп-5 по охвату:")
    for p in sorted(posts, key=lambda x: x.get("views", 0), reverse=True)[:5]:
        t = classify_post(p.get("text", ""))
        lines.append(f"  [{p.get('views',0)} views, {t}] {p.get('text','')[:120]}")

    lines.append("\nАутсайдеры (5 худших):")
    for p in sorted(posts, key=lambda x: x.get("views", 0))[:5]:
        t = classify_post(p.get("text", ""))
        lines.append(f"  [{p.get('views',0)} views, {t}] {p.get('text','')[:120]}")

    return "\n".join(lines)


@app.post("/api/analyze", dependencies=[Depends(check_auth)])
async def run_analysis(days: int = Query(7), depth: str = Query("standard")):
    """GPT-анализ с привязкой к маркетинговой стратегии."""
    if not OPENAI_API_KEY:
        raise HTTPException(400, "OPENAI_API_KEY не настроен в .env")

    with get_db() as conn:
        snapshots = conn.execute(
            "SELECT * FROM snapshots ORDER BY collected_at DESC LIMIT 30"
        ).fetchall()
        snapshots = [dict(s) for s in snapshots]
        for s in snapshots:
            s.pop("raw_json", None)

        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        posts = conn.execute("""
            SELECT post_id, date, text, views, forwards, reactions, shares, link
            FROM posts WHERE date >= ?
            ORDER BY date DESC
        """, (cutoff,)).fetchall()
        posts = [dict(p) for p in posts]

    post_summary = _build_post_summary(posts)

    snap_summary = ""
    if snapshots:
        la = snapshots[0]
        snap_summary = (
            f"Подписчики: {la.get('participants', '?')}, "
            f"Ср.охват: {la.get('avg_reach', '?')}, "
            f"ERR: {la.get('err_percent', '?')}%, "
            f"Дневной охват: {la.get('daily_reach', '?')}, "
            f"CI: {la.get('ci_index', '?')}"
        )
        if len(snapshots) > 1:
            pr = snapshots[-1]
            snap_summary += (
                f"\nПредыдущий снимок ({pr.get('collected_at', '?')[:10]}): "
                f"Подписчики: {pr.get('participants', '?')}, "
                f"Ср.охват: {pr.get('avg_reach', '?')}"
            )

    strategy = load_strategy()
    strategy_excerpt = strategy[:3000] if strategy else "Стратегия не загружена."

    system_prompt = f"""Ты — head of growth Telegram-канала @mostcareer (iGaming рекрутинг).
Ты проводишь аналитику для команды, которая принимает решения по контенту.

КОНТЕКСТ КАНАЛА:
- ~1100 подписчиков, цель — привлечь специалистов iGaming (и активных соискателей, и пассивных)
- Комментарии отключены
- Индустрия закрытая: нельзя давать конкретные кейсы компаний, цифры из NDA, рецепты по трафику

НАША СТРАТЕГИЯ (следуй ей при оценке):
{strategy_excerpt}

ПРАВИЛА АНАЛИЗА:
1. КАЖДЫЙ пост оценивай конкретно: сработал / средне / провалился. Укажи ПОЧЕМУ (тема, формат, время, длина, тип).
2. Сравнивай охваты по типам контента: vacancy, career, story, announce.
3. Привязывай всё к стратегии: регулярный контент ~65% карьера/HR + ~35% истории. Вакансии и анонсы — ситуативные, не планируются. Соблюдается ли баланс? Скажи прямо.
4. Подписчики: рост или отток? Что могло повлиять?
5. НЕ ДАВАЙ поверхностных советов типа «делитесь кейсами», «больше вовлекайте аудиторию», «используйте storytelling». Только конкретные, actionable рекомендации.
6. Формат: структурированный Markdown, с цифрами и процентами.
7. В конце — 3 конкретных действия на следующую неделю с днями и темами."""

    depth_prompts = {
        "standard": f"""Проведи анализ канала за последние {days} дней.

ТЕКУЩИЕ МЕТРИКИ: {snap_summary}

ДАННЫЕ ПО ПОСТАМ:
{post_summary}

Структура отчёта:
1. **Вердикт** — одно предложение: канал растёт / стагнирует / падает?
2. **Метрики** — подписчики, охват, ERR: динамика с цифрами
3. **Контент-микс** — фактическое соотношение типов vs стратегия (30/30/20/10/10)
4. **Топ-3 и антитоп-3** — конкретные посты с разбором: почему сработал / провалился
5. **Соответствие стратегии** — что соблюдается, что нет
6. **Рекомендации** — 3-5 конкретных шагов (день, формат, тема)""",

        "deep": f"""Проведи глубокий анализ канала за последние {days} дней. Это стратегический отчёт для принятия решений.

ТЕКУЩИЕ МЕТРИКИ: {snap_summary}

ДАННЫЕ ПО ПОСТАМ:
{post_summary}

Структура:
1. **Executive summary** — 3 предложения: состояние канала, главная проблема, главная возможность
2. **Динамика метрик** — понедельный тренд охватов, сравнение с предыдущим периодом, % изменений
3. **Аудит контент-микса** — таблица: тип / кол-во / доля / ср.охват / vs стратегия
4. **Разбор каждого поста** — таблица: дата / тип / охват / vs среднее / вердикт (1-2 слова почему)
5. **Анализ времени** — какие дни и часы дают лучший результат (даты постов в UTC, прибавляй +3ч для МСК, все выводы по времени давай в МСК)
6. **Подписчики** — рост/отток, что на это влияет
7. **Оценка стратегии** — работает ли текущий подход? Что корректировать?
8. **Риски** — что может ухудшить ситуацию
9. **План на следующие 2 недели** — конкретные посты с датами, темами, форматами
10. **Эксперименты** — 2-3 идеи, которые стоит попробовать и как измерить результат"""
    }

    user_prompt = depth_prompts.get(depth, depth_prompts["standard"])

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o",
                "temperature": 0.3,
                "max_tokens": 8000,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }
        )
        result = resp.json()

    gpt_text = result.get("choices", [{}])[0].get("message", {}).get("content", "Ошибка GPT")

    now = datetime.utcnow().isoformat()
    period_start = (datetime.utcnow() - timedelta(days=days)).isoformat()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO analyses (created_at, period_start, period_end, analysis_type, gpt_response, snapshots_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, period_start, now, depth, gpt_text,
             json.dumps([s["id"] for s in snapshots])))

    return {
        "analysis": gpt_text,
        "depth": depth,
        "period_days": days,
        "posts_analyzed": len(posts),
    }


@app.get("/api/analyses", dependencies=[Depends(check_auth)])
def get_analyses(limit: int = 20):
    """История всех GPT-анализов."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, period_start, period_end, analysis_type, gpt_response "
            "FROM analyses ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Export ────────────────────────────────────────────────────────────

@app.get("/api/export/csv", dependencies=[Depends(check_auth)])
def export_csv(days: int = 30):
    """Экспорт постов в CSV."""
    import csv
    import io

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT post_id, date, text, views, forwards, reactions, shares, link
            FROM posts WHERE date >= ?
            ORDER BY date DESC
        """, (cutoff,)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["post_id", "date", "text", "views", "forwards", "reactions", "shares", "link"])
    for r in rows:
        writer.writerow([r["post_id"], r["date"], r["text"][:200], r["views"],
                        r["forwards"], r["reactions"], r["shares"], r["link"]])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=most_posts_{days}d.csv"}
    )


@app.get("/api/export/report", dependencies=[Depends(check_auth)])
async def export_report(days: int = 7):
    """Экспорт полного отчёта в Markdown."""
    analysis = await run_analysis(days=days, depth="standard")
    with get_db() as conn:
        snap = conn.execute(
            "SELECT * FROM snapshots ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()

    md = f"# Отчёт MOST Analytics\n\n"
    md += f"**Период:** последние {days} дней\n"
    md += f"**Дата:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}\n\n"
    if snap:
        md += f"## Метрики\n"
        md += f"- Подписчики: {snap['participants']:,}\n"
        md += f"- Средний охват: {snap['avg_reach']:,}\n"
        md += f"- ERR: {snap['err_percent']}%\n\n"
    md += f"## Анализ\n\n{analysis['analysis']}\n"

    return StreamingResponse(
        iter([md]),
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=most_report_{days}d.md"}
    )


# ── Posts with classification ─────────────────────────────────────────

@app.get("/api/posts-classified", dependencies=[Depends(check_auth)])
def get_posts_classified(days: int = 30):
    """Posts with auto-classified type and vs-average delta."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT post_id, date, text, views, forwards, reactions, shares, link
            FROM posts WHERE date >= ? ORDER BY date DESC
        """, (cutoff,)).fetchall()
    posts = [dict(r) for r in rows]
    if not posts:
        return []
    avg_views = sum(p["views"] for p in posts) / len(posts)
    for p in posts:
        p["type"] = classify_post(p.get("text", ""))
        p["vs_avg"] = round((p["views"] - avg_views) / avg_views * 100, 1) if avg_views else 0
    return posts


@app.get("/api/posts-timeline", dependencies=[Depends(check_auth)])
def get_posts_timeline():
    """Понедельная агрегация постов для графиков (views, reach, count)."""
    from collections import defaultdict
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date, views, text FROM posts WHERE date != '' ORDER BY date ASC"
        ).fetchall()

    weeks = defaultdict(lambda: {"views": 0, "count": 0, "types": defaultdict(int)})
    months = defaultdict(lambda: {"views": 0, "count": 0})

    for r in rows:
        try:
            dt = datetime.fromisoformat(r["date"].replace("+00:00", "").replace("Z", ""))
            wk = dt.strftime("%Y-W%W")
            mo = dt.strftime("%Y-%m")
            weeks[wk]["views"] += r["views"]
            weeks[wk]["count"] += 1
            weeks[wk]["types"][classify_post(r["text"])] += 1
            months[mo]["views"] += r["views"]
            months[mo]["count"] += 1
        except Exception:
            pass

    weekly = []
    for wk in sorted(weeks):
        d = weeks[wk]
        weekly.append({
            "week": wk,
            "total_views": d["views"],
            "avg_views": round(d["views"] / d["count"]) if d["count"] else 0,
            "posts_count": d["count"],
            "types": dict(d["types"]),
        })

    monthly = []
    for mo in sorted(months):
        d = months[mo]
        monthly.append({
            "month": mo,
            "total_views": d["views"],
            "avg_views": round(d["views"] / d["count"]) if d["count"] else 0,
            "posts_count": d["count"],
        })

    return {"weekly": weekly, "monthly": monthly}


@app.get("/api/content-mix", dependencies=[Depends(check_auth)])
def get_content_mix(days: int = 30):
    """Content mix breakdown for charts."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT text, views FROM posts WHERE date >= ?", (cutoff,)
        ).fetchall()
    from collections import defaultdict
    mix = defaultdict(lambda: {"count": 0, "total_views": 0})
    for r in rows:
        t = classify_post(r["text"])
        mix[t]["count"] += 1
        mix[t]["total_views"] += r["views"]
    result = []
    for t, d in mix.items():
        result.append({
            "type": t,
            "count": d["count"],
            "avg_views": round(d["total_views"] / d["count"]) if d["count"] else 0,
            "total_views": d["total_views"]
        })
    return sorted(result, key=lambda x: -x["count"])


# ── Posting Analysis (hours, days, types) ────────────────────────────

@app.get("/api/posting-analysis", dependencies=[Depends(check_auth)])
def get_posting_analysis(days: int = 180):
    """Real posting time analysis: hours (MSK), days, type performance."""
    from collections import defaultdict, Counter
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date, views, text FROM posts WHERE date >= ? AND date != '' ORDER BY date ASC",
            (cutoff,)
        ).fetchall()

    seen_links = set()
    posts = []
    for r in rows:
        d = dict(r)
        date_key = d["date"][:16]
        if date_key in seen_links:
            continue
        seen_links.add(date_key)
        posts.append(d)

    hours = Counter()
    day_views = defaultdict(list)
    type_views = defaultdict(list)
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    for p in posts:
        try:
            dt = datetime.fromisoformat(p["date"].replace("+00:00", "").replace("Z", ""))
            msk_hour = (dt.hour + 3) % 24
            hours[msk_hour] += 1
            wd = dt.weekday()
            day_views[wd].append(p.get("views", 0))
        except Exception:
            pass
        ptype = classify_post(p.get("text", ""))
        type_views[ptype].append(p.get("views", 0))

    hours_data = [{"hour": h, "count": hours.get(h, 0)} for h in range(24)]

    days_data = []
    for wd in range(7):
        vs = day_views.get(wd, [])
        days_data.append({
            "day": day_names[wd],
            "day_idx": wd,
            "posts": len(vs),
            "avg_views": round(sum(vs) / len(vs)) if vs else 0,
        })

    types_data = []
    for t, vs in type_views.items():
        types_data.append({
            "type": t,
            "count": len(vs),
            "avg_views": round(sum(vs) / len(vs)) if vs else 0,
            "total_views": sum(vs),
        })
    types_data.sort(key=lambda x: -x["avg_views"])

    peak_hour = max(hours, key=hours.get) if hours else 15
    best_day = max(days_data, key=lambda x: x["avg_views"]) if days_data else None

    return {
        "total_posts": len(posts),
        "hours": hours_data,
        "days": days_data,
        "types": types_data,
        "peak_posting_hour": peak_hour,
        "best_day": best_day["day"] if best_day else "Чт",
        "best_day_avg": best_day["avg_views"] if best_day else 0,
    }


# ── Cron (daily auto-collect) ────────────────────────────────────────

@app.post("/api/cron/daily")
async def cron_daily(request: Request):
    """Ежедневный сбор данных (вызывается Render cron)."""
    secret = request.headers.get("X-Cron-Secret", "")
    if secret != CRON_SECRET:
        raise HTTPException(403, "Invalid cron secret")

    results = {}

    try:
        _tgstat_cache["ts"] = 0  # force refresh
        channel_info = await tgstat_request("channels/get")
        channel_stat = await tgstat_request("channels/stat")
        _tgstat_cache.update({"stat": channel_stat, "info": channel_info, "ts": time.time()})
        now_ts = int(time.time())
        two_days_ago = now_ts - 2 * 86400
        posts_data = await tgstat_request("channels/posts", {
            "startTime": str(two_days_ago), "endTime": str(now_ts), "limit": "50"
        })
        posts_list = posts_data if isinstance(posts_data, list) else posts_data.get("items", [])

        collected_at = datetime.utcnow().isoformat()
        snap_params = (
            collected_at, channel_stat.get("participants_count", 0),
            channel_stat.get("avg_post_reach", 0), channel_stat.get("err_percent", 0),
            channel_stat.get("daily_reach", 0), channel_stat.get("ci_index", 0),
            len(posts_list), channel_info.get("title", "MOST"),
            json.dumps({"channel_stat": channel_stat}, ensure_ascii=False))

        with get_db() as conn:
            row = conn.execute(INSERT_SNAPSHOT, snap_params).fetchone()
            snapshot_id = row["id"] if isinstance(row, dict) else row[0]

            for p in posts_list:
                post_id = p.get("id") or hashlib.md5(
                    (p.get("link", "") + str(p.get("date", ""))).encode()
                ).hexdigest()
                date_val = p.get("date", "")
                if isinstance(date_val, (int, float)):
                    date_val = datetime.utcfromtimestamp(date_val).isoformat()
                conn.execute(UPSERT_POST, (
                    str(post_id), snapshot_id, date_val,
                    (p.get("text") or "")[:500], p.get("views", 0),
                    p.get("forwards_count", p.get("forwards", 0)),
                    p.get("reactions_count", 0),
                    p.get("shares_count", p.get("shares", 0)),
                    p.get("link", "")))

        results["tgstat"] = {"ok": True, "snapshot_id": snapshot_id, "posts": len(posts_list)}
    except Exception as e:
        results["tgstat"] = {"ok": False, "error": str(e)}

    try:
        scraped = await _scrape_telegram_channel(CHANNEL_ID, max_pages=2)
        new_count = 0
        if scraped:
            with get_db() as conn:
                for p in scraped:
                    try:
                        conn.execute(IGNORE_POST, (
                            str(p["post_id"]), 0, p["date"], p["text"],
                            p["views"], 0, 0, 0, p["link"]))
                        new_count += 1
                    except Exception:
                        pass
        results["scrape"] = {"ok": True, "posts_checked": len(scraped), "new": new_count}
    except Exception as e:
        results["scrape"] = {"ok": False, "error": str(e)}

    return {"status": "ok", "collected_at": datetime.utcnow().isoformat(), "results": results}


# ── Health & Config ───────────────────────────────────────────────────

@app.get("/api/health")
def health():
    with get_db() as conn:
        last_snap = conn.execute(
            "SELECT collected_at FROM snapshots ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()
    last_ts = None
    if last_snap:
        last_ts = last_snap["collected_at"] if isinstance(last_snap, dict) else dict(last_snap)["collected_at"]
    return {
        "status": "ok",
        "tgstat_configured": bool(TGSTAT_TOKEN and TGSTAT_TOKEN != "your_tgstat_token_here"),
        "openai_configured": bool(OPENAI_API_KEY and OPENAI_API_KEY != "sk-your_openai_key_here"),
        "channel_id": CHANNEL_ID,
        "db": "postgresql" if (DATABASE_URL and psycopg2) else "sqlite",
        "last_collected": last_ts
    }


# ── Strategy & Audit API ──────────────────────────────────────────────

@app.get("/api/strategy", dependencies=[Depends(check_auth)])
def get_strategy_content():
    return {"strategy": load_strategy(), "audit": load_audit()}


# ── Serve Frontend ────────────────────────────────────────────────────

@app.get("/strategy")
def serve_strategy(request: Request):
    if DASHBOARD_PASSWORD:
        token = request.cookies.get("session", "")
        if token not in VALID_TOKENS:
            return RedirectResponse("/login")
    return FileResponse(Path(__file__).parent / "strategy.html")


# ── Keep-alive (prevent Render free-tier cold starts) ────────────────

async def _keep_alive_loop():
    """Ping self every 10 minutes to prevent Render from sleeping."""
    url = RENDER_URL.rstrip("/") + "/api/health" if RENDER_URL else None
    if not url:
        return
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                await client.get(url)
            except Exception:
                pass
            await asyncio.sleep(600)

@app.on_event("startup")
async def start_keep_alive():
    if RENDER_URL:
        asyncio.create_task(_keep_alive_loop())


@app.get("/")
def serve_index(request: Request):
    if DASHBOARD_PASSWORD:
        token = request.cookies.get("session", "")
        if token not in VALID_TOKENS:
            return RedirectResponse("/login")
    return FileResponse(Path(__file__).parent / "index.html")
