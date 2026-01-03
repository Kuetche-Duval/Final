"""
drive.py - Google Drive helper (defensive).
Call initialize(service_account_path) once from your app (pass path string or None).
Functions fall back gracefully when google-api-python-client is not installed.
"""

import os
import io
import json
import logging
import mimetypes

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

GOOGLEAPI_AVAILABLE = False
_SERVICE_ACCOUNT_PATH = None
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive"]

# Lazy imports (populated in initialize)
_credentials_mod = None
_build = None
_MediaIoBaseDownload = None
_MediaIoBaseUpload = None

def initialize(service_account_path: str | None):
    """
    Try to initialize google API clients. Pass service_account_path (string) or None.
    Returns dict: {"ok": True/False, "reason": "..."}
    """
    global GOOGLEAPI_AVAILABLE, _SERVICE_ACCOUNT_PATH, _credentials_mod, _build, _MediaIoBaseDownload, _MediaIoBaseUpload
    _SERVICE_ACCOUNT_PATH = service_account_path or os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "") or ""
    try:
        from google.oauth2 import service_account as _creds_mod
        from googleapiclient.discovery import build as _build_f
        from googleapiclient.http import MediaIoBaseDownload as _mid, MediaIoBaseUpload as _miu
        _credentials_mod = _creds_mod
        _build = _build_f
        _MediaIoBaseDownload = _mid
        _MediaIoBaseUpload = _miu
        # quick check that file exists (non-blocking if path missing)
        if _SERVICE_ACCOUNT_PATH and not os.path.exists(_SERVICE_ACCOUNT_PATH):
            logger.warning("drive.initialize: service account file not found at %s", _SERVICE_ACCOUNT_PATH)
        GOOGLEAPI_AVAILABLE = True
        logger.info("drive: google-api client appears available")
        return {"ok": True}
    except Exception as e:
        GOOGLEAPI_AVAILABLE = False
        logger.warning("drive.initialize: google apis not available: %s", e)
        return {"ok": False, "reason": str(e)}

def _make_drive_service():
    if not GOOGLEAPI_AVAILABLE:
        raise RuntimeError("google API not available")
    if not _SERVICE_ACCOUNT_PATH or not os.path.exists(_SERVICE_ACCOUNT_PATH):
        raise RuntimeError("service account JSON not found at " + str(_SERVICE_ACCOUNT_PATH))
    creds = _credentials_mod.service_account.Credentials.from_service_account_file(_SERVICE_ACCOUNT_PATH, scopes=_SCOPES)
    svc = _build("drive", "v3", credentials=creds, cache_discovery=False)
    return svc

def download_file(file_id: str) -> bytes:
    """Return bytes or b'' on error."""
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("drive.download_file: google api unavailable")
        return b""
    try:
        svc = _make_drive_service()
        request = svc.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = _MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read()
    except Exception:
        logger.exception("drive.download_file error for %s", file_id)
        return b""

def fetch_drive_image_bytes(file_id: str):
    """
    Convenience wrapper for images: returns (bytes, mime_type) or (None, None).
    Tries to infer mimeType via metadata, falls back to mimetypes module.
    """
    if not GOOGLEAPI_AVAILABLE:
        # no google client — caller may still want to know
        logger.debug("fetch_drive_image_bytes: google api not available")
        return None, None
    try:
        svc = _make_drive_service()
        meta = svc.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        mime = meta.get("mimeType") or mimetypes.guess_type(meta.get("name") or "")[0] or "application/octet-stream"
        data = download_file(file_id)
        if data:
            return data, mime
        return None, None
    except Exception:
        logger.exception("fetch_drive_image_bytes failed for %s", file_id)
        return None, None

def get_file_metadata(file_id: str) -> dict:
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("get_file_metadata: google api not available")
        return {}
    try:
        svc = _make_drive_service()
        fields = "id, name, mimeType, webContentLink, thumbnailLink"
        return svc.files().get(fileId=file_id, fields=fields).execute() or {}
    except Exception:
        logger.exception("get_file_metadata error for %s", file_id)
        return {}

def update_file(file_id: str, content_bytes: bytes, mime_type="application/json") -> bool:
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("update_file: google api not available")
        return False
    try:
        svc = _make_drive_service()
        fh = io.BytesIO(content_bytes)
        media = _MediaIoBaseUpload(fh, mimetype=mime_type, resumable=True)
        svc.files().update(fileId=file_id, media_body=media).execute()
        return True
    except Exception:
        logger.exception("update_file failed for %s", file_id)
        return False

def create_file_in_folder(folder_id: str, name: str, content_bytes: bytes, mime_type="application/json"):
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("create_file_in_folder: google api not available")
        return None
    try:
        svc = _make_drive_service()
        fh = io.BytesIO(content_bytes)
        media = _MediaIoBaseUpload(fh, mimetype=mime_type, resumable=True)
        body = {"name": name, "parents": [folder_id], "mimeType": mime_type}
        file = svc.files().create(body=body, media_body=media, fields="id").execute()
        return file.get("id")
    except Exception:
        logger.exception("create_file_in_folder failed for folder %s", folder_id)
        return None

def from_google_drive(base_folder_id: str, mimeType: str | None = None) -> list:
    """Return a list of file metadata dicts inside a Drive folder or [] on error."""
    if not GOOGLEAPI_AVAILABLE:
        logger.debug("from_google_drive: google api not available")
        return []
    if not base_folder_id:
        logger.debug("from_google_drive called with empty folder id")
        return []
    try:
        svc = _make_drive_service()
        q = f"'{base_folder_id}' in parents and trashed=false"
        if mimeType:
            q += f" and mimeType = '{mimeType}'"
        files = []
        page_token = None
        while True:
            resp = svc.files().list(q=q, spaces='drive', fields="nextPageToken, files(id, name, description, mimeType, webViewLink)", pageSize=200, pageToken=page_token).execute()
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

# End of drive.py
