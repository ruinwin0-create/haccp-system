# streamlit_app.py
import streamlit as st
import pandas as pd
import gspread
import time
import io
import altair as alt

from datetime import datetime
from PIL import Image, ImageOps
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError


# =========================
# 1) 환경 설정
# =========================
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1BcMaaKnZG9q4qabwR1moRiE_QyC04jU3dZYR7grHQsc/edit?gid=0#gid=0"

# 👇 드라이브 업로드 대상 폴더 ID (공유드라이브/내드라이브 모두 가능)
DRIVE_FOLDER_ID = "117a_UMGDl6YoF8J32a6Y3uwkvl30JClG"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

st.set_page_config(page_title="천안공장 HACCP", layout="wide")


# =========================
# 2) 구글 연동
# =========================
@st.cache_resource
def connect_google():
    if "google_key_json" not in st.secrets:
        st.error("🚨 오류: Secrets 설정(st.secrets['google_key_json'])이 없습니다.")
        st.stop()

    try:
        key_dict = dict(st.secrets["google_key_json"])
        creds = service_account.Credentials.from_service_account_info(key_dict, scopes=SCOPES)
        gc = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds)
        return gc, drive_service
    except Exception as e:
        st.error(f"🚨 인증 오류: {e}")
        st.stop()


# =========================
# 3) 시트 데이터 로드
# =========================
@st.cache_data(ttl=10)
def load_data(_gc):
    try:
        sh = _gc.open_by_url(SPREADSHEET_URL)
        ws = sh.sheet1
        data = ws.get_all_records(value_render_option="UNFORMATTED_VALUE")
        df = pd.DataFrame(data)

        if df.empty:
            return pd.DataFrame()

        # 날짜 처리
        if "일시" in df.columns:
            df["일시"] = df["일시"].astype(str).str.replace(".", "-", regex=False).str.strip()
            df["일시"] = pd.to_datetime(df["일시"], errors="coerce")
            df["일시"] = df["일시"].fillna(pd.Timestamp("1900-01-01"))
            df["Year"] = df["일시"].dt.year
            df["Month"] = df["일시"].dt.month
            df["Week"] = df["일시"].dt.isocalendar().week

        # 개선 필요사항 공백 제거 필터
        if "개선 필요사항" in df.columns:
            df = df[df["개선 필요사항"].astype(str).str.strip() != ""]

        return df
    except Exception as e:
        st.error(f"데이터 로딩 실패: {e}")
        return pd.DataFrame()


# =========================
# 4) Drive 파일ID 파싱 + 다운로드
# =========================
def extract_drive_file_id(link_or_id: str) -> str | None:
    """webViewLink(https://drive.google.com/file/d/ID/view...) 또는 ...?id=ID 또는 이미 fileId인 경우도 처리"""
    if not isinstance(link_or_id, str) or not link_or_id.strip():
        return None

    s = link_or_id.strip()

    # 이미 fileId만 들어온 경우(대부분 20~40자)
    if "drive.google.com" not in s and "/" not in s and " " not in s and len(s) >= 15:
        return s

    if "drive.google.com" in s:
        if "/d/" in s:
            return s.split("/d/")[1].split("/")[0]
        if "id=" in s:
            return s.split("id=")[1].split("&")[0]
    return None


@st.cache_data(show_spinner=False)
def download_image_bytes(_drive_service, file_id_or_link: str):
    file_id = extract_drive_file_id(file_id_or_link)
    if not file_id:
        return None
    try:
        # 공유드라이브까지 포함 지원
        return _drive_service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    except HttpError:
        return None
    except Exception:
        return None


