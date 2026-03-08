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
import humanize
from datetime import datetime, timedelta
from flask import Flask

from pySmartDL import SmartDL
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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

# Kullanıcı ayarları (in-memory)
user_settings = {}

# İstatistikler
stats = {
    "total_uploads": 0,
    "total_bytes": 0,
    "start_time": datetime.now()
}

logger.info(f"BOT_TOKEN: {bool(BOT_TOKEN)}")
logger.info(f"GDRIVE_JSON: {bool(GDRIVE_SERVICE_ACCOUNT_JSON)}")
logger.info(f"FOLDER_ID: {bool(GDRIVE_FOLDER_ID)}")
logger.info(f"AUTH_USER: {AUTHORIZED_USER_ID}")
logger.info(f"DEFAULT_DELETE_HOURS: {DEFAULT_DELETE_HOURS}")

# ═══════════════════════════════════════════════
# FLASK - HEALTH CHECK
# ═══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    uptime = datetime.now() - stats["start_time"]
    return f"""
    <h1>🤖 Upload Bot Status</h1>
    <p>✅ Bot is running!</p>
    <p>📊 Total uploads: {stats['total_uploads']}</p>
    <p>📦 Total uploaded: {humanize.naturalsize(stats['total_bytes'])}</p>
    <p>⏱️ Uptime: {humanize.naturaldelta(uptime)}</p>
    """, 200

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
def format_size(bytes):
    return humanize.naturalsize(bytes, binary=True)

def format_speed(bytes_per_sec):
    return f"{humanize.naturalsize(bytes_per_sec, binary=True)}/s"

