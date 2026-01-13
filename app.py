import json
import streamlit as st
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

@st.cache_resource
def connect_google():
    raw = st.secrets["GOOGLE_KEY_JSON_TEXT"]

    # ✅ Streamlit Secrets에서 들어온 문자열 정리 (핵심)
    raw = raw.strip()

    # 가끔 맨 앞/뒤에 쌍따옴표가 붙는 경우 제거
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]

    try:
        key_dict = json.loads(raw)
    except Exception as e:
        st.error("🚨 Google Key JSON 파싱 실패")
        st.code(raw[:300])  # 앞부분만 출력 (디버깅용)
        st.error(e)
        st.stop()

    creds = service_account.Credentials.from_service_account_info(
        key_dict,
        scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service
