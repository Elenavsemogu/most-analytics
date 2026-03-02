"""
MOST Analytics Dashboard — Backend
FastAPI сервер: сбор данных TGStat, хранение в SQLite, GPT-анализ.
Запуск: uvicorn server:app --reload --port 8090
"""

import os
import json
import time
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

load_dotenv()

TGSTAT_TOKEN = os.getenv("TGSTAT_TOKEN", "")
CHANNEL_ID = os.getenv("MOST_CHANNEL_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
TGSTAT_BASE = "https://api.tgstat.ru"
DB_PATH = Path(__file__).parent / "analytics.db"

app = FastAPI(title="MOST Analytics")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

VALID_TOKENS: set[str] = set()

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

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at TEXT NOT NULL,
                participants INTEGER,
                avg_reach INTEGER,
                err_percent REAL,
                daily_reach INTEGER,
                ci_index REAL,
                posts_count INTEGER,
                channel_title TEXT,
                raw_json TEXT
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY,
                post_id TEXT UNIQUE,
                snapshot_id INTEGER,
                date TEXT,
                text TEXT,
                views INTEGER DEFAULT 0,
                forwards INTEGER DEFAULT 0,
                reactions INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                link TEXT,
                FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
            );
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT,
                analysis_type TEXT DEFAULT 'weekly',
                gpt_response TEXT,
                snapshots_used TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(collected_at);
            CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date);
            CREATE INDEX IF NOT EXISTS idx_posts_views ON posts(views);
        """)

init_db()


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

    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO snapshots (collected_at, participants, avg_reach, err_percent,
                daily_reach, ci_index, posts_count, channel_title, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            collected_at,
            channel_stat.get("participants_count", 0),
            channel_stat.get("avg_post_reach", 0),
            channel_stat.get("err_percent", 0),
            channel_stat.get("daily_reach", 0),
            channel_stat.get("ci_index", 0),
            len(posts_list),
            channel_info.get("title", "MOST"),
            json.dumps(raw, ensure_ascii=False)
        ))
        snapshot_id = cur.lastrowid

        for p in posts_list:
            post_id = p.get("id") or hashlib.md5(
                (p.get("link", "") + str(p.get("date", ""))).encode()
            ).hexdigest()
            date_val = p.get("date", "")
            if isinstance(date_val, (int, float)):
                date_val = datetime.utcfromtimestamp(date_val).isoformat()
            conn.execute("""
                INSERT OR REPLACE INTO posts
                    (post_id, snapshot_id, date, text, views, forwards, reactions, shares, link)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(post_id), snapshot_id, date_val,
                (p.get("text") or "")[:500],
                p.get("views", 0),
                p.get("forwards_count", p.get("forwards", 0)),
                p.get("reactions_count", 0),
                p.get("shares_count", p.get("shares", 0)),
                p.get("link", "")
            ))

    return {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "participants": channel_stat.get("participants_count", 0),
        "posts_collected": len(posts_list),
        "collected_at": collected_at
    }


# ── Data Retrieval ────────────────────────────────────────────────────

@app.get("/api/snapshots", dependencies=[Depends(check_auth)])
def get_snapshots(limit: int = 100):
    """Все снэпшоты (история сборов)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, collected_at, participants, avg_reach, err_percent, "
            "daily_reach, ci_index, posts_count, channel_title "
            "FROM snapshots ORDER BY collected_at DESC LIMIT ?", (limit,)
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
    """Временной ряд ключевых метрик (для графиков)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT collected_at, participants, avg_reach, err_percent, daily_reach, posts_count "
            "FROM snapshots ORDER BY collected_at ASC"
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