def format_time(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

def create_progress_bar(percent, length=20):
    filled = int(length * percent / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.1f}%"

async def download_with_progress(url, dest_dir, status_msg, msg_prefix="📥"):
    """İndirme işlemi - canlı ilerleme güncellemesi ile"""
    os.makedirs(dest_dir, exist_ok=True)
    
    obj = SmartDL(url, dest_dir, progress_bar=False, timeout=300)
    obj.start(blocking=False)
    
    last_update = 0
    update_interval = 2  # Her 2 saniyede bir güncelle
    
    while not obj.isFinished():
        await asyncio.sleep(0.5)
        
        current_time = time.time()
        if current_time - last_update >= update_interval:
            try:
                progress = obj.get_progress() * 100
                speed = obj.get_speed(human=False) or 0
                eta = obj.get_eta(human=False) or 0
                downloaded = obj.get_dl_size()
                total = obj.get_final_filesize() or 0
                
                progress_bar = create_progress_bar(progress)
                
                status_text = (
                    f"{msg_prefix} **İndiriliyor...**\n\n"
                    f"{progress_bar}\n\n"
                    f"📦 Boyut: {format_size(downloaded)}"
                )
                
                if total > 0:
                    status_text += f" / {format_size(total)}"
                
                status_text += f"\n⚡ Hız: {format_speed(speed)}"
                
                if eta > 0:
                    status_text += f"\n⏱️ Kalan: {format_time(eta)}"
                
                await status_msg.edit_text(status_text, parse_mode="Markdown")
                last_update = current_time
            except Exception as e:
                logger.debug(f"Progress update error: {e}")
    
    return obj

async def upload_with_progress(filepath, filename, status_msg, msg_prefix="📤"):
    """Yükleme işlemi - ilerleme güncellemesi ile"""
    global drive_client
    
    if not drive_client:
        get_drive()
    if not drive_client:
        raise RuntimeError("No Drive client")
    
    file_size = os.path.getsize(filepath)
    
    await status_msg.edit_text(
        f"{msg_prefix} **Google Drive'a yükleniyor...**\n\n"
        f"📁 Dosya: `{filename}`\n"
        f"📦 Boyut: {format_size(file_size)}\n\n"
        f"⏳ Lütfen bekleyin...",
        parse_mode="Markdown"
    )
    
    meta = {"title": filename}
    if GDRIVE_FOLDER_ID:
        meta["parents"] = [{"id": GDRIVE_FOLDER_ID}]
    
    gf = await asyncio.to_thread(_upload_file_sync, filepath, meta)
    
    return gf, file_size

def _upload_file_sync(filepath, meta):
    """Senkron yükleme işlemi"""
    gf = drive_client.CreateFile(meta)
    gf.SetContentFile(filepath)
    gf.Upload()
    return gf

async def auto_delete(file_id, filename, hours):
    """Otomatik silme işlemi"""
    try:
        global drive_client
        if drive_client:
            gf = await asyncio.to_thread(drive_client.CreateFile, {"id": file_id})
            await asyncio.to_thread(gf.Delete)
            logger.info(f"🗑️ Auto-deleted: {filename} ({file_id}) after {hours}h")
    except Exception as e:
        logger.error(f"Delete error: {e}")

# ═══════════════════════════════════════════════
# BOT HANDLERS
# ═══════════════════════════════════════════════
lock = None
sched = None

def get_delete_hours(user_id):
    """Kullanıcının silme süresini al"""
    return user_settings.get(user_id, {}).get("delete_hours", DEFAULT_DELETE_HOURS)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    delete_hours = get_delete_hours(update.effective_user.id)
    
    welcome_text = """
🚀 **Upload Bot'a Hoş Geldin!**

Bana bir direkt indirme linki gönder, senin için:
1️⃣ Dosyayı sunucuya indirir
2️⃣ Google Drive'a yükler
3️⃣ Sana link verir
4️⃣ Belirlenen süre sonra otomatik siler

**📋 Komutlar:**
/help - Yardım menüsü
/settime <saat> - Silme süresini ayarla
/status - Bot durumu ve istatistikler

**⚙️ Mevcut Ayarlar:**
⏱️ Otomatik silme: {hours} saat sonra

**📝 Kullanım:**
Sadece bir link gönder! Örnek:
`https://example.com/file.zip`
""".format(hours=delete_hours)
    
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    help_text = """
📚 **Yardım Menüsü**

**🔗 Link Gönderme:**
Direkt indirme linki gönder. Bot otomatik olarak:
• Dosyayı indirir (ilerleme gösterir)
• Google Drive'a yükler
• Paylaşılabilir link verir

**⏱️ Silme Süresini Ayarlama:**
`/settime 1` → 1 saat sonra sil
`/settime 6` → 6 saat sonra sil
`/settime 24` → 24 saat sonra sil
`/settime 0` → Silme (kalıcı)

**📊 Desteklenen Limitler:**
• Maksimum dosya: 5GB
• Timeout: 5 dakika

**💡 İpuçları:**
• Direkt indirme linki kullan
• Kısa linkler (bit.ly vb.) çalışmayabilir
• Google Drive linki değil, dosya linki gönder
"""
    
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    user_id = update.effective_user.id
    
    if not context.args:
        current = get_delete_hours(user_id)
        await update.message.reply_text(
            f"⏱️ Mevcut silme süresi: **{current} saat**\n\n"
            f"Değiştirmek için: `/settime <saat>`\n"
            f"Örnek: `/settime 6` → 6 saat sonra sil\n"
            f"Kalıcı: `/settime 0` → Hiç silme",
            parse_mode="Markdown"
        )
        return
    
    try:
        hours = int(context.args[0])
        if hours < 0 or hours > 168:  # Max 1 hafta
            await update.message.reply_text("⚠️ Süre 0-168 saat arası olmalı.")
            return
        
        if user_id not in user_settings:
            user_settings[user_id] = {}
        user_settings[user_id]["delete_hours"] = hours
        
        if hours == 0:
            await update.message.reply_text("✅ Dosyalar artık **silinmeyecek** (kalıcı).", parse_mode="Markdown")
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
