import streamlit as st
import pandas as pd
import time
import io
import json
import re
import urllib.request
from datetime import datetime, date
from PIL import Image, ImageOps

import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build

import xlsxwriter
from supabase import create_client

# =========================
# 0) 기본 설정
# =========================
st.set_page_config(page_title="천안공장 HACCP", layout="wide")

SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1BcMaaKnZG9q4qabwR1moRiE_QyC04jU3dZYR7grHQsc/edit?gid=0#gid=0"

# Drive 기존 사진(구글드라이브 링크)을 Supabase로 옮길 때만 drive.readonly 필요
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

REQUIRED_SECRETS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_BUCKET", "GOOGLE_KEY_JSON_TEXT"]

def require_secrets():
    missing = [k for k in REQUIRED_SECRETS if k not in st.secrets]
    if missing:
        st.error(f"🚨 Secrets 설정이 없습니다: {', '.join(missing)}")
        st.stop()

require_secrets()


# =========================
# 1) Google / Supabase 연결
# =========================
@st.cache_resource
def connect_google():
    key_dict = json.loads(st.secrets["GOOGLE_KEY_JSON_TEXT"])
    creds = service_account.Credentials.from_service_account_info(key_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service

@st.cache_resource
def get_supabase():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])

gc, drive_service = connect_google()
sb = get_supabase()
BUCKET = st.secrets["SUPABASE_BUCKET"]


# =========================
# 2) 유틸: 컬럼 보강(새 컬럼 자동 생성)
# =========================
REQUIRED_COLUMNS = [
    "ID", "일시", "공정", "개선 필요사항", "발견자", "담당자", "진행상태",
    "개선계획(일정)", "개선내용", "개선완료일", "사진_전", "사진_후"
]

def ensure_sheet_columns(ws):
    values = ws.get_all_values()
    if not values:
        ws.update([REQUIRED_COLUMNS])
        return

    header = values[0]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if not missing:
        return

    new_header = header + missing
    ws.update("A1", [new_header])

def col_index(ws, col_name):
    header = ws.row_values(1)
    return header.index(col_name) + 1  # 1-indexed


# =========================
# 3) 데이터 로딩
# =========================
@st.cache_data(ttl=10)
def load_data():
    sh = gc.open_by_url(SPREADSHEET_URL)
    ws = sh.sheet1
    ensure_sheet_columns(ws)
    data = ws.get_all_records(value_render_option="UNFORMATTED_VALUE")
    df = pd.DataFrame(data)

    if df.empty:
        return df

    # 날짜 파싱
    if "일시" in df.columns:
        df["일시"] = df["일시"].astype(str).str.replace(".", "-", regex=False).str.strip()
        df["일시"] = pd.to_datetime(df["일시"], errors="coerce")
        df["일시"] = df["일시"].fillna(pd.Timestamp("1900-01-01"))
        df["Year"] = df["일시"].dt.year
        df["Month"] = df["일시"].dt.month
        df["Week"] = df["일시"].dt.isocalendar().week.astype(int)

    # 상태 기본값
    if "진행상태" in df.columns:
        df["진행상태"] = df["진행상태"].astype(str).str.strip().replace({"": "미배정"})
    else:
        df["진행상태"] = "미배정"

    return df


# =========================
# 4) 이미지: 압축/리사이즈 (강화 버전)
# =========================
def compress_images(files, max_side=1280, quality=68):
    """UploadedFile list -> list of (bytes, filename)"""
    out = []
    for f in files or []:
        try:
            img = Image.open(f)
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            buf.seek(0)
            name = re.sub(r"\s+", "_", getattr(f, "name", f"img_{int(time.time())}.jpg"))
            out.append((buf.read(), name))
        except Exception:
            # 실패하면 원본 그대로
            try:
                f.seek(0)
                out.append((f.read(), getattr(f, "name", f"img_{int(time.time())}")))
            except Exception:
                pass
    return out


# =========================
# 5) Supabase Storage 업로드/삭제/URL 처리
# =========================
def make_path(prefix, filename):
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return f"{prefix}/{datetime.now().strftime('%Y/%m/%d')}/{int(time.time())}_{safe}"

