import streamlit as st
import reprlib
st.write("GOOGLE_KEY_JSON_TEXT repr:")
st.code(reprlib.repr(st.secrets["GOOGLE_KEY_JSON_TEXT"]))
st.stop()
import pandas as pd
import time
import io
import json
import re
import urllib.request
from datetime import datetime, date

import altair as alt
from PIL import Image, ImageOps

import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build

import xlsxwriter
from supabase import create_client


# =========================================================
# 0) 설정
# =========================================================
st.set_page_config(page_title="천안공장 HACCP", layout="wide")

SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1BcMaaKnZG9q4qabwR1moRiE_QyC04jU3dZYR7grHQsc/edit?gid=0#gid=0"

# ✅ readonly만 (기존 Drive 사진 “이전(매칭)”을 하려면 drive.readonly 필요)
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


@st.cache_resource
def connect_google():
    # GOOGLE_KEY_JSON_TEXT는 secrets에 """{...json...}""" 형태로 저장
    key_dict = json.loads(st.secrets["GOOGLE_KEY_JSON_TEXT"])
    creds = service_account.Credentials.from_service_account_info(key_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service


@st.cache_resource
def connect_supabase():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])


gc, drive_service = connect_google()
sb = connect_supabase()
BUCKET = st.secrets["SUPABASE_BUCKET"]


# =========================================================
# 1) 시트 컬럼 자동 보강 (오류 최소 핵심)
# =========================================================
REQUIRED_COLUMNS = [
    "ID", "일시", "공정", "개선 필요사항",
    "발견자", "담당자", "진행상태",
    "개선계획(일정)", "개선내용", "개선완료일",
    "사진_전", "사진_후"
]


def ensure_sheet_columns(ws):
    values = ws.get_all_values()
    if not values:
        ws.update([REQUIRED_COLUMNS])
        return

    header = values[0]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        ws.update("A1", [header + missing])


def col_index(ws, col_name: str) -> int:
    header = ws.row_values(1)
    return header.index(col_name) + 1  # 1-indexed


# =========================================================
# 2) 데이터 로딩
# =========================================================
@st.cache_data(ttl=10)
def load_data():
    sh = gc.open_by_url(SPREADSHEET_URL)
    ws = sh.sheet1
    ensure_sheet_columns(ws)

    data = ws.get_all_records(value_render_option="UNFORMATTED_VALUE")
    df = pd.DataFrame(data)
    if df.empty:
        return df

    df["ID"] = df["ID"].astype(str)

    # 날짜 처리
    df["일시"] = df.get("일시", "").astype(str).str.replace(".", "-", regex=False).str.strip()
    df["일시"] = pd.to_datetime(df["일시"], errors="coerce")

    # 진행상태 기본값
    df["진행상태"] = df.get("진행상태", "").astype(str).str.strip()
    df.loc[df["진행상태"] == "", "진행상태"] = "미배정"

    df["공정"] = df.get("공정", "").astype(str).str.strip()

    # 보고서/필터용 파생
    df["Year"] = df["일시"].dt.year
    df["Month"] = df["일시"].dt.month
    df["Week"] = df["일시"].dt.isocalendar().week.astype("Int64")

    return df


# =========================================================
# 3) 사진: 압축/리사이즈 + Supabase 업로드/삭제
# =========================================================
def compress_images(files, max_side=1280, quality=68):
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
            # fallback
            try:
                f.seek(0)
                out.append((f.read(), getattr(f, "name", f"img_{int(time.time())}")))
            except Exception:
                pass
    return out


def make_path(prefix, filename):
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return f"{prefix}/{datetime.now().strftime('%Y/%m/%d')}/{int(time.time())}_{safe}"


def public_url(path):
    return f"{st.secrets['SUPABASE_URL']}/storage/v1/object/public/{BUCKET}/{path}"


def upload_many(prefix, uploaded_files):
    items = compress_images(uploaded_files)
    saved = []
    for content, name in items:
        path = make_path(prefix, name)
        try:
            sb.storage.from_(BUCKET).upload(path, content, {"content-type": "image/jpeg", "upsert": False})
            saved.append(f"{path}|{public_url(path)}")
        except Exception as e:
            st.error(f"📸 업로드 실패: {e}")
    return "\n".join(saved)


def parse_photo_field(text):
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


# =========================================================
# 4) Drive 링크 -> bytes (기존 사진 매칭/이전용)
# =========================================================
def extract_drive_file_id(url):
    if not url or "drive.google.com" not in url:
        return None
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


