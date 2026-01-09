import streamlit as st
import pandas as pd
import gspread
import json
import time
import xlsxwriter
import io
import altair as alt
from datetime import datetime
from PIL import Image, ImageOps
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- 1. 환경 설정 ---
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1BcMaaKnZG9q4qabwR1moRiE_QyC04jU3dZYR7grHQsc/edit?gid=0#gid=0"
DRIVE_FOLDER_ID = "117a_UMGDl6YoF8J32a6Y3uwkvl30JClG"
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

st.set_page_config(page_title="천안공장 HACCP", layout="wide")

# --- 2. 구글 연동 함수 (디버깅 버전) ---
@st.cache_resource
def connect_google():
    # [디버깅] 금고에 무슨 키가 있는지 확인 (내용은 안 보여주고 이름만 확인)
    if "google_key_json" not in st.secrets:
        st.error("🚨 오류: 'google_key_json'이라는 이름표를 찾을 수 없습니다!")
        st.warning(f"현재 금고에 있는 이름표들: {list(st.secrets.keys())}")
        st.info("Secrets 맨 첫 줄이 'google_key_json = \"\"\"' 로 시작하는지 확인하세요.")
        st.stop()

    try:
        # JSON 변환 시도
        key_dict = json.loads(st.secrets["google_key_json"])
        creds = service_account.Credentials.from_service_account_info(
            key_dict, scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        drive_service = build('drive', 'v3', credentials=creds)
        return gc, drive_service
    except json.JSONDecodeError as e:
        st.error(f"🚨 JSON 형식 오류: 복사/붙여넣기가 잘못되었습니다. ({e})")
        st.stop()
    except Exception as e:
        st.error(f"🚨 인증 오류 발생: {e}")
        st.stop()

@st.cache_data(ttl=10)
def load_data(_gc):
    try:
        sh = _gc.open_by_url(SPREADSHEET_URL)
        ws = sh.sheet1
        data = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
        df = pd.DataFrame(data)
        
        if df.empty: return pd.DataFrame()
        
        if '일시' in df.columns:
            df['일시'] = df['일시'].astype(str).str.replace('.', '-', regex=False).str.strip()
            df['일시'] = pd.to_datetime(df['일시'], errors='coerce')
            df['일시'] = df['일시'].fillna(pd.Timestamp('1900-01-01'))
            df['Year'] = df['일시'].dt.year
            df['Month'] = df['일시'].dt.month
            df['Week'] = df['일시'].dt.isocalendar().week
        
        if '개선 필요사항' in df.columns:
            df = df[df['개선 필요사항'].astype(str).str.strip() != '']

        return df
    except Exception as e:
        st.error(f"데이터 로딩 실패: {e}")
        return pd.DataFrame()

# [공통] 구글 드라이브 이미지 다운로드
@st.cache_data(show_spinner=False)
def download_image_bytes(_drive_service, file_link):
    try:
        if isinstance(file_link, str) and "drive.google.com" in file_link and "/d/" in file_link:
            file_id = file_link.split("/d/")[1].split("/")[0]
            return _drive_service.files().get_media(fileId=file_id).execute()
    except: pass
    return None

# [공통] 이미지 자동 압축
def compress_image(uploaded_file):
    try:
        image = Image.open(uploaded_file)
        image = ImageOps.exif_transpose(image)
        image = image.convert('RGB')
        image.thumbnail((1024, 1024))
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=70)
        output.seek(0)
        output.name = uploaded_file.name
        output.type = 'image/jpeg'
        return output
    except Exception as e:
        return uploaded_file

def upload_photo(drive_service, uploaded_file):
    if uploaded_file is None: return ""
    compressed_file = compress_image(uploaded_file)
    file_metadata = {'name': f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uploaded_file.name}", 'parents': [DRIVE_FOLDER_ID]}
    media = MediaIoBaseUpload(compressed_file, mimetype='image/jpeg')
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
    return file.get('webViewLink')

