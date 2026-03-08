import collections
import collections.abc
if not hasattr(collections, "Iterable"):
    collections.Iterable = collections.abc.Iterable

import os
import logging
import tempfile
import asyncio
import threading
import time
import uuid
import humanize
import requests
from datetime import datetime, timedelta
from flask import Flask
from urllib.parse import urlparse, unquote

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials
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

logger.info(f"BOT_TOKEN: {bool(BOT_TOKEN)}")
logger.info(f"GDRIVE_JSON: {bool(GDRIVE_SERVICE_ACCOUNT_JSON)}")

# ═══════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return f"OK | Uploads: {stats['total_uploads']}", 200

# ═══════════════════════════════════════════════
# GOOGLE DRIVE
# ═══════════════════════════════════════════════
drive_client = None

def get_drive():
    global drive_client
    if not GDRIVE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            f.write(GDRIVE_SERVICE_ACCOUNT_JSON)
            path = f.name
        gauth = GoogleAuth()
        gauth.auth_method = "service"
        gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
            path, ["https://www.googleapis.com/auth/drive"]
        )
        drive_client = GoogleDrive(gauth)
        os.unlink(path)
        logger.info("✅ Google Drive connected")
        return drive_client
    except Exception as e:
        logger.error(f"Drive auth error: {e}")
        return None

# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════
def format_size(b):
    return humanize.naturalsize(b, binary=True)

def format_speed(b):
    return f"{humanize.naturalsize(b, binary=True)}/s"

def format_time(s):
    if s < 60: return f"{int(s)}s"
    if s < 3600: return f"{int(s//60)}m {int(s%60)}s"
    return f"{int(s//3600)}h {int((s%3600)//60)}m"

def progress_bar(p, l=15):
    p = min(100, max(0, p))
    f = int(l * p / 100)
    return f"[{'█'*f}{'░'*(l-f)}]"

def get_delete_hours(uid):
    return user_settings.get(uid, {}).get("delete_hours", DEFAULT_DELETE_HOURS)

def get_filename_from_url(url):
    try:
        path = urlparse(url).path
        name = unquote(os.path.basename(path))
        if name and '.' in name:
            return name
    except:
        pass
    return None

def get_filename_from_headers(headers):
    cd = headers.get('Content-Disposition', '')
    if 'filename=' in cd:
        try:
            parts = cd.split('filename=')
            if len(parts) > 1:
                name = parts[1].strip().strip('"').strip("'")
                if name:
                    return name
        except:
            pass
    return None

# ═══════════════════════════════════════════════
# DOWNLOAD CLASS
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

