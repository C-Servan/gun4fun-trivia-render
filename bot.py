import os
import sqlite3
import json
import random
import logging
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Tuple, List, Dict

import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters, PicklePersistence
)

# ---------- Config ----------
load_dotenv()
TOKEN = os.getenv("TOKEN")
TZNAME = os.getenv("TZ", "Europe/Madrid")
TZ = pytz.timezone(TZNAME)

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger("GUN4FUN-LG-TRIVIA")

if not TOKEN:
    raise RuntimeError("Falta TOKEN en variables de entorno")

DB_PATH = "trivia.db"
QUESTION_WINDOW_SECONDS = 180
DAILY_TIMES = ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"]
SUMMARY_TIME = "21:00"

PHRASE_START = [
    "🎯 *Instructor GUN4FUN:* ¡Hora de afinar puntería!",
    "🔫 *Instructor GUN4FUN:* Carga, apunta… ¡y dispara a la respuesta!",
    "🎮 *Instructor GUN4FUN:* ¿Listos para la siguiente ronda?",
    "🏹 *Instructor GUN4FUN:* Precisión ante todo. ¡A por ello!",
]
PHRASE_ENCOURAGE = [
    "👉 ¡Participa y sube en el ranking!",
    "💥 ¡Tu acierto puede decidir el top 5 del día!",
    "⚡ Velocidad y puntería marcan la diferencia.",
    "🏆 Cada punto cuenta para las medallas diarias.",
]
PHRASE_SUMMARY = [
    "📢 *Instructor GUN4FUN:* Gran jornada, equipo.",
    "📝 *Instructor GUN4FUN:* Resumen del día listo.",
    "🎉 *Instructor GUN4FUN:* ¡Buen trabajo! Vamos con el ranking.",
]

BADGES = [
    {"code":"BRONCE_DIA","name":"Medalla de Bronce (Día)","desc":"≥ 3 aciertos hoy","type":"dia"},
    {"code":"PLATA_DIA","name":"Medalla de Plata (Día)","desc":"≥ 5 aciertos hoy","type":"dia"},
    {"code":"ORO_DIA","name":"Medalla de Oro (Día)","desc":"≥ 6 aciertos hoy","type":"dia"},
    {"code":"RACHA_3","name":"Racha x3","desc":"3 aciertos seguidos","type":"streak"},
    {"code":"RACHA_5","name":"Racha x5","desc":"5 aciertos seguidos","type":"streak"},
]

# ---------- Data ----------
def load_questions(path="questions_lightgun_es.json"):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cleaned = [q for q in data if "q" in q and "choices" in q and "answer" in q and q["answer"] in q["choices"]]
    if not cleaned:
        raise RuntimeError("No se cargaron preguntas válidas del JSON.")
    return cleaned

QUESTIONS = load_questions()

