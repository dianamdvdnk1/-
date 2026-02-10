import os
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv
import pandas as pd
import telebot
from telebot import types
from html import escape
import requests

# -------------------------
# Настройка логирования
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------------
# Загрузка переменных окружения
# -------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

if not BOT_TOKEN:
    logger.error("❌ Не найден BOT_TOKEN в .env — добавь BOT_TOKEN=твой_токен")
    raise SystemExit("BOT_TOKEN not set")

# -------------------------
# Путь к данным
# -------------------------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")
CSV_PATH = os.path.join(DATA_DIR, "demo.csv")

# -------------------------
# Инициализация бота
# -------------------------
bot = telebot.TeleBot(BOT_TOKEN)

# -------------------------
# Вспомогательные функции БД и миграции
# -------------------------
def connect():
    return sqlite3.connect(DB_PATH)

def table_exists(conn, name):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None

def column_exists(conn, table, column):
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return column in cols

def ensure_tables_and_columns():
    conn = connect()
    cur = conn.cursor()

    # users
    if not table_exists(conn, "users"):
        cur.execute("""
            CREATE TABLE users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                fullname TEXT,
                reg_date TEXT
            )
        """)
        logger.info("Создана таблица users")
    else:
        if not column_exists(conn, "users", "fullname"):
            cur.execute("ALTER TABLE users ADD COLUMN fullname TEXT")
            logger.info("Добавлена колонка users.fullname")
        if not column_exists(conn, "users", "reg_date"):
            cur.execute("ALTER TABLE users ADD COLUMN reg_date TEXT")
            logger.info("Добавлена колонка users.reg_date")

    # queries
    if not table_exists(conn, "queries"):
        cur.execute("""
            CREATE TABLE queries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                source TEXT,
                params TEXT,
                ts TEXT
            )
        """)
        logger.info("Создана таблица queries")
    else:
        for col in ("source", "params", "ts"):
            if not column_exists(conn, "queries", col):
                cur.execute(f"ALTER TABLE queries ADD COLUMN {col} TEXT")
                logger.info(f"Добавлена колонка queries.{col}")

    # presets
    if not table_exists(conn, "presets"):
        cur.execute("""
            CREATE TABLE presets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                content TEXT,
                created_at TEXT
            )
        """)
        logger.info("Создана таблица presets")
    else:
        if not column_exists(conn, "presets", "content"):
            cur.execute("ALTER TABLE presets ADD COLUMN content TEXT")
            logger.info("Добавлена колонка presets.content")
        if not column_exists(conn, "presets", "created_at"):
            cur.execute("ALTER TABLE presets ADD COLUMN created_at TEXT")
            logger.info("Добавлена колонка presets.created_at")

    conn.commit()
    conn.close()

ensure_tables_and_columns()

# -------------------------
# Функции БД: пользователи, запросы, пресеты
# -------------------------
def datetime_now():
    return datetime.utcnow().isoformat(sep=" ", timespec="seconds")

def register_user(user):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
    if not cur.fetchone():
        fullname = " ".join(filter(None, [user.first_name, user.last_name])) if user else ""
        cur.execute("INSERT INTO users(user_id, username, fullname, reg_date) VALUES (?, ?, ?, ?)",
                    (user.id, user.username, fullname, datetime_now()))
        conn.commit()
        logger.info(f"Зарегистрирован пользователь {user.username} ({user.id})")
    conn.close()

def log_query(user_id, text, source="user", params=None):
    conn = connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO queries(user_id, text, source, params, ts) VALUES(?,?,?,?,?)",
                (user_id, text, source, str(params or {}), datetime_now()))
    conn.commit()
    qid = cur.lastrowid
    conn.close()
    return qid

def list_history(user_id, limit=10):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT text, ts FROM queries WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def add_preset_db(user_id, name, content):
    conn = connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO presets(user_id, name, content, created_at) VALUES(?,?,?,?)",
                (user_id, name, content, datetime_now()))
    conn.commit()
    conn.close()

