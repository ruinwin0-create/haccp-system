import streamlit as st
import json
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

@st.cache_resource
def connect_google():
    raw = st.secrets["GOOGLE_KEY_JSON_TEXT"].strip()

    # 가끔 앞뒤 따옴표가 붙는 경우 방어
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]

    key_dict = json.loads(raw)

    creds = service_account.Credentials.from_service_account_info(
        key_dict,
        scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service


# ✅ 여기부터가 화면(UI) 코드 — import 아래에 있어야 함
st.set_page_config(page_title="천안공장 HACCP", layout="wide")
st.title("천안공장 HACCP")
st.write("✅ 앱 실행됨 (화면 테스트)")

try:
    gc, drive_service = connect_google()
    st.success("✅ Google 연결 성공!")
except Exception as e:
    st.error(f"❌ Google 연결 실패: {e}")