@app.post("/api/analyze", dependencies=[Depends(check_auth)])
async def run_analysis(days: int = Query(7), depth: str = Query("standard")):
    """GPT-анализ с историческим контекстом."""
    if not OPENAI_API_KEY:
        raise HTTPException(400, "OPENAI_API_KEY не настроен в .env")

    with get_db() as conn:
        snapshots = conn.execute(
            "SELECT * FROM snapshots ORDER BY collected_at DESC LIMIT 12"
        ).fetchall()
        snapshots = [dict(s) for s in snapshots]
        for s in snapshots:
            s.pop("raw_json", None)

        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        posts = conn.execute("""
            SELECT post_id, date, text, views, forwards, reactions, shares, link
            FROM posts WHERE date >= ?
            ORDER BY views DESC LIMIT 30
        """, (cutoff,)).fetchall()
        posts = [dict(p) for p in posts]

        prev_analyses = conn.execute(
            "SELECT created_at, period_start, period_end, analysis_type FROM analyses "
            "ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        prev_analyses = [dict(a) for a in prev_analyses]

    data_context = json.dumps({
        "snapshots_history": snapshots,
        "posts_last_period": posts,
        "analysis_period_days": days,
        "previous_analyses_count": len(prev_analyses)
    }, ensure_ascii=False, default=str)

    system_prompt = """Ты — старший аналитик Telegram-каналов с глубокой экспертизой в контент-маркетинге и iGaming.
Ты анализируешь данные канала MOST. У тебя есть доступ к истории снэпшотов (несколько сборов данных) и постам.

Твои задачи:
1. Выявить тренды роста/падения подписчиков и охватов
2. Определить какие посты сработали лучше всего и ПОЧЕМУ (формат, тема, время, длина)
3. Проанализировать отток и прирост аудитории
4. Дать конкретные рекомендации: что публиковать, когда, в каком формате
5. Предложить стратегии привлечения новых подписчиков
6. Сравнить текущий период с предыдущими (если есть история)

Пиши на русском, структурированно, с конкретными цифрами. Используй Markdown."""

    depth_prompts = {
        "quick": "Сделай краткий обзор за период: 3-5 ключевых наблюдений и 2-3 рекомендации.",
        "standard": """Проанализируй данные и сформируй отчёт:
1. Ключевые метрики и их динамика
2. Топ-5 постов: почему они сработали (тема, формат, время публикации)
3. Анализ аудитории: прирост, отток, тренды
4. Что можно улучшить: конкретные рекомендации по контенту
5. Стратегии привлечения новых подписчиков
6. Риски и точки внимания""",
        "deep": """Сделай углублённый анализ:
1. Детальная динамика метрик с процентами изменений между периодами
2. Сегментация контента: какие типы/форматы/темы дают лучший отклик
3. Анализ лучшего времени публикаций (по дням и часам если видно из дат)
4. Топ-10 постов с разбором: что именно зацепило аудиторию
5. Глубокий анализ роста: органика vs вирусность, откуда приходят подписчики
6. Конкурентные рекомендации: что делают успешные каналы в нише
7. Прогноз на следующий период
8. Пошаговый план действий на неделю: что публиковать, когда, в каком формате
9. Идеи для экспериментов и A/B тестов контента
10. Стратегия роста на месяц: как найти 500+ новых подписчиков"""
    }

    user_prompt = f"""{depth_prompts.get(depth, depth_prompts['standard'])}

Данные канала:
{data_context}"""

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "temperature": 0.4,
                "max_tokens": 4000,
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
        conn.execute("""
            INSERT INTO analyses (created_at, period_start, period_end, analysis_type, gpt_response, snapshots_used)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now, period_start, now, depth, gpt_text,
              json.dumps([s["id"] for s in snapshots])))

    return {
        "analysis": gpt_text,
        "depth": depth,
        "period_days": days,
        "posts_analyzed": len(posts),
        "snapshots_used": len(snapshots),
        "created_at": now
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


# ── Health & Config ───────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "tgstat_configured": bool(TGSTAT_TOKEN and TGSTAT_TOKEN != "your_tgstat_token_here"),
        "openai_configured": bool(OPENAI_API_KEY and OPENAI_API_KEY != "sk-your_openai_key_here"),
        "channel_id": CHANNEL_ID,
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists()
    }


# ── Serve Frontend ────────────────────────────────────────────────────

@app.get("/")
def serve_index(request: Request):
    if DASHBOARD_PASSWORD:
        token = request.cookies.get("session", "")
        if token not in VALID_TOKENS:
            return RedirectResponse("/login")
    return FileResponse(Path(__file__).parent / "index.html")