def process_and_upload(gc, uploaded_file):
    try:
        if uploaded_file.name.endswith('.csv'): df_raw = pd.read_csv(uploaded_file)
        else: df_raw = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"파일 읽기 실패: {e}")
        return

    header_idx = None
    for idx, row in df_raw.iterrows():
        if row.astype(str).str.contains('점검일').any() or row.astype(str).str.contains('번호').any():
            header_idx = idx
            break
    
    if header_idx is None:
        st.error("헤더를 찾을 수 없습니다.")
        return

    if uploaded_file.name.endswith('.csv'):
        uploaded_file.seek(0)
        df = pd.read_csv(uploaded_file, header=header_idx)
    else:
        df = pd.read_excel(uploaded_file, header=header_idx)

    all_data = []
    cols = df.columns.astype(str)
    try:
        col_date = cols[cols.str.contains('점검일')][0]
        col_issue = cols[cols.str.contains('개선 필요사항') | cols.str.contains('내용')][0]
        col_dept = cols[cols.str.contains('관리부서') | cols.str.contains('담당')][0]
        col_status = cols[cols.str.contains('진행상태')][0]
        col_action = cols[cols.str.contains('개선내용')][0]
        col_complete = cols[cols.str.contains('개선완료일')][0]
    except:
        st.error("필수 컬럼 누락")
        return

    progress = st.progress(0)
    for i, row in df.iterrows():
        if pd.isna(row[col_date]): continue 
        raw_issue = str(row[col_issue])
        location = raw_issue.split('\n')[0].strip() if '\n' in raw_issue else "기타"
        try: d_date = pd.to_datetime(str(row[col_date]).replace('.', '-')).strftime('%Y-%m-%d')
        except: d_date = ""
        try: c_date = pd.to_datetime(str(row[col_complete]).replace('.', '-')).strftime('%Y-%m-%d')
        except: c_date = ""

        row_data = {
            'ID': f"IMPORTED_{int(time.time())}_{i}",
            '일시': d_date, '공정': location, '개선 필요사항': raw_issue,
            '담당자': str(row[col_dept]), '진행상태': str(row[col_status]).strip(),
            '개선내용': str(row[col_action]) if pd.notna(row[col_action]) else "",
            '개선완료일': c_date, '사진_전': "", '사진_후': ""
        }
        all_data.append(row_data)
        progress.progress((i+1)/len(df))

    final_df = pd.DataFrame(all_data)
    final_df = final_df[['ID', '일시', '공정', '개선 필요사항', '담당자', '진행상태', '개선내용', '개선완료일', '사진_전', '사진_후']]
    
    sh = gc.open_by_url(SPREADSHEET_URL)
    ws = sh.sheet1
    current_data = ws.get_all_values()
    if len(current_data) <= 1: ws.update([final_df.columns.values.tolist()] + final_df.values.tolist())
    else: ws.append_rows(final_df.values.tolist())
    st.success(f"✅ 총 {len(final_df)}건 업로드 완료!")

# --- 3. 메인 앱 ---
try:
    gc, drive_service = connect_google()
    df = load_data(gc)
except Exception as e:
    # 여기가 핵심! 이제 그냥 멈추지 않고 상세 에러를 보여줌
    st.error(f"❌ 접속 중단: {e}")
    st.stop()

st.sidebar.markdown("## ☁️ 천안공장 위생 점검 (Cloud)")
menu = st.sidebar.radio("메뉴", ["📊 대시보드", "📝 문제 등록", "🛠️ 조치 입력"])
st.sidebar.markdown("---")

with st.sidebar.expander("📂 엑셀 데이터 업로드"):
    st.info("실행과제서 파일 업로드")
    uploaded_file = st.file_uploader("엑셀/CSV 선택", type=['xlsx', 'xls', 'csv'])
    if uploaded_file and st.button("🚀 데이터 전송"):
        with st.spinner('전송 중...'):
            process_and_upload(gc, uploaded_file)
        st.balloons() 
        st.success("✅ 완료! (3초 후 새로고침)")
        time.sleep(3)
        st.rerun()

st.sidebar.markdown("---")
if st.sidebar.button("🔄 새로고침"): st.rerun()