def public_url(path):
    # SDK 버전별 차이를 피해 안전하게 URL 생성
    return f"{st.secrets['SUPABASE_URL']}/storage/v1/object/public/{BUCKET}/{path}"

def upload_many(prefix, uploaded_files):
    """여러 장 업로드 -> 'path|url' 을 줄바꿈으로 반환"""
    items = compress_images(uploaded_files)
    saved = []
    for content, name in items:
        path = make_path(prefix, name)
        try:
            sb.storage.from_(BUCKET).upload(
                path,
                content,
                {"content-type": "image/jpeg", "upsert": False},
            )
            saved.append(f"{path}|{public_url(path)}")
        except Exception as e:
            st.error(f"📸 업로드 실패: {e}")
    return "\n".join(saved)

def parse_photo_field(text):
    """
    사진 필드 형식:
    - Supabase: 'path|url' 줄바꿈 여러 개
    - Drive/기타: url 줄바꿈 여러 개
    반환: list of dict {kind, path, url}
    """
    if not text:
        return []
    lines = [l.strip() for l in str(text).splitlines() if l.strip()]
    out = []
    for line in lines:
        if "|" in line:
            path, url = line.split("|", 1)
            out.append({"kind": "supabase", "path": path.strip(), "url": url.strip()})
        else:
            out.append({"kind": "url", "path": "", "url": line})
    return out

def delete_supabase_path(path):
    try:
        sb.storage.from_(BUCKET).remove([path])
        return True
    except Exception as e:
        st.error(f"삭제 실패: {e}")
        return False


# =========================
# 6) Drive 링크 -> 파일ID 추출 & 다운로드
# =========================
def extract_drive_file_id(url):
    if not url or "drive.google.com" not in url:
        return None
    # /d/<id>/ or id=<id>
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None

def download_drive_bytes(file_id):
    try:
        return drive_service.files().get_media(fileId=file_id).execute()
    except Exception:
        return None


# =========================
# 7) 기존 Drive 사진을 Supabase로 “매칭(이전)”
# =========================
def migrate_drive_photos_to_supabase(ws, row_id, col_name):
    """
    특정 row의 특정 컬럼(사진_전/사진_후)에서
    drive.google.com 링크를 찾아 supabase로 업로드 후
    cell을 supabase 형식(path|url)로 교체
    """
    header = ws.row_values(1)
    id_col = header.index("ID") + 1
    target_col = header.index(col_name) + 1

    # ID 찾기
    cell = ws.find(str(row_id))
    row = cell.row

    current = ws.cell(row, target_col).value
    photos = parse_photo_field(current)

    new_lines = []
    changed = False

    for p in photos:
        if p["kind"] == "supabase":
            new_lines.append(f"{p['path']}|{p['url']}")
            continue

        url = p["url"]
        file_id = extract_drive_file_id(url)
        if not file_id:
            # 그냥 URL은 유지
            new_lines.append(url)
            continue

        b = download_drive_bytes(file_id)
        if not b:
            new_lines.append(url)
            continue

        # 업로드
        path = make_path("migrated", f"{file_id}.jpg")
        try:
            sb.storage.from_(BUCKET).upload(path, b, {"content-type": "image/jpeg", "upsert": False})
            new_lines.append(f"{path}|{public_url(path)}")
            changed = True
        except Exception:
            new_lines.append(url)

    if changed:
        ws.update_cell(row, target_col, "\n".join(new_lines))
    return changed


