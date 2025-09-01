import os, base64
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Load env
load_dotenv()

SERVICE_ACCOUNT_B64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
if not SERVICE_ACCOUNT_B64:
    raise ValueError("SERVICE_ACCOUNT_JSON_B64 not set in .env")

# Decode service account
with open("service_account.json", "wb") as f:
    f.write(base64.b64decode(SERVICE_ACCOUNT_B64))

# Scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Authorize
creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
client = gspread.authorize(creds)

# --- TEST GOOGLE SHEETS ---
spreadsheet_id = os.environ.get("TEST_SHEET_ID")  # put in .env
if spreadsheet_id:
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.sheet1
    print("Sheet title:", ws.title)
    print("First row:", ws.row_values(1))
else:
    print("⚠️ No TEST_SHEET_ID in .env")
