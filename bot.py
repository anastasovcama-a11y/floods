import os
import asyncio
import logging
from datetime import datetime, timedelta
from io import BytesIO

import aiosqlite
import matplotlib
matplotlib.use('Agg')  # Обязательно для серверной среды, иначе matplotlib попытается использовать GUI
import matplotlib.pyplot as plt
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my_secret_token")
BASE_URL = os.getenv("BASE_URL", "")  # например https://xxx.bothost.ru
WEBHOOK_PATH = "/webhook"
DB_PATH = "stats.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- Инициализация базы данных ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица ежедневной статистики (ядро скользящего окна)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                chat_id INTEGER,
                user_id INTEGER,
                date TEXT,
                msg_count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, date)
            )
        """)
        # Таблица пользователей (для имён и общего количества)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT
            )
        """)
        await db.commit()

# ---------- Вспомогательные функции ----------
async def update_user_info(user: types.User):
    """Обновление информации о пользователе (для отображения имени)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, full_name, username) VALUES (?, ?, ?)",
            (user.id, user.full_name, user.username or "")
        )
        await db.commit()

async def record_message(chat_id: int, user_id: int):
    """Запись сообщения: обновление счётчика за сегодня"""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO daily_stats (chat_id, user_id, date, msg_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, date) DO UPDATE SET msg_count = msg_count + 1
        """, (chat_id, user_id, today))
        await db.commit()

def get_rank(total_count: int) -> str:
    """Система рангов: на основе общего количества сообщений"""
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

# ---------- Обработчики команд ----------
@dp.message(Command("start", "help"))
async def cmd_help(message: types.Message):
    text = (
        "📊 **Бот статистики**\n\n"
        "**Команды:**\n"
        "/whoami — посмотреть мою статистику и ранг\n"
        "/top day — топ за сегодня\n"
        "/top week — топ за 7 дней (скользящее окно)\n"
        "/top all — топ за всё время\n"
        "/inactive — список участников без сообщений за 7 дней\n"
        "/chart — график активности за 7 дней (моя активность)\n"
    )
    await message.answer(text)

@dp.message(Command("whoami"))
async def cmd_whoami(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    
    user = message.from_user
    chat_id = message.chat.id
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Сегодня
        async with db.execute(
            "SELECT msg_count FROM daily_stats WHERE chat_id=? AND user_id=? AND date=?",
            (chat_id, user.id, today)
        ) as cur:
            today_count = (await cur.fetchone() or [0])[0]
        
        # За неделю
        async with db.execute(
            "SELECT SUM(msg_count) FROM daily_stats WHERE chat_id=? AND user_id=? AND date>=?",
            (chat_id, user.id, week_ago)
        ) as cur:
            week_count = (await cur.fetchone() or [0])[0]
        
        # Всего
        total = await get_user_total(chat_id, user.id)
    
    rank = get_rank(total)
    text = (
        f"👤 **{user.full_name}**\n"
        f"Ранг: {rank}\n"
        f"📅 Сегодня: {today_count or 0}\n"
        f"📅 За 7 дней: {week_count or 0}\n"
        f"📅 Всего: {total}"
    )
    await message.reply(text)

@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("Укажите период: /top day, /top week или /top all")
    
    period = args[1].lower()
    chat_id = message.chat.id
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_PATH) as db:
        if period == "day":
            query = """
                SELECT ds.user_id, SUM(ds.msg_count) as total, u.full_name
                FROM daily_stats ds LEFT JOIN users u ON ds.user_id = u.user_id
                WHERE ds.chat_id=? AND ds.date=?
                GROUP BY ds.user_id ORDER BY total DESC LIMIT 10
            """
            params = (chat_id, today)
        elif period == "week":
            query = """
                SELECT ds.user_id, SUM(ds.msg_count) as total, u.full_name
                FROM daily_stats ds LEFT JOIN users u ON ds.user_id = u.user_id
                WHERE ds.chat_id=? AND ds.date>=?
                GROUP BY ds.user_id ORDER BY total DESC LIMIT 10
            """
            params = (chat_id, week_ago)
        elif period == "all":
            query = """
                SELECT ds.user_id, SUM(ds.msg_count) as total, u.full_name
                FROM daily_stats ds LEFT JOIN users u ON ds.user_id = u.user_id
                WHERE ds.chat_id=?
                GROUP BY ds.user_id ORDER BY total DESC LIMIT 10
            """
            params = (chat_id,)
        else:
            return await message.answer("Неверный период. Используйте: day, week, all")
        
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        return await message.answer("Пока нет данных.")
    
    lines = [f"🏆 **Топ ({period})**\n"]
    for i, (uid, cnt, name) in enumerate(rows, 1):
        display_name = name or f"User {uid}"
        lines.append(f"{i}. {display_name}: **{cnt}**")
    
    await message.answer("\n".join(lines))

