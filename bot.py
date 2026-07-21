import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------
# Настройки. Обязательно задаются через переменные окружения:
#   BOT_TOKEN — токен от @BotFather
#   ADMIN_IDS — твой telegram id (можно несколько через запятую)
#   DB_PATH   — путь к файлу базы данных (по умолчанию videos.db)
#
# На Railway: Variables → добавь BOT_TOKEN, ADMIN_IDS, DB_PATH.
# Локально: export BOT_TOKEN="..." && export ADMIN_IDS="..."
# ------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
}
DB_PATH = os.getenv("DB_PATH", "videos.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# База данных
# ------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            code TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            title TEXT NOT NULL,
            added_by INTEGER,
            added_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_video(code: str, file_id: str, title: str, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO videos (code, file_id, title, added_by, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (code, file_id, title, user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_video(code: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT file_id, title FROM videos WHERE code = ?", (code,)
    ).fetchone()
    conn.close()
    return row


def delete_video(code: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM videos WHERE code = ?", (code,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def list_videos():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT code, title FROM videos ORDER BY code").fetchall()
    conn.close()
    return rows


def is_admin(user_id: int) -> bool:
    # Если админы не заданы вообще — разрешаем добавлять всем (удобно для теста).
    return not ADMIN_IDS or user_id in ADMIN_IDS


def add_report(report_type: str, user_id: int, username: str, full_name: str, text: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO reports (type, user_id, username, full_name, text, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (report_type, user_id, username, full_name, text, datetime.utcnow().isoformat()),
    )
    conn.commit()
    report_id = cur.lastrowid
    conn.close()
    return report_id


def list_reports(report_type: str, status: str = "open"):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, full_name, username, text FROM reports "
        "WHERE type = ? AND status = ? ORDER BY id",
        (report_type, status),
    ).fetchall()
    conn.close()
    return rows


def close_report(report_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE reports SET status = 'closed' WHERE id = ? AND status = 'open'",
        (report_id,),
    )
    conn.commit()
    closed = cur.rowcount > 0
    conn.close()
    return closed


def touch_user(user_id: int, username: str, full_name: str):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO users (user_id, username, full_name, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            last_seen = excluded.last_seen
        """,
        (user_id, username, full_name, now, now),
    )
    conn.commit()
    conn.close()


def count_users():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return total


def count_active_users(days: int = 7):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now', ?)",
        (f"-{days} days",),
    ).fetchone()
    conn.close()
    return row[0]


# ------------------------------------------------------------------
# Защита от повторной обработки одного и того же апдейта.
# Если по какой-то причине Telegram (или хостинг) присылает один
# и тот же update дважды — второй раз бот его просто проигнорирует.
# ------------------------------------------------------------------
_processed_update_ids: set[int] = set()
_MAX_TRACKED_IDS = 5000


async def dedup_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.update_id
    if uid in _processed_update_ids:
        logger.warning("Повторный update_id=%s — игнорирую дубликат", uid)
        raise ApplicationHandlerStop
    _processed_update_ids.add(uid)
    if len(_processed_update_ids) > _MAX_TRACKED_IDS:
        _processed_update_ids.clear()


# ------------------------------------------------------------------
# Хендлеры
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_admin(user_id):
        total = count_users()
        active_7d = count_active_users(7)
        await update.message.reply_text(
            "Привет! Пришли мне код — я найду видео.\n\n"
            f"👥 Пользователей всего: {total} (активны за 7 дней: {active_7d})\n\n"
            "Чтобы добавить видео: отправь мне видео с подписью вида:\n"
            "`код | Название видео`\n\n"
            "Команды:\n"
            "/list — список всех кодов\n"
            "/del код — удалить запись\n"
            "/listbug — открытые баги\n"
            "/listreclama — заявки по рекламе\n"
            "/close номер — закрыть баг/заявку\n"
            "/stats — подробная статистика по пользователям\n"
            "/reclamoblodatel текст — разослать сообщение всем пользователям",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Здравствуйте! Напишите код видео.\n\n"
            "Нашли баг — напишите /bug и опишите проблему "
            "(желательно со скриншотом).\n"
            "По рекламе — /reclama."
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Здравствуйте! Напишите код видео.")
        return

    caption = update.message.caption
    if not caption or "|" not in caption:
        await update.message.reply_text(
            "Добавь подпись к видео в формате:\n`код | Название видео`",
            parse_mode="Markdown",
        )
        return

    code_part, title_part = caption.split("|", 1)
    code = code_part.strip().lower()
    title = title_part.strip()

    if not code or not title:
        await update.message.reply_text(
            "Не удалось разобрать код или название. Формат:\n`код | Название видео`",
            parse_mode="Markdown",
        )
        return

    file_id = update.message.video.file_id
    save_video(code, file_id, title, user_id)
    await update.message.reply_text(f"Сохранено ✅\nКод: {code}\nНазвание: {title}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().lower()
    row = get_video(code)
    if row is None:
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    file_id, title = row
    await update.message.reply_video(video=file_id, caption=title)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    rows = list_videos()
    if not rows:
        await update.message.reply_text("Пока нет ни одного сохранённого видео.")
        return

    text = "\n".join(f"• `{code}` — {title}" for code, title in rows)
    await update.message.reply_text(text, parse_mode="Markdown")


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    if not context.args:
        await update.message.reply_text("Использование: /del код")
        return

    code = context.args[0].strip().lower()
    if delete_video(code):
        await update.message.reply_text(f"Код `{code}` удалён.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Код `{code}` не найден.", parse_mode="Markdown")


async def bug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Опишите баг после команды, например:\n"
            "`/bug видео не открывается по коду клип1`\n\n"
            "Если есть возможность — лучше сразу отдельным сообщением "
            "пришлите скриншот, так баг найдут и починят быстрее.",
            parse_mode="Markdown",
        )
        return

    user = update.effective_user
    bug_text = " ".join(context.args)
    username = user.username or ""
    report_id = add_report("bug", user.id, username, user.full_name, bug_text)

    display_username = f"@{username}" if username else "без username"
    report = (
        f"🐞 Баг #{report_id}\n"
        f"От: {user.full_name} ({display_username}, id={user.id})\n\n"
        f"{bug_text}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=report)
        except Exception:
            logger.exception("Не удалось отправить баг-репорт админу %s", admin_id)

    await update.message.reply_text(
        "Спасибо, баг отправлен разработчику! Если есть скриншот — пришлите его отдельным сообщением, это поможет."
    )


async def reclama_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Опишите ваше предложение по рекламе после команды, например:\n"
            "`/reclama хочу разместить рекламу канала, бюджет и т.д.`",
            parse_mode="Markdown",
        )
        return

    user = update.effective_user
    ad_text = " ".join(context.args)
    username = user.username or ""
    report_id = add_report("reclama", user.id, username, user.full_name, ad_text)

    display_username = f"@{username}" if username else "без username"
    report = (
        f"📢 Реклама #{report_id}\n"
        f"От: {user.full_name} ({display_username}, id={user.id})\n\n"
        f"{ad_text}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=report)
        except Exception:
            logger.exception("Не удалось отправить рекламный запрос админу %s", admin_id)

    await update.message.reply_text("Спасибо! Ваше предложение по рекламе передано, с вами свяжутся.")


async def list_bug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    rows = list_reports("bug", "open")
    if not rows:
        await update.message.reply_text("Открытых багов нет 🎉")
        return

    lines = [f"#{rid} — {name} (@{uname or 'нет'}): {text}" for rid, name, uname, text in rows]
    await update.message.reply_text(
        "Открытые баги:\n\n" + "\n\n".join(lines) + "\n\nЗакрыть: /close номер"
    )


async def list_reclama_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    rows = list_reports("reclama", "open")
    if not rows:
        await update.message.reply_text("Открытых заявок по рекламе нет.")
        return

    lines = [f"#{rid} — {name} (@{uname or 'нет'}): {text}" for rid, name, uname, text in rows]
    await update.message.reply_text(
        "Заявки по рекламе:\n\n" + "\n\n".join(lines) + "\n\nЗакрыть: /close номер"
    )


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /close номер (номер бага или рекламы из списка)")
        return

    report_id = int(context.args[0])
    if close_report(report_id):
        await update.message.reply_text(f"Заявка #{report_id} закрыта ✅")
    else:
        await update.message.reply_text(f"Заявка #{report_id} не найдена или уже закрыта.")


def get_all_user_ids():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows]


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    # Можно либо ответить этой командой на сообщение (текст/фото/видео —
    # разошлётся как есть), либо написать текст прямо после команды.
    source_message = update.message.reply_to_message
    text_after_command = " ".join(context.args) if context.args else None

    if source_message is None and not text_after_command:
        await update.message.reply_text(
            "Как разослать сообщение всем подписчикам:\n\n"
            "1) Просто текст:\n`/reclamoblodatel Новое видео уже в боте!`\n\n"
            "2) Фото/видео/что угодно: ответь этой командой "
            "(Reply) на нужное сообщение — оно разошлётся всем как есть.",
            parse_mode="Markdown",
        )
        return

    user_ids = get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("Пока нет ни одного пользователя для рассылки.")
        return

    status_msg = await update.message.reply_text(
        f"Начинаю рассылку на {len(user_ids)} пользователей..."
    )

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            if source_message is not None:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=source_message.chat_id,
                    message_id=source_message.message_id,
                )
            else:
                await context.bot.send_message(chat_id=uid, text=text_after_command)
            sent += 1
        except Exception:
            failed += 1
        # небольшая пауза, чтобы не упереться в лимиты Telegram на отправку
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"Рассылка завершена ✅\nОтправлено: {sent}\nНе доставлено: {failed}"
    )


async def track_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    touch_user(user.id, user.username or "", user.full_name)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Ничего не нашёл по этому коду 🤷")
        return

    total = count_users()
    active_7d = count_active_users(7)
    active_30d = count_active_users(30)
    await update.message.reply_text(
        f"👥 Пользователей всего: {total}\n"
        f"Активны за 7 дней: {active_7d}\n"
        f"Активны за 30 дней: {active_30d}"
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Задай переменную окружения BOT_TOKEN "
            "(в Railway: Variables → BOT_TOKEN)."
        )
    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS не задан — добавлять видео сможет любой пользователь!"
        )

    # Фикс для новых версий Python (3.12+), где по умолчанию
    # больше не создаётся текущий event loop автоматически.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("del", del_command))
    app.add_handler(CommandHandler("bug", bug_command))
    app.add_handler(CommandHandler("reclama", reclama_command))
    app.add_handler(CommandHandler(["list_bug", "listbug"], list_bug_command))
    app.add_handler(CommandHandler(["list_reclama", "listreclama"], list_reclama_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("reclamoblodatel", broadcast_command))
    app.add_handler(MessageHandler(filters.ALL, dedup_guard), group=-2)
    app.add_handler(MessageHandler(filters.ALL, track_any_message), group=-1)
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
 "__main__":
    main()