def list_presets_db(user_id):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT name, content FROM presets WHERE user_id=?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_preset_db(user_id, name):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT content FROM presets WHERE user_id=? AND name=?", (user_id, name))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def delete_preset_db(user_id, name):
    conn = connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM presets WHERE user_id=? AND name=?", (user_id, name))
    conn.commit()
    conn.close()

# -------------------------
# CSV: создание демо, чтение с кодировками
# -------------------------
def ensure_demo_csv():
    if not os.path.exists(CSV_PATH):
        import csv
        sample = [
            ["title","city","salary","skills","date"],
            ["Python developer","Москва",180000,"Django;SQL;Docker","2025-09-10"],
            ["Data analyst","Санкт-Петербург",160000,"SQL;Tableau;Python","2025-09-12"],
            ["SMM manager","Москва",120000,"Content;UGC;Short video","2025-09-14"]
        ]
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerows(sample)
        logger.info("Создан demo.csv (пример)")

def load_csv_safe():
    ensure_demo_csv()
    try:
        df = pd.read_csv(CSV_PATH, encoding="utf-8")
        logger.info(f"Загружен {CSV_PATH} (utf-8), строк: {len(df)}")
        return df
    except UnicodeDecodeError:
        df = pd.read_csv(CSV_PATH, encoding="cp1251")
        logger.info(f"Загружен {CSV_PATH} (cp1251), строк: {len(df)}")
        return df
    except Exception as e:
        logger.error(f"Ошибка при чтении CSV: {e}")
        return pd.DataFrame()

# -------------------------
# Анализ CSV — отчёт (без ошибок NaN)
# -------------------------
def generate_report_from_demo():
    df = load_csv_safe()
    if df.empty:
        return "⚠️ Данные отсутствуют или CSV некорректен."
    try:
        avg_salary = df["salary"].astype(float).mean() if "salary" in df.columns else None
        top_cities = df["city"].value_counts().head(5) if "city" in df.columns else None
        top_titles = df["title"].value_counts().head(5) if "title" in df.columns else None

        skills_series = []
        if "skills" in df.columns:
            df_sk = df["skills"].dropna().astype(str)
            skills = ";".join(df_sk.tolist()).split(";")
            skills_series = pd.Series(skills).value_counts().head(10)

        parts = []
        parts.append("<b>📊 Аналитический отчёт (demo.csv)</b>")

        if avg_salary is not None and not pd.isna(avg_salary):
            # безопасная конвертация
            parts.append(f"💰 Средняя зарплата: <b>{int(round(avg_salary)):,} ₽</b>")

        if top_cities is not None and not top_cities.empty:
            cities_str = ", ".join([escape(str(c)) for c in top_cities.index])
            parts.append(f"🏙️ Топ городов: {cities_str}")

        if top_titles is not None and not top_titles.empty:
            titles_str = ", ".join([escape(str(t)) for t in top_titles.index])
            parts.append(f"💼 Топ вакансий: {titles_str}")

        if isinstance(skills_series, pd.Series) and not skills_series.empty:
            skills_str = ", ".join([escape(str(s)) for s in skills_series.index])
            parts.append(f"🔥 Частые навыки: {skills_str}")

        parts.append("\n⚠️ Это демонстрационный отчёт. Ответ составлен ботом-ассистентом.")
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"Ошибка формирования отчёта: {e}")
        return "⚠️ Ошибка формирования отчёта."

# -------------------------
# Клавиатура / меню
# -------------------------
def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📊 Отчёт", "🧠 Пресеты")
    kb.row("📜 История", "👤 Профиль")
    kb.row("❓ Помощь")
    return kb

# -------------------------
# API-интеграции: OpenRouter и NewsAPI
# -------------------------
def ask_neuron(question):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    # OpenRouter / OpenAI-like request (пример для OpenRouter)
    url = "https://api.openrouter.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    payload = {
        "model": "gpt-4.1-mini",
        "messages": [{"role": "user", "content": question}],
        "max_tokens": 800
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    j = r.json()
    # безопасный обход — ищем первый доступный текст
    try:
        return j["choices"][0]["message"]["content"]
    except Exception:
        return j.get("text") or str(j)