# =========================
# 8) 엑셀 출력(사진 포함)
# =========================
def fetch_image_bytes(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read()
    except Exception:
        return None

def build_excel_with_images(df):
    """
    df의 사진_전/사진_후에 들어있는 URL들을 다운받아 삽입.
    사진은 너무 크면 엑셀 열이 망가지므로 썸네일 형태로 삽입.
    """
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    wsx = workbook.add_worksheet("HACCP")

    # 헤더
    headers = [
        "ID", "일시", "공정", "개선 필요사항", "발견자", "담당자", "진행상태",
        "개선계획(일정)", "개선내용", "개선완료일", "사진_전", "사진_후"
    ]
    header_fmt = workbook.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1})
    cell_fmt = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True})

    for c, h in enumerate(headers):
        wsx.write(0, c, h, header_fmt)

    # column width
    widths = [18, 12, 14, 40, 12, 12, 10, 14, 30, 12, 18, 18]
    for i, w in enumerate(widths):
        wsx.set_column(i, i, w)

    # 이미지 삽입을 위해 행 높이 지정
    wsx.set_default_row(90)

    for r, row in enumerate(df.to_dict("records"), start=1):
        for c, h in enumerate(headers):
            if h in ["사진_전", "사진_후"]:
                wsx.write(r, c, "", cell_fmt)
            else:
                v = row.get(h, "")
                # 날짜 보기 좋게
                if isinstance(v, (datetime, pd.Timestamp)):
                    v = v.strftime("%Y-%m-%d")
                wsx.write(r, c, v, cell_fmt)

        # 이미지: 전/후 첫 장만 삽입 (여러 장은 엑셀 크기 폭발 방지)
        for h, col in [("사진_전", 10), ("사진_후", 11)]:
            items = parse_photo_field(row.get(h, ""))
            if not items:
                continue
            url = items[0]["url"]
            img_bytes = fetch_image_bytes(url)
            if not img_bytes:
                continue

            wsx.insert_image(
                r, col, "img.jpg",
                {
                    "image_data": io.BytesIO(img_bytes),
                    "x_scale": 0.25,
                    "y_scale": 0.25,
                    "x_offset": 2,
                    "y_offset": 2,
                }
            )

    workbook.close()
    output.seek(0)
    return output


# =========================
# 9) UI
# =========================
df = load_data()

st.sidebar.markdown("## ☁️ 천안공장 위생 점검 (Cloud)")

menu = st.sidebar.radio(
    "메뉴",
    ["📊 대시보드", "📝 개선과제등록", "🗓️ 개선계획수립", "✅ 개선완료 입력", "📦 엑셀 출력"]
)

st.sidebar.markdown("---")
if st.sidebar.button("🔄 새로고침"):
    st.cache_data.clear()
    st.rerun()


# =========================
# 10) 공통: 시트 객체
# =========================
sh = gc.open_by_url(SPREADSHEET_URL)
ws = sh.sheet1
ensure_sheet_columns(ws)


# =========================
# 11) 대시보드
# =========================
if menu == "📊 대시보드":
    st.markdown("### 📊 천안공장 위생점검 현황")

    if df.empty:
        st.warning("데이터가 없습니다.")
    else:
        # 기간 필터
        st.sidebar.markdown("### 📅 기간 필터")
        years = sorted(df["Year"].dropna().unique()) if "Year" in df.columns else []
        year_options = [int(y) for y in years]
        selected_years = st.sidebar.multiselect("연도", year_options, default=year_options)

        dff = df.copy()
        if selected_years:
            dff = dff[dff["Year"].isin(selected_years)]

        months = sorted(dff["Month"].dropna().unique()) if "Month" in dff.columns else []
        month_options = [f"{int(m)}월" for m in months]
        selected_months = st.sidebar.multiselect("월", month_options, default=month_options)
        if selected_months:
            mm = [int(x.replace("월", "")) for x in selected_months]
            dff = dff[dff["Month"].isin(mm)]

        weeks = sorted(dff["Week"].dropna().unique()) if "Week" in dff.columns else []
        week_options = [f"{int(w)}주차" for w in weeks]
        selected_weeks = st.sidebar.multiselect("주차", week_options, default=week_options)
        if selected_weeks:
            ww = [int(x.replace("주차", "")) for x in selected_weeks]
            dff = dff[dff["Week"].isin(ww)]

        m1, m2, m3, m4 = st.columns(4)
        total = len(dff)
        done = len(dff[dff["진행상태"] == "완료"])
        planned = len(dff[dff["진행상태"].isin(["계획수립", "진행중"])])
        rate = (done / total * 100) if total else 0
        m1.metric("총 건수", f"{total}건")
        m2.metric("진행(계획/진행중)", f"{planned}건")
        m3.metric("완료", f"{done}건")
        m4.metric("개선율", f"{rate:.1f}%")

        st.divider()

        # 최근 10건
        st.subheader("📋 최근 10건")
        recent = dff.iloc[::-1].head(10)
        for _, r in recent.iterrows():
            date_str = r["일시"].strftime("%Y-%m-%d") if pd.notnull(r.get("일시")) else ""
            icon = "✅" if r.get("진행상태") == "완료" else "🟠" if r.get("진행상태") in ["계획수립", "진행중"] else "🔥"
            summary = str(r.get("개선 필요사항", ""))[:20]

            with st.expander(f"{icon} [{r.get('진행상태','')}] {date_str} | {r.get('공정','')} - {summary}..."):
                c1, c2, c3 = st.columns([1, 1, 2])

                def show_photos(field, title):
                    st.caption(title)
                    items = parse_photo_field(r.get(field, ""))
                    if not items:
                        st.info("사진 없음")
                        return
                    for it in items:
                        st.image(it["url"], use_container_width=True)

                with c1:
                    show_photos("사진_전", "❌ 전(여러장)")

                with c2:
                    show_photos("사진_후", "✅ 후(여러장)")

                with c3:
                    st.markdown(f"**내용:** {r.get('개선 필요사항','')}")
                    st.markdown(f"**발견자:** {r.get('발견자','')}")
                    st.markdown(f"**담당자:** {r.get('담당자','')}")
                    st.markdown(f"**개선계획(일정):** {r.get('개선계획(일정)','')}")
                    if str(r.get("개선내용", "")).strip():
                        st.info(f"조치: {r.get('개선내용','')}")