if menu == "📊 대시보드":
    st.markdown("### 📊 천안공장 위생점검 현황")
    if df.empty:
        st.warning("데이터가 없습니다.")
    else:
        st.sidebar.markdown("### 📅 기간 필터")
        years = sorted(df['Year'].dropna().unique())
        year_options = [int(y) for y in years]
        selected_years = st.sidebar.multiselect("연도", year_options, default=year_options)
        
        if selected_years: 
            df = df[df['Year'].isin(selected_years)]
            available_months = sorted(df['Month'].dropna().unique().astype(int))
            month_options = [f"{m}월" for m in available_months]
            selected_months_str = st.sidebar.multiselect("월", month_options, default=month_options)
            
            if selected_months_str:
                selected_months = [int(m.replace("월", "")) for m in selected_months_str]
                df = df[df['Month'].isin(selected_months)]
                available_weeks = sorted(df['Week'].dropna().unique().astype(int))
                week_options = [f"{w}주차" for w in available_weeks]
                selected_weeks_str = st.sidebar.multiselect("주차(Week)", week_options, default=week_options)
                
                if selected_weeks_str:
                    selected_weeks = [int(w.replace("주차", "")) for w in selected_weeks_str]
                    df = df[df['Week'].isin(selected_weeks)]
                else: st.warning("주차를 선택해주세요.")
            else: st.warning("월을 선택해주세요.")
        else: st.warning("연도를 선택해주세요.")

        m1, m2, m3 = st.columns(3)
        total_count = len(df)
        done_count = len(df[df['진행상태'] == '완료'])
        rate = (done_count / total_count * 100) if total_count > 0 else 0
        m1.metric("총 점검 건수", f"{total_count}건")
        m2.metric("조치 완료", f"{done_count}건")
        m3.metric("총 개선율", f"{rate:.1f}%", delta_color="normal")
        st.divider()

        c1, c2 = st.columns(2)
        if len(selected_months_str) > 1: group_col, x_title = 'Month', "월"
        else: group_col, x_title = '공정', "장소"

        chart_df = df.groupby(group_col).agg(
            총발생=('ID', 'count'),
            조치완료=('진행상태', lambda x: (x == '완료').sum())
        ).reset_index()
        chart_df['진행률'] = (chart_df['조치완료'] / chart_df['총발생'] * 100).fillna(0).round(1)
        chart_df['라벨'] = chart_df['진행률'].astype(str) + '%'

        with c1:
            st.markdown(f"**🔴 총 발생 건수 ({x_title}별)**")
            chart1 = alt.Chart(chart_df).mark_bar(color='#FF4B4B').encode(
                x=alt.X(f'{group_col}:N', axis=alt.Axis(labelAngle=0, title=None)),
                y=alt.Y('총발생:Q'), tooltip=[group_col, '총발생']
            )
            st.altair_chart(chart1, use_container_width=True)

        with c2:
            st.markdown(f"**🟢 조치 완료율 (%)**")
            base = alt.Chart(chart_df).encode(
                x=alt.X(f'{group_col}:N', axis=alt.Axis(labelAngle=0, title=None)),
                y=alt.Y('조치완료:Q')
            )
            bars = base.mark_bar(color='#2ECC71')
            text = base.mark_text(dy=-15, color='black').encode(text=alt.Text('라벨:N'))
            st.altair_chart(bars + text, use_container_width=True)

        st.divider()
        st.markdown("**🏆 장소별 개선율 순위**")
        loc_stats = df.groupby('공정')['진행상태'].apply(lambda x: (x == '완료').mean()).reset_index(name='율')
        loc_stats['율'] = loc_stats['율'] * 100
        st.dataframe(loc_stats.sort_values('율', ascending=False), column_config={"공정": "장소", "율": st.column_config.ProgressColumn("개선율", format="%.1f%%", min_value=0, max_value=100)}, hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("📋 상세 내역 리스트 (최근 10건)")
        recent_df = df.iloc[::-1].head(10)
        for _, r in recent_df.iterrows():
            date_str = r['일시'].strftime('%Y-%m-%d') if pd.notnull(r['일시']) else ""
            summary = str(r['개선 필요사항'])[:20]
            icon = "✅" if r['진행상태'] == '완료' else "🔥"
            with st.expander(f"{icon} [{r['진행상태']}] {date_str} | {r['공정']} - {summary}..."):
                c_1, c_2, c_3 = st.columns([1, 1, 2])
                with c_1:
                    st.caption("❌ 전")
                    if r['사진_전']: 
                        img = download_image_bytes(drive_service, r['사진_전'])
                        if img: st.image(img, use_container_width=True)
                with c_2:
                    st.caption("✅ 후")
                    if r['사진_후']: 
                        img = download_image_bytes(drive_service, r['사진_후'])
                        if img: st.image(img, use_container_width=True)
                with c_3:
                    st.markdown(f"**내용:** {r['개선 필요사항']}")
                    st.markdown(f"**담당:** {r['담당자']}")
                    if r['개선내용']: st.info(f"조치: {r['개선내용']}")

elif menu == "📝 문제 등록":
    st.markdown("### 📝 문제 등록")
    with st.form("input"):
        dt = st.date_input("일자")
        loc = st.selectbox("장소", ["전처리실", "입국실", "발효실", "제성실", "병입/포장실", "원료창고", "제품창고", "실험실", "화장실/탈의실", "기타"])
        iss = st.text_area("내용")
        mgr = st.text_input("담당")
        pho = st.file_uploader("사진")
        if st.form_submit_button("저장"):
            with st.spinner('저장 중...'):
                lnk = upload_photo(drive_service, pho)
                sh = gc.open_by_url(SPREADSHEET_URL)
                new_id = int(time.time())
                sh.sheet1.append_row([f"{new_id}", dt.strftime('%Y-%m-%d'), loc, iss, mgr, '진행중', '', '', lnk, ''])
            st.balloons()
            st.success("✅ 저장 완료!")
            time.sleep(2)
            st.rerun()

elif menu == "🛠️ 조치 입력":
    st.markdown("### 🛠️ 조치 입력")
    if '진행상태' in df.columns: tasks = df[df['진행상태'] != '완료']
    else: tasks = pd.DataFrame()
    
    if not tasks.empty:
        managers = ["전체"] + sorted(tasks['담당자'].astype(str).unique().tolist())
        selected_manager = st.selectbox("👤 담당자 선택", managers)
        if selected_manager != "전체": filtered_tasks = tasks[tasks['담당자'] == selected_manager]
        else: filtered_tasks = tasks

        if filtered_tasks.empty: st.info("할 일이 없습니다.")
        else:
            task_options = {row['ID']: f"{str(row['개선 필요사항'])[:30]}... ({row['공정']})" for index, row in filtered_tasks.iterrows()}
            selected_id = st.selectbox("해결할 문제", options=list(task_options.keys()), format_func=lambda x: task_options[x])
            target_row = filtered_tasks[filtered_tasks['ID'] == selected_id].iloc[0]
            
            st.divider()
            c1, c2 = st.columns([1, 2])
            with c1:
                st.caption("📸 개선 전")
                if target_row['사진_전']:
                    img = download_image_bytes(drive_service, target_row['사진_전'])
                    if img: st.image(img, use_container_width=True)
            with c2:
                st.markdown(f"**장소:** {target_row['공정']} / **담당:** {target_row['담당자']}")
                st.info(target_row['개선 필요사항'])
            st.divider()

            with st.form("act_form"):
                atxt = st.text_area("조치 내용")
                adt = st.date_input("완료일")
                aph = st.file_uploader("조치 후 사진")
                if st.form_submit_button("완료 저장"):
                    if not atxt: st.warning("내용 입력!")
                    else:
                        with st.spinner('저장 중...'):
                            lnk = upload_photo(drive_service, aph) if aph else ""
                            sh = gc.open_by_url(SPREADSHEET_URL)
                            ws = sh.sheet1
                            try:
                                cell = ws.find(str(selected_id))
                                ws.update_cell(cell.row, 7, atxt) 
                                ws.update_cell(cell.row, 8, adt.strftime('%Y-%m-%d'))
                                ws.update_cell(cell.row, 6, '완료')
                                if lnk: ws.update_cell(cell.row, 10, lnk)
                                st.balloons()
                                st.success("저장 완료!")
                                time.sleep(2)
                                st.rerun()
                            except: st.error("오류 발생")
    else: st.info("조치할 항목이 없습니다.")
