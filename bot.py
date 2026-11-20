import os
import logging
import json
import urllib.parse
import calendar
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
        
        # ⬇️ ВОТ ЭТИ СТРОКИ ДОБАВЛЯЮТ ИНДЕКСЫ ДЛЯ СКОРОСТИ ⬇️
        await db.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_chat_user_date ON daily_stats (chat_id, user_id, date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_chat_date ON daily_stats (chat_id, date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_user_chat ON daily_stats (user_id, chat_id)")
        
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
    years = today.year - joined_date.year
    months = today.month - joined_date.month
    days = today.day - joined_date.day

    if days < 0:
        months -= 1
        prev_month = today.month - 1 if today.month > 1 else 12
        prev_year = today.year if today.month > 1 else today.year - 1
        days += calendar.monthrange(prev_year, prev_month)[1]

    if months < 0:
        years -= 1
        months += 12
        
    parts = []
    if years > 0: parts.append(f"{years} г.")
    if months > 0: parts.append(f"{months} мес.")
    if days > 0 or not parts: parts.append(f"{days} дн.")
    return " ".join(parts)

# ---------- Функция отправки статистики ----------
async def send_user_stats(message: types.Message):
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
    
    text = (
        f"👤 <b>{mention}</b>\n"
        f"Ранг: {rank}\n"
        f"📅 В чате: {duration}\n"
        f"📊 Сегодня: {today_count or 0}\n"
        f"📊 За 7 дней: {week_count or 0}\n"
        f"📊 Всего: {total}"
    )
    
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
        "options": {"title": {"display": False}, "scales": {"yAxes": [{"ticks": {"beginAtZero": True}}]}}
    }
    
    encoded_config = urllib.parse.quote(json.dumps(chart_config))
    chart_url = f"https://quickchart.io/chart?c={encoded_config}&w=600&h=200&bkg=white"
    
    try:
        await message.reply_photo(photo=chart_url, caption=text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки графика: {e}")
        await message.reply(text, parse_mode="HTML")

# ---------- Обработчики команд ----------
@dp.message(Command("start", "help"))
async def cmd_help(message: types.Message):
    text = (
        "📊 <b>Бот статистики</b>\n\n"
        "<b>Команды:</b>\n"
        "/mystats — моя статистика, стаж и график\n"
        "/top — интерактивное меню топов\n"
        "/chatstats — общая статистика чата\n"
        "/inactive — кто молчит больше суток\n\n"
        "💡 Также можно просто написать <b>«моя стата»</b>"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("mystats"))
async def cmd_mystats(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    await send_user_stats(message)

@dp.message(F.text.lower().in_(["моя стата", "моя стату", "моя статистика"]))
async def text_trigger_stats(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    await send_user_stats(message)

# ---------- Приветствие нового участника ----------
@dp.message(F.new_chat_members)
async def greet_new_member(message: types.Message):
    # Игнорируем самого бота, если его добавили
    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
            
        mention = get_mention(new_member.id, new_member.full_name)
        
        text = (
            f"👋 Привет, {mention}!\n\n"
            f"Я — бот-считалка. Я слежу за активностью в чате, считаю сообщения и выдаю статистику.\n\n"
            f"📖 <b>Полезные команды:</b>\n"
            f"• /mystats — посмотреть свою стату и график\n"
            f"• /top — посмотреть топ самых активных\n"
            f"• /chatstats — общая статистика чата\n"
            f"• /inactive — список тех, кто давно молчит\n\n"
            f"💡 <i>Подсказка: можно просто написать «моя стата»</i>"
        )
        try:
            await message.reply(text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка приветствия: {e}")

@dp.message(Command("chatstats"))
async def cmd_chatstats(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT SUM(msg_count) FROM daily_stats WHERE chat_id=?", (chat_id,)) as cur:
            total_msgs = (await cur.fetchone())[0] or 0
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM daily_stats WHERE chat_id=?", (chat_id,)) as cur:
            total_users = (await cur.fetchone())[0] or 0
        async with db.execute("""
            SELECT u.full_name, SUM(ds.msg_count) as cnt 
            FROM daily_stats ds JOIN users u ON ds.user_id = u.user_id 
            WHERE ds.chat_id=? GROUP BY ds.user_id ORDER BY cnt DESC LIMIT 1
        """, (chat_id,)) as cur:
            top_user = await cur.fetchone()
            
    text = f"📈 <b>Статистика чата</b>\n\n"
    text += f"💬 Всего сообщений: <b>{total_msgs}</b>\n"
    text += f"👥 Уникальных участников: <b>{total_users}</b>\n"
    if top_user:
        text += f"🏆 Самый активный: <b>{top_user[0]}</b> ({top_user[1]} сообщ.)"
    await message.answer(text, parse_mode="HTML")

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
            mention = get_mention(uid, name or f"User {uid}")
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

# ---------- НЕАКТИВ ----------
@dp.message(Command("inactive"))
async def cmd_inactive(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    
    chat_id = message.chat.id
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # ОДИН запрос: находим последнюю дату сообщения для каждого пользователя, 
        # у которого эта дата меньше или равна вчерашнему дню
        query = """
            SELECT user_id, MAX(date) as last_date
            FROM daily_stats
            WHERE chat_id = ?
            GROUP BY user_id
            HAVING last_date < ?
            ORDER BY last_date ASC
            LIMIT 20
        """
        async with db.execute(query, (chat_id, yesterday)) as cur:
            inactive_users = await cur.fetchall()
            
        if not inactive_users:
            return await message.answer("🎉 Все писали за последние сутки!")

        # Получаем имена для этих пользователей (тоже одним запросом)
        user_ids = [row[0] for row in inactive_users]
        placeholders = ",".join("?" * len(user_ids))
        async with db.execute(f"SELECT user_id, full_name FROM users WHERE user_id IN ({placeholders})", user_ids) as cur:
            users_data = {row[0]: row[1] for row in await cur.fetchall()}
            
    lines = ["😴 <b>Кто молчит (более суток)</b>\n"]
    today = datetime.now().date()
    
    for uid, last_date_str in inactive_users:
        name = users_data.get(uid, f"User {uid}")
        mention = get_mention(uid, name)
        
        # Считаем, сколько дней прошло
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
        days_ago = (today - last_date).days
        
        if days_ago == 1: time_str = "1 день"
        elif days_ago < 5: time_str = f"{days_ago} дня"
        else: time_str = f"{days_ago} дней"
        
        lines.append(f"• {mention} — молчит {time_str}")
        
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
        BotCommand(command="mystats", description="Моя статистика и график"),
        BotCommand(command="top", description="Топ участников (кнопки)"),
        BotCommand(command="chatstats", description="Общая статистика чата"),
        BotCommand(command="inactive", description="Кто молчит больше суток"),
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
