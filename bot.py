import os
import logging
import json
import urllib.parse
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my_secret_token")
BASE_URL = os.getenv("BASE_URL", "")
WEBHOOK_PATH = "/webhook"
DB_PATH = os.path.join(os.getenv("DATA_DIR", "."), "stats.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- Инициализация базы данных ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                chat_id INTEGER,
                user_id INTEGER,
                date TEXT,
                msg_count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, date)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                joined_at TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN joined_at TEXT")
        except aiosqlite.OperationalError:
            pass
        await db.commit()

# ---------- Вспомогательные функции ----------
async def update_user_info(user: types.User):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)) as cur:
            exists = await cur.fetchone()
            
        if not exists:
            await db.execute(
                "INSERT INTO users (user_id, full_name, username, joined_at) VALUES (?, ?, ?, ?)",
                (user.id, user.full_name, user.username or "", today)
            )
        else:
            await db.execute(
                "UPDATE users SET full_name=?, username=? WHERE user_id=?",
                (user.full_name, user.username or "", user.id)
            )
        await db.commit()

async def record_message(chat_id: int, user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO daily_stats (chat_id, user_id, date, msg_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, date) DO UPDATE SET msg_count = msg_count + 1
        """, (chat_id, user_id, today))
        await db.commit()

def get_rank(total_count: int) -> str:
    if total_count < 50: return "🐣 Новичок"
    elif total_count < 200: return "💬 Болтун"
    elif total_count < 500: return "🔥 Активист"
    elif total_count < 1000: return "⭐ Ветеран"
    else: return "👑 Легенда"

async def get_user_total(chat_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT SUM(msg_count) FROM daily_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] or 0

def get_mention(user_id: int, full_name: str) -> str:
    safe_name = full_name.replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

def calculate_membership_duration(joined_at_str: str) -> str:
    if not joined_at_str:
        return "Неизвестно"
    try:
        joined_date = datetime.strptime(joined_at_str, "%Y-%m-%d").date()
    except ValueError:
        return "Неизвестно"
    today = datetime.now().date()
    months = (today.year - joined_date.year) * 12 + today.month - joined_date.month
    if today.day < joined_date.day:
        months -= 1
    years = months // 12
    months_left = months % 12
    parts = []
    if years > 0: parts.append(f"{years} г.")
    if months_left > 0: parts.append(f"{months_left} мес.")
    if not parts:
        total_days = (today - joined_date).days
        return f"{total_days} дн."
    return " ".join(parts)

# ---------- Обработчики команд ----------
@dp.message(Command("start", "help"))
async def cmd_help(message: types.Message):
    text = (
        "📊 <b>Бот статистики</b>\n\n"
        "<b>Команды:</b>\n"
        "/whoami — посмотреть мою статистику, ранг и график\n"
        "/top — интерактивное меню топов\n"
        "/inactive — список участников без сообщений за 7 дней\n"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("whoami"))
async def cmd_whoami(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    
    user = message.from_user
    chat_id = message.chat.id
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT msg_count FROM daily_stats WHERE chat_id=? AND user_id=? AND date=?",
            (chat_id, user.id, today)
        ) as cur:
            today_count = (await cur.fetchone() or [0])[0]
        async with db.execute(
            "SELECT SUM(msg_count) FROM daily_stats WHERE chat_id=? AND user_id=? AND date>=?",
            (chat_id, user.id, week_ago)
        ) as cur:
            week_count = (await cur.fetchone() or [0])[0]
        total = await get_user_total(chat_id, user.id)
        async with db.execute("SELECT joined_at FROM users WHERE user_id=?", (user.id,)) as cur:
            row = await cur.fetchone()
            joined_at = row[0] if row else None
        
        # Данные для графика
        counts = []
        for d in days:
            async with db.execute(
                "SELECT msg_count FROM daily_stats WHERE chat_id=? AND user_id=? AND date=?",
                (chat_id, user.id, d)
            ) as cur:
                row = await cur.fetchone()
                counts.append(row[0] if row else 0)
    
    rank = get_rank(total)
    mention = get_mention(user.id, user.full_name)
    duration = calculate_membership_duration(joined_at)
    
    # Формируем текст (он пойдет в подпись к картинке)
    text = (
        f"👤 <b>{mention}</b>\n"
        f"Ранг: {rank}\n"
        f"📅 В чате: {duration}\n"
        f"📊 Сегодня: {today_count or 0}\n"
        f"📊 За 7 дней: {week_count or 0}\n"
        f"📊 Всего: {total}"
    )
    
    # Формируем URL для графика через QuickChart
    labels = [d[5:] for d in days]
    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "Сообщений",
                "data": counts,
                "backgroundColor": "rgba(54, 162, 235, 0.7)",
                "borderColor": "rgba(54, 162, 235, 1)",
                "borderWidth": 1
            }]
        },
        "options": {
            "title": {"display": False}, # Убираем заголовок, чтобы не дублировать имя
            "scales": {"yAxes": [{"ticks": {"beginAtZero": True}}]}
        }
    }
    
    encoded_config = urllib.parse.quote(json.dumps(chart_config))
    chart_url = f"https://quickchart.io/chart?c={encoded_config}&w=600&h=200&bkg=white"
    
    try:
        # Отправляем картинку с подписью (текстом статистики)
        await message.reply_photo(
            photo=chart_url, 
            caption=text, 
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка отправки графика: {e}")
        # Если не удалось отправить фото, отправляем хотя бы текст
        await message.reply(text, parse_mode="HTML")

# ---------- Топ с кнопками ----------
@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    args = message.text.split()
    if len(args) > 1:
        period = args[1].lower()
        if period in ["day", "week", "all"]:
            return await send_top_data(message, period)
        else:
            return await message.answer("Неверный период. Используйте: day, week, all")

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📅 За сутки", callback_data="top_day"),
        InlineKeyboardButton(text="🗓 За неделю", callback_data="top_week")
    )
    builder.row(
        InlineKeyboardButton(text="🏆 За всё время", callback_data="top_all")
    )
    await message.answer("Выберите период для топа:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("top_"))
async def process_top_callback(callback: types.CallbackQuery):
    period = callback.data.split("_")[1]
    await callback.answer()
    await send_top_data(callback.message, period, edit=True)

async def send_top_data(message: types.Message, period: str, edit: bool = False):
    chat_id = message.chat.id
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_PATH) as db:
        if period == "day":
            query = "SELECT ds.user_id, SUM(ds.msg_count) as total, u.full_name FROM daily_stats ds LEFT JOIN users u ON ds.user_id = u.user_id WHERE ds.chat_id=? AND ds.date=? GROUP BY ds.user_id ORDER BY total DESC LIMIT 10"
            params = (chat_id, today)
            title = "за сутки"
        elif period == "week":
            query = "SELECT ds.user_id, SUM(ds.msg_count) as total, u.full_name FROM daily_stats ds LEFT JOIN users u ON ds.user_id = u.user_id WHERE ds.chat_id=? AND ds.date>=? GROUP BY ds.user_id ORDER BY total DESC LIMIT 10"
            params = (chat_id, week_ago)
            title = "за неделю"
        elif period == "all":
            query = "SELECT ds.user_id, SUM(ds.msg_count) as total, u.full_name FROM daily_stats ds LEFT JOIN users u ON ds.user_id = u.user_id WHERE ds.chat_id=? GROUP BY ds.user_id ORDER BY total DESC LIMIT 10"
            params = (chat_id,)
            title = "за всё время"
        else:
            return
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        text = "Пока нет данных."
    else:
        lines = [f"🏆 <b>Топ {title}</b>\n"]
        for i, (uid, cnt, name) in enumerate(rows, 1):
            display_name = name or f"User {uid}"
            mention = get_mention(uid, display_name)
            lines.append(f"{i}. {mention}: <b>{cnt}</b>")
        text = "\n".join(lines)
    
    if edit:
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="📅 За сутки", callback_data="top_day"),
            InlineKeyboardButton(text="🗓 За неделю", callback_data="top_week")
        )
        builder.row(
            InlineKeyboardButton(text="🏆 За всё время", callback_data="top_all")
        )
        try:
            await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            pass
    else:
        await message.answer(text, parse_mode="HTML")

@dp.message(Command("inactive"))
async def cmd_inactive(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    chat_id = message.chat.id
    week_ago = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM daily_stats WHERE chat_id=? AND date>=?", (chat_id, week_ago)) as cur:
            active_ids = {row[0] for row in await cur.fetchall()}
        async with db.execute("SELECT DISTINCT user_id FROM daily_stats WHERE chat_id=?", (chat_id,)) as cur:
            all_ids = {row[0] for row in await cur.fetchall()}
        
        inactive_ids = all_ids - active_ids
        if not inactive_ids:
            return await message.answer("🎉 Все активны за последнюю неделю!")
        
        placeholders = ",".join("?" * len(inactive_ids))
        async with db.execute(f"SELECT user_id, full_name FROM users WHERE user_id IN ({placeholders})", list(inactive_ids)) as cur:
            users = await cur.fetchall()
    
    lines = ["😴 <b>Неактивные за 7 дней</b> (писали раньше, но молчат сейчас):\n"]
    for uid, name in users[:20]:
        display_name = name or f"User {uid}"
        mention = get_mention(uid, display_name)
        lines.append(f"• {mention}")
    await message.answer("\n".join(lines), parse_mode="HTML")

# ---------- Универсальный обработчик ----------
@dp.message(~F.text.startswith("/"))
async def handle_message(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    if message.content_type in ["new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo", "pinned_message"]:
        return
    await update_user_info(message.from_user)
    await record_message(message.chat.id, message.from_user.id)

# ---------- Точка входа (Webhook) ----------
async def on_startup(bot: Bot):
    await init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота / Помощь"),
        BotCommand(command="whoami", description="Моя статистика и график"),
        BotCommand(command="top", description="Топ участников (кнопки)"),
        BotCommand(command="inactive", description="Кто молчит больше 7 дней"),
    ])
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET)
    logging.info(f"Webhook установлен: {webhook_url}")

def main():
    dp.startup.register(on_startup)
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    port = int(os.getenv("PORT") or os.getenv("SERVER_PORT") or 8080)
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