def get_news(topic, limit=5):
    if not NEWS_API_KEY:
        raise RuntimeError("NEWS_API_KEY not set")
    url = f"https://newsapi.org/v2/everything"
    params = {"q": topic, "pageSize": limit, "language": "ru", "apiKey": NEWS_API_KEY}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    articles = j.get("articles", [])
    result_lines = []
    for a in articles:
        title = escape(a.get("title") or "Без заголовка")
        urla = a.get("url") or ""
        result_lines.append(f"{title}\n{escape(urla)}")
    return result_lines

# -------------------------
# Хендлеры команд
# -------------------------
@bot.message_handler(commands=["start"])
def handle_start(message):
    register_user(message.from_user)
    # убрали небезопасный вид <вопрос> — используем пояснения без <...>
    msg = (
        f"👋 Привет, <b>{escape(message.from_user.first_name or message.from_user.username)}</b>!\n\n"
        "Я — Помощник_Аналитика. Выбери действие:\n\n"
        "• Используй команду /ask  чтобы задать вопрос нейросети\n"
        "• Используй команду /news чтобы получить новости по теме\n\n"
        "Или выбери пункт в меню ниже."
    )
    bot.send_message(message.chat.id, msg, parse_mode="HTML", reply_markup=main_keyboard())

@bot.message_handler(commands=["help"])
def handle_help(message):
    help_text = (
        "Команды:\n"
        "/start — показать приветствие\n"
        "/report — сформировать отчёт по data/demo.csv\n"
        "/preset_add имя текст — сохранить пресет\n"
        "/preset_list — список пресетов\n"
        "/preset_use имя — применить пресет\n"
        "/preset_del имя — удалить пресет\n"
        "/profile — показать профиль\n"
        "/history — история запросов\n"
        "/ask <вопрос> — задать вопрос нейросети (пиши после команды)\n"
        "/news <тема> — получить новости по теме (пиши после команды)\n"
    )
    # help_text содержит символы < и > в описании команд — отправим без HTML-парсинга
    bot.send_message(message.chat.id, help_text, reply_markup=main_keyboard())

@bot.message_handler(commands=["report"])
def handle_report_cmd(message):
    qid = log_query(message.from_user.id, "/report", source="command")
    report = generate_report_from_demo()
    bot.send_message(message.chat.id, report, parse_mode="HTML", reply_markup=main_keyboard())

@bot.message_handler(commands=["preset_add"])
def handle_preset_add(message):
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "Использование: /preset_add имя текст")
            return
        _, name, content = parts
        add_preset_db(message.from_user.id, name, content)
        log_query(message.from_user.id, f"/preset_add {name}", source="command")
        bot.reply_to(message, f"✅ Пресет '{escape(name)}' сохранён.")
    except Exception as e:
        logger.exception("preset_add error")
        bot.reply_to(message, "⚠️ Ошибка при сохранении пресета.")

@bot.message_handler(commands=["preset_list"])
def handle_preset_list(message):
    rows = list_presets_db(message.from_user.id)
    if not rows:
        bot.reply_to(message, "У тебя нет пресетов.")
        return
    text = "📚 Твои пресеты:\n" + "\n".join([f"• {escape(r[0])} — {escape(r[1][:60])}..." for r in rows])
    bot.reply_to(message, text)

@bot.message_handler(commands=["preset_use"])
def handle_preset_use(message):
    try:
        _, name = message.text.split(maxsplit=1)
    except Exception:
        bot.reply_to(message, "Использование: /preset_use имя")
        return
    content = get_preset_db(message.from_user.id, name)
    if not content:
        bot.reply_to(message, f"Пресет '{escape(name)}' не найден.")
    else:
        log_query(message.from_user.id, f"/preset_use {name}", source="command")
        bot.reply_to(message, f"📋 Пресет '{escape(name)}':\n\n{escape(content)}")

