import os
import json
import logging
import tempfile
import asyncio
from datetime import datetime, timedelta
from pySmartDL import SmartDL
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GDRIVE_SERVICE_ACCOUNT_JSON = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID")

if AUTHORIZED_USER_ID:
    AUTHORIZED_USER_ID = int(AUTHORIZED_USER_ID)

# Global Drive Client variable and processing lock
drive_client = None
processing_lock = asyncio.Lock()

# Google Drive Authentication
def get_gdrive_client():
    global drive_client
    if not GDRIVE_SERVICE_ACCOUNT_JSON:
        logger.error("GDRIVE_SERVICE_ACCOUNT_JSON not found in environment variables.")
        return None

    try:
        # Save JSON to a temporary file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_json:
            temp_json.write(GDRIVE_SERVICE_ACCOUNT_JSON)
            temp_json_path = temp_json.name

        scope = ['https://www.googleapis.com/auth/drive']
        gauth = GoogleAuth()
        gauth.auth_method = 'service'
        gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(temp_json_path, scope)

        drive_client = GoogleDrive(gauth)

        # Clean up the temporary file after initializing
        os.unlink(temp_json_path)
        return drive_client
    except Exception as e:
        logger.error(f"Error authenticating with Google Drive: {e}")
        return None

# Scheduler for automatic deletion
scheduler = BackgroundScheduler()
scheduler.start()

def delete_from_drive(file_id):
    try:
        global drive_client
        if not drive_client:
            drive_client = get_gdrive_client()

        if drive_client:
            file = drive_client.CreateFile({'id': file_id})
            file.Delete()
            logger.info(f"File {file_id} deleted successfully after 2 hours.")
    except Exception as e:
        logger.error(f"Error deleting file {file_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AUTHORIZED_USER_ID or update.effective_user.id != AUTHORIZED_USER_ID:
        return
    await update.message.reply_text("Merhaba! Bana bir direkt indirme linki gönder, senin için Google Drive'a yükleyeyim.")

def download_file(url, dest):
    obj = SmartDL(url, dest, progress_bar=False, timeout=30)
    obj.start()
    return obj

def upload_file(file_path, file_name):
    global drive_client
    if not drive_client:
        drive_client = get_gdrive_client()

    metadata = {'title': file_name}
    if GDRIVE_FOLDER_ID:
        metadata['parents'] = [{'id': GDRIVE_FOLDER_ID}]

    gfile = drive_client.CreateFile(metadata)
    gfile.SetContentFile(file_path)
    gfile.Upload()
    return gfile

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AUTHORIZED_USER_ID or update.effective_user.id != AUTHORIZED_USER_ID:
        logger.warning(f"Unauthorized access attempt by user {update.effective_user.id}")
        return

    url = update.message.text
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("Geçersiz link. Lütfen geçerli bir HTTP veya HTTPS linki gönder.")
        return

    async with processing_lock:
        status_message = await update.message.reply_text("⏳ İşlem sıraya alındı ve başlatılıyor...")

        try:
            # Download
            dest = tempfile.gettempdir()
            await status_message.edit_text("📥 Dosya sunucuya indiriliyor...")

            obj = await asyncio.to_thread(download_file, url, dest)

            if not obj.is_successful():
                await status_message.edit_text("❌ İndirme başarısız oldu. Linkin doğruluğunu kontrol edin.")
                return

            file_path = obj.get_dest()
            file_name = os.path.basename(file_path)

            # Size check (Approx 5GB)
            file_size = os.path.getsize(file_path)
            if file_size > 5 * 1024 * 1024 * 1024:
                await status_message.edit_text(f"⚠️ Dosya çok büyük ({file_size / (1024**3):.2f} GB). Maksimum limit 5GB.")
                os.remove(file_path)
                return

            await status_message.edit_text(f"✅ İndirme tamamlandı: `{file_name}`\n📤 Google Drive'a yükleniyor...", parse_mode='Markdown')

            # Upload to Google Drive
            gfile = await asyncio.to_thread(upload_file, file_path, file_name)

            file_id = gfile['id']
            drive_link = gfile['alternateLink']

            await status_message.edit_text(
                f"🚀 Yükleme başarılı!\n\n"
                f"📁 Dosya: `{file_name}`\n"
                f"🔗 Link: {drive_link}\n\n"
                f"⏱ Bu dosya 2 saat sonra otomatik olarak silinecektir.",
                parse_mode='Markdown'
            )

            # Clean up local file
            if os.path.exists(file_path):
                os.remove(file_path)

            # Schedule deletion
            run_date = datetime.now() + timedelta(hours=2)
            scheduler.add_job(delete_from_drive, 'date', run_date=run_date, args=[file_id])

        except Exception as e:
            logger.error(f"Error during processing: {e}")
            await update.message.reply_text(f"❌ Bir hata oluştu: {str(e)}")

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found!")
        return

    # Initialize Drive Client
    get_gdrive_client()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("Bot started...")
    application.run_polling()

if __name__ == '__main__':
    main()