# ---------- DB ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER,
            user_id INTEGER,
            name TEXT,
            last_seen_ts INTEGER,
            PRIMARY KEY(chat_id, user_id)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            question TEXT,
            choices TEXT,
            answer TEXT,
            start_ts INTEGER,
            end_ts INTEGER
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS answers(
            event_id INTEGER,
            user_id INTEGER,
            choice TEXT,
            correct INTEGER,
            ts INTEGER,
            PRIMARY KEY(event_id, user_id)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS streaks(
            chat_id INTEGER,
            user_id INTEGER,
            streak INTEGER DEFAULT 0,
            best_streak INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS badges(
            chat_id INTEGER,
            user_id INTEGER,
            code TEXT,
            name TEXT,
            ts INTEGER,
            period TEXT,
            period_key TEXT,
            PRIMARY KEY(chat_id, user_id, code, period_key)
        );
        """)
        c.commit()

def now_ts() -> int:
    return int(datetime.now(tz=TZ).timestamp())

def parse_hhmm(s: str) -> Optional[Tuple[int,int]]:
    try:
        hh, mm = s.split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except Exception:
        return None
    return None

def local_time(hh: int, mm: int) -> dtime:
    return dtime(hour=hh, minute=mm, tzinfo=TZ)

# ---------- Bot logic ----------
async def touch_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type not in ("group","supergroup"):
        return
    u = update.effective_user
    ch = update.effective_chat
    with db() as c:
        c.execute("""
        INSERT INTO users(chat_id, user_id, name, last_seen_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
        name=excluded.name, last_seen_ts=excluded.last_seen_ts
        """, (ch.id, u.id, u.full_name, now_ts()))
        c.execute("""
        INSERT INTO streaks(chat_id, user_id, streak, best_streak)
        VALUES(?, ?, 0, 0)
        ON CONFLICT(chat_id, user_id) DO NOTHING
        """, (ch.id, u.id))
        c.commit()

async def schedule_jobs(app):
    for idx, hhmm in enumerate(DAILY_TIMES):
        hh, mm = parse_hhmm(hhmm)
        app.job_queue.run_daily(trivia_job, time=local_time(hh, mm), name=f"q_{idx}")
    hh, mm = parse_hhmm(SUMMARY_TIME)
    app.job_queue.run_daily(daily_summary_job, time=local_time(hh, mm), name="daily_summary")

def pick_question() -> dict:
    return random.choice(QUESTIONS)

async def trivia_job(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = getattr(context.application.bot_data, "chat_ids", set())
    if not chat_ids:
        return
    for chat_id in chat_ids:
        q = pick_question()
        start = now_ts()
        end = start + QUESTION_WINDOW_SECONDS
        with db() as c:
            c.execute("""
                INSERT INTO events(chat_id, question, choices, answer, start_ts, end_ts)
                VALUES(?, ?, ?, ?, ?, ?)
            """, (chat_id, q["q"], "|".join(q["choices"]), q["answer"], start, end))
            event_id = c.lastrowid
            c.commit()

        buttons = [[InlineKeyboardButton(opt, callback_data=f"ans|{event_id}|{opt}")]
                   for opt in q["choices"]]
        msg = (
            f"{random.choice(PHRASE_START)}\n\n"
            "🎯 *TRIVIA LIGHT-GUN*\n\n"
            f"{q['q']}\n\n"
            f"⏱️ Tienes {QUESTION_WINDOW_SECONDS//60} min.\n"
            f"{random.choice(PHRASE_ENCOURAGE)}"
        )
        await context.bot.send_message(
            chat_id,
            text=msg,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        context.job_queue.run_once(close_event_job, when=QUESTION_WINDOW_SECONDS, data={"event_id": event_id}, chat_id=chat_id)

async def close_event_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    event_id = data.get("event_id")
    if not event_id:
        return

    with db() as c:
        ev = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            return
        ans = c.execute("""
            SELECT a.*, u.name FROM answers a
            LEFT JOIN users u ON u.chat_id=? AND u.user_id=a.user_id
            WHERE a.event_id=?
        """, (ev["chat_id"], event_id)).fetchall()

        # update streaks
        for r in ans:
            ok = 1 if r["correct"] == 1 else 0
            row = c.execute("SELECT streak, best_streak FROM streaks WHERE chat_id=? AND user_id=?", (ev["chat_id"], r["user_id"])).fetchone()
            if not row:
                c.execute("INSERT INTO streaks(chat_id, user_id, streak, best_streak) VALUES(?,?,0,0)", (ev["chat_id"], r["user_id"]))
                row = {"streak":0, "best_streak":0}
            streak = (row["streak"] + 1) if ok else 0
            best = max(row["best_streak"], streak)
            c.execute("UPDATE streaks SET streak=?, best_streak=? WHERE chat_id=? AND user_id=?", (streak, best, ev["chat_id"], r["user_id"]))
        c.commit()

        cutoff = now_ts() - 30*24*3600
        roster = c.execute("""
            SELECT user_id, name FROM users WHERE chat_id=? AND last_seen_ts>=?
        """, (ev["chat_id"], cutoff)).fetchall()

    answered_ids = {r["user_id"] for r in ans}
    roster_ids = {r["user_id"] for r in roster}
    not_answered_ids = roster_ids - answered_ids

    correct = sum(1 for r in ans if r["correct"] == 1)
    wrong = sum(1 for r in ans if r["correct"] == 0)
    not_ans = len(not_answered_ids)

    winners = [r["name"] or f"ID {r['user_id']}" for r in ans if r["correct"] == 1][:5]
    txt = (
        f"📊 *Cierre de pregunta*\n"
        f"❓ {ev['question']}\n\n"
        f"✅ Aciertos: {correct}   ❌ Fallos: {wrong}   👀 No contestaron: {not_ans}\n"
        + (("🏅 Aciertos: " + ", ".join(winners)) if winners else "")
    )
    await context.bot.send_message(ev["chat_id"], txt, parse_mode="Markdown")

async def answer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, event_id_str, choice = q.data.split("|", 2)
        event_id = int(event_id_str)
    except Exception:
        return

    with db() as c:
        ev = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            await q.answer("Evento no encontrado.")
            return
        now = now_ts()
        if now > ev["end_ts"]:
            await q.answer("⏱️ Fuera de tiempo.")
            return

        user = q.from_user
        c.execute("""
        INSERT INTO users(chat_id, user_id, name, last_seen_ts)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
        name=excluded.name, last_seen_ts=excluded.last_seen_ts
        """, (ev["chat_id"], user.id, user.full_name, now))
        c.execute("""
        INSERT INTO streaks(chat_id, user_id, streak, best_streak)
        VALUES(?, ?, 0, 0)
        ON CONFLICT(chat_id, user_id) DO NOTHING
        """, (ev["chat_id"], user.id))

        try:
            c.execute("""
                INSERT INTO answers(event_id, user_id, choice, correct, ts)
                VALUES(?, ?, ?, ?, ?)
            """, (event_id, user.id, choice, 1 if choice == ev["answer"] else 0, now))
            c.commit()
            if choice == ev["answer"]:
                await q.answer("✅ ¡Correcto!")
            else:
                await q.answer("❌ Incorrecto")
        except sqlite3.IntegrityError:
            await q.answer("Ya respondiste esta pregunta.")

def period_bounds(kind: str) -> Tuple[int,int]:
    now_local = datetime.now(TZ)
    if kind == "dia":
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif kind == "semana":
        start = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    else:
        start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year+1, month=1)
        else:
            end = start.replace(month=start.month+1)
    return int(start.timestamp()), int(end.timestamp())

def fetch_rank(chat_id: int, kind: str):
    t0, t1 = period_bounds(kind)
    with db() as c:
        evs = c.execute(
            "SELECT id FROM events WHERE chat_id=? AND start_ts>=? AND start_ts<?",
            (chat_id, t0, t1)
        ).fetchall()
        if not evs:
            return [], [], []
        ids = tuple(r["id"] for r in evs)
        q = f"""
            SELECT a.user_id, u.name,
                   SUM(CASE WHEN a.correct=1 THEN 1 ELSE 0 END) AS aciertos,
                   SUM(CASE WHEN a.correct=0 THEN 1 ELSE 0 END) AS fallos
            FROM answers a
            LEFT JOIN users u ON u.chat_id=? AND u.user_id=a.user_id
            WHERE a.event_id IN ({",".join("?"*len(ids))})
            GROUP BY a.user_id
            ORDER BY aciertos DESC, fallos ASC
        """
        rows = c.execute(q, (chat_id, *ids)).fetchall()

        cutoff = now_ts() - 30*24*3600
        roster = c.execute(
            "SELECT user_id, name FROM users WHERE chat_id=? AND last_seen_ts>=?",
            (chat_id, cutoff)
        ).fetchall()
        answered = {r["user_id"] for r in c.execute(
            f"SELECT DISTINCT user_id FROM answers WHERE event_id IN ({','.join('?'*len(ids))})", ids
        ).fetchall()}
        not_ans = [r for r in roster if r["user_id"] not in answered]
        return rows, roster, not_ans

def fmt_names(items, limit=10) -> str:
    names = [(r["name"] or f"ID {r['user_id']}") for r in items]
    if not names:
        return "—"
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" … (+{len(names)-limit})"
    return ", ".join(names)

def award_daily_badges(chat_id: int) -> Dict[int, List[Dict]]:
    t0, t1 = period_bounds("dia")
    today_key = datetime.fromtimestamp(t0, tz=TZ).strftime("%Y-%m-%d")
    awarded: Dict[int, List[Dict]] = {}
    with db() as c:
        rows = c.execute(f"""
            SELECT a.user_id, COALESCE(u.name, 'ID '||a.user_id) AS name,
                   SUM(CASE WHEN a.correct=1 THEN 1 ELSE 0 END) AS aciertos
            FROM answers a
            LEFT JOIN events e ON e.id=a.event_id
            LEFT JOIN users u ON u.chat_id=e.chat_id AND u.user_id=a.user_id
            WHERE e.chat_id=? AND e.start_ts>=? AND e.start_ts<?
            GROUP BY a.user_id
        """, (chat_id, t0, t1)).fetchall()

        for r in rows:
            uid = r["user_id"]; acc = r["aciertos"] or 0
            for code, th in [("BRONCE_DIA",3),("PLATA_DIA",5),("ORO_DIA",6)]:
                if acc >= th:
                    bd = next(b for b in BADGES if b["code"]==code)
                    try:
                        c.execute("""
                        INSERT INTO badges(chat_id, user_id, code, name, ts, period, period_key)
                        VALUES(?,?,?,?,?,?,?)
                        """, (chat_id, uid, bd["code"], bd["name"], now_ts(), "dia", today_key))
                        awarded.setdefault(uid, []).append(bd)
                    except sqlite3.IntegrityError:
                        pass
        # rachas
        streak_rows = c.execute("SELECT user_id, streak, best_streak FROM streaks WHERE chat_id=?", (chat_id,)).fetchall()
        for r in streak_rows:
            uid = r["user_id"]; s = r["streak"] or 0
            for code, th in [("RACHA_3",3),("RACHA_5",5)]:
                if s >= th:
                    bd = next(b for b in BADGES if b["code"]==code)
                    try:
                        c.execute("""
                        INSERT INTO badges(chat_id, user_id, code, name, ts, period, period_key)
                        VALUES(?,?,?,?,?,?,?)
                        """, (chat_id, uid, bd["code"], bd["name"], now_ts(), "dia", today_key))
                        awarded.setdefault(uid, []).append(bd)
                    except sqlite3.IntegrityError:
                        pass
        c.commit()
    return awarded

async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = getattr(context.application.bot_data, "chat_ids", set())
    if not chat_ids:
        return
    for chat_id in chat_ids:
        rows, roster, not_ans = fetch_rank(chat_id, "dia")
        if not rows and not roster:
            continue
        lines = [f"{random.choice(PHRASE_SUMMARY)}", "🏁 *Resumen diario* (ranking del día)"]
        top5 = rows[:5]
        pos = 1
        for r in top5:
            nm = r["name"] or f"ID {r['user_id']}"
            lines.append(f"{pos}. {nm} — ✅ {r['aciertos']}  ❌ {r['fallos']}")
            pos += 1
        if top5:
            lines.append("\n🎉 ¡Enhorabuena a los 5 primeros!")

        newly = award_daily_badges(chat_id)
        if newly:
            lines.append("\n🏅 *Medallas/insignias de hoy:*")
            for uid, badges in newly.items():
                name = next((r["name"] for r in rows if r["user_id"]==uid), f"ID {uid}")
                uniq = {b["name"] for b in badges}
                lines.append(f"• {name}: " + ", ".join(sorted(uniq)))

        lines.append(f"\n👀 No participaron hoy: {len(not_ans)}")
        if not_ans:
            names = fmt_names(not_ans, limit=12)
            lines.append(names)
        lines.append("\n👉 Mañana hay más preguntas cada 2 horas. ¡Únete y suma puntos!")
        await context.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

# ---------- Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch = update.effective_chat
    ids = getattr(context.application.bot_data, "chat_ids", set())
    ids.add(ch.id)
    context.application.bot_data["chat_ids"] = ids
    await update.message.reply_text(
        "🎯 GUN4FUN Trivia Light-Gun\n\n"
        "Lanzamos 6 preguntas al día (10:00, 12:00, 14:00, 16:00, 18:00, 20:00) y un resumen a las 21:00.\n"
        "Comandos:\n• /ranking [dia|semana|mes]\n• /pregunta_ahora (prueba)\n\n"
        "¡Participa para subir en el ranking!"
    )

async def pregunta_ahora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    class DummyJob:
        def __init__(self, chat_id): self.chat_id = chat_id
    context.job = DummyJob(update.effective_chat.id)
    await trivia_job(context)

async def ranking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind = (context.args[0].lower() if context.args else "dia")
    if kind not in ("dia","semana","mes"):
        kind = "dia"
    chat_id = update.effective_chat.id
    rows, roster, not_ans = fetch_rank(chat_id, kind)
    if not rows and not roster:
        await update.message.reply_text("Aún no hay datos para el ranking.")
        return

    lines = [f"📈 RANKING {kind.upper()}"]
    pos = 1
    for r in rows[:20]:
        nm = r["name"] or f"ID {r['user_id']}"
        lines.append(f"{pos:>2}. {nm} — ✅ {r['aciertos']}  ❌ {r['fallos']}")
        pos += 1
    lines.append("")
    lines.append(f"👀 No contestaron: {len(not_ans)}")
    lines.append(fmt_names(not_ans, limit=12))
    await update.message.reply_text("\n".join(lines))

def main():
    ensure_db()
    persistence = PicklePersistence(filepath="bot_state.pkl")
    app = ApplicationBuilder().token(TOKEN).persistence(persistence).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ranking", ranking_cmd))
    app.add_handler(CommandHandler("pregunta_ahora", pregunta_ahora))
    app.add_handler(CallbackQueryHandler(answer_cb, pattern=r"^ans\|"))
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), touch_user))
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, touch_user))
    app.post_init = schedule_jobs
    log.info("Arrancando bot…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