# =========================
# 12) 개선과제등록 (발견자/품질팀)
# =========================
elif menu == "📝 개선과제등록":
    st.markdown("### 📝 개선과제등록")

    with st.form("reg_form"):
        dt = st.date_input("발견일", value=date.today())
        loc = st.selectbox("장소", ["전처리실", "입국실", "발효실", "제성실", "병입/포장실", "원료창고", "제품창고", "실험실", "화장실/탈의실", "기타"])
        finder = st.text_input("발견자(품질팀/발견자 이름)")
        iss = st.text_area("개선 필요사항(내용)")
        photos_before = st.file_uploader("사진(개선 전) 여러장 가능", type=["jpg", "jpeg", "png"], accept_multiple_files=True)

        submitted = st.form_submit_button("등록")

    if submitted:
        if not iss.strip():
            st.warning("내용을 입력해주세요.")
        else:
            with st.spinner("등록 중..."):
                new_id = int(time.time())
                before_field = upload_many("before", photos_before) if photos_before else ""
                # 진행상태: 미배정
                ws.append_row([
                    str(new_id),
                    dt.strftime("%Y-%m-%d"),
                    loc,
                    iss,
                    finder,
                    "",                 # 담당자(관리자가 계획에서 지정)
                    "미배정",            # 진행상태
                    "",                 # 개선계획(일정)
                    "",                 # 개선내용
                    "",                 # 개선완료일
                    before_field,       # 사진_전
                    ""                  # 사진_후
                ])
            st.success("✅ 등록 완료!")
            st.balloons()
            st.cache_data.clear()
            st.rerun()


# =========================
# 13) 개선계획수립 (관리자용)
# =========================
elif menu == "🗓️ 개선계획수립":
    st.markdown("### 🗓️ 개선계획수립 (관리자)")

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        # 미배정/계획수립 대상
        targets = df[df["진행상태"].isin(["미배정", "계획수립"])].copy()
        if targets.empty:
            st.info("계획 수립할 항목이 없습니다.")
        else:
            # 선택
            options = {row["ID"]: f"{str(row['개선 필요사항'])[:40]}... ({row['공정']})" for _, row in targets.iterrows()}
            selected_id = st.selectbox("대상 선택", list(options.keys()), format_func=lambda x: options[x])
            row = targets[targets["ID"] == selected_id].iloc[0]

            st.divider()
            st.write(f"**ID:** {row['ID']}")
            st.write(f"**발견일:** {row['일시'].strftime('%Y-%m-%d') if pd.notnull(row['일시']) else ''}")
            st.write(f"**장소:** {row.get('공정','')}")
            st.write(f"**발견자:** {row.get('발견자','')}")
            st.info(row.get("개선 필요사항", ""))

            # 사진(전) 미리보기
            items = parse_photo_field(row.get("사진_전", ""))
            if items:
                st.caption("📸 개선 전(여러장)")
                for it in items:
                    st.image(it["url"], use_container_width=True)

            st.divider()

            with st.form("plan_form"):
                manager_pick = st.text_input("담당자 지정", value=str(row.get("담당자", "") or ""))
                due = st.date_input("개선일정(목표 완료일)", value=date.today())
                status = st.selectbox("진행상태", ["계획수립", "진행중"], index=0)
                save = st.form_submit_button("계획 저장")

            if save:
                # 시트 row 찾기
                cell = ws.find(str(selected_id))
                r = cell.row
                ws.update_cell(r, col_index(ws, "담당자"), manager_pick)
                ws.update_cell(r, col_index(ws, "개선계획(일정)"), due.strftime("%Y-%m-%d"))
                ws.update_cell(r, col_index(ws, "진행상태"), status)
                st.success("✅ 계획 저장 완료!")
                st.cache_data.clear()
                time.sleep(1)
                st.rerun()


