import os
import logging
import tempfile
import asyncio
import threading
import time
import uuid
import json
import humanize
import requests as req_lib
from datetime import datetime, timedelta
from flask import Flask
from urllib.parse import urlparse, unquote

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ═══════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
GDRIVE_SERVICE_ACCOUNT_JSON = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID")
DEFAULT_DELETE_HOURS = int(os.getenv("DEFAULT_DELETE_HOURS", "2"))

if AUTHORIZED_USER_ID:
    try:
        AUTHORIZED_USER_ID = int(AUTHORIZED_USER_ID)
    except:
        AUTHORIZED_USER_ID = None

user_settings = {}
active_downloads = {}
stats = {
    "total_uploads": 0,
    "total_bytes": 0,
    "start_time": datetime.now()
}

# ═══════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return f"OK | Uploads: {stats['total_uploads']}", 200

# ═══════════════════════════════════════════════
# GOOGLE DRIVE (google-auth)
# ═══════════════════════════════════════════════
drive_service = None

def get_drive():
    global drive_service
    if not GDRIVE_SERVICE_ACCOUNT_JSON:
        logger.error("No GDRIVE_SERVICE_ACCOUNT_JSON")
        return None
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            f.write(GDRIVE_SERVICE_ACCOUNT_JSON)
            path = f.name

        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/drive"]
        )
        os.unlink(path)

        drive_service = build("drive", "v3", credentials=creds)
        logger.info("Google Drive connected")
        return drive_service
    except Exception as e:
        logger.error(f"Drive error: {e}")
        return None

# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════
def format_size(b):
    return humanize.naturalsize(b, binary=True)

def format_speed(b):
    return f"{humanize.naturalsize(b, binary=True)}/s"

def format_time(s):
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s//60)}m {int(s%60)}s"
    return f"{int(s//3600)}h {int((s%3600)//60)}m"

def progress_bar(p, length=15):
    p = min(100, max(0, p))
    filled = int(length * p / 100)
    return "[" + "█" * filled + "░" * (length - filled) + "]"

def get_delete_hours(uid):
    return user_settings.get(uid, {}).get("delete_hours", DEFAULT_DELETE_HOURS)

def get_filename_from_url(url):
    try:
        path = urlparse(url).path
        name = unquote(os.path.basename(path))
        if name and "." in name:
            return name
    except:
        pass
    return None

def get_filename_from_headers(headers):
    cd = headers.get("Content-Disposition", "")
    if "filename=" in cd:
        try:
            parts = cd.split("filename=")
            if len(parts) > 1:
                name = parts[1].strip().strip('"').strip("'")
                if name:
                    return name
        except:
            pass
    return None

# ═══════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════
class DownloadTask:
    def __init__(self, task_id, url, user_id):
        self.task_id = task_id
        self.url = url
        self.user_id = user_id
        self.status = "downloading"
        self.progress = 0
        self.speed = 0
        self.downloaded = 0
        self.total_size = 0
        self.filename = ""
        self.filepath = ""
        self.cancelled = False
        self.start_time = time.time()

def download_file(task, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)

    response = req_lib.get(task.url, stream=True, timeout=30, allow_redirects=True)
    response.raise_for_status()

    task.filename = get_filename_from_headers(response.headers)
    if not task.filename:
        task.filename = get_filename_from_url(task.url)
    if not task.filename:
        task.filename = f"file_{task.task_id}"

    task.total_size = int(response.headers.get("content-length", 0))
    task.filepath = os.path.join(dest_dir, task.filename)
    task.downloaded = 0

    chunk_size = 1024 * 1024
    last_time = time.time()
    last_downloaded = 0

    with open(task.filepath, "wb") as f:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if task.cancelled:
                return False
            if chunk:
                f.write(chunk)
                task.downloaded += len(chunk)
                if task.total_size > 0:
                    task.progress = (task.downloaded / task.total_size) * 100
                now = time.time()
                elapsed = now - last_time
                if elapsed >= 0.5:
                    task.speed = (task.downloaded - last_downloaded) / elapsed
                    last_time = now
                    last_downloaded = task.downloaded

    task.progress = 100
    return True