# =========================
# 5) 이미지 압축 (안정형: seek(0), HEIC 등 실패 시 None 반환)
# =========================
def compress_image(uploaded_file):
    """
    - 업로드 스트림 포인터 문제 방지: seek(0)
    - PIL이 못 여는 형식(HEIC 등)이나 손상 파일: None 반환
    """
    try:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass

        img = Image.open(uploaded_file)
        img = ImageOps.exif_transpose(img)  # 회전 정보 반영
        img = img.convert("RGB")
        img.thumbnail((1024, 1024))

        output = io.BytesIO()
        img.save(output, format="JPEG", quality=70, optimize=True)
        output.seek(0)

        base = getattr(uploaded_file, "name", "photo")
        if "." in base:
            base = base.rsplit(".", 1)[0]
        output.name = f"{base}.jpg"
        output.type = "image/jpeg"
        return output

    except Exception as e:
        st.error(f"이미지 압축 실패: {e} (HEIC/손상파일 가능)")
        return None


# =========================
# 6) Drive 업로드 (핵심 수정 반영)
#    - supportsAllDrives=True
#    - resumable=True
#    - seek(0)
#    - HttpError content 출력
#    - 저장값은 webViewLink 대신 fileId 저장(권장)
# =========================
def upload_photo(drive_service, uploaded_file):
    if uploaded_file is None:
        return ""  # 빈 값 저장

    compressed = compress_image(uploaded_file)
    if compressed is None:
        return ""  # 압축 실패 시 업로드 중단

    # ✅ 업로드 직전 스트림 포인터 초기화
    try:
        compressed.seek(0)
    except Exception:
        pass

    file_metadata = {
        "name": f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{compressed.name}",
        "parents": [DRIVE_FOLDER_ID],
    }

    media = MediaIoBaseUpload(
        compressed,
        mimetype="image/jpeg",
        resumable=True,  # 큰 파일/네트워크 불안정에 안정적
    )

    try:
        created = (
            drive_service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

        # ✅ 권장: fileId 저장(다운로드/조회 안정성↑)
        return created.get("id", "")

    except HttpError as e:
        st.error(f"Drive 업로드 실패(HttpError): {e}")
        try:
            st.code(e.content.decode("utf-8", "ignore"))
        except Exception:
            pass
        return ""
    except Exception as e:
        st.error(f"Drive 업로드 실패: {e}")
        return ""


# =========================
# 7) 엑셀/CSV -> 시트 업로드
# =========================
def process_and_upload(gc, uploaded_file):
    try:
        if uploaded_file.name.endswith(".csv"):
            df_raw = pd.read_csv(uploaded_file)
        else:
            df_raw = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"파일 읽기 실패: {e}")
        return

    header_idx = None
    for idx, row in df_raw.iterrows():
        if row.astype(str).str.contains("점검일").any() or row.astype(str).str.contains("번호").any():
            header_idx = idx
            break

    if header_idx is None:
        st.error("헤더를 찾을 수 없습니다.")
        return

    # 다시 로드(헤더 적용)
    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    if uploaded_file.name.endswith(".csv"):
        df = pd.read_csv(uploaded_file, header=header_idx)
    else:
        df = pd.read_excel(uploaded_file, header=header_idx)

    cols = df.columns.astype(str)

    try:
        col_date = cols[cols.str.contains("점검일")][0]
        col_issue = cols[cols.str.contains("개선 필요사항") | cols.str.contains("내용")][0]
        col_dept = cols[cols.str.contains("관리부서") | cols.str.contains("담당")][0]
        col_status = cols[cols.str.contains("진행상태")][0]
        col_action = cols[cols.str.contains("개선내용")][0]
        col_complete = cols[cols.str.contains("개선완료일")][0]
    except Exception:
        st.error("필수 컬럼 누락(점검일/개선 필요사항/관리부서/진행상태/개선내용/개선완료일)")
        return

    all_data = []
    progress = st.progress(0)

    base_ts = int(time.time())
    total_rows = len(df)

    for i, row in df.iterrows():
        if pd.isna(row[col_date]):
            continue

        raw_issue = str(row[col_issue])
        location = raw_issue.split("\n")[0].strip() if "\n" in raw_issue else "기타"

        try:
            d_date = pd.to_datetime(str(row[col_date]).replace(".", "-")).strftime("%Y-%m-%d")
        except Exception:
            d_date = ""

        try:
            c_date = pd.to_datetime(str(row[col_complete]).replace(".", "-")).strftime("%Y-%m-%d")
        except Exception:
            c_date = ""

        row_data = {
            "ID": f"IMPORTED_{base_ts}_{i}",
            "일시": d_date,
            "공정": location,
            "개선 필요사항": raw_issue,
            "담당자": str(row[col_dept]),
            "진행상태": str(row[col_status]).strip(),
            "개선내용": str(row[col_action]) if pd.notna(row[col_action]) else "",
            "개선완료일": c_date,
            "사진_전": "",
            "사진_후": "",
        }
        all_data.append(row_data)

        if total_rows > 0:
            progress.progress((i + 1) / total_rows)

    final_df = pd.DataFrame(all_data)
    final_df = final_df[
        ["ID", "일시", "공정", "개선 필요사항", "담당자", "진행상태", "개선내용", "개선완료일", "사진_전", "사진_후"]
    ]

    try:
        sh = gc.open_by_url(SPREADSHEET_URL)
        ws = sh.sheet1
        current_data = ws.get_all_values()

        if len(current_data) <= 1:
            ws.update([final_df.columns.values.tolist()] + final_df.values.tolist())
        else:
            ws.append_rows(final_df.values.tolist())

        st.success(f"✅ 총 {len(final_df)}건 업로드 완료!")
    except Exception as e:
        st.error(f"시트 업로드 실패: {e}")


