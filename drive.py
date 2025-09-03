# drive.py
import os
import time
import random
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

try:
    from pydrive2.auth import ServiceAccountCredentials, GoogleAuth
    from pydrive2.drive import GoogleDrive
except Exception:
    # pydrive2 may not be installed in dev; handle gracefully
    ServiceAccountCredentials = None
    GoogleAuth = None
    GoogleDrive = None

SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

_cache = {"data": None, "timestamp": 0}
CACHE_TTL = int(os.environ.get("DRIVE_CACHE_TTL", "180"))

def get_work_drive():
    if GoogleDrive is None:
        raise RuntimeError("pydrive2 not installed")
    # Use ServiceAccountCredentials from pydrive2 if available
    gauth = GoogleAuth()
    gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
        SERVICE_ACCOUNT_PATH,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return GoogleDrive(gauth)

def from_google_drive(base_folder_id, mimeType="image/jpeg"):
    """
    Return list of files in folder `base_folder_id` filtered by mimeType.
    Returns list of dicts: [{'title': ..., 'id': ..., 'description': ...}, ...]
    """
    drive = get_work_drive()
    # Build simple query (note: pydrive2 query syntax expects proper quoting)
    q = f"'{base_folder_id}' in parents and trashed=false"
    if mimeType:
        q += f" and mimeType='{mimeType}'"
    file_list = drive.ListFile({'q': q}).GetList()
    out = []
    for f in file_list:
        out.append({
            "title": f.get("title"),
            "id": f.get("id"),
            "description": f.get("description", "")
        })
    return out

def folders_and_images():
    """
    Returns a small sample mapping of title -> (folderTitle, id).
    Structure used in templates: list of tuples [(title, (folderTitle, id)), ...]
    """
    now = time.time()
    if _cache["data"] and now - _cache["timestamp"] < CACHE_TTL:
        return _cache["data"]

    parent_folder_id = os.environ.get("DRIVE_PARENT_FOLDER_ID", "")
    if not parent_folder_id or GoogleDrive is None:
        return []

    to_return = {}
    # List child folders
    try:
        drive = get_work_drive()
        folders = drive.ListFile({'q': f"'{parent_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"}).GetList()
        for folder in folders:
            # list images inside folder
            items = drive.ListFile({'q': f"'{folder['id']}' in parents and trashed=false"}).GetList()
            for item in items:
                # use the file title as key (avoid duplicates)
                to_return[item['title']] = (folder['title'], item['id'])
    except Exception:
        logger.exception("folders_and_images encountered an error")
        return []

    data = random.sample(list(to_return.items()), min(10, len(to_return)))
    _cache["data"] = data
    _cache["timestamp"] = now
    return data