@bot.message_handler(commands=["preset_del"])
def handle_preset_del(message):
    try:
        _, name = message.text.split(maxsplit=1)
    except Exception:
        bot.reply_to(message, "Использование: /preset_del имя")
        return
    delete_preset_db(message.from_user.id, name)
    log_query(message.from_user.id, f"/preset_del {name}", source="command")
    bot.reply_to(message, f"🗑 Пресет '{escape(name)}' удалён.")

@bot.message_handler(commands=["profile"])
def handle_profile(message):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT username, fullname, reg_date FROM users WHERE user_id=?", (message.from_user.id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        bot.reply_to(message, "Профиль не найден. Нажми /start.")
        return
    username, fullname, reg_date = row
    bot.reply_to(message, f"👤 Профиль:\nИмя: {escape(fullname)}\nUsername: @{escape(username or '')}\nРегистрация: {escape(reg_date or '')}")

@bot.message_handler(commands=["history"])
def handle_history(message):
    rows = list_history(message.from_user.id, limit=10)
    if not rows:
        bot.reply_to(message, "История пуста.", reply_markup=main_keyboard())
        return
    lines = [f"• {escape(r[0])}  —  {escape(r[1])}" for r in rows]
    bot.reply_to(message, "📜 Последние запросы:\n\n" + "\n".join(lines), reply_markup=main_keyboard())

@bot.message_handler(commands=["ask"])
def handle_ask(message):
    # команда: /ask текст вопроса
    text = message.text.partition(" ")[2].strip()
    if not text:
        bot.reply_to(message, "Использование: /ask текст_вопроса")
        return
    log_query(message.from_user.id, text, source="ask")
    try:
        answer = ask_neuron(text)
    except Exception as e:
        logger.exception("ask_neuron error")
        bot.reply_to(message, f"⚠️ Ошибка при обращении к нейросети: {escape(str(e))}")
        return
    # ответ может содержать любые символы — экранируем перед отправкой с HTML
    bot.send_message(message.chat.id, escape(answer), parse_mode="HTML", reply_markup=main_keyboard())

@bot.message_handler(commands=["news"])
def handle_news(message):
    # команда: /news тема
    topic = message.text.partition(" ")[2].strip()
    if not topic:
        bot.reply_to(message, "Использование: /news тема")
        return
    log_query(message.from_user.id, topic, source="news")
    try:
        articles = get_news(topic, limit=5)
    except Exception as e:
        logger.exception("get_news error")
        bot.reply_to(message, f"⚠️ Ошибка при получении новостей: {escape(str(e))}")
        return
    if not articles:
        bot.reply_to(message, "📰 Новостей не найдено или сервис недоступен.", reply_markup=main_keyboard())
        return
    text = "\n\n".join(articles)
    # articles уже экранированы в get_news
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard())

# -------------------------
# Обработка обычных сообщений (меню)
# -------------------------
@bot.message_handler(func=lambda m: True)
def handle_all(message):
    text = (message.text or "").strip()
    register_user(message.from_user)
    try:
        log_query(message.from_user.id, text, source="message")
    except Exception:
        logger.exception("Ошибка логирования")

    if text == "📊 Отчёт":
        handle_report_cmd(message)
    elif text == "🧠 Пресеты":
        bot.send_message(message.chat.id,
                         "Управление пресетами:\n/preset_add имя текст\n/preset_list\n/preset_use имя\n/preset_del имя",
                         reply_markup=main_keyboard())
    elif text == "📜 История":
        handle_history(message)
    elif text == "👤 Профиль":
        handle_profile(message)
    elif text == "❓ Помощь":
        handle_help(message)
    else:
        bot.send_message(message.chat.id, "Не распознано. Используй меню или /help.", reply_markup=main_keyboard())

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    ensure_demo_csv()
    logger.info("✅ База данных и demo.csv готовы.")
    logger.info("🚀 Бот Помощник_Аналитика запущен")
    bot.infinity_polling()

