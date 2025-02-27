import sqlite3
from telegram import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
import schedule
import time
import threading
import re
from datetime import datetime
from dotenv import load_dotenv
import os
import logging
from logging.handlers import RotatingFileHandler

log_handler = RotatingFileHandler('bot.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
EMPLOYEE_CHAT_ID = os.getenv("EMPLOYEE_CHAT_ID")
REMINDER_TIME = os.getenv("REMINDER_TIME", "09:00")
REMINDER_COUNT = int(os.getenv("REMINDER_COUNT", 1))
REMINDER_DAYS = int(os.getenv("REMINDER_DAYS", 1))

if not TELEGRAM_TOKEN or not EMPLOYEE_CHAT_ID:
    logger.error("TELEGRAM_TOKEN или EMPLOYEE_CHAT_ID не найдены в .env")
    exit(1)

def init_db():
    try:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS requests
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_date TEXT, company TEXT, name TEXT, address TEXT, 
                      contact_number TEXT, track_number TEXT, status TEXT, chat_id TEXT, received INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS electronic_requests
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_date TEXT, company TEXT, iin TEXT, documents TEXT, 
                      delivery_method TEXT, status TEXT, chat_id TEXT, attachment TEXT)''')
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка базы данных: {e}")

MAIN_MENU = ReplyKeyboardMarkup([
    [KeyboardButton("Создать заявку"), KeyboardButton("Проверить статус")],
    [KeyboardButton("Помощь"), KeyboardButton("Отмена")]
], resize_keyboard=True)

def send_telegram_message(chat_id, text, context, reply_markup=MAIN_MENU):
    try:
        context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        logger.info(f"Сообщение отправлено в {chat_id}: {text}")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        context.bot.send_message(chat_id=chat_id, text="⚠️ Произошла ошибка. Попробуйте позже.")

def validate_phone(phone):
    return re.match(r'^\+?\d{10,15}$', phone) is not None

def start(update, context):
    logger.info(f"Команда /start от {update.message.chat_id}")
    send_telegram_message(update.message.chat_id,
                          "👋 Привет! Я ZaprosBot. Выбери действие:\n"
                          "📝 Создать заявку\n"
                          "✅ Проверить статус\n"
                          "ℹ️ Помощь", context)

def handle_message(update, context):
    message_text = update.message.text
    chat_id = update.message.chat_id
    logger.info(f"Сообщение от {chat_id}: {message_text}")

    if message_text == "Создать заявку":
        context.user_data['step'] = 'document_type'
        send_telegram_message(chat_id,
                              "📝 Какие документы вам нужны?\n1. Оригиналы документов\n2. Электронный вариант",
                              context,
                              reply_markup=ReplyKeyboardMarkup([
                                  [KeyboardButton("Оригиналы документов"), KeyboardButton("Электронный вариант")],
                                  [KeyboardButton("Отмена")]
                              ], resize_keyboard=True))
    elif message_text == "Проверить статус":
        try:
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            c.execute("SELECT company, status, track_number FROM requests WHERE chat_id = ?", (str(chat_id),))
            requests = c.fetchall()
            c.execute("SELECT company, status, delivery_method FROM electronic_requests WHERE chat_id = ?", (str(chat_id),))
            electronic_requests = c.fetchall()
            conn.close()
            response = "📋 Твои заявки:\n"
            if requests:
                response += "Оригиналы:\n"
                for req in requests:
                    company, status, track_number = req
                    response += f"🏢 {company}: {status} (Трек: {track_number or 'Нет'})\n"
            if electronic_requests:
                response += "Электронные:\n"
                for req in electronic_requests:
                    company, status, delivery_method = req
                    response += f"🏢 {company}: {status} (Способ: {delivery_method})\n"
            if not requests and not electronic_requests:
                response = "🤷 У тебя пока нет заявок."
            send_telegram_message(chat_id, response, context)
        except Exception as e:
            logger.error(f"Ошибка проверки статуса: {e}")
            send_telegram_message(chat_id, "⚠️ Ошибка при проверке статуса.", context)
    elif message_text == "Помощь":
        send_telegram_message(chat_id,
                              "ℹ️ Я помогу тебе:\n"
                              "📝 'Создать заявку' — добавь новую заявку.\n"
                              "✅ 'Проверить статус' — узнай статус своих заявок.\n"
                              "🚪 'Отмена' — вернись в меню.", context)
    elif message_text == "Отмена":
        context.user_data.clear()
        send_telegram_message(chat_id, "🚪 Возвращаемся в меню!", context)
    elif 'step' in context.user_data:
        step = context.user_data['step']
        if step == 'document_type':
            if message_text == "Оригиналы документов":
                context.user_data['document_type'] = 'original'
                context.user_data['step'] = 'company'
                send_telegram_message(chat_id,
                                      "📝 Давай создадим заявку! Введи название компании:",
                                      context,
                                      reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
            elif message_text == "Электронный вариант":
                context.user_data['document_type'] = 'electronic'
                context.user_data['step'] = 'company_electronic'
                send_telegram_message(chat_id,
                                      "📝 Введи название компании:",
                                      context,
                                      reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
        # Оригиналы документов
        elif step == 'company':
            context.user_data['company'] = message_text
            context.user_data['step'] = 'name'
            send_telegram_message(chat_id,
                                  f"🏢 Компания: {message_text}\n👤 Теперь введи ФИО получателя:",
                                  context,
                                  reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
        elif step == 'name':
            context.user_data['name'] = message_text
            context.user_data['step'] = 'address'
            send_telegram_message(chat_id,
                                  f"👤 ФИО: {message_text}\n📍 Введи адрес доставки:",
                                  context,
                                  reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
        elif step == 'address':
            context.user_data['address'] = message_text
            context.user_data['step'] = 'contact_number'
            send_telegram_message(chat_id,
                                  f"📍 Адрес: {message_text}\n📞 Введи контактный номер:",
                                  context,
                                  reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
        elif step == 'contact_number':
            if validate_phone(message_text):
                context.user_data['contact_number'] = message_text
                context.user_data['step'] = 'confirm'
                confirmation_text = (
                    "📋 Проверь данные заявки:\n"
                    f"🏢 Компания: {context.user_data['company']}\n"
                    f"👤 ФИО: {context.user_data['name']}\n"
                    f"📍 Адрес: {context.user_data['address']}\n"
                    f"📞 Телефон: {context.user_data['contact_number']}\n"
                    "Всё верно?"
                )
                inline_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Да", callback_data='confirm_yes'),
                     InlineKeyboardButton("❌ Нет", callback_data='confirm_no')]
                ])
                context.bot.send_message(chat_id=chat_id, text=confirmation_text, reply_markup=inline_keyboard)
            else:
                send_telegram_message(chat_id, "📞 Неверный формат номера. Введите номер в формате +79991234567:", context)
        # Электронный вариант
        elif step == 'company_electronic':
            context.user_data['company'] = message_text
            context.user_data['step'] = 'iin'
            send_telegram_message(chat_id,
                                  f"🏢 Компания: {message_text}\n📑 Введи ИИН компании:",
                                  context,
                                  reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
        elif step == 'iin':
            context.user_data['iin'] = message_text
            context.user_data['step'] = 'documents'
            send_telegram_message(chat_id,
                                  f"📑 ИИН: {message_text}\n📜 Какие именно документы нужны?",
                                  context,
                                  reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
        elif step == 'documents':
            context.user_data['documents'] = message_text
            context.user_data['step'] = 'delivery_method'
            send_telegram_message(chat_id,
                                  f"📜 Документы: {message_text}\n📤 Как хотите получить?\n1. ЭДО\n2. В этой переписке",
                                  context,
                                  reply_markup=ReplyKeyboardMarkup([
                                      [KeyboardButton("ЭДО"), KeyboardButton("В этой переписке")],
                                      [KeyboardButton("Отмена")]
                                  ], resize_keyboard=True))
        elif step == 'delivery_method':
            if message_text in ["ЭДО", "В этой переписке"]:
                context.user_data['delivery_method'] = message_text
                context.user_data['step'] = 'confirm_electronic'
                confirmation_text = (
                    "📋 Проверь данные заявки:\n"
                    f"🏢 Компания: {context.user_data['company']}\n"
                    f"📑 ИИН: {context.user_data['iin']}\n"
                    f"📜 Документы: {context.user_data['documents']}\n"
                    f"📤 Способ получения: {context.user_data['delivery_method']}\n"
                    "Всё верно?"
                )
                inline_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Да", callback_data='confirm_yes_electronic'),
                     InlineKeyboardButton("❌ Нет", callback_data='confirm_no_electronic')]
                ])
                context.bot.send_message(chat_id=chat_id, text=confirmation_text, reply_markup=inline_keyboard)
            else:
                send_telegram_message(chat_id, "📤 Выберите 'ЭДО' или 'В этой переписке':", context)

def handle_callback(update, context):
    query = update.callback_query
    chat_id = query.message.chat_id
    data = query.data
    logger.info(f"Callback от {chat_id}: {data}")

    if data == 'confirm_yes' and context.user_data.get('step') == 'confirm':
        try:
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            submission_date = datetime.now().strftime('%d.%m.%Y %H:%M')
            c.execute(
                "INSERT INTO requests (submission_date, company, name, address, contact_number, status, chat_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (submission_date, context.user_data['company'], context.user_data['name'],
                 context.user_data['address'], context.user_data['contact_number'], 'Новая', str(chat_id)))
            conn.commit()
            conn.close()
            send_telegram_message(chat_id,
                                  f"✅ Заявка для {context.user_data['company']} создана!\nСтатус: Новая",
                                  context)
            send_telegram_message(EMPLOYEE_CHAT_ID,
                                  f"📩 Новая заявка (оригиналы) от {context.user_data['company']} ({submission_date})",
                                  context)
            context.user_data.clear()
        except Exception as e:
            logger.error(f"Ошибка создания заявки: {e}")
            send_telegram_message(chat_id, "⚠️ Ошибка при сохранении.", context)
    elif data == 'confirm_no' and context.user_data.get('step') == 'confirm':
        context.user_data['step'] = 'company'
        send_telegram_message(chat_id,
                              "📝 Давай начнём заново. Введи название компании:",
                              context,
                              reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
    elif data == 'confirm_yes_electronic' and context.user_data.get('step') == 'confirm_electronic':
        try:
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            submission_date = datetime.now().strftime('%d.%m.%Y %H:%M')
            c.execute(
                "INSERT INTO electronic_requests (submission_date, company, iin, documents, delivery_method, status, chat_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (submission_date, context.user_data['company'], context.user_data['iin'],
                 context.user_data['documents'], context.user_data['delivery_method'], 'Новая', str(chat_id)))
            conn.commit()
            conn.close()
            send_telegram_message(chat_id,
                                  f"✅ Заявка для {context.user_data['company']} создана!\nСтатус: Новая",
                                  context)
            send_telegram_message(EMPLOYEE_CHAT_ID,
                                  f"📩 Новая заявка (электронная) от {context.user_data['company']} ({submission_date})",
                                  context)
            context.user_data.clear()
        except Exception as e:
            logger.error(f"Ошибка создания заявки: {e}")
            send_telegram_message(chat_id, "⚠️ Ошибка при сохранении.", context)
    elif data == 'confirm_no_electronic' and context.user_data.get('step') == 'confirm_electronic':
        context.user_data['step'] = 'company_electronic'
        send_telegram_message(chat_id,
                              "📝 Давай начнём заново. Введи название компании:",
                              context,
                              reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True))
    query.answer()

def check_reminders():
    try:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("SELECT company, submission_date, status, chat_id FROM requests WHERE status != 'Отправлено'")
        for req in c.fetchall():
            company, submission_date, status, chat_id = req
            submission_datetime = datetime.strptime(submission_date, '%d.%m.%Y %H:%M')
            days_since = (datetime.now() - submission_datetime).days
            if days_since >= REMINDER_DAYS:
                message = f"⏰ Просрочка (оригиналы): заявка от {company} ({submission_date}) — {status}"
                send_telegram_message(EMPLOYEE_CHAT_ID, message, None)
        c.execute("SELECT company, submission_date, status, chat_id FROM electronic_requests WHERE status != 'Отправлено'")
        for req in c.fetchall():
            company, submission_date, status, chat_id = req
            submission_datetime = datetime.strptime(submission_date, '%d.%m.%Y %H:%M')
            days_since = (datetime.now() - submission_datetime).days
            if days_since >= REMINDER_DAYS:
                message = f"⏰ Просрочка (электронные): заявка от {company} ({submission_date}) — {status}"
                send_telegram_message(EMPLOYEE_CHAT_ID, message, None)
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка напоминаний: {e}")

def run_scheduler():
    for _ in range(REMINDER_COUNT):
        schedule.every().day.at(REMINDER_TIME).do(check_reminders)
    while True:
        schedule.run_pending()
        time.sleep(60)

def main():
    init_db()
    logger.info("Запуск бота...")
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(CallbackQueryHandler(handle_callback))
    updater.start_polling()
    logger.info("Бот запущен")
    threading.Thread(target=run_scheduler, daemon=True).start()
    updater.idle()

if __name__ == '__main__':
    main()