# ═══════════════════════════════════════════════
# DOWNLOAD FUNCTION (requests ile)
# ═══════════════════════════════════════════════
def download_file(task, dest_dir):
    """Senkron indirme - requests ile"""
    os.makedirs(dest_dir, exist_ok=True)
    
    try:
        response = requests.get(task.url, stream=True, timeout=30, allow_redirects=True)
        response.raise_for_status()
        
        # Dosya adını bul
        task.filename = get_filename_from_headers(response.headers)
        if not task.filename:
            task.filename = get_filename_from_url(task.url)
        if not task.filename:
            task.filename = f"file_{task.task_id}"
        
        # Toplam boyut
        task.total_size = int(response.headers.get('content-length', 0))
        
        # Dosya yolu
        task.filepath = os.path.join(dest_dir, task.filename)
        
        # İndir
        task.downloaded = 0
        chunk_size = 1024 * 1024  # 1MB
        last_time = time.time()
        last_downloaded = 0
        
        with open(task.filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if task.cancelled:
                    return False
                
                if chunk:
                    f.write(chunk)
                    task.downloaded += len(chunk)
                    
                    # Progress ve hız hesapla
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
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        raise e

def upload_file(filepath, filename):
    """Senkron yükleme"""
    global drive_client
    if not drive_client:
        get_drive()
    if not drive_client:
        raise RuntimeError("Drive bağlantısı yok")
    
    meta = {"title": filename}
    if GDRIVE_FOLDER_ID:
        meta["parents"] = [{"id": GDRIVE_FOLDER_ID}]
    
    gf = drive_client.CreateFile(meta)
    gf.SetContentFile(filepath)
    gf.Upload()
    return gf

# ═══════════════════════════════════════════════
# AUTO DELETE
# ═══════════════════════════════════════════════
async def auto_delete(file_id, filename):
    try:
        global drive_client
        if drive_client:
            gf = await asyncio.to_thread(drive_client.CreateFile, {"id": file_id})
            await asyncio.to_thread(gf.Delete)
            logger.info(f"🗑️ Silindi: {filename}")
    except Exception as e:
        logger.error(f"Silme hatası: {e}")

# ═══════════════════════════════════════════════
# BOT HANDLERS
# ═══════════════════════════════════════════════
sched = None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    h = get_delete_hours(update.effective_user.id)
    await update.message.reply_text(
        f"🚀 **Upload Bot**\n\n"
        f"Link gönder → Drive'a yükle\n\n"
        f"**Komutlar:**\n"
        f"/downloads - Aktif indirmeler\n"
        f"/settime {h} - Silme süresi\n"
        f"/status - Durum",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    await update.message.reply_text(
        "📚 **Yardım**\n\n"
        "Link gönder, ben hallederim.\n"
        "`/settime 0` = kalıcı\n"
        "`/settime 24` = 24 saat",
        parse_mode="Markdown"
    )

async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    uid = update.effective_user.id
    if not context.args:
        h = get_delete_hours(uid)
        await update.message.reply_text(f"⏱️ Şu an: {h} saat\n`/settime <saat>`", parse_mode="Markdown")
        return
    
    try:
        h = int(context.args[0])
        if h < 0 or h > 168:
            await update.message.reply_text("0-168 arası")
            return
        if uid not in user_settings:
            user_settings[uid] = {}
        user_settings[uid]["delete_hours"] = h
        await update.message.reply_text(f"✅ {'Kalıcı' if h==0 else f'{h} saat'}")
    except:
        await update.message.reply_text("Sayı girin")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    active = len([t for t in active_downloads.values() if t.status in ("downloading", "uploading")])
    await update.message.reply_text(
        f"📊 **Durum**\n\n"
        f"📤 Yükleme: {stats['total_uploads']}\n"
        f"📦 Toplam: {format_size(stats['total_bytes'])}\n"
        f"🔄 Aktif: {active}",
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
    
    text = "📥 **Aktif İndirmeler:**\n\n"
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
    
    if q.data.startswith("c_"):
        tid = q.data[2:]
        if tid in active_downloads:
            t = active_downloads[tid]
            t.cancelled = True
            if t.filepath and os.path.exists(t.filepath):
                try: os.remove(t.filepath)
                except: pass
            await q.edit_message_text(f"❌ İptal: `{t.filename or 'İndirme'}`", parse_mode="Markdown")
        else:
            await q.edit_message_text("⚠️ Bulunamadı")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stats, active_downloads

    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return

    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("⛔ Geçersiz link")
        return

    uid = update.effective_user.id
    delete_hours = get_delete_hours(uid)
    task_id = str(uuid.uuid4())[:8]
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ İptal", callback_data=f"c_{task_id}")]])
    msg = await update.message.reply_text("📥 **Başlatılıyor...**", parse_mode="Markdown", reply_markup=kb)
    
    task = DownloadTask(task_id, url, uid)
    active_downloads[task_id] = task
    
    # Ayrı task olarak çalıştır (lock yok!)
    asyncio.create_task(process_download(task, msg, kb, delete_hours))

async def process_download(task, msg, kb, delete_hours):
    """Ana indirme/yükleme işlemi - ayrı task olarak çalışır"""
    global stats, active_downloads, sched
    
    dest = os.path.join(os.getcwd(), "downloads")
    
    try:
        # ══════════════════════════════════════════
        # İNDİRME
        # ══════════════════════════════════════════
        task.status = "downloading"
        
        # Progress güncelleyici
        async def update_progress():
            last = 0
            while task.status == "downloading" and not task.cancelled:
                await asyncio.sleep(1.5)
                if task.progress != last:
                    try:
                        n = task.filename or "İndiriliyor..."
                        text = (
                            f"📥 **İndiriliyor**\n\n"
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
        
        # Progress task başlat
        progress_task = asyncio.create_task(update_progress())
        
        # İndirmeyi yap
        success = await asyncio.to_thread(download_file, task, dest)
        
        # Progress task'ı durdur
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass
        
        if task.cancelled:
            raise asyncio.CancelledError()
        
        if not success:
            raise Exception("İndirme başarısız")
        
        sz = os.path.getsize(task.filepath)
        
        # Boyut kontrolü
        if sz > 5 * 1024**3:
            await msg.edit_text(f"⚠️ Çok büyük: {format_size(sz)} > 5GB", parse_mode="Markdown")
            os.remove(task.filepath)
            del active_downloads[task.task_id]
            return
        
        # ══════════════════════════════════════════
        # YÜKLEME
        # ══════════════════════════════════════════
        task.status = "uploading"
        
        await msg.edit_text(
            f"📤 **Yükleniyor...**\n\n"
            f"📁 `{task.filename}`\n"
            f"📦 {format_size(sz)}",
            parse_mode="Markdown",
            reply_markup=kb
        )
        
        if task.cancelled:
            raise asyncio.CancelledError()
        
        gf = await asyncio.to_thread(upload_file, task.filepath, task.filename)
        
        if task.cancelled:
            raise asyncio.CancelledError()
        
        fid = gf["id"]
        link = gf.get("alternateLink", f"https://drive.google.com/file/d/{fid}/view")
        
        # Stats
        stats["total_uploads"] += 1
        stats["total_bytes"] += sz
        
        elapsed = time.time() - task.start_time
        
        # ══════════════════════════════════════════
        # TAMAMLANDI
        # ══════════════════════════════════════════
        task.status = "completed"
        
        result = (
            f"✅ **Tamamlandı!**\n\n"
            f"📁 `{task.filename}`\n"
            f"📦 {format_size(sz)} • ⏱️ {format_time(elapsed)}\n\n"
            f"🔗 [Drive Link]({link})\n\n"
        )
        
        if delete_hours > 0:
            result += f"🗑️ {delete_hours}h sonra silinecek"
            sched.add_job(auto_delete, "date", run_date=datetime.now() + timedelta(hours=delete_hours), args=[fid, task.filename])
        else:
            result += "♾️ Kalıcı"
        
        await msg.edit_text(result, parse_mode="Markdown", disable_web_page_preview=True)
        
        # Temizle
        if os.path.exists(task.filepath):
            os.remove(task.filepath)
        
        del active_downloads[task.task_id]
    
    except asyncio.CancelledError:
        task.status = "cancelled"
        await msg.edit_text("❌ **İptal edildi**", parse_mode="Markdown")
        if task.filepath and os.path.exists(task.filepath):
            try: os.remove(task.filepath)
            except: pass
        if task.task_id in active_downloads:
            del active_downloads[task.task_id]
    
    except Exception as e:
        task.status = "failed"
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ **Hata:** `{str(e)[:100]}`", parse_mode="Markdown")
        if task.filepath and os.path.exists(task.filepath):
            try: os.remove(task.filepath)
            except: pass
        if task.task_id in active_downloads:
            del active_downloads[task.task_id]

# ═══════════════════════════════════════════════
# BOT RUNNER
# ═══════════════════════════════════════════════
async def run_bot():
    global sched

    if not BOT_TOKEN:
        logger.error("❌ NO BOT_TOKEN")
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
        BotCommand("start", "Başlat"),
        BotCommand("downloads", "Aktif indirmeler"),
        BotCommand("settime", "Silme süresi"),
        BotCommand("status", "Durum"),
        BotCommand("help", "Yardım"),
    ]
    
    await app.initialize()
    await app.bot.set_my_commands(commands)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Bot is running!")
    
    while True:
        await asyncio.sleep(3600)

def start_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())

if __name__ == "__main__":
    bot_thread = threading.Thread(target=start_bot_thread, daemon=True)
    bot_thread.start()
    logger.info("✅ Bot thread started")
    
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)cancel_"):
        task_id = data.replace("cancel_", "")
        
        if task_id in active_downloads:
            task = active_downloads[task_id]
            task.cancel()
            
            # Dosyayı temizle
            if task.filepath and os.path.exists(task.filepath):
                try:
                    os.remove(task.filepath)
                except:
                    pass
            
            await query.edit_message_text(f"❌ İptal edildi: `{task.filename or 'İndirme'}`", parse_mode="Markdown")
        else:
            await query.edit_message_text("⚠️ İndirme bulunamadı (zaten bitti olabilir)")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lock, sched, stats, active_downloads

    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return

    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("⛔ Geçersiz link")
        return

    user_id = update.effective_user.id
    delete_hours = get_delete_hours(user_id)
    
    # Yeni task oluştur
    task_id = str(uuid.uuid4())[:8]
    
    # İptal butonu
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ İptal", callback_data=f"cancel_{task_id}")]
    ])
    
    msg = await update.message.reply_text(
        "🔄 **Başlatılıyor...**",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    
    task = DownloadTask(task_id, url, user_id, msg)
    active_downloads[task_id] = task

    async with lock:
        fp = None
        try:
            dest = os.path.join(os.getcwd(), "downloads")
            os.makedirs(dest, exist_ok=True)
            
            # ═══════════════════════════════════════
            # İNDİRME
            # ═══════════════════════════════════════
            task.status = "downloading"
            
            await msg.edit_text(
                "📥 **İndirme başlıyor...**\n\n⏳ Bağlanıyor...",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            
            # SmartDL başlat - BLOCKING=TRUE kullan, thread içinde çalıştır
            def do_download():
                obj = SmartDL(url, dest, progress_bar=False, timeout=300)
                task.smartdl = obj
                obj.start(blocking=False)
                return obj
            
            obj = await asyncio.to_thread(do_download)
            
            last_update = 0
            update_interval = 1.5
            
            while not obj.isFinished():
                if task.cancelled:
                    raise asyncio.CancelledError("Kullanıcı iptal etti")
                
                await asyncio.sleep(0.3)
                
                now = time.time()
                if now - last_update >= update_interval:
                    try:
                        task.progress = (obj.get_progress() or 0) * 100
                        task.speed = obj.get_speed(human=False) or 0
                        task.eta = obj.get_eta(human=False) or 0
                        task.downloaded = obj.get_dl_size() or 0
                        task.total_size = obj.get_final_filesize() or 0
                        
                        # Dosya adını almaya çalış
                        if not task.filename:
                            try:
                                dest_path = obj.get_dest()
                                if dest_path:
                                    task.filename = os.path.basename(dest_path)
                            except:
                                pass
                        
                        bar = create_progress_bar(task.progress)
                        
                        text = f"📥 **İndiriliyor...**\n\n"
                        
                        if task.filename:
                            text += f"📁 `{task.filename}`\n\n"
                        
                        text += f"{bar} **{task.progress:.1f}%**\n\n"
                        text += f"📦 {format_size(task.downloaded)}"
                        
                        if task.total_size > 0:
                            text += f" / {format_size(task.total_size)}"
                        
                        text += f"\n⚡ {format_speed(task.speed)}"
                        
                        if task.eta > 0 and task.eta < 86400:
                            text += f"\n⏱️ Kalan: {format_time(task.eta)}"
                        
                        await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
                        last_update = now
                    except Exception as e:
                        logger.debug(f"Update error: {e}")
            
            if task.cancelled:
                raise asyncio.CancelledError("Kullanıcı iptal etti")
            
            if not obj.isSuccessful():
                raise Exception("İndirme başarısız")
            
            fp = obj.get_dest()
            task.filepath = fp
            task.filename = os.path.basename(fp)
            sz = os.path.getsize(fp)
            task.total_size = sz
            
            # Boyut kontrolü
            if sz > 5 * 1024**3:
                await msg.edit_text(
                    f"⚠️ **Çok büyük!**\n\n{format_size(sz)} > 5GB limit",
                    parse_mode="Markdown"
                )
                os.remove(fp)
                fp = None
                del active_downloads[task_id]
                return
            
            # ═══════════════════════════════════════
            # YÜKLEME
            # ═══════════════════════════════════════
            task.status = "uploading"
            task.progress = 0
            
            await msg.edit_text(
                f"📤 **Drive'a yükleniyor...**\n\n"
                f"📁 `{task.filename}`\n"
                f"📦 {format_size(sz)}\n\n"
                f"⏳ Lütfen bekleyin...",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            
            if task.cancelled:
                raise asyncio.CancelledError("Kullanıcı iptal etti")
            
            # Yükleme
            def do_upload():
                global drive_client
                if not drive_client:
                    get_drive()
                if not drive_client:
                    raise RuntimeError("Drive bağlantısı yok")
                
                meta = {"title": task.filename}
                if GDRIVE_FOLDER_ID:
                    meta["parents"] = [{"id": GDRIVE_FOLDER_ID}]
                
                gf = drive_client.CreateFile(meta)
                gf.SetContentFile(fp)
                gf.Upload()
                return gf
            
            gf = await asyncio.to_thread(do_upload)
            
            if task.cancelled:
                raise asyncio.CancelledError("Kullanıcı iptal etti")
            
            fid = gf["id"]
            link = gf.get("alternateLink", f"https://drive.google.com/file/d/{fid}/view")
            
            # İstatistikleri güncelle
            stats["total_uploads"] += 1
            stats["total_bytes"] += sz
            
            elapsed = time.time() - task.start_time
            
            # ═══════════════════════════════════════
            # TAMAMLANDI
            # ═══════════════════════════════════════
            task.status = "completed"
            
            result = (
                f"✅ **Tamamlandı!**\n\n"
                f"📁 `{task.filename}`\n"
                f"📦 {format_size(sz)}\n"
                f"⏱️ {format_time(elapsed)}\n\n"
                f"🔗 [Google Drive]({link})\n\n"
            )
            
            if delete_hours > 0:
                delete_time = datetime.now() + timedelta(hours=delete_hours)
                result += f"🗑️ {delete_hours}h sonra silinecek"
                
                sched.add_job(
                    auto_delete, "date",
                    run_date=delete_time,
                    args=[fid, task.filename, delete_hours]
                )
            else:
                result += "♾️ Kalıcı"
            
            await msg.edit_text(result, parse_mode="Markdown", disable_web_page_preview=True)
            
            # Temizle
            if fp and os.path.exists(fp):
                os.remove(fp)
                fp = None
            
            del active_downloads[task_id]
        
        except asyncio.CancelledError:
            task.status = "cancelled"
            await msg.edit_text("❌ **İptal edildi**", parse_mode="Markdown")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except:
                    pass
            if task_id in active_downloads:
                del active_downloads[task_id]
        
        except Exception as e:
            task.status = "failed"
            logger.error(f"Error: {e}", exc_info=True)
            await msg.edit_text(f"❌ **Hata!**\n\n`{str(e)[:100]}`", parse_mode="Markdown")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except:
                    pass
            if task_id in active_downloads:
                del active_downloads[task_id]

# ═══════════════════════════════════════════════
# BOT RUNNER
# ═══════════════════════════════════════════════
async def run_bot():
    global lock, sched

    if not BOT_TOKEN:
        logger.error("❌ NO BOT_TOKEN")
        return

    logger.info("Initializing bot...")
    
    lock = asyncio.Lock()
    sched = AsyncIOScheduler()
    sched.start()
    
    get_drive()

    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHareply_text("✅ Dosyalar artık **silinmeyecek** (kalıcı).", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"✅ Silme süresi **{hours} saat** olarak ayarlandı.", parse_mode="Markdown")
    
    except ValueError:
        await update.message.reply_text("⚠️ Geçerli bir sayı girin. Örnek: `/settime 6`", parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    uptime = datetime.now() - stats["start_time"]
    delete_hours = get_delete_hours(update.effective_user.id)
    
    # Planlanan silme işlerini say
    scheduled_jobs = len(sched.get_jobs()) if sched else 0
    
    status_text = f"""
📊 **Bot Durumu**

**🤖 Sistem:**
• Durum: ✅ Çalışıyor
• Uptime: {humanize.naturaldelta(uptime)}
• Google Drive: {"✅ Bağlı" if drive_client else "❌ Bağlı değil"}

**📈 İstatistikler:**
• Toplam yükleme: {stats['total_uploads']}
• Toplam veri: {format_size(stats['total_bytes'])}
• Bekleyen silme: {scheduled_jobs} dosya

**⚙️ Ayarlarınız:**
• Silme süresi: {delete_hours} saat {"(kalıcı)" if delete_hours == 0 else ""}
"""
    
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lock, sched, stats

    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return

    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("⛔ Geçersiz link. HTTP veya HTTPS ile başlamalı.")
        return

    user_id = update.effective_user.id
    delete_hours = get_delete_hours(user_id)

    async with lock:
        start_time = time.time()
        msg = await update.message.reply_text("🔄 **İşlem başlatılıyor...**", parse_mode="Markdown")
        fp = None
        
        try:
            dest = os.path.join(os.getcwd(), "downloads")
            
            # İndirme
            obj = await download_with_progress(url, dest, msg)

            if not obj.isSuccessful():
                await msg.edit_text("❌ **İndirme başarısız!**\n\nLinkin geçerliliğini kontrol edin.", parse_mode="Markdown")
                return

            fp = obj.get_dest()
            fn = os.path.basename(fp)
            sz = os.path.getsize(fp)

            # Boyut kontrolü
            if sz > 5 * 1024**3:
                await msg.edit_text(
                    f"⚠️ **Dosya çok büyük!**\n\n"
                    f"📦 Boyut: {format_size(sz)}\n"
                    f"📏 Limit: 5 GB",
                    parse_mode="Markdown"
                )
                os.remove(fp)
                fp = None
                return

            # Yükleme
            gf, file_size = await upload_with_progress(fp, fn, msg)
            
            fid = gf["id"]
            link = gf.get("alternateLink", f"https://drive.google.com/file/d/{fid}/view")
            
            # İstatistikleri güncelle
            stats["total_uploads"] += 1
            stats["total_bytes"] += file_size

            # Süre hesapla
            elapsed = time.time() - start_time
            
            # Sonuç mesajı
            result_text = (
                f"✅ **Yükleme Tamamlandı!**\n\n"
                f"📁 **Dosya:** `{fn}`\n"
                f"📦 **Boyut:** {format_size(sz)}\n"
                f"⏱️ **Süre:** {format_time(elapsed)}\n"
                f"🔗 **Link:** [Google Drive]({link})\n\n"
            )
            
            if delete_hours > 0:
                delete_time = datetime.now() + timedelta(hours=delete_hours)
                result_text += f"🗑️ **Otomatik silme:** {delete_hours} saat sonra\n"
                result_text += f"📅 **Silinecek:** {delete_time.strftime('%H:%M %d/%m/%Y')}"
                
                # Silme işlemini planla
                sched.add_job(
                    auto_delete, "date",
                    run_date=delete_time,
                    args=[fid, fn, delete_hours]
                )
            else:
                result_text += "♾️ **Kalıcı** - Otomatik silinmeyecek"

            await msg.edit_text(result_text, parse_mode="Markdown", disable_web_page_preview=True)

            # Lokal dosyayı temizle
            if fp and os.path.exists(fp):
                os.remove(fp)
                fp = None

        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            try:
                await msg.edit_text(
                    f"❌ **Hata oluştu!**\n\n"
                    f"```\n{str(e)[:200]}\n```",
                    parse_mode="Markdown"
                )
            except:
                pass
        finally:
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except:
                    pass

# ═══════════════════════════════════════════════
# BOT RUNNER
# ═══════════════════════════════════════════════
async def run_bot():
    global lock, sched

    if not BOT_TOKEN:
        logger.error("❌ NO BOT_TOKEN")
        return

    logger.info("Initializing bot...")
    
    lock = asyncio.Lock()
    sched = AsyncIOScheduler()
    sched.start()
    
    get_drive()

    app = Application.builder().token(BOT_TOKEN).build()
    
    # Komutları ekle
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Bot komutlarını ayarla
    commands = [
        BotCommand("start", "Botu başlat"),
        BotCommand("help", "Yardım menüsü"),
        BotCommand("settime", "Silme süresini ayarla"),
        BotCommand("status", "Bot durumu"),
    ]
    
    logger.info("Starting polling...")
    await app.initialize()
    await app.bot.set_my_commands(commands)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Bot is running!")
    
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
    logger.info("✅ Bot thread started")
    
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
