import collections
import collections.abc
if not hasattr(collections, "Iterable"):
    collections.Iterable = collections.abc.Iterable

import os
import logging
import tempfile
import asyncio
import threading
from datetime import datetime, timedelta
from flask import Flask

from pySmartDL import SmartDL
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
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

if AUTHORIZED_USER_ID:
    try:
        AUTHORIZED_USER_ID = int(AUTHORIZED_USER_ID)
    except:
        AUTHORIZED_USER_ID = None

logger.info(f"BOT_TOKEN: {bool(BOT_TOKEN)}")
logger.info(f"GDRIVE_JSON: {bool(GDRIVE_SERVICE_ACCOUNT_JSON)}")
logger.info(f"FOLDER_ID: {bool(GDRIVE_FOLDER_ID)}")
logger.info(f"AUTH_USER: {AUTHORIZED_USER_ID}")

# ═══════════════════════════════════════════════
# FLASK - HEALTH CHECK
# ═══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "Bot is running!", 200

@flask_app.route("/health")
def health2():
    return "OK", 200

# ═══════════════════════════════════════════════
# GOOGLE DRIVE
# ═══════════════════════════════════════════════
drive_client = None

def get_drive():
    global drive_client
    if not GDRIVE_SERVICE_ACCOUNT_JSON:
        logger.warning("No GDRIVE_SERVICE_ACCOUNT_JSON")
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
def do_download(url, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    obj = SmartDL(url, dest_dir, progress_bar=False, timeout=120)
    obj.start()
    return obj

def do_upload(filepath, filename):
    global drive_client
    if not drive_client:
        get_drive()
    if not drive_client:
        raise RuntimeError("No Drive client")
    meta = {"title": filename}
    if GDRIVE_FOLDER_ID:
        meta["parents"] = [{"id": GDRIVE_FOLDER_ID}]
    gf = drive_client.CreateFile(meta)
    gf.SetContentFile(filepath)
    gf.Upload()
    return gf

async def auto_delete(file_id):
    try:
        global drive_client
        if drive_client:
            gf = await asyncio.to_thread(drive_client.CreateFile, {"id": file_id})
            await asyncio.to_thread(gf.Delete)
            logger.info(f"🗑 Deleted {file_id}")
    except Exception as e:
        logger.error(f"Delete error: {e}")

# ═══════════════════════════════════════════════
# BOT HANDLERS
# ═══════════════════════════════════════════════
lock = None
sched = None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return
    await update.message.reply_text(
        "Merhaba! Bana bir direkt indirme linki gönder, "
        "senin için Google Drive'a yükleyeyim. 🚀"
    )

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lock, sched

    if AUTHORIZED_USER_ID and update.effective_user.id != AUTHORIZED_USER_ID:
        return

    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("⛔ Geçersiz link.")
        return

    async with lock:
        msg = await update.message.reply_text("⏳ Başlıyor...")
        fp = None
        try:
            dest = os.path.join(os.getcwd(), "downloads")
            await msg.edit_text("📥 İndiriliyor...")
            obj = await asyncio.to_thread(do_download, url, dest)

            if not obj.isSuccessful():
                await msg.edit_text("❌ İndirme başarısız.")
                return

            fp = obj.get_dest()
            fn = os.path.basename(fp)
            sz = os.path.getsize(fp)

            if sz > 5 * 1024**3:
                await msg.edit_text(f"⚠️ Çok büyük: {sz/1024**3:.1f}GB. Max 5GB.")
                os.remove(fp)
                fp = None
                return

            size_str = f"{sz/1024**2:.1f} MB" if sz > 1024**2 else f"{sz/1024:.1f} KB"
            await msg.edit_text(f"✅ İndirildi: `{fn}` ({size_str})\n📤 Yükleniyor...", parse_mode="Markdown")

            gf = await asyncio.to_thread(do_upload, fp, fn)
            fid = gf["id"]
            link = gf.get("alternateLink", f"https://drive.google.com/file/d/{fid}/view")

            await msg.edit_text(
                f"🚀 Tamam!\n\n"
                f"📁 `{fn}`\n"
                f"📦 {size_str}\n"
                f"🔗 {link}\n\n"
                f"⏱ 2 saat sonra silinecek.",
                parse_mode="Markdown",
            )

            if fp and os.path.exists(fp):
                os.remove(fp)
                fp = None

            sched.add_job(
                auto_delete, "date",
                run_date=datetime.now() + timedelta(hours=2),
                args=[fid]
            )

        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            try:
                await msg.edit_text(f"❌ Hata: `{e}`", parse_mode="Markdown")
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("Starting polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Bot is running!")
    
    # Sonsuza kadar çalış
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Bot stopping...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

def start_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    # Bot'u ayrı thread'de başlat
    bot_thread = threading.Thread(target=start_bot_thread, daemon=True)
    bot_thread.start()
    logger.info("✅ Bot thread started")
    
    # Flask
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
