# test_sheets.py
from sheets import get_questions
import traceback

try:
    rows = get_questions()
    print("OK: got", len(rows), "rows")
    for i, r in enumerate(rows[:10], start=1):
        print(i, r)
except Exception as e:
    print("ERROR:", e)
    traceback.print_exc()