def migrate_drive_photos_to_supabase(ws, row_id, col_name):
    cell = ws.find(str(row_id))
    row = cell.row
    target_col = col_index(ws, col_name)

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
            new_lines.append(url)
            continue

        b = download_drive_bytes(file_id)
        if not b:
            new_lines.append(url)
            continue

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


# =========================================================
# 5) 엑셀 출력(사진 포함) - 안정: 전/후 “첫 장만”
# =========================================================
def fetch_image_bytes(url):
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            return r.read()
    except Exception:
        return None


def build_excel_with_images(df_export: pd.DataFrame):
    output = io.BytesIO()
    wb = xlsxwriter.Workbook(output, {"in_memory": True})
    wsx = wb.add_worksheet("HACCP")

    headers = REQUIRED_COLUMNS[:]  # 그대로
    header_fmt = wb.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1})
    cell_fmt = wb.add_format({"border": 1, "valign": "top", "text_wrap": True})

    for c, h in enumerate(headers):
        wsx.write(0, c, h, header_fmt)

    widths = [16, 12, 14, 42, 12, 12, 10, 14, 32, 12, 18, 18]
    for i, w in enumerate(widths):
        wsx.set_column(i, i, w)

    wsx.set_default_row(92)

    rows = df_export.to_dict("records")
    for r, row in enumerate(rows, start=1):
        for c, h in enumerate(headers):
            if h in ["사진_전", "사진_후"]:
                wsx.write(r, c, "", cell_fmt)
            else:
                wsx.write(r, c, row.get(h, ""), cell_fmt)

        # 사진: 첫 장만 삽입
        for h, col in [("사진_전", headers.index("사진_전")), ("사진_후", headers.index("사진_후"))]:
            items = parse_photo_field(row.get(h, ""))
            if not items:
                continue
            img_bytes = fetch_image_bytes(items[0]["url"])
            if not img_bytes:
                continue
            wsx.insert_image(
                r, col, "img.jpg",
                {"image_data": io.BytesIO(img_bytes), "x_scale": 0.25, "y_scale": 0.25, "x_offset": 2, "y_offset": 2}
            )

    wb.close()
    output.seek(0)
    return output


# =========================================================
# 6) 보고서(HTML) - 브라우저가 그래프 렌더링 (설치 오류 0)
# =========================================================
def room_stats(df_period):
    if df_period.empty:
        return pd.DataFrame(columns=["공정", "총발굴", "개선완료", "개선율(%)"])
    g = df_period.groupby("공정").agg(
        총발굴=("ID", "count"),
        개선완료=("진행상태", lambda x: (x == "완료").sum())
    ).reset_index()
    g["개선율(%)"] = (g["개선완료"] / g["총발굴"] * 100).fillna(0).round(1)
    return g.sort_values(["개선율(%)", "총발굴"], ascending=[False, False])


def build_report_html(title, period_text, df_period):
    total = len(df_period)
    done = int((df_period["진행상태"] == "완료").sum()) if total else 0
    rate = round(done / total * 100, 1) if total else 0.0

    rs = room_stats(df_period).head(20)

    # Vega-Lite spec 2개 (요약/실별)
    # 1) summary
    summary_data = [
        {"항목": "총 발굴건수", "건수": total},
        {"항목": "개선완료건수", "건수": done},
    ]
    spec1 = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": summary_data},
        "mark": "bar",
        "encoding": {
            "x": {"field": "항목", "type": "nominal", "axis": {"labelAngle": 0}},
            "y": {"field": "건수", "type": "quantitative"},
            "tooltip": [{"field": "항목"}, {"field": "건수"}],
        },
        "title": "총 발굴 vs 개선완료",
    }

    # 2) room bars
    room_data = rs.to_dict("records")
    spec2 = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": room_data},
        "mark": "bar",
        "encoding": {
            "x": {"field": "공정", "type": "nominal", "sort": "-y", "axis": {"labelAngle": -35}},
            "y": {"field": "총발굴", "type": "quantitative"},
            "tooltip": [{"field": "공정"}, {"field": "총발굴"}, {"field": "개선완료"}, {"field": "개선율(%)"}],
        },
        "title": "실/장소별 총 발굴(상위 20)",
    }

    # 상세 30건
    detail_cols = ["일시", "공정", "개선 필요사항", "발견자", "담당자", "진행상태", "개선계획(일정)", "개선완료일"]
    d = df_period.sort_values("일시", ascending=False).head(30).copy()
    if "일시" in d.columns:
        d["일시"] = d["일시"].dt.strftime("%Y-%m-%d")
    detail_html = d[detail_cols].to_html(index=False)

    room_html = rs.to_html(index=False)

    html = f"""
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 24px; }}
.small {{ color:#666; margin-bottom: 12px; }}
.card {{ border:1px solid #ddd; border-radius:10px; padding:14px; margin:12px 0; }}
.kpi {{ display:flex; gap:12px; }}
.kpi div {{ flex:1; border:1px solid #eee; border-radius:10px; padding:12px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border:1px solid #ddd; padding:6px; vertical-align: top; }}
th {{ background:#f5f5f5; }}
</style>

<script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
</head>
<body>
<h1>{title}</h1>
<div class="small">기간: {period_text} / 생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>

<div class="card">
  <h2>요약</h2>
  <div class="kpi">
    <div><b>총 발굴건수</b><br>{total}건</div>
    <div><b>개선완료건수</b><br>{done}건</div>
    <div><b>개선율</b><br>{rate}%</div>
  </div>
</div>

<div class="card">
  <h2>그래프</h2>
  <div id="chart1"></div>
  <div style="height:18px"></div>
  <div id="chart2"></div>
</div>

<div class="card">
  <h2>실/장소별 요약(상위 20)</h2>
  {room_html}
</div>

<div class="card">
  <h2>상세 내역(최근 30건)</h2>
  {detail_html}
</div>

<script>
const spec1 = {json.dumps(spec1, ensure_ascii=False)};
const spec2 = {json.dumps(spec2, ensure_ascii=False)};
vegaEmbed("#chart1", spec1, {{actions:false}});
vegaEmbed("#chart2", spec2, {{actions:false}});
</script>

</body>
</html>
"""
    return html


