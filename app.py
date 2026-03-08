import os
import json
import tempfile
from flask import Flask
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

flask_app = Flask(__name__)

GDRIVE_SERVICE_ACCOUNT_JSON = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")

@flask_app.route("/")
def test():
    r = []
    
    try:
        r.append(f"FOLDER_ID: {GDRIVE_FOLDER_ID}")
        r.append(f"JSON len: {len(GDRIVE_SERVICE_ACCOUNT_JSON) if GDRIVE_SERVICE_ACCOUNT_JSON else 0}")
        
        sa = json.loads(GDRIVE_SERVICE_ACCOUNT_JSON)
        r.append(f"Email: {sa.get('client_email')}")
        r.append(f"Project: {sa.get('project_id')}")
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            f.write(GDRIVE_SERVICE_ACCOUNT_JSON)
            path = f.name
        
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/drive"]
        )
        os.unlink(path)
        r.append("✅ Credentials OK")
        
        service = build("drive", "v3", credentials=creds)
        r.append("✅ Service OK")
        
        folder = service.files().get(fileId=GDRIVE_FOLDER_ID, fields="id,name").execute()
        r.append(f"✅ Klasor: {folder.get('name')}")
        
        media = MediaInMemoryUpload(b"test 123", mimetype="text/plain")
        file_meta = {"name": "test.txt", "parents": [GDRIVE_FOLDER_ID]}
        
        uploaded = service.files().create(
            body=file_meta,
            media_body=media,
            fields="id,webViewLink"
        ).execute()
        
        r.append(f"✅ YUKLEME BASARILI: {uploaded.get('webViewLink')}")
        
        service.files().delete(fileId=uploaded["id"]).execute()
        r.append("✅ Test dosyasi silindi")
        
    except Exception as e:
        r.append(f"❌ HATA: {type(e).__name__}: {e}")
    
    return "<pre>" + "\n".join(r) + "</pre>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