# ═══════════════════════════════════════════════
# UPLOAD (google-api-python-client)
# ═══════════════════════════════════════════════
def upload_file(filepath, filename):
    global drive_service
    if not drive_service:
        get_drive()
    if not drive_service:
        raise RuntimeError("Drive baglantisi yok")
    if not GDRIVE_FOLDER_ID:
        raise RuntimeError("GDRIVE_FOLDER_ID ayarlanmamis")

    file_meta = {
        "name": filename,
        "parents": [GDRIVE_FOLDER_ID]
    }

    media = MediaFileUpload(filepath, resumable=True)

    gf = drive_service.files().create(
        body=file_meta,
        media_body=media,
        fields="id,name,webViewLink,webContentLink"
    ).execute()

    # Herkese acik yap
    drive_service.permissions().create(
        fileId=gf["id"],
        body={"type": "anyone", "role": "reader"}
    ).execute()

    # Link guncelle
    gf = drive_service.files().get(
        fileId=gf["id"],
        fields="id,name,webViewLink,webContentLink"
    ).execute()

    return gf

# ═══════════════════════════════════════════════
# AUTO DELETE
# ═══════════════════════════════════════════════
async def auto_delete(file_id, filename):
    try:
        global drive_service
        if drive_service:
            await asyncio.to_thread(
                drive_service.files().delete(fileId=file_id).execute
            )
            logger.info(f"Silindi: {filename}")
    except Exception as e:
        logger.error(f"Silme hatasi: {e}")