# =========================
# 14) 개선완료 입력 (조치/후 사진/삭제/교체)
# =========================
elif menu == "✅ 개선완료 입력":
    st.markdown("### ✅ 개선완료 입력")

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        tasks = df[df["진행상태"] != "완료"].copy()
        if tasks.empty:
            st.info("완료 입력할 항목이 없습니다.")
        else:
            managers = ["전체"] + sorted(tasks["담당자"].astype(str).fillna("").unique().tolist())
            selected_manager = st.selectbox("담당자 필터", managers)
            if selected_manager != "전체":
                tasks = tasks[tasks["담당자"].astype(str) == selected_manager]

            if tasks.empty:
                st.info("해당 담당자의 항목이 없습니다.")
            else:
                options = {row["ID"]: f"{str(row['개선 필요사항'])[:40]}... ({row['공정']})" for _, row in tasks.iterrows()}
                selected_id = st.selectbox("대상 선택", list(options.keys()), format_func=lambda x: options[x])
                row = tasks[tasks["ID"] == selected_id].iloc[0]

                st.divider()
                c1, c2 = st.columns([1, 1])

                def render_field(field, title):
                    st.caption(title)
                    items = parse_photo_field(row.get(field, ""))
                    if not items:
                        st.info("사진 없음")
                        return items
                    for it in items:
                        st.image(it["url"], use_container_width=True)
                    return items

                with c1:
                    before_items = render_field("사진_전", "📸 개선 전(여러장)")
                with c2:
                    after_items = render_field("사진_후", "📸 개선 후(여러장)")

                st.divider()
                st.write(f"**장소:** {row.get('공정','')} / **담당자:** {row.get('담당자','')}")
                st.info(row.get("개선 필요사항", ""))

                # (관리자/담당자) 기존 사진을 Supabase로 이전(매칭)
                with st.expander("🧩 기존 Drive 사진을 Supabase로 이전(매칭)"):
                    st.warning("주의: Drive 파일이 서비스계정(haccp-bot)에게 공유되어 있어야 다운로드가 됩니다.")
                    if st.button("사진_전 Drive→Supabase 이전"):
                        changed = migrate_drive_photos_to_supabase(ws, selected_id, "사진_전")
                        st.success("완료" if changed else "변경 없음(다운로드 불가 또는 이미 이전됨)")
                        st.cache_data.clear()
                        st.rerun()
                    if st.button("사진_후 Drive→Supabase 이전"):
                        changed = migrate_drive_photos_to_supabase(ws, selected_id, "사진_후")
                        st.success("완료" if changed else "변경 없음(다운로드 불가 또는 이미 이전됨)")
                        st.cache_data.clear()
                        st.rerun()

                # 사진 삭제/교체
                with st.expander("🗑️ 사진 삭제/교체 (Supabase만 완전 삭제 가능)"):
                    st.caption("Supabase 사진은 path|url 형식이라 삭제 가능. Drive/외부 URL은 링크만 제거됩니다.")
                    # 사진_전 삭제
                    del_target = st.selectbox("삭제 대상", ["사진_전", "사진_후"])
                    items = parse_photo_field(row.get(del_target, ""))

                    if items:
                        labels = []
                        for i, it in enumerate(items):
                            tag = "Supabase" if it["kind"] == "supabase" else "URL"
                            labels.append((i, f"{tag} - {it['url'][:60]}..."))
                        idx = st.selectbox("삭제할 사진 선택", [x[0] for x in labels], format_func=lambda i: dict(labels)[i])

                        if st.button("선택 사진 삭제"):
                            it = items[idx]
                            # supabase면 실제 삭제
                            if it["kind"] == "supabase" and it["path"]:
                                delete_supabase_path(it["path"])
                            # 목록에서 제거
                            items.pop(idx)
                            # 다시 저장
                            new_text = "\n".join([f"{x['path']}|{x['url']}" if x["kind"]=="supabase" else x["url"] for x in items])
                            cell = ws.find(str(selected_id))
                            r = cell.row
                            ws.update_cell(r, col_index(ws, del_target), new_text)
                            st.success("삭제 완료")
                            st.cache_data.clear()
                            st.rerun()

                    # 교체(간단히: 삭제 후 업로드)
                    st.caption("교체는: 기존 삭제 → 새 사진 업로드(아래 완료 입력에서 업로드) 방식으로 처리합니다.")

                # 완료 입력 폼
                with st.form("done_form"):
                    atxt = st.text_area("조치 내용(개선내용)")
                    adt = st.date_input("완료일", value=date.today())
                    photos_after = st.file_uploader("조치 후 사진(여러장)", type=["jpg", "jpeg", "png"], accept_multiple_files=True)
                    mark_done = st.checkbox("완료 처리", value=True)
                    save = st.form_submit_button("저장")

                if save:
                    if not atxt.strip():
                        st.warning("조치 내용을 입력해주세요.")
                    else:
                        with st.spinner("저장 중..."):
                            cell = ws.find(str(selected_id))
                            r = cell.row

                            # 기존 후 사진 유지 + 추가 업로드(append)
                            existing_after = ws.cell(r, col_index(ws, "사진_후")).value
                            existing_lines = [l.strip() for l in str(existing_after).splitlines() if l.strip()] if existing_after else []
                            new_after = upload_many("after", photos_after) if photos_after else ""
                            if new_after:
                                existing_lines.extend([l.strip() for l in new_after.splitlines() if l.strip()])
                            ws.update_cell(r, col_index(ws, "사진_후"), "\n".join(existing_lines))

                            ws.update_cell(r, col_index(ws, "개선내용"), atxt)
                            ws.update_cell(r, col_index(ws, "개선완료일"), adt.strftime("%Y-%m-%d"))
                            ws.update_cell(r, col_index(ws, "진행상태"), "완료" if mark_done else "진행중")

                        st.success("✅ 저장 완료!")
                        st.balloons()
                        st.cache_data.clear()
                        time.sleep(1)
                        st.rerun()