# =========================
# 8) 메인 앱
# =========================
try:
    gc, drive_service = connect_google()
    df = load_data(gc)
except Exception as e:
    st.error(f"❌ 접속 중단: {e}")
    st.stop()

st.sidebar.markdown("## ☁️ 천안공장 위생 점검 (Cloud)")
menu = st.sidebar.radio("메뉴", ["📊 대시보드", "📝 문제 등록", "🛠️ 조치 입력"])
st.sidebar.markdown("---")

with st.sidebar.expander("📂 엑셀 데이터 업로드"):
    st.info("실행과제서 파일 업로드")
    uploaded_file = st.file_uploader("엑셀/CSV 선택", type=["xlsx", "xls", "csv"])
    if uploaded_file and st.button("🚀 데이터 전송"):
        with st.spinner("전송 중..."):
            process_and_upload(gc, uploaded_file)
        st.balloons()
        st.success("✅ 완료! (3초 후 새로고침)")
        time.sleep(3)
        st.rerun()

st.sidebar.markdown("---")
if st.sidebar.button("🔄 새로고침"):
    st.rerun()


# =========================
# 9) 화면: 대시보드
# =========================
if menu == "📊 대시보드":
    st.markdown("### 📊 천안공장 위생점검 현황")

    if df.empty:
        st.warning("데이터가 없습니다.")
    else:
        st.sidebar.markdown("### 📅 기간 필터")
        years = sorted(df["Year"].dropna().unique())
        year_options = [int(y) for y in years]
        selected_years = st.sidebar.multiselect("연도", year_options, default=year_options)

        if selected_years:
            df = df[df["Year"].isin(selected_years)]

            available_months = sorted(df["Month"].dropna().unique().astype(int))
            month_options = [f"{m}월" for m in available_months]
            selected_months_str = st.sidebar.multiselect("월", month_options, default=month_options)

            if selected_months_str:
                selected_months = [int(m.replace("월", "")) for m in selected_months_str]
                df = df[df["Month"].isin(selected_months)]

                available_weeks = sorted(df["Week"].dropna().unique().astype(int))
                week_options = [f"{w}주차" for w in available_weeks]
                selected_weeks_str = st.sidebar.multiselect("주차(Week)", week_options, default=week_options)

                if selected_weeks_str:
                    selected_weeks = [int(w.replace("주차", "")) for w in selected_weeks_str]
                    df = df[df["Week"].isin(selected_weeks)]
                else:
                    st.warning("주차를 선택해주세요.")
            else:
                st.warning("월을 선택해주세요.")
        else:
            st.warning("연도를 선택해주세요.")

        m1, m2, m3 = st.columns(3)
        total_count = len(df)
        done_count = len(df[df["진행상태"] == "완료"])
        rate = (done_count / total_count * 100) if total_count > 0 else 0
        m1.metric("총 점검 건수", f"{total_count}건")
        m2.metric("조치 완료", f"{done_count}건")
        m3.metric("총 개선율", f"{rate:.1f}%")
        st.divider()

        c1, c2 = st.columns(2)
        if "selected_months_str" in locals() and len(selected_months_str) > 1:
            group_col, x_title = "Month", "월"
        else:
            group_col, x_title = "공정", "장소"

        chart_df = (
            df.groupby(group_col)
            .agg(총발생=("ID", "count"), 조치완료=("진행상태", lambda x: (x == "완료").sum()))
            .reset_index()
        )
        chart_df["진행률"] = (chart_df["조치완료"] / chart_df["총발생"] * 100).fillna(0).round(1)
        chart_df["라벨"] = chart_df["진행률"].astype(str) + "%"

        with c1:
            st.markdown(f"**🔴 총 발생 건수 ({x_title}별)**")
            chart1 = (
                alt.Chart(chart_df)
                .mark_bar(color="#FF4B4B")
                .encode(
                    x=alt.X(f"{group_col}:N", axis=alt.Axis(labelAngle=0, title=None)),
                    y=alt.Y("총발생:Q"),
                    tooltip=[group_col, "총발생"],
                )
            )
            st.altair_chart(chart1, use_container_width=True)

        with c2:
            st.markdown("**🟢 조치 완료율 (%)**")
            base = alt.Chart(chart_df).encode(
                x=alt.X(f"{group_col}:N", axis=alt.Axis(labelAngle=0, title=None)),
                y=alt.Y("조치완료:Q"),
            )
            bars = base.mark_bar(color="#2ECC71")
            text = base.mark_text(dy=-15, color="black").encode(text=alt.Text("라벨:N"))
            st.altair_chart(bars + text, use_container_width=True)

        st.divider()
        st.markdown("**🏆 장소별 개선율 순위**")
        loc_stats = df.groupby("공정")["진행상태"].apply(lambda x: (x == "완료").mean()).reset_index(name="율")
        loc_stats["율"] = (loc_stats["율"] * 100).round(1)

        st.dataframe(
            loc_stats.sort_values("율", ascending=False),
            column_config={
                "공정": "장소",
                "율": st.column_config.ProgressColumn("개선율", format="%.1f%%", min_value=0, max_value=100),
            },
            hide_index=True,
            use_container_width=True,
        )

        st.divider()
        st.subheader("📋 상세 내역 리스트 (최근 10건)")
        recent_df = df.iloc[::-1].head(10)

        for _, r in recent_df.iterrows():
            date_str = r["일시"].strftime("%Y-%m-%d") if pd.notnull(r["일시"]) else ""
            summary = str(r["개선 필요사항"])[:20]
            icon = "✅" if r["진행상태"] == "완료" else "🔥"

            with st.expander(f"{icon} [{r['진행상태']}] {date_str} | {r['공정']} - {summary}..."):
                c_1, c_2, c_3 = st.columns([1, 1, 2])

                with c_1:
                    st.caption("❌ 전")
                    if r.get("사진_전"):
                        img = download_image_bytes(drive_service, r["사진_전"])
                        if img:
                            st.image(img, use_container_width=True)

                with c_2:
                    st.caption("✅ 후")
                    if r.get("사진_후"):
                        img = download_image_bytes(drive_service, r["사진_후"])
                        if img:
                            st.image(img, use_container_width=True)

                with c_3:
                    st.markdown(f"**내용:** {r['개선 필요사항']}")
                    st.markdown(f"**담당:** {r['담당자']}")
                    if str(r.get("개선내용", "")).strip():
                        st.info(f"조치: {r['개선내용']}")