# =========================================================
# 7) UI
# =========================================================
df = load_data()
sh = gc.open_by_url(SPREADSHEET_URL)
ws = sh.sheet1
ensure_sheet_columns(ws)

st.sidebar.markdown("## ☁️ 천안공장 위생 점검 (Cloud)")
menu = st.sidebar.radio(
    "메뉴",
    ["📊 대시보드", "📝 개선과제등록", "🗓️ 개선계획수립", "✅ 개선완료 입력", "📦 엑셀 출력", "🧾 보고서(주간/월간)"]
)

st.sidebar.markdown("---")
if st.sidebar.button("🔄 새로고침"):
    st.cache_data.clear()
    st.rerun()


# =========================================================
# A) 대시보드
# =========================================================
if menu == "📊 대시보드":
    st.markdown("### 📊 천안공장 위생점검 현황")

    if df.empty:
        st.warning("데이터가 없습니다.")
    else:
        st.sidebar.markdown("### 📅 기간 필터")
        years = sorted(df["Year"].dropna().unique().tolist())
        year_options = [int(y) for y in years]
        selected_years = st.sidebar.multiselect("연도", year_options, default=year_options)

        dff = df.copy()
        if selected_years:
            dff = dff[dff["Year"].isin(selected_years)]

        months = sorted(dff["Month"].dropna().unique().tolist())
        month_options = [f"{int(m)}월" for m in months]
        selected_months_str = st.sidebar.multiselect("월", month_options, default=month_options)

        if selected_months_str:
            selected_months = [int(x.replace("월", "")) for x in selected_months_str]
            dff = dff[dff["Month"].isin(selected_months)]
        else:
            selected_months = []

        weeks = sorted(dff["Week"].dropna().unique().tolist())
        week_options = [f"{int(w)}주차" for w in weeks]
        selected_weeks_str = st.sidebar.multiselect("주차(Week)", week_options, default=week_options)
        if selected_weeks_str:
            selected_weeks = [int(x.replace("주차", "")) for x in selected_weeks_str]
            dff = dff[dff["Week"].isin(selected_weeks)]

        # KPI
        m1, m2, m3 = st.columns(3)
        total_count = len(dff)
        done_count = int((dff["진행상태"] == "완료").sum()) if total_count else 0
        rate = (done_count / total_count * 100) if total_count else 0
        m1.metric("총 발굴 건수", f"{total_count}건")
        m2.metric("개선 완료", f"{done_count}건")
        m3.metric("개선율", f"{rate:.1f}%")

        st.divider()

        # 그룹 기준: 월 여러개면 월별, 아니면 장소별
        if len(selected_months) > 1:
            dff["_grp"] = dff["Month"].astype(int).astype(str) + "월"
            x_title = "월"
        else:
            dff["_grp"] = dff["공정"].astype(str)
            x_title = "장소"

        chart_df = (
            dff.groupby("_grp")
            .agg(
                총발생=("ID", "count"),
                완료=("진행상태", lambda x: (x == "완료").sum())
            )
            .reset_index()
        )
        chart_df["완료율"] = (chart_df["완료"] / chart_df["총발생"] * 100).fillna(0).round(1)
        chart_df["라벨"] = chart_df["완료율"].astype(str) + "%"

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**🔴 총 발생 건수 ({x_title}별)**")
            ch1 = alt.Chart(chart_df).mark_bar().encode(
                x=alt.X("_grp:N", axis=alt.Axis(labelAngle=0, title=None)),
                y="총발생:Q",
                tooltip=["_grp", "총발생"]
            )
            st.altair_chart(ch1, use_container_width=True)

        with c2:
            st.markdown(f"**🟢 완료율 (%) ({x_title}별)**")
            base = alt.Chart(chart_df).encode(
                x=alt.X("_grp:N", axis=alt.Axis(labelAngle=0, title=None)),
                y=alt.Y("완료율:Q", scale=alt.Scale(domain=[0, 100]))
            )
            bars = base.mark_bar()
            text = base.mark_text(dy=-12).encode(text="라벨:N")
            st.altair_chart(bars + text, use_container_width=True)

        st.divider()

        st.markdown("**🏆 장소별 개선율 순위**")
        loc = (
            dff.groupby("공정")["진행상태"]
            .apply(lambda x: (x == "완료").mean() * 100)
            .reset_index(name="개선율(%)")
        )
        loc["개선율(%)"] = loc["개선율(%)"].fillna(0).round(1)
        st.dataframe(loc.sort_values("개선율(%)", ascending=False), hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("📋 최근 10건(사진)")

        recent = dff.sort_values("일시", ascending=False).head(10)
        for _, r in recent.iterrows():
            date_str = r["일시"].strftime("%Y-%m-%d") if pd.notnull(r["일시"]) else ""
            icon = "✅" if r["진행상태"] == "완료" else "🟠" if r["진행상태"] in ["계획수립", "진행중"] else "🔥"
            summary = str(r.get("개선 필요사항", ""))[:20]

            with st.expander(f"{icon} [{r['진행상태']}] {date_str} | {r.get('공정','')} - {summary}..."):
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


# =========================================================
# B) 개선과제등록
# =========================================================
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
                new_id = str(int(time.time()))
                before_field = upload_many("before", photos_before) if photos_before else ""
                ws.append_row([
                    new_id,
                    dt.strftime("%Y-%m-%d"),
                    loc,
                    iss,
                    finder,
                    "",          # 담당자
                    "미배정",     # 진행상태
                    "",          # 개선계획
                    "",          # 개선내용
                    "",          # 완료일
                    before_field,
                    ""
                ])
            st.success("✅ 등록 완료!")
            st.cache_data.clear()
            st.rerun()


# =========================================================
# C) 개선계획수립 (관리자)
# =========================================================
elif menu == "🗓️ 개선계획수립":
    st.markdown("### 🗓️ 개선계획수립 (관리자)")

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        targets = df[df["진행상태"].isin(["미배정", "계획수립"])].copy()
        if targets.empty:
            st.info("계획 수립할 항목이 없습니다.")
        else:
            options = {row["ID"]: f"{str(row['개선 필요사항'])[:40]}... ({row['공정']})" for _, row in targets.iterrows()}
            selected_id = st.selectbox("대상 선택", list(options.keys()), format_func=lambda x: options[x])
            row = targets[targets["ID"] == selected_id].iloc[0]

            st.divider()
            st.write(f"**ID:** {row['ID']}")
            st.write(f"**발견일:** {row['일시'].strftime('%Y-%m-%d') if pd.notnull(row['일시']) else ''}")
            st.write(f"**장소:** {row.get('공정','')}")
            st.write(f"**발견자:** {row.get('발견자','')}")
            st.info(row.get("개선 필요사항", ""))

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
                cell = ws.find(str(selected_id))
                r = cell.row
                ws.update_cell(r, col_index(ws, "담당자"), manager_pick)
                ws.update_cell(r, col_index(ws, "개선계획(일정)"), due.strftime("%Y-%m-%d"))
                ws.update_cell(r, col_index(ws, "진행상태"), status)
                st.success("✅ 계획 저장 완료!")
                st.cache_data.clear()
                st.rerun()


# =========================================================
# D) 개선완료 입력 (다중사진/삭제/Drive이전)
# =========================================================
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

                with st.expander("🧩 기존 Drive 사진을 Supabase로 이전(매칭)"):
                    st.warning("Drive 파일이 서비스계정에 공유되어 있어야 다운로드가 됩니다.")
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

                with st.expander("🗑️ 사진 삭제 (Supabase는 실제 삭제)"):
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
                            if it["kind"] == "supabase" and it["path"]:
                                delete_supabase_path(it["path"])
                            items.pop(idx)
                            new_text = "\n".join([f"{x['path']}|{x['url']}" if x["kind"] == "supabase" else x["url"] for x in items])
                            cell = ws.find(str(selected_id))
                            r = cell.row
                            ws.update_cell(r, col_index(ws, del_target), new_text)
                            st.success("삭제 완료")
                            st.cache_data.clear()
                            st.rerun()
                    else:
                        st.info("삭제할 사진이 없습니다.")

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
                        st.cache_data.clear()
                        st.rerun()


# =========================================================
# E) 엑셀 출력(사진 포함)
# =========================================================
elif menu == "📦 엑셀 출력":
    st.markdown("### 📦 엑셀 출력 (사진 포함)")
    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        status_filter = st.multiselect(
            "진행상태",
            sorted(df["진행상태"].unique().tolist()),
            default=sorted(df["진행상태"].unique().tolist())
        )
        dff = df[df["진행상태"].isin(status_filter)].copy()
        st.caption("엑셀에는 사진이 너무 많으면 문제가 생길 수 있어 **전/후 첫 장만 삽입**합니다.")

        if st.button("엑셀 생성"):
            with st.spinner("엑셀 생성 중..."):
                dff2 = dff.copy()
                dff2["일시"] = dff2["일시"].dt.strftime("%Y-%m-%d")
                bio = build_excel_with_images(dff2[REQUIRED_COLUMNS])
                st.download_button(
                    "⬇️ 다운로드",
                    data=bio,
                    file_name=f"haccp_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )


# =========================================================
# F) 보고서(주간/월간/기간) - HTML(그래프 포함)
# =========================================================
elif menu == "🧾 보고서(주간/월간)":
    st.markdown("### 🧾 보고서 출력 (주간/월간/기간 선택)")

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        mode = st.radio("보고서 단위", ["주간", "월간", "기간(직접선택)"], horizontal=True)
        dff = df.copy()

        if mode == "주간":
            years = sorted(dff["Year"].dropna().unique().tolist())
            y = st.selectbox("연도", years, index=len(years) - 1 if years else 0)
            tmp = dff[dff["Year"] == y]
            weeks = sorted(tmp["Week"].dropna().unique().tolist())
            w = st.selectbox("주차(ISO Week)", weeks, index=len(weeks) - 1 if weeks else 0)
            df_period = dff[(dff["Year"] == y) & (dff["Week"] == w)].copy()
            period_text = f"{y}년 {w}주차"

        elif mode == "월간":
            years = sorted(dff["Year"].dropna().unique().tolist())
            y = st.selectbox("연도", years, index=len(years) - 1 if years else 0)
            tmp = dff[dff["Year"] == y]
            months = sorted(tmp["Month"].dropna().unique().tolist())
            m = st.selectbox("월", months, index=len(months) - 1 if months else 0)
            df_period = dff[(dff["Year"] == y) & (dff["Month"] == m)].copy()
            period_text = f"{y}년 {m}월"

        else:
            start = st.date_input("시작일", value=date.today().replace(day=1))
            end = st.date_input("종료일", value=date.today())
            df_period = dff[(dff["일시"] >= pd.to_datetime(start)) & (dff["일시"] <= pd.to_datetime(end))].copy()
            period_text = f"{start} ~ {end}"

        total = len(df_period)
        done = int((df_period["진행상태"] == "완료").sum()) if total else 0
        rate = round(done / total * 100, 1) if total else 0.0

        st.info(f"기간: {period_text} | 총 발굴 {total}건 / 개선완료 {done}건 / 개선율 {rate}%")

        # 화면 미리보기 그래프(Altair)
        chart1_df = pd.DataFrame([{"항목": "총 발굴", "건수": total}, {"항목": "개선완료", "건수": done}])
        ch1 = alt.Chart(chart1_df).mark_bar().encode(x="항목:N", y="건수:Q", tooltip=["항목", "건수"]).properties(title="총 발굴 vs 개선완료")
        st.altair_chart(ch1, use_container_width=True)

        rs = room_stats(df_period)
        st.dataframe(rs, hide_index=True, use_container_width=True)

        st.divider()

        html = build_report_html("천안공장 위생점검 보고서", period_text, df_period)
        st.download_button(
            "⬇️ 보고서 다운로드 (HTML)",
            data=html.encode("utf-8"),
            file_name=f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
            mime="text/html"
        )
        st.caption("HTML을 열어서 인쇄 → PDF 저장하면 ‘보고서’로 바로 제출 가능합니다.")
