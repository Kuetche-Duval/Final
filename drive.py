# drive.py (corrected)
import os
import logging
import io

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
    GOOGLEAPI_AVAILABLE = True
except Exception:
    GOOGLEAPI_AVAILABLE = False

SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]

def _make_drive_service():
    if not GOOGLEAPI_AVAILABLE:
        raise RuntimeError("google-api-python-client or google-auth not installed")
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise RuntimeError("service account json not found at " + SERVICE_ACCOUNT_PATH)
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return service

def download_file(file_id):
    """Return bytes or b'' on error."""
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("Google API not available - download_file fallback")
        return b""
    try:
        svc = _make_drive_service()
        request = svc.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read()
    except Exception:
        logger.exception("download_file error for %s", file_id)
        return b""

def update_file(file_id, content_bytes, mime_type="application/json"):
    """
    Update the content of an existing Drive file (by file_id).
    Returns True on success, False on failure.
    """
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("Google API not available - update_file fallback")
        return False
    try:
        svc = _make_drive_service()
        fh = io.BytesIO(content_bytes)
        media = MediaIoBaseUpload(fh, mimetype=mime_type, resumable=True)
        svc.files().update(fileId=file_id, media_body=media).execute()
        return True
    except Exception:
        logger.exception("update_file failed for %s", file_id)
        return False

def create_file_in_folder(folder_id, name, content_bytes, mime_type="application/json"):
    """
    Create a new file in a folder and return the new file id, or None on failure.
    """
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("Google API not available - create_file_in_folder fallback")
        return None
    try:
        svc = _make_drive_service()
        fh = io.BytesIO(content_bytes)
        media = MediaIoBaseUpload(fh, mimetype=mime_type, resumable=True)
        body = {"name": name, "parents": [folder_id], "mimeType": mime_type}
        file = svc.files().create(body=body, media_body=media, fields="id").execute()
        return file.get("id")
    except Exception:
        logger.exception("create_file_in_folder failed for folder %s", folder_id)
        return None

# existing listing helpers
def from_google_drive(base_folder_id, mimeType=None):
    """
    Return a list of file metadata dicts inside a Drive folder.
    Each item will contain at least:
      { "id": ..., "name": ..., "description": ..., "mimeType": ..., "webViewLink": ... }
    If google-api client not available or folder id missing, returns [].
    """
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("Google API not available - from_google_drive returns []")
        return []
    if not base_folder_id:
        logger.debug("from_google_drive called with empty folder id")
        return []

    try:
        svc = _make_drive_service()
        # Build query - immediate children of the folder and not trashed
        q = f"'{base_folder_id}' in parents and trashed=false"
        if mimeType:
            q += f" and mimeType = '{mimeType}'"

        files = []
        page_token = None
        while True:
            resp = svc.files().list(q=q,
                                    spaces='drive',
                                    fields="nextPageToken, files(id, name, description, mimeType, webViewLink)",
                                    pageSize=200,
                                    pageToken=page_token).execute()
            for f in resp.get("files", []):
                files.append({
                    "id": f.get("id"),
                    "name": f.get("name") or "",
                    "description": f.get("description") or "",
                    "mimeType": f.get("mimeType") or "",
                    "webViewLink": f.get("webViewLink") or ""
                })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return files
    except Exception:
        logger.exception("from_google_drive error")
        return []

def folders_and_images(parent_folder_id=None, sample_limit=12):
    parent_folder_id = parent_folder_id or os.environ.get("DRIVE_PARENT_FOLDER_ID", "")
    if not parent_folder_id or not GOOGLEAPI_AVAILABLE:
        return []
    try:
        svc = _make_drive_service()
        q_folders = f"'{parent_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders_resp = svc.files().list(q=q_folders, pageSize=200, fields="files(id, name)").execute()
        folders = folders_resp.get("files", [])
        to_return = {}
        for folder in folders:
            folder_id = folder.get("id")
            folder_name = folder.get("name")
            q_items = f"'{folder_id}' in parents and trashed=false"
            items_resp = svc.files().list(q=q_items, pageSize=200, fields="files(id, name, mimeType)").execute()
            items = items_resp.get("files", [])
            for item in items:
                to_return[item.get("name")] = (folder_name, item.get("id"))
        items = list(to_return.items())
        if len(items) <= sample_limit:
            return items
        import random
        return random.sample(items, sample_limit)
    except Exception:
        logger.exception("folders_and_images error")
        return []
def get_file_metadata(file_id):
    """
    Return metadata dict for a single Drive file (id, name, mimeType, webContentLink, thumbnailLink).
    Returns {} on error or if Google API is not available.
    """
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("Google API not available - get_file_metadata fallback")
        return {}
    if not file_id:
        return {}
    try:
        svc = _make_drive_service()
        # request common useful fields
        fields = "id, name, mimeType, webContentLink, thumbnailLink"
        meta = svc.files().get(fileId=file_id, fields=fields).execute()
        return meta or {}
    except Exception:
        logger.exception("get_file_metadata error for %s", file_id)
        return {}