# =========================
# 10) 화면: 문제 등록
# =========================
elif menu == "📝 문제 등록":
    st.markdown("### 📝 문제 등록")

    with st.form("input"):
        dt = st.date_input("일자")
        loc = st.selectbox(
            "장소",
            ["전처리실", "입국실", "발효실", "제성실", "병입/포장실", "원료창고", "제품창고", "실험실", "화장실/탈의실", "기타"],
        )
        iss = st.text_area("내용")
        mgr = st.text_input("담당")
        pho = st.file_uploader("사진", type=["jpg", "jpeg", "png", "webp"])  # HEIC 제외(실패 원인 방지)

        if st.form_submit_button("저장"):
            with st.spinner("저장 중..."):
                photo_file_id = upload_photo(drive_service, pho)  # ✅ fileId 저장
                sh = gc.open_by_url(SPREADSHEET_URL)
                new_id = int(time.time())
                sh.sheet1.append_row(
                    [
                        f"{new_id}",
                        dt.strftime("%Y-%m-%d"),
                        loc,
                        iss,
                        mgr,
                        "진행중",
                        "",
                        "",
                        photo_file_id,  # 사진_전
                        "",  # 사진_후
                    ]
                )

            st.balloons()
            st.success("✅ 저장 완료!")
            time.sleep(2)
            st.rerun()