# ═══════════════════════════════════════════════
# BOT HANDLERS
# ═══════════════════════════════════════════════
sched = None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    h = get_delete_hours(update.effective_user.id)
    await update.message.reply_text(
        f"🚀 *Upload Bot*\n\n"
        f"Link gonder, Drive'a yuklerim\n\n"
        f"/downloads - Aktif indirmeler\n"
        f"/settime - Silme suresi ({h}h)\n"
        f"/status - Durum\n"
        f"/help - Yardim",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    await update.message.reply_text(
        "📚 *Yardim*\n\n"
        "Link gonder, hallederim.\n"
        "`/settime 0` = kalici\n"
        "`/settime 24` = 24 saat",
        parse_mode="Markdown"
    )

async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    uid = update.effective_user.id
    if not context.args:
        h = get_delete_hours(uid)
        await update.message.reply_text(f"Su an: {h} saat\n`/settime <saat>`", parse_mode="Markdown")
        return
    try:
        h = int(context.args[0])
        if h < 0 or h > 168:
            await update.message.reply_text("0-168 arasi")
            return
        if uid not in user_settings:
            user_settings[uid] = {}
        user_settings[uid]["delete_hours"] = h
        await update.message.reply_text(f"✅ {'Kalici' if h==0 else f'{h} saat'}")
    except:
        await update.message.reply_text("Sayi girin")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    active = len([t for t in active_downloads.values() if t.status in ("downloading", "uploading")])
    await update.message.reply_text(
        f"📊 *Durum*\n\n"
        f"Yukleme: {stats['total_uploads']}\n"
        f"Toplam: {format_size(stats['total_bytes'])}\n"
        f"Aktif: {active}",
        parse_mode="Markdown"
    )

async def cmd_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    uid = update.effective_user.id
    tasks = [t for t in active_downloads.values() if t.user_id == uid and t.status in ("downloading", "uploading")]
    if not tasks:
        await update.message.reply_text("📭 Aktif indirme yok")
        return
    text = "📥 *Aktif:*\n\n"
    buttons = []
    for t in tasks:
        e = "📥" if t.status == "downloading" else "📤"
        n = (t.filename[:20] + "...") if len(t.filename) > 20 else (t.filename or "...")
        text += f"{e} `{n}` {t.progress:.0f}%\n"
        buttons.append([InlineKeyboardButton(f"❌ {n[:15]}", callback_data=f"c_{t.task_id}")])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if AUTHORIZED_USER_ID and q.from_user.id != AUTHORIZED_USER_ID:
        return
    data = q.data
    if data.startswith("c_"):
        tid = data[2:]
        if tid in active_downloads:
            t = active_downloads[tid]
            t.cancelled = True
            if t.filepath and os.path.exists(t.filepath):
                try:
                    os.remove(t.filepath)
                except:
                    pass
            await q.edit_message_text(f"❌ Iptal: `{t.filename or 'Indirme'}`", parse_mode="Markdown")
        else:
            await q.edit_message_text("Bulunamadi")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("⛔ Gecersiz link")
        return
    uid = update.effective_user.id
    delete_hours = get_delete_hours(uid)
    task_id = str(uuid.uuid4())[:8]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Iptal", callback_data=f"c_{task_id}")]])
    msg = await update.message.reply_text("📥 *Baslatiliyor...*", parse_mode="Markdown", reply_markup=kb)
    task = DownloadTask(task_id, url, uid)
    active_downloads[task_id] = task
    asyncio.create_task(process_download(task, msg, kb, delete_hours))

async def process_download(task, msg, kb, delete_hours):
    global stats, active_downloads, sched
    dest = os.path.join(os.getcwd(), "downloads")

    try:
        task.status = "downloading"

        async def update_progress():
            last = 0
            while task.status == "downloading" and not task.cancelled:
                await asyncio.sleep(1.5)
                if task.progress != last:
                    try:
                        n = task.filename or "Indiriliyor..."
                        text = (
                            f"📥 *Indiriliyor*\n\n"
                            f"📁 `{n}`\n"
                            f"{progress_bar(task.progress)} {task.progress:.1f}%\n\n"
                            f"📦 {format_size(task.downloaded)}"
                        )
                        if task.total_size > 0:
                            text += f" / {format_size(task.total_size)}"
                        if task.speed > 0:
                            text += f"\n⚡ {format_speed(task.speed)}"
                        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
                        last = task.progress
                    except:
                        pass

        progress_task = asyncio.create_task(update_progress())
        success = await asyncio.to_thread(download_file, task, dest)
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

        if task.cancelled:
            raise asyncio.CancelledError()
        if not success:
            raise Exception("Indirme basarisiz")

        sz = os.path.getsize(task.filepath)
        if sz > 5 * 1024 * 1024 * 1024:
            await msg.edit_text(f"⚠️ Cok buyuk: {format_size(sz)}", parse_mode="Markdown")
            os.remove(task.filepath)
            del active_downloads[task.task_id]
            return

        task.status = "uploading"
        await msg.edit_text(
            f"📤 *Yukleniyor...*\n\n📁 `{task.filename}`\n📦 {format_size(sz)}",
            parse_mode="Markdown", reply_markup=kb
        )

        if task.cancelled:
            raise asyncio.CancelledError()

        gf = await asyncio.to_thread(upload_file, task.filepath, task.filename)

        if task.cancelled:
            raise asyncio.CancelledError()

        fid = gf["id"]
        link = gf.get("webViewLink", f"https://drive.google.com/file/d/{fid}/view")

        stats["total_uploads"] += 1
        stats["total_bytes"] += sz
        elapsed = time.time() - task.start_time
        task.status = "completed"

        result = (
            f"✅ *Tamamlandi!*\n\n"
            f"📁 `{task.filename}`\n"
            f"📦 {format_size(sz)} | ⏱️ {format_time(elapsed)}\n\n"
            f"🔗 [Drive Link]({link})\n\n"
        )
        if delete_hours > 0:
            result += f"🗑️ {delete_hours}h sonra silinecek"
            sched.add_job(auto_delete, "date",
                          run_date=datetime.now() + timedelta(hours=delete_hours),
                          args=[fid, task.filename])
        else:
            result += "♾️ Kalici"

        await msg.edit_text(result, parse_mode="Markdown", disable_web_page_preview=True)

        if os.path.exists(task.filepath):
            os.remove(task.filepath)
        del active_downloads[task.task_id]

    except asyncio.CancelledError:
        task.status = "cancelled"
        await msg.edit_text("❌ *Iptal edildi*", parse_mode="Markdown")
        if task.filepath and os.path.exists(task.filepath):
            try:
                os.remove(task.filepath)
            except:
                pass
        if task.task_id in active_downloads:
            del active_downloads[task.task_id]

    except Exception as e:
        task.status = "failed"
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ *Hata:* `{str(e)[:100]}`", parse_mode="Markdown")
        if task.filepath and os.path.exists(task.filepath):
            try:
                os.remove(task.filepath)
            except:
                pass
        if task.task_id in active_downloads:
            del active_downloads[task.task_id]

# ═══════════════════════════════════════════════
# BOT RUNNER
# ═══════════════════════════════════════════════
async def run_bot():
    global sched
    if not BOT_TOKEN:
        logger.error("NO BOT_TOKEN")
        return

    sched = AsyncIOScheduler()
    sched.start()
    get_drive()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("downloads", cmd_downloads))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    commands = [
        BotCommand("start", "Baslat"),
        BotCommand("downloads", "Aktif indirmeler"),
        BotCommand("settime", "Silme suresi"),
        BotCommand("status", "Durum"),
        BotCommand("help", "Yardim"),
    ]

    await app.initialize()
    await app.bot.set_my_commands(commands)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot is running!")

    while True:
        await asyncio.sleep(3600)

def start_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    bot_thread = threading.Thread(target=start_bot_thread, daemon=True)
    bot_thread.start()
    logger.info("Bot thread started")
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