@dp.message(Command("inactive"))
async def cmd_inactive(message: types.Message):
    """Неактивные: участники, которые писали когда-либо, но не писали за последние 7 дней"""
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    
    chat_id = message.chat.id
    week_ago = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Находим всех, кто активен за последнюю неделю
        async with db.execute(
            "SELECT DISTINCT user_id FROM daily_stats WHERE chat_id=? AND date>=?",
            (chat_id, week_ago)
        ) as cur:
            active_ids = {row[0] for row in await cur.fetchall()}
        
        # Находим всех, кто вообще писал в этом чате
        async with db.execute(
            "SELECT DISTINCT user_id FROM daily_stats WHERE chat_id=?",
            (chat_id,)
        ) as cur:
            all_ids = {row[0] for row in await cur.fetchall()}
        
        inactive_ids = all_ids - active_ids
        if not inactive_ids:
            return await message.answer("🎉 Все активны за последнюю неделю!")
        
        # Получаем имена
        placeholders = ",".join("?" * len(inactive_ids))
        async with db.execute(
            f"SELECT user_id, full_name FROM users WHERE user_id IN ({placeholders})",
            list(inactive_ids)
        ) as cur:
            users = await cur.fetchall()
    
    lines = ["😴 **Неактивные за 7 дней** (писали раньше, но молчат сейчас):\n"]
    for uid, name in users[:20]:  # Ограничиваем 20, чтобы не спамить
        lines.append(f"• {name or uid}")
    
    await message.answer("\n".join(lines))

@dp.message(Command("chart"))
async def cmd_chart(message: types.Message):
    """Личный график за 7 дней"""
    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("Эта команда работает только в группах.")
    
    user = message.from_user
    chat_id = message.chat.id
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    
    async with aiosqlite.connect(DB_PATH) as db:
        counts = []
        for d in days:
            async with db.execute(
                "SELECT msg_count FROM daily_stats WHERE chat_id=? AND user_id=? AND date=?",
                (chat_id, user.id, d)
            ) as cur:
                row = await cur.fetchone()
                counts.append(row[0] if row else 0)
    
    # Генерация графика
    plt.figure(figsize=(8, 4))
    plt.bar([d[5:] for d in days], counts, color='skyblue')  # Показываем только MM-DD
    plt.title(f"Активность: {user.full_name}")
    plt.xlabel("Дата")
    plt.ylabel("Сообщений")
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    
    photo = BufferedInputFile(buf.read(), filename="chart.png")
    await message.reply_photo(photo, caption=f"📊 Ваша активность за 7 дней (всего: {sum(counts)})")

# ---------- Универсальный обработчик сообщений (запись) ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_message(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    
    await update_user_info(message.from_user)
    await record_message(message.chat.id, message.from_user.id)

# ---------- Точка входа (Webhook) ----------
async def on_startup(bot: Bot):
    await init_db()
    # Установка webhook
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
    # BotHost передаёт порт через переменную SERVER_PORT
    port = int(os.getenv("SERVER_PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