# =========================
# 11) 화면: 조치 입력
# =========================
elif menu == "🛠️ 조치 입력":
    st.markdown("### 🛠️ 조치 입력")

    tasks = df[df["진행상태"] != "완료"] if "진행상태" in df.columns else pd.DataFrame()

    if not tasks.empty:
        managers = ["전체"] + sorted(tasks["담당자"].astype(str).unique().tolist())
        selected_manager = st.selectbox("👤 담당자 선택", managers)

        filtered_tasks = tasks[tasks["담당자"] == selected_manager] if selected_manager != "전체" else tasks
        if filtered_tasks.empty:
            st.info("할 일이 없습니다.")
        else:
            task_options = {
                row["ID"]: f"{str(row['개선 필요사항'])[:30]}... ({row['공정']})"
                for _, row in filtered_tasks.iterrows()
            }
            selected_id = st.selectbox(
                "해결할 문제",
                options=list(task_options.keys()),
                format_func=lambda x: task_options[x],
            )
            target_row = filtered_tasks[filtered_tasks["ID"] == selected_id].iloc[0]

            st.divider()
            c1, c2 = st.columns([1, 2])
            with c1:
                st.caption("📸 개선 전")
                if target_row.get("사진_전"):
                    img = download_image_bytes(drive_service, target_row["사진_전"])
                    if img:
                        st.image(img, use_container_width=True)
                    else:
                        st.error("사진을 불러오지 못했습니다. (권한/파일ID 확인)")
            with c2:
                st.markdown(f"**장소:** {target_row['공정']} / **담당:** {target_row['담당자']}")
                st.info(target_row["개선 필요사항"])

            st.divider()

            with st.form("act_form"):
                atxt = st.text_area("조치 내용")
                adt = st.date_input("완료일")
                aph = st.file_uploader("조치 후 사진", type=["jpg", "jpeg", "png", "webp"])

                if st.form_submit_button("완료 저장"):
                    if not atxt.strip():
                        st.warning("내용 입력!")
                    else:
                        with st.spinner("저장 중..."):
                            photo_after_id = upload_photo(drive_service, aph) if aph else ""

                            sh = gc.open_by_url(SPREADSHEET_URL)
                            ws = sh.sheet1
                            try:
                                cell = ws.find(str(selected_id))
                                ws.update_cell(cell.row, 7, atxt)  # 개선내용
                                ws.update_cell(cell.row, 8, adt.strftime("%Y-%m-%d"))  # 개선완료일
                                ws.update_cell(cell.row, 6, "완료")  # 진행상태
                                if photo_after_id:
                                    ws.update_cell(cell.row, 10, photo_after_id)  # 사진_후

                                st.balloons()
                                st.success("저장 완료!")
                                time.sleep(2)
                                st.rerun()
                            except Exception as e:
                                st.error(f"오류 발생: {e}")
    else:
        st.info("조치할 항목이 없습니다.")
