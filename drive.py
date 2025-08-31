import os
import time
import random
from pydrive2.auth import GoogleAuth, ServiceAccountCredentials
from pydrive2.drive import GoogleDrive

SERVICE_ACCOUNT_PATH = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

def get_work_drive():
    gauth = GoogleAuth()
    gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
        SERVICE_ACCOUNT_PATH,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return GoogleDrive(gauth)

# Cache setup
_cache = {"data": None, "timestamp": 0}
CACHE_TTL = int(os.environ.get("DRIVE_CACHE_TTL", "180"))  # default 3 minutes

def from_google_drive(base_folder_id, mimeType="image/jpeg"):
    drive = get_work_drive()
    query = {"q": f"'{base_folder_id}' in parents and mimeType='{mimeType}' and trashed=false"}
    return drive.ListFile(query).GetList()

def folders_and_images():
    now = time.time()
    if _cache["data"] and now - _cache["timestamp"] < CACHE_TTL:
        return _cache["data"]

    parent_folder_id = os.environ.get("DRIVE_PARENT_FOLDER_ID", "")
    if not parent_folder_id:
        return []

    to_return_list = {}
    for folder in from_google_drive(parent_folder_id, "application/vnd.google-apps.folder"):
        for item in from_google_drive(folder["id"]):
            to_return_list[item["title"]] = (folder["title"], item["id"])

    data = random.sample(list(to_return_list.items()), min(10, len(to_return_list)))
    _cache["data"] = data
    _cache["timestamp"] = now
    return data