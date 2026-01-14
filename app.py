import json
import streamlit as st
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =========================
# 기본 설정
# =========================
st.set_page_config(
    page_title="천안공장 HACCP",
    layout="wide"
)

st.title("천안공장 HACCP")
st.write("✅ 앱 실행됨 (베이스 화면)")

# =========================
# Google 설정
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

@st.cache_resource
def connect_google():
    try:
        key_dict = dict(st.secrets["google_key_json"])
    except Exception as e:
        st.error("🚨 Google Secrets 설정이 없습니다.")
        st.error(e)
        st.stop()

    creds = service_account.Credentials.from_service_account_info(
        key_dict,
        scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service

# =========================
# 연결 테스트
# =========================
try:
    gc, drive_service = connect_google()
    st.success("✅ Google Sheets / Drive 연결 성공")
except Exception as e:
    st.error("❌ Google 연결 실패")
    st.exception(e)

st.divider()

# =========================
# 다음 단계 안내
# =========================
st.subheader("다음 단계")
st.markdown("""
- ✅ 배포 정상
- ✅ Secrets 정상
- ⏭️ 다음:  
  - Google Sheet 데이터 로딩  
  - Supabase 연동  
  - 사진 업로드/조회  
  - 대시보드 & 보고서
""")