# =========================
# 15) 엑셀 출력 (사진 포함)
# =========================
elif menu == "📦 엑셀 출력":
    st.markdown("### 📦 엑셀 출력 (사진 포함)")

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        # 기간/상태 필터
        status_filter = st.multiselect("진행상태", sorted(df["진행상태"].unique().tolist()), default=sorted(df["진행상태"].unique().tolist()))
        dff = df[df["진행상태"].isin(status_filter)].copy()

        st.caption("엑셀에는 사진이 너무 크면 문제가 되므로 **전/후 첫 장만** 썸네일로 삽입됩니다.")
        if st.button("엑셀 생성"):
            with st.spinner("엑셀 생성 중..."):
                # 필요한 컬럼만 정리
                cols = ["ID","일시","공정","개선 필요사항","발견자","담당자","진행상태","개선계획(일정)","개선내용","개선완료일","사진_전","사진_후"]
                dff2 = dff.copy()
                # 날짜 문자열화
                if "일시" in dff2.columns:
                    dff2["일시"] = dff2["일시"].apply(lambda x: x.strftime("%Y-%m-%d") if isinstance(x, (pd.Timestamp, datetime)) else str(x))
                bio = build_excel_with_images(dff2[cols])
                st.download_button(
                    "⬇️ 다운로드",
                    data=bio,
                    file_name=f"haccp_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

# =========================
# (참고) Supabase DB 완전 이전 로드맵
# =========================
# 1) Supabase에 tasks 테이블 생성(SQL)
# 2) 시트 데이터 읽어서 supabase upsert
# 3) 앱에서 gspread 대신 supabase select/insert/update 사용
# 4) 권한/로그인/감사로그까지 확장 가능
