# -*- coding: utf-8 -*-
"""
sheets_sync.py — 把掃描結果寫入 Google Sheets「US_WarRoom」
需求：pip install gspread google-auth
環境變數：
  GSA_JSON      : Service Account 金鑰 JSON 內容（GitHub Secret）
  US_SHEET_ID   : US_WarRoom 試算表 ID

用法：
  python sheets_sync.py                          # 預設：scan_result.csv → 掃描結果
  python sheets_sync.py chips_proxy.csv 籌碼替代   # 指定檔案與分頁名稱
"""
import json
import os
import sys
import pandas as pd


def sync(csv_file: str = "scan_result.csv", sheet_name: str = "掃描結果"):
    gsa_json = os.environ.get("GSA_JSON")
    sheet_id = os.environ.get("US_SHEET_ID")
    if not gsa_json or not sheet_id:
        print("⚠ 未設定 GSA_JSON / US_SHEET_ID，跳過 Sheets 同步（結果仍在 CSV/JSON）")
        return

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(gsa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    base = os.path.dirname(os.path.abspath(__file__))
    df = pd.read_csv(os.path.join(base, csv_file)).fillna("")

    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(sheet_name, rows=len(df) + 10, cols=len(df.columns) + 2)

    ws.update([df.columns.tolist()] + df.astype(str).values.tolist())
    print(f"✅ 已寫入 Sheets「{sheet_name}」：{len(df)} 筆")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 2:
        sync(csv_file=args[0], sheet_name=args[1])
    else:
        sync()
