from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_caching import Cache
from flask_compress import Compress
from flask_wtf import FlaskForm, CSRFProtect
from wtforms import StringField, PasswordField, SubmitField, IntegerField
from wtforms.validators import DataRequired, Length
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv, find_dotenv
import os
import shutil
import schedule
import time
import threading
import requests
from io import StringIO
import csv

app = Flask(__name__)
app.secret_key = os.urandom(24)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

cache = Cache(app, config={'CACHE_TYPE': 'simple'})
Compress(app)
csrf = CSRFProtect(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {'.pdf', '.jpeg', '.jpg', '.png', '.txt', '.doc', '.docx'}

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
EMPLOYEE_CHAT_ID = os.getenv("EMPLOYEE_CHAT_ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", generate_password_hash("admin"))
REMINDER_TIME = os.getenv("REMINDER_TIME", "09:00")
REMINDER_COUNT = int(os.getenv("REMINDER_COUNT", 1))
REMINDER_DAYS = int(os.getenv("REMINDER_DAYS", 1))


class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role


@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    return User(user[0], user[1], user[2]) if user else None


class LoginForm(FlaskForm):
    username = StringField('Логин', validators=[DataRequired(), Length(min=1, max=50)])
    password = PasswordField('Пароль', validators=[DataRequired()])
    submit = SubmitField('Войти')


class SettingsForm(FlaskForm):
    username = StringField('Новый логин', validators=[Length(max=50)])
    password = PasswordField('Новый пароль')
    telegram_token = StringField('Telegram Token', validators=[Length(max=100)])
    employee_chat_id = StringField('ID администратора', validators=[Length(max=20)])
    reminder_time = StringField('Время уведомлений (HH:MM)', validators=[Length(max=5)])
    reminder_count = IntegerField('Количество уведомлений в день', validators=[DataRequired()])
    reminder_days = IntegerField('Дни для просрочки', validators=[DataRequired()])
    submit = SubmitField('Сохранить настройки')


def send_telegram_message(chat_id, text, file_path=None, token=TELEGRAM_TOKEN):
    try:
        if file_path:
            file_url = f"https://api.telegram.org/bot{token}/sendDocument"
            with open(file_path, 'rb') as file:
                response = requests.post(file_url, data={'chat_id': chat_id, 'caption': text}, files={'document': file})
        else:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            response = requests.post(url, data={'chat_id': chat_id, 'text': text})
        if response.status_code != 200:
            raise Exception(f"Ошибка Telegram: {response.text}")
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        return False
    return True


def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS requests
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_date TEXT, company TEXT, name TEXT, address TEXT, 
                  contact_number TEXT, track_number TEXT, status TEXT, chat_id TEXT, received INTEGER DEFAULT 0, 
                  attachment TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS electronic_requests
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_date TEXT, company TEXT, iin TEXT, documents TEXT, 
                  delivery_method TEXT, status TEXT, chat_id TEXT, attachment TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password_hash TEXT, role TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS request_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, change_date TEXT, old_status TEXT, new_status TEXT, 
                  changed_by TEXT, table_name TEXT)''')
    c.execute("INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, ?)",
              (ADMIN_USERNAME, ADMIN_PASSWORD_HASH, 'admin'))

    c.execute("PRAGMA table_info(requests)")
    columns = [col[1] for col in c.fetchall()]
    if 'attachment' not in columns:
        c.execute("ALTER TABLE requests ADD COLUMN attachment TEXT")

    c.execute("PRAGMA table_info(electronic_requests)")
    columns = [col[1] for col in c.fetchall()]
    if 'attachment' not in columns:
        c.execute("ALTER TABLE electronic_requests ADD COLUMN attachment TEXT")

    conn.commit()
    conn.close()


init_db()


def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def clean_old_files():
    now = datetime.now()
    cutoff = now - timedelta(days=7)
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT attachment FROM requests WHERE attachment IS NOT NULL")
    active_files_requests = {row[0] for row in c.fetchall()}
    c.execute("SELECT attachment FROM electronic_requests WHERE attachment IS NOT NULL")
    active_files_electronic = {row[0] for row in c.fetchall()}
    active_files = active_files_requests.union(active_files_electronic)
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(file_path) and datetime.fromtimestamp(
                os.path.getmtime(file_path)) < cutoff and filename not in active_files:
            os.remove(file_path)
    conn.close()


def run_cleanup_scheduler():
    schedule.every().day.at("00:00").do(clean_old_files)
    while True:
        schedule.run_pending()
        time.sleep(60)


def update_env(key, value):
    env_file = find_dotenv()
    with open(env_file, 'r') as file:
        lines = file.readlines()
    with open(env_file, 'w') as file:
        found = False
        for line in lines:
            if line.startswith(f"{key}="):
                file.write(f"{key}={value}\n")
                found = True
            else:
                file.write(line)
        if not found:
            file.write(f"{key}={value}\n")
    os.environ[key] = value


@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data
        password = form.password.data
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("SELECT id, username, password_hash, role FROM users WHERE username = ?", (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user[2], password):
            login_user(User(user[0], user[1], user[3]))
            return redirect(url_for('index'))
        flash('Пожалуйста, введите логин и пароль.')
    return render_template('login.html', form=form)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/', methods=['GET'])
@login_required
def index():
    table = request.args.get('table', 'requests')
    ITEMS_PER_PAGE = 10
    page = int(request.args.get('page', 1))
    offset = (page - 1) * ITEMS_PER_PAGE
    search = request.args.get('search', '')
    status = request.args.get('status', '')

    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    if table == 'requests':
        count_query = "SELECT COUNT(*) FROM requests WHERE 1=1"
        count_params = []
        if search:
            count_query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
            count_params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
        if status:
            count_query += " AND status = ?"
            count_params.append(status)
        c.execute(count_query, count_params)
        total_items = c.fetchone()[0]
        total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

        query = "SELECT id, submission_date, company, name, address, contact_number, track_number, status, attachment FROM requests WHERE 1=1"
        params = []
        if search:
            query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
            params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " LIMIT ? OFFSET ?"
        params.extend([ITEMS_PER_PAGE, offset])
        c.execute(query, params)
        requests_list = c.fetchall()

        c.execute("SELECT COUNT(*) FROM requests WHERE status != 'Отправлено'")
        overdue_count = c.fetchone()[0]

        requests_json = [{'id': r[0], 'submission_date': r[1], 'company': r[2], 'name': r[3], 'address': r[4],
                          'contact_number': r[5], 'track_number': r[6], 'status': r[7], 'attachment': r[8]} for r in
                         requests_list]
    else:  # electronic_requests
        count_query = "SELECT COUNT(*) FROM electronic_requests WHERE 1=1"
        count_params = []
        if search:
            count_query += " AND (company LIKE ? OR iin LIKE ? OR documents LIKE ?)"
            count_params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
        if status:
            count_query += " AND status = ?"
            count_params.append(status)
        c.execute(count_query, count_params)
        total_items = c.fetchone()[0]
        total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

        query = "SELECT id, submission_date, company, iin, documents, delivery_method, status, attachment FROM electronic_requests WHERE 1=1"
        params = []
        if search:
            query += " AND (company LIKE ? OR iin LIKE ? OR documents LIKE ?)"
            params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " LIMIT ? OFFSET ?"
        params.extend([ITEMS_PER_PAGE, offset])
        c.execute(query, params)
        requests_list = c.fetchall()

        c.execute("SELECT COUNT(*) FROM electronic_requests WHERE status != 'Отправлено'")
        overdue_count = c.fetchone()[0]

        requests_json = [{'id': r[0], 'submission_date': r[1], 'company': r[2], 'iin': r[3], 'documents': r[4],
                          'delivery_method': r[5], 'status': r[6], 'attachment': r[7]} for r in requests_list]

    conn.close()

    return render_template('index.html', requests=requests_json, overdue_count=overdue_count, page=page,
                           total_pages=total_pages,
                           search=search, status=status, table=table)


@app.route('/update_status', methods=['POST'])
@login_required
def update_status():
    try:
        request_id = request.form['request_id']
        action = request.form.get('action')
        table = request.form.get('table', 'requests')

        conn = sqlite3.connect('database.db')
        c = conn.cursor()

        if table == 'requests':
            if action == 'delete':
                c.execute("DELETE FROM requests WHERE id = ?", (request_id,))
            elif action == 'status':
                new_status = request.form['status']
                c.execute("SELECT status FROM requests WHERE id = ?", (request_id,))
                old_status = c.fetchone()[0]
                if new_status == 'Отправлено':
                    track_number = request.form.get('track_number', '')
                    attachment = request.files.get('attachment')
                    if not track_number:
                        conn.close()
                        return jsonify({'success': False, 'message': 'Трек-номер обязателен для "Отправлено"'})
                    if attachment and allowed_file(attachment.filename) and attachment.content_length <= MAX_FILE_SIZE:
                        filename = f"{request_id}_{attachment.filename}"
                        attachment.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                        c.execute("UPDATE requests SET status = ?, track_number = ?, attachment = ? WHERE id = ?",
                                  (new_status, track_number, filename, request_id))
                    else:
                        c.execute("UPDATE requests SET status = ?, track_number = ? WHERE id = ?",
                                  (new_status, track_number, request_id))
                else:
                    c.execute("UPDATE requests SET status = ?, track_number = NULL, attachment = NULL WHERE id = ?",
                              (new_status, request_id))
                c.execute(
                    "INSERT INTO request_history (request_id, change_date, old_status, new_status, changed_by, table_name) VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, datetime.now().strftime('%d.%m.%Y %H:%M'), old_status, new_status,
                     current_user.username, 'requests'))
                c.execute("SELECT company, chat_id FROM requests WHERE id = ?", (request_id,))
                company, chat_id = c.fetchone()
                message = f"📋 Заявка {company}: статус изменён на '{new_status}'" + (
                    f", трек: {track_number}" if new_status == 'Отправлено' else "")
                send_telegram_message(chat_id, message, os.path.join(app.config['UPLOAD_FOLDER'],
                                                                     filename) if 'filename' in locals() else None)
        else:  # electronic_requests
            if action == 'delete':
                c.execute("DELETE FROM electronic_requests WHERE id = ?", (request_id,))
            elif action == 'status':
                new_status = request.form['status']
                c.execute("SELECT status, delivery_method FROM electronic_requests WHERE id = ?", (request_id,))
                old_status, delivery_method = c.fetchone()
                if new_status == 'Отправлено':
                    if delivery_method == 'ЭДО':
                        c.execute("UPDATE electronic_requests SET status = ? WHERE id = ?", (new_status, request_id))
                        c.execute("SELECT company, chat_id FROM electronic_requests WHERE id = ?", (request_id,))
                        company, chat_id = c.fetchone()
                        send_telegram_message(chat_id, f"📋 Заявка {company}: документы отправлены в ЭДО")
                    else:  # В этой переписке
                        attachment = request.files.get('attachment')
                        if attachment and allowed_file(
                                attachment.filename) and attachment.content_length <= MAX_FILE_SIZE:
                            filename = f"{request_id}_{attachment.filename}"
                            attachment.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                            c.execute("UPDATE electronic_requests SET status = ?, attachment = ? WHERE id = ?",
                                      (new_status, filename, request_id))
                            c.execute("SELECT company, chat_id FROM electronic_requests WHERE id = ?", (request_id,))
                            company, chat_id = c.fetchone()
                            send_telegram_message(chat_id, f"📋 Заявка {company}: документы отправлены",
                                                  os.path.join(app.config['UPLOAD_FOLDER'], filename))
                        else:
                            conn.close()
                            return jsonify({'success': False, 'message': 'Файл обязателен для отправки в переписке'})
                else:
                    c.execute("UPDATE electronic_requests SET status = ? WHERE id = ?", (new_status, request_id))
                    c.execute(
                        "INSERT INTO request_history (request_id, change_date, old_status, new_status, changed_by, table_name) VALUES (?, ?, ?, ?, ?, ?)",
                        (request_id, datetime.now().strftime('%d.%m.%Y %H:%M'), old_status, new_status,
                         current_user.username, 'electronic_requests'))
                    c.execute("SELECT company, chat_id FROM electronic_requests WHERE id = ?", (request_id,))
                    company, chat_id = c.fetchone()
                    message = f"📋 Заявка {company}: статус изменён на '{new_status}'"
                    send_telegram_message(chat_id, message)

        conn.commit()
        conn.close()
        cache.clear()
        return filter_requests()
    except Exception as e:
        print(f"Error in /update_status: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/filter_requests', methods=['POST'])
@login_required
def filter_requests():
    try:
        table = request.form.get('table', 'requests')
        ITEMS_PER_PAGE = 10
        page = int(request.args.get('page', 1))
        search = request.form.get('search', '')
        status = request.form.get('status', '')

        conn = sqlite3.connect('database.db')
        c = conn.cursor()

        if table == 'requests':
            count_query = "SELECT COUNT(*) FROM requests WHERE 1=1"
            count_params = []
            if search:
                count_query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
                count_params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
            if status:
                count_query += " AND status = ?"
                count_params.append(status)
            c.execute(count_query, count_params)
            total_items = c.fetchone()[0]
            total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

            if page > total_pages:
                page = max(1, total_pages)
            offset = (page - 1) * ITEMS_PER_PAGE

            query = "SELECT id, submission_date, company, name, address, contact_number, track_number, status, attachment FROM requests WHERE 1=1"
            params = []
            if search:
                query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
                params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " LIMIT ? OFFSET ?"
            params.extend([ITEMS_PER_PAGE, offset])
            c.execute(query, params)
            requests_list = c.fetchall()

            c.execute("SELECT COUNT(*) FROM requests WHERE status != 'Отправлено'")
            overdue_count = c.fetchone()[0]

            requests_json = [{'id': r[0], 'submission_date': r[1], 'company': r[2], 'name': r[3], 'address': r[4],
                              'contact_number': r[5], 'track_number': r[6], 'status': r[7], 'attachment': r[8]} for r in
                             requests_list]
        else:  # electronic_requests
            count_query = "SELECT COUNT(*) FROM electronic_requests WHERE 1=1"
            count_params = []
            if search:
                count_query += " AND (company LIKE ? OR iin LIKE ? OR documents LIKE ?)"
                count_params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
            if status:
                count_query += " AND status = ?"
                count_params.append(status)
            c.execute(count_query, count_params)
            total_items = c.fetchone()[0]
            total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

            if page > total_pages:
                page = max(1, total_pages)
            offset = (page - 1) * ITEMS_PER_PAGE

            query = "SELECT id, submission_date, company, iin, documents, delivery_method, status, attachment FROM electronic_requests WHERE 1=1"
            params = []
            if search:
                query += " AND (company LIKE ? OR iin LIKE ? OR documents LIKE ?)"
                params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " LIMIT ? OFFSET ?"
            params.extend([ITEMS_PER_PAGE, offset])
            c.execute(query, params)
            requests_list = c.fetchall()

            c.execute("SELECT COUNT(*) FROM electronic_requests WHERE status != 'Отправлено'")
            overdue_count = c.fetchone()[0]

            requests_json = [{'id': r[0], 'submission_date': r[1], 'company': r[2], 'iin': r[3], 'documents': r[4],
                              'delivery_method': r[5], 'status': r[6], 'attachment': r[7]} for r in requests_list]

        conn.close()

        return jsonify({
            'success': True,
            'requests': requests_json,
            'overdue_count': overdue_count,
            'page': page,
            'total_pages': total_pages,
            'search': search,
            'status': status,
            'table': table
        })
    except Exception as e:
        print(f"Error in /filter_requests: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/analytics')
@login_required
def analytics():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM requests GROUP BY status")
    stats_requests = c.fetchall()
    c.execute("SELECT status, COUNT(*) FROM electronic_requests GROUP BY status")
    stats_electronic = c.fetchall()
    conn.close()
    return render_template('analytics.html', stats_requests=stats_requests, stats_electronic=stats_electronic)


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    form = SettingsForm()
    if form.validate_on_submit():
        new_username = form.username.data
        new_password = form.password.data
        if new_username and new_password:
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            c.execute("UPDATE users SET username = ?, password_hash = ? WHERE role = 'admin'",
                      (new_username, generate_password_hash(new_password)))
            conn.commit()
            conn.close()
            flash('Логин и пароль успешно обновлены')

        if form.telegram_token.data:
            update_env('TELEGRAM_TOKEN', form.telegram_token.data)
        if form.employee_chat_id.data:
            update_env('EMPLOYEE_CHAT_ID', form.employee_chat_id.data)
        if form.reminder_time.data:
            update_env('REMINDER_TIME', form.reminder_time.data)
        if form.reminder_count.data:
            update_env('REMINDER_COUNT', str(form.reminder_count.data))
        if form.reminder_days.data:
            update_env('REMINDER_DAYS', str(form.reminder_days.data))
        return redirect(url_for('settings'))

    form.telegram_token.data = os.getenv("TELEGRAM_TOKEN")
    form.employee_chat_id.data = os.getenv("EMPLOYEE_CHAT_ID")
    form.reminder_time.data = os.getenv("REMINDER_TIME")
    form.reminder_count.data = int(os.getenv("REMINDER_COUNT", 1))
    form.reminder_days.data = int(os.getenv("REMINDER_DAYS", 1))
    return render_template('settings.html', form=form)


@app.route('/export', methods=['GET'])
@login_required
def export():
    table = request.args.get('table', 'requests')
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    if table == 'requests':
        c.execute("SELECT * FROM requests")
        rows = c.fetchall()
        headers = ['id', 'submission_date', 'company', 'name', 'address', 'contact_number', 'track_number', 'status',
                   'chat_id', 'received', 'attachment']
    else:
        c.execute("SELECT * FROM electronic_requests")
        rows = c.fetchall()
        headers = ['id', 'submission_date', 'company', 'iin', 'documents', 'delivery_method', 'status', 'chat_id',
                   'attachment']
    conn.close()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)

    response = app.response_class(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={table}.csv'}
    )
    return response


if __name__ == '__main__':
    threading.Thread(target=run_cleanup_scheduler, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5000)