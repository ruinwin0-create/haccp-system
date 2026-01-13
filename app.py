st.title("천안공장 HACCP")
st.write("✅ 앱 실행됨 (화면 테스트)")

try:
    gc, drive_service = connect_google()
    st.success("✅ Google 연결 성공!")
except Exception as e:
    st.error(f"❌ Google 연결 실패: {e}")
