"""
UNI-PASS 통관 백엔드 (웹 대시보드용).

기존 데스크톱 앱 `통합_수입관리_트래킹.pyw` 의 tkinter 를 걷어내고,
관세청 API 호출 / 구글시트 트래킹 로직만 그대로 이식했습니다.
forecast_tool.py 의 serve 서버가 이 모듈의 함수들을 호출해 JSON 으로 응답합니다.

- 인증키는 customs_config.json (이 파일과 같은 폴더) 에 저장되며 클라이언트로는 절대 나가지 않습니다.
- 구글시트 연동은 gspread OAuth 토큰(기존 앱과 동일, %APPDATA%/gspread/authorized_user.json)을 재사용합니다.
"""
import csv
import io
import json
import os
import re
import datetime
import urllib.parse
import urllib.request
import urllib.error
from xml.etree import ElementTree

try:
    import gspread
except ImportError:
    gspread = None

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "customs_config.json")

ENDPOINTS = {
    "API001": "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo",
    "API005": "https://unipass.customs.go.kr:38010/ext/rest/shedInfoQry/retrieveShedInfo",
    "API012": "https://unipass.customs.go.kr:38010/ext/rest/trifFxrtInfoQry/retrieveTrifFxrtInfo",
    "API018": "https://unipass.customs.go.kr:38010/ext/rest/hsSgnQry/searchHsSgn",
    "API020": "https://unipass.customs.go.kr:38010/ext/rest/cntrQryBrkdQry/retrieveCntrQryBrkd",
    "API030": "https://unipass.customs.go.kr:38010/ext/rest/trrtQry/retrieveTrrt",
    "API047": "https://unipass.customs.go.kr:38010/ext/rest/bdgdFccmShedQry/retrievebdgdFccmShed",
}

ITEM_TAGS = {
    "API001": "cargCsclPrgsInfoQryVo",
    "API005": "shedInfoQryRsltVo",
    "API012": "trifFxrtInfoQryRsltVo",
    "API018": "hsSgnSrchRsltVo",
    "API020": "cntrQryBrkdQryVo",
    "API030": "TrrtQryRsltVo",
    "API047": "bdgdFccmShedQryRsltVo",
}

# ── 구글시트(트래킹) 설정 ──
SPREADSHEET_ID = "1pM4TGaPVPRzrkiuANKA5EkaDihr3rbrw1SLaC619WZY"
TRACKING_WS = "통관트래킹"
ORDER_HEADER = ["주문명", "BL번호", "선적일", "예상입항일", "품명", "개당가격(EUR)", "소비기한",
                "산도(%)", "배치넘버", "넷중량(KG)", "총중량(KG)"]
TRACKING_HEADER = ["BL번호", "제조사", "주문명", "선적일", "예상입항일", "화물관리번호",
                   "상태", "통관진행상태", "통관진행사항", "입항일", "반출일", "갱신시각",
                   "수입신고", "이상사항"]
# 예전엔 이 키워드 목록으로 판정했다. 맨 끝의 "수리" 가 '하선신고수리'·'입항보고수리'
# 까지 잡아서 '수입신고전' 인 건이 통관완료로 올라갔다. 지금은 is_cleared_text() 등
# 문구 단위 판정을 쓴다. 다시 만들지 말 것.
# 처리 타임라인의 상태 문구가 담기는 필드들 (이 세 개만 단계 판정에 쓴다)
STATUS_FIELDS = ("csclPrgsStts", "prgsStts", "cargTrcnRelaBsopTpcd")
ANOMALY_KEYS = ["검사", "통관보류", "보류", "정정", "반송", "취하", "각하", "오류",
                "서류제출", "심사", "요구", "반입정정"]
STAGE_ARRIVAL_EXPECTED = "입항예정"
STAGE_IN_PROGRESS = "통관진행중"
STAGE_DONE = "통관완료"
STAGES = [STAGE_ARRIVAL_EXPECTED, STAGE_IN_PROGRESS, STAGE_DONE]


# ── 설정(인증키) 저장/로드 ──
def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("keys", {})
    data.setdefault("sheet_url", "")
    return data


def save_config(data: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_key(config: dict, api_id: str) -> str:
    return config.get("keys", {}).get(api_id, "")


def extract_sheet_id(url_or_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()


def sync_keys_from_sheet(url_or_id: str) -> dict:
    sheet_id = extract_sheet_id(url_or_id)
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    try:
        with urllib.request.urlopen(csv_url, timeout=20) as resp:
            raw = resp.read().decode("utf-8-sig", errors="replace")
    except urllib.error.URLError as e:
        raise RuntimeError(f"구글시트 접속 실패: {e}") from e

    reader = csv.reader(io.StringIO(raw))
    new_keys = {}
    for row in reader:
        if len(row) < 6:
            continue
        api_id = row[1].strip()
        key_val = row[5].strip()
        if re.match(r"^API\d+$", api_id) and key_val:
            new_keys[api_id] = key_val
    if not new_keys:
        raise RuntimeError("시트에서 인증키를 찾지 못했습니다. B열=API ID, F열=인증키 형식인지 확인해주세요.")
    return new_keys


# ── 공통 호출/파싱 ──
def call_api(api_id: str, params: dict) -> list:
    key = params.pop("__key__")
    if not key:
        raise RuntimeError(f"{api_id} 인증키가 없습니다. 구글시트 동기화를 먼저 해주세요.")
    all_params = {"crkyCn": key}
    all_params.update({k: v for k, v in params.items() if v})
    url = ENDPOINTS[api_id] + "?" + urllib.parse.urlencode(all_params)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"네트워크 오류({api_id}): {e}") from e
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raw = body.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{api_id} 응답 파싱 실패: {e}\n[원본 응답] {raw}") from e

    item_tag = ITEM_TAGS[api_id]
    results = [{child.tag: (child.text or "") for child in vo} for vo in root.findall(f".//{item_tag}")]
    if not results:
        notice = root.findtext(".//ntceInfo")
        result_code = root.findtext(".//resultCode")
        result_msg = root.findtext(".//resultMsg")
        err_msg = root.findtext(".//errMsgCn")
        parts = []
        if notice:
            parts.append(f"안내: {notice}")
        if result_code or result_msg:
            parts.append(f"결과코드: {result_code} / {result_msg}")
        if err_msg:
            parts.append(f"오류메시지: {err_msg}")
        detail = " / ".join(parts) if parts else "(서버가 안내 메시지를 주지 않음)"
        raise RuntimeError(f"{api_id} 조회 결과 없음 - {detail}")
    return results


# ── API001 요약/타임라인 (웹용 구조화) ──
def format_api001_structured(results: list) -> dict:
    summary_fields = [
        ("csclPrgsStts", "통관진행상태"), ("prgsStts", "진행상태"), ("prcsDttm", "처리일시"),
        ("prnm", "품명"), ("dclrNo", "신고번호"), ("shedSgn", "장치장부호"), ("shedNm", "장치장명"),
        ("etprDt", "입항일자"), ("cntrNo", "컨테이너번호"), ("frwrEntsConm", "포워더명"),
        ("cargMtNo", "화물관리번호"),
    ]
    summary = []
    for k, label in summary_fields:
        val = next((r.get(k) for r in results if r.get(k)), "")
        if val:
            summary.append({"label": label, "value": val})

    timeline = []
    for r in results:
        stage = r.get("cargTrcnRelaBsopTpcd") or r.get("prgsStts") or r.get("csclPrgsStts") or ""
        when = r.get("prcsDttm") or r.get("rlbrDttm") or ""
        extra = []
        if r.get("shedNm"):
            extra.append(f"장치장:{r['shedNm']}")
        if r.get("pckGcnt"):
            extra.append(f"포장:{r['pckGcnt']}{r.get('pckUt', '')}")
        if r.get("rlbrCn"):
            extra.append(f"반출입:{r['rlbrCn']}")
        if r.get("dclrNo"):
            extra.append(f"신고번호:{r['dclrNo']}")
        timeline.append({"when": when, "stage": stage, "extra": " | ".join(extra)})

    chain = {
        "shed_sgn": next((r.get("shedSgn") for r in results if r.get("shedSgn")), ""),
        "etpr_dt": next((r.get("etprDt") for r in results if r.get("etprDt")), ""),
        "carg_mt_no": next((r.get("cargMtNo") for r in results if r.get("cargMtNo")), ""),
    }
    return {"summary": summary, "timeline": timeline, "chain": chain}


# ── 단계/상태 계산 ──
def _blob(results: list) -> str:
    return " ".join(
        " ".join([r.get("csclPrgsStts", "") or "", r.get("prgsStts", "") or "",
                  r.get("cargTrcnRelaBsopTpcd", "") or "", r.get("rlbrCn", "") or "",
                  r.get("rmk", "") or ""])
        for r in results
    )


# ── 수입신고 판정 (처리 타임라인 문구 기준) ──
# 예전에는 blob 전체에서 "수리" 를 찾았는데, 이러면 '하선신고수리'·'입항보고수리' 같은
# 수입신고와 무관한 수리까지 걸려서 '수입신고전' 인 건이 통관완료로 올라갔다.
# 그래서 상태 문구를 한 줄씩 보고, 아래 규칙으로만 판정한다.
def is_cleared_text(t: str) -> bool:
    """수입신고수리가 실제로 떨어진 문구인지.

    '수입신고수리전반출' 처럼 아직 수리 전인 문구는 완료로 보지 않는다.
    """
    if "수입신고수리전" in t:
        return False
    return "수입신고수리" in t


def is_pending_text(t: str) -> bool:
    """아직 수입신고 전 — 통관진행중으로 본다."""
    return "수입신고전" in t


def is_accepted_text(t: str) -> bool:
    """수입신고가 들어간 문구인지. '수입신고전' 과 '수입신고수리' 는 뺀다."""
    if is_pending_text(t) or is_cleared_text(t):
        return False
    return "수입신고" in t


def status_texts(results: list) -> list:
    """처리 타임라인 각 줄의 상태 문구만 모은다 (공백 제거)."""
    out = []
    for r in results:
        for f in STATUS_FIELDS:
            t = (r.get(f) or "").replace(" ", "")
            if t:
                out.append(t)
    return out


def decl_cleared(results: list) -> bool:
    return any(is_cleared_text(t) for t in status_texts(results))


def decl_pending(results: list) -> bool:
    return any(is_pending_text(t) for t in status_texts(results))


def decl_accepted(results: list) -> bool:
    return any(is_accepted_text(t) for t in status_texts(results))


def _event_time_where(results: list, pred) -> str:
    """조건에 맞는 첫 타임라인 줄의 처리시각."""
    for r in results:
        for f in STATUS_FIELDS:
            t = (r.get(f) or "").replace(" ", "")
            if t and pred(t):
                when = r.get("prcsDttm") or r.get("rlbrDttm") or ""
                if when:
                    return when
    return ""


def _release_date(results: list) -> str:
    fallback = ""
    for r in results:
        txt = (r.get("csclPrgsStts", "") or "") + (r.get("cargTrcnRelaBsopTpcd", "") or "") + (r.get("rlbrCn", "") or "")
        if "반출" in txt:
            when = r.get("prcsDttm") or r.get("rlbrDttm") or ""
            if when:
                return when
            fallback = fallback or "(시간정보 없음)"
    return fallback


def compute_stage(results: list):
    def firstval(k):
        return next((r.get(k) for r in results if r.get(k)), "")
    cscl = firstval("csclPrgsStts")
    cargno = firstval("cargMtNo")
    etpr = firstval("etprDt")
    blob = _blob(results)
    # 처리 타임라인 기준:
    #   수입신고수리 → 통관완료 (반출 여부와 무관)
    #   수입신고전   → 통관진행중
    if decl_cleared(results):
        stage = STAGE_DONE
    elif decl_pending(results) or decl_accepted(results) or etpr or "입항" in blob:
        stage = STAGE_IN_PROGRESS
    else:
        stage = STAGE_ARRIVAL_EXPECTED
    return stage, cscl, cargno, etpr


def declaration_status(results: list) -> str:
    if decl_cleared(results):
        when = _event_time_where(results, is_cleared_text)
        return f"수입신고 수리{f' ({when})' if when else ''}"
    if decl_accepted(results):
        when = _event_time_where(results, is_accepted_text)
        return f"수입신고 접수{f' ({when})' if when else ''}"
    return "수입신고 전"


def find_anomaly(results: list) -> str:
    blob = _blob(results)
    hits = []
    for k in ANOMALY_KEYS:
        if k in blob and k not in hits:
            hits.append(k)
    return ", ".join(hits)


def build_progress_text(results: list) -> str:
    parts = []
    cscl = next((r.get("csclPrgsStts") for r in results if r.get("csclPrgsStts")), "")
    if cscl:
        parts.append(f"통관진행상태:{cscl}")
    for r in results:
        stage = r.get("cargTrcnRelaBsopTpcd") or r.get("prgsStts") or ""
        when = r.get("prcsDttm") or r.get("rlbrDttm") or ""
        seg = " ".join(x for x in [when, stage] if x)
        if r.get("shedNm"):
            seg += f"@{r['shedNm']}"
        if seg.strip():
            parts.append(seg.strip())
    return " / ".join(parts)[:1000]


# 선사 접두어(SCAC). 시트에 접두어 없이 적힌 BL을 조회할 때 붙여볼 후보들.
CARRIER_PREFIXES = ["HDMU", "ONEY", "MAEU", "HLCU", "MSCU", "COSU",
                    "EGLV", "YMLU", "OOLU", "CMDU", "SUDU", "APLU"]


def _normalize_bl(bl: str) -> str:
    bl = bl.strip()
    if bl.startswith("27"):
        return "MAEU" + bl
    return bl


def _bl_candidates(bl: str) -> list:
    """조회에 써볼 BL 후보 목록.

    시트에 선사 접두어(SCAC)가 빠진 채 적히는 경우가 있어
    (예: HDMU 누락 'ALGA80504000'), 접두어를 붙인 형태도 함께 시도한다.
    """
    bl = (bl or "").strip()
    out = []

    def add(x):
        if x and x not in out:
            out.append(x)

    norm = _normalize_bl(bl)
    add(norm)          # 머스크(27로 시작) 규칙 우선
    add(bl)            # 원본

    # 이미 선사 접두어가 붙어 있으면 추가 후보는 만들지 않음
    upper = bl.upper()
    if not any(upper.startswith(p) for p in CARRIER_PREFIXES) and not bl.startswith("27"):
        for p in CARRIER_PREFIXES:
            add(p + bl)
    return out


def _bl_year(row: dict) -> str:
    for k in ("예상입항일", "선적일"):
        m = re.search(r"(20\d{2})", row.get(k, ""))
        if m:
            return m.group(1)
    return str(datetime.date.today().year)


def query_customs_by_bl(config: dict, bl: str, year: str) -> list:
    """BL번호로 API001 조회. 선사 접두어가 빠진 번호도 자동으로 붙여서 재시도한다."""
    last_err = None
    for cand in _bl_candidates(bl):
        for mode_key in ("mblNo", "hblNo"):
            try:
                return call_api("API001", {"__key__": get_key(config, "API001"),
                                           mode_key: cand, "blYy": year})
            except Exception as e:
                last_err = e
    raise last_err if last_err else RuntimeError("조회 실패")


# ── 구글시트(트래킹) 연동 ──
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.getenv("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]


def prefer_chrome() -> bool:
    """기본 브라우저(Edge 등) 대신 크롬을 우선 쓰도록 등록한다.
    구글 로그인 창이 Edge로 열리면서 다른 계정으로 자동 연결되는 것을 막기 위함."""
    import webbrowser
    for path in CHROME_PATHS:
        if path and os.path.exists(path):
            webbrowser.register("chrome", None,
                                webbrowser.BackgroundBrowser(path), preferred=True)
            return True
    return False


def _select_account_flow(client_config, scopes, port=0):
    """구글 로그인 시 '계정 선택' 화면을 반드시 띄운다.
    (브라우저에 이미 로그인된 다른 계정으로 자동 연결되는 것을 막기 위함)"""
    from google_auth_oauthlib.flow import InstalledAppFlow
    prefer_chrome()
    flow = InstalledAppFlow.from_client_config(client_config, scopes)
    return flow.run_local_server(
        port=port,
        prompt="select_account",
        authorization_prompt_message="브라우저에서 구글 로그인 창이 열립니다. 연동할 계정을 선택해주세요.",
    )


def gs_client():
    """구글시트 로그인 클라이언트만 (스프레드시트 지정 없이).
    통관관리 · 업무일지 · 이커머스 · 태블로 연동이 이 토큰 하나를 같이 쓴다."""
    if gspread is None:
        raise RuntimeError("gspread 가 설치되어 있지 않습니다. 'pip install gspread' 후 다시 실행하세요.")
    return gspread.oauth(flow=_select_account_flow)


def gs_open():
    return gs_client().open_by_key(SPREADSHEET_ID)


def reset_google_login() -> str:
    """저장된 구글 로그인 토큰을 지운다. 다음 연동 때 계정을 다시 고를 수 있다."""
    path = os.path.join(os.getenv("APPDATA", ""), "gspread", "authorized_user.json")
    if os.path.exists(path):
        os.remove(path)
        return "저장된 구글 로그인을 해제했어요. 다음에 계정을 다시 선택할 수 있어요."
    return "저장된 구글 로그인이 없어요."


def get_tracking_ws(sh):
    try:
        ws = sh.worksheet(TRACKING_WS)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TRACKING_WS, rows=1000, cols=len(TRACKING_HEADER))
        ws.update([TRACKING_HEADER], "A1", value_input_option="USER_ENTERED")
    return ws


def read_tracking(ws) -> list:
    values = ws.get_all_values()
    rows = []
    for i, r in enumerate(values[1:], start=2):
        r = list(r) + [""] * (len(TRACKING_HEADER) - len(r))
        d = {h: r[j] for j, h in enumerate(TRACKING_HEADER)}
        d["_sheet_row"] = i
        rows.append(d)
    return rows


def write_tracking(ws, rows: list) -> None:
    data = [TRACKING_HEADER]
    for r in rows:
        data.append([r.get(h, "") for h in TRACKING_HEADER])
    try:
        prev = len(ws.get_all_values())
    except Exception:
        prev = len(data)
    ncols = len(TRACKING_HEADER)
    while len(data) < prev:
        data.append([""] * ncols)
    ws.update(data, "A1", value_input_option="USER_ENTERED")


def update_tracking_row(ws, sheet_row: int, row: dict) -> None:
    values = [[row.get(h, "") for h in TRACKING_HEADER]]
    ws.update(values, f"A{sheet_row}", value_input_option="USER_ENTERED")


def delete_tracking_row(ws, sheet_row: int) -> None:
    ws.delete_rows(sheet_row)


def read_orders_from_sheet(sh) -> list:
    """제조사 탭에서 주문을 읽어온다.

    - 열 위치가 아니라 헤더 이름으로 찾는다 (사장님이 탭마다 열 순서를 바꾸므로).
    - BL번호가 아직 없는 주문(선하증권 미발행)도 포함한다. 이 경우 bl=""이고,
      보드에서는 'BL 미발행'으로 표시하며 관세청 조회는 건너뛴다.
    """
    orders = []
    for ws in sh.worksheets():
        if ws.title == TRACKING_WS:
            continue
        try:
            values = ws.get_all_values()
        except Exception:
            continue
        if not values:
            continue
        hdr = values[0]

        def idx(name):
            return hdr.index(name) if name in hdr else -1

        c_o, c_b, c_s, c_e = idx("주문명"), idx("BL번호"), idx("선적일"), idx("예상입항일")
        if c_o < 0:
            continue

        def cell(r, i):
            return r[i].strip() if 0 <= i < len(r) else ""

        for r in values[1:]:
            order_name = cell(r, c_o)
            bl = cell(r, c_b)
            # 주문 시작행(주문명이 있는 행)만 하나의 주문으로 본다
            if not order_name and not bl:
                continue
            if not order_name and bl:
                order_name = ""
            if not order_name:
                continue
            orders.append({"manufacturer": ws.title, "order_name": order_name,
                           "bl": bl, "shipped": cell(r, c_s), "eta": cell(r, c_e)})
    return orders


MFR_ARRIVAL_HEADER = "실제입항일"


def push_arrival_dates_to_manufacturer_tabs(sh, tracking_rows: list) -> int:
    by_mfr = {}
    for row in tracking_rows:
        etpr = row.get("입항일", "").strip()
        bl = row.get("BL번호", "").strip()
        mfr = row.get("제조사", "").strip()
        if etpr and bl and mfr:
            by_mfr.setdefault(mfr, []).append((bl, etpr))
    if not by_mfr:
        return 0
    updated = 0
    for mfr, pairs in by_mfr.items():
        try:
            ws = sh.worksheet(mfr)
        except gspread.WorksheetNotFound:
            continue
        header = ws.row_values(1)
        if "BL번호" not in header:
            continue
        bl_col = header.index("BL번호") + 1
        if MFR_ARRIVAL_HEADER in header:
            arrival_col = header.index(MFR_ARRIVAL_HEADER) + 1
        else:
            arrival_col = len(header) + 1
            ws.update_cell(1, arrival_col, MFR_ARRIVAL_HEADER)
        values = ws.get_all_values()
        bl_to_row = {}
        for i, r in enumerate(values[1:], start=2):
            bl_val = r[bl_col - 1].strip() if len(r) >= bl_col else ""
            if bl_val:
                bl_to_row[bl_val] = i
        for bl, etpr in pairs:
            row_num = bl_to_row.get(bl)
            if not row_num:
                continue
            current = values[row_num - 1][arrival_col - 1] if len(values[row_num - 1]) >= arrival_col else ""
            if current.strip() != etpr:
                ws.update_cell(row_num, arrival_col, etpr)
                updated += 1
    return updated


NO_BL_NOTE = "BL 미발행"


def _order_key(mfr, order_name, bl):
    """중복 판정 키. BL이 있으면 BL, 없으면 제조사+주문명."""
    bl = (bl or "").strip()
    return bl if bl else f"@{(mfr or '').strip()}/{(order_name or '').strip()}"


def sync_orders_into_tracking(sh):
    ws = get_tracking_ws(sh)
    existing = read_tracking(ws)
    seen = set()
    for row in existing:
        seen.add(_order_key(row.get("제조사"), row.get("주문명"), row.get("BL번호")))

    orders = read_orders_from_sheet(sh)
    new_rows = []
    for o in orders:
        k = _order_key(o["manufacturer"], o["order_name"], o["bl"])
        if k in seen:
            continue
        # BL이 아직 없으면 통관진행상태 칸에 'BL 미발행'을 남겨 화면에서 구분되게 함
        note = "" if o["bl"] else NO_BL_NOTE
        new_rows.append([o["bl"], o["manufacturer"], o["order_name"], o["shipped"], o["eta"],
                         "", STAGE_ARRIVAL_EXPECTED, note, "", "", "", ""])
        seen.add(k)
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    return len(new_rows), len(orders)


# ── 웹 서버가 부르는 고수준 함수들 (JSON 친화적 dict 반환) ──
def _strip_rows(rows: list) -> list:
    """트래킹 행에서 프론트로 보낼 필드만 남긴다 (_sheet_row 는 편집용으로 유지)."""
    return rows


def status() -> dict:
    cfg = load_config()
    return {
        "gspread_available": gspread is not None,
        "key_count": len(cfg.get("keys", {})),
        "sheet_url": cfg.get("sheet_url", ""),
        "has_api001": bool(get_key(cfg, "API001")),
        "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}",
    }


def query(mode: str, number: str, year: str) -> dict:
    """탭1(기존수입건) 조회: API001 + (자동연동) API020 컨테이너 상세."""
    cfg = load_config()
    number = (number or "").strip()
    if not number:
        raise RuntimeError("번호를 입력해주세요.")
    params = {"__key__": get_key(cfg, "API001")}
    cargno = ""
    if mode == "cargo":
        params["cargMtNo"] = number
        cargno = number
    elif mode == "mbl":
        params["mblNo"] = number
        params["blYy"] = year
    else:
        params["hblNo"] = number
        params["blYy"] = year

    out = {"api001": None, "api001_error": None, "containers": None, "containers_error": None}
    try:
        r1 = call_api("API001", params)
        struct = format_api001_structured(r1)
        out["api001"] = struct
        if not cargno:
            cargno = struct["chain"]["carg_mt_no"]
    except Exception as e:
        out["api001_error"] = str(e)

    if not cargno:
        out["containers_error"] = "화물관리번호를 확보하지 못해 컨테이너 상세 조회를 건너뜁니다."
    else:
        try:
            r2 = call_api("API020", {"__key__": get_key(cfg, "API020"), "cargMtNo": cargno})
            out["containers"] = [
                {"no": it.get("cntrNo", ""), "size": it.get("cntrStszCd", ""), "seal": it.get("cntrSelgNo1", "")}
                for it in r2
            ]
            out["cargo_no"] = cargno
        except Exception as e:
            out["containers_error"] = str(e)
    return out


ORDER_COLS = ["품명", "병입수", "카톤수", "총수량", "파렛트별박스수", "개당가격(EUR)", "개당가격(USD)",
              "소비기한", "산도(%)", "배치넘버", "넷중량(KG)", "총중량(KG)"]


def _a1col(i):
    n, s = i + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def save_invoice_order(tab, order_name, bl, shipped, eta, items, pi="") -> dict:
    """인보이스에서 읽은 주문을 제조사 탭에 넣고 통관보드까지 동기화.

    열은 헤더 '이름'으로 찾는다 (시트에서 열 순서가 바뀌어도 안 깨지게).
    같은 주문명이 이미 있으면 아무것도 하지 않는다."""
    order_name = (order_name or "").strip()
    if not order_name:
        raise RuntimeError("주문명이 비어 있습니다.")
    if not items:
        raise RuntimeError("등록할 품목이 없습니다.")

    sh = gs_open()
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        raise RuntimeError("'%s' 탭을 찾지 못했습니다." % tab)

    values = ws.get_all_values()
    if not values:
        raise RuntimeError("'%s' 탭이 비어 있습니다 (헤더가 없습니다)." % tab)
    header = values[0]

    for r in values[1:]:
        if r and r[0].strip() == order_name:
            return {"ok": False, "skipped": True,
                    "message": "이미 '%s' 이 시트에 있습니다." % order_name}

    for need in ["주문명", "BL번호", "선적일", "예상입항일"]:
        if need not in header:
            raise RuntimeError("시트에 '%s' 열이 없습니다." % need)

    first = len(values) + 1
    rows = []
    for n, it in enumerate(items):
        d = {k: it.get(k, "") for k in ORDER_COLS if k in header}
        # 배치번호는 '0000352725' 처럼 앞자리 0이 있는 경우가 있다.
        # 그냥 넣으면 시트가 숫자로 읽어 0을 지워버리므로 텍스트로 고정한다.
        b = str(d.get("배치넘버", "") or "")
        if b.startswith("0"):
            d["배치넘버"] = "'" + b
        if n == 0:
            d["주문명"] = order_name
            d["BL번호"] = bl or ""
            d["선적일"] = shipped or ""
            d["예상입항일"] = eta or ""
            if pi and "PI기준발주일" in header:
                d["PI기준발주일"] = pi
        rows.append([d.get(h, "") for h in header])

    ws.append_rows(rows, value_input_option="USER_ENTERED")

    last = first + len(rows) - 1
    merged = 0
    if last > first:
        for name in ("주문명", "BL번호", "선적일", "예상입항일"):
            c = _a1col(header.index(name))
            try:
                ws.merge_cells("%s%d:%s%d" % (c, first, c, last))
                merged += 1
            except Exception:
                pass

    new_n, total = sync_orders_into_tracking(sh)
    return {"ok": True, "rows": len(rows), "first_row": first, "last_row": last,
            "merged": merged, "new_in_board": new_n, "orders_total": total,
            "sheet_url": sh.url,
            "message": "%s — %d줄 등록 완료" % (order_name, len(rows))}


def board_sync() -> dict:
    sh = gs_open()
    new_n, total = sync_orders_into_tracking(sh)
    ws = get_tracking_ws(sh)
    rows = read_tracking(ws)
    return {"rows": rows, "new_n": new_n, "total": total, "sheet_url": sh.url}


def board_refresh() -> dict:
    cfg = load_config()
    if not get_key(cfg, "API001"):
        raise RuntimeError("API001 인증키가 없습니다. 구글시트 동기화를 먼저 해주세요.")
    sh = gs_open()
    ws = get_tracking_ws(sh)
    rows = read_tracking(ws)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    changed = 0
    for row in rows:
        # 완료된 건은 다시 조회하지 않는다. 단, 통관진행상태가 실제로 수입신고수리가
        # 아닌데 완료로 올라가 있는 건(예전 '수리' 오판정)은 한 번 더 확인한다.
        if row.get("상태") == STAGE_DONE:
            if is_cleared_text((row.get("통관진행상태") or "").replace(" ", "")):
                continue
        bl = row.get("BL번호", "").strip()
        if not bl:
            # 선하증권이 아직 안 나온 주문 — 조회할 번호가 없으니 표시만 하고 넘어감
            row["통관진행상태"] = NO_BL_NOTE
            row["통관진행사항"] = "선하증권(BL) 발행 대기 중 — BL번호가 들어오면 자동으로 조회됩니다."
            row["갱신시각"] = now
            continue
        year = _bl_year(row)
        try:
            results = query_customs_by_bl(cfg, bl, year)
            stage, cscl, cargno, etpr = compute_stage(results)
            prev = row.get("상태")
            row["상태"] = stage
            row["통관진행상태"] = cscl
            row["통관진행사항"] = build_progress_text(results)
            row["수입신고"] = declaration_status(results)
            row["이상사항"] = find_anomaly(results)
            if cargno:
                row["화물관리번호"] = cargno
            if etpr:
                row["입항일"] = etpr
            if stage == STAGE_DONE:
                row["반출일"] = _release_date(results)
            row["갱신시각"] = now
            if prev != stage:
                changed += 1
        except Exception as e:
            row["통관진행사항"] = f"[조회실패] {e}"[:200]
            row["갱신시각"] = now
    write_tracking(ws, rows)
    try:
        push_arrival_dates_to_manufacturer_tabs(sh, rows)
    except Exception:
        pass
    return {"rows": rows, "changed": changed}


def board_add(bl: str, mfr: str, order_name: str, shipped: str) -> dict:
    bl = (bl or "").strip()
    if not bl:
        raise RuntimeError("BL번호는 필수입니다.")
    sh = gs_open()
    ws = get_tracking_ws(sh)
    ws.append_row([bl, mfr or "", order_name or "", shipped or "", "", "", STAGE_ARRIVAL_EXPECTED,
                   "", "", "", "", ""], value_input_option="USER_ENTERED")
    return {"rows": read_tracking(ws)}


def board_edit(sheet_row: int, values: dict) -> dict:
    if not values.get("BL번호", "").strip():
        raise RuntimeError("BL번호는 비울 수 없습니다.")
    sh = gs_open()
    ws = get_tracking_ws(sh)
    update_tracking_row(ws, sheet_row, values)
    return {"rows": read_tracking(ws)}


def board_delete(sheet_row: int) -> dict:
    sh = gs_open()
    ws = get_tracking_ws(sh)
    delete_tracking_row(ws, sheet_row)
    return {"rows": read_tracking(ws)}


# ── 물류입고정보 (제조사 탭에서 주문 상세를 읽어옴) ──
MFR_LABEL = {
    "COOSUR": "아세수르",
    "BORGES": "보르헤스",
    "ZENAT": "제나트",
    "ZHONGPU": "종푸",
    "OLEOESTEPA": "올레오에스테파",
    "SANCHEZ": "산체스(소르바스)",
}
PALLET_COL = "파렛트별박스수"


def _num(s):
    """'1,694' / '3.5' -> 숫자. 비었거나 숫자가 아니면 None."""
    s = (s or "").replace(",", "").strip()
    if not s:
        return None
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def order_detail(bl: str) -> dict:
    """BL번호로 제조사 탭에서 해당 주문의 품목 줄들을 모아 물류입고정보용 데이터를 만든다.

    시트는 주문당 첫 줄에만 주문명/BL번호가 있고 나머지 줄은 비어 있으므로,
    BL이 적힌 줄부터 다음 주문이 시작되기 전까지를 한 주문으로 본다.
    """
    bl = (bl or "").strip()
    if not bl:
        raise RuntimeError("BL번호가 필요합니다.")
    sh = gs_open()
    for ws in sh.worksheets():
        if ws.title == TRACKING_WS:
            continue
        values = ws.get_all_values()
        if not values:
            continue
        hdr = values[0]

        def col(name):
            return hdr.index(name) if name in hdr else -1

        c_order, c_bl = col("주문명"), col("BL번호")
        if c_bl < 0:
            continue
        start = None
        for i, r in enumerate(values[1:], start=1):
            if len(r) > c_bl and r[c_bl].strip() == bl:
                start = i
                break
        if start is None:
            continue

        # 다음 주문이 시작되는 줄 직전까지
        end = len(values)
        for i in range(start + 1, len(values)):
            r = values[i]
            if (len(r) > c_bl and r[c_bl].strip()) or (len(r) > c_order and r[c_order].strip()):
                end = i
                break

        ci = {n: col(n) for n in ("품명", "소비기한", "카톤수", "병입수", "총수량",
                                  PALLET_COL, "선적일", "예상입항일", "실제입항일")}

        def cell(row, name):
            j = ci.get(name, -1)
            return row[j].strip() if 0 <= j < len(row) else ""

        items = []
        for i in range(start, end):
            r = values[i]
            name = cell(r, "품명")
            if not name:
                continue
            cartons = _num(cell(r, "카톤수"))          # 카톤수량
            per_carton = _num(cell(r, "병입수"))        # 카톤입수
            total = _num(cell(r, "총수량"))             # 수량(유닛)
            if total is None and cartons is not None and per_carton is not None:
                total = cartons * per_carton
            boxes_per_pallet = _num(cell(r, PALLET_COL))
            pallets = None
            if cartons and boxes_per_pallet:
                pallets = round(cartons / boxes_per_pallet, 2)
            items.append({
                "row": i + 1,                            # 시트 행번호(파렛트 수정용)
                "name": name,
                "per_carton": per_carton,
                "cartons": cartons,
                "total": total,
                "pallets": pallets,
                "boxes_per_pallet": boxes_per_pallet,
                "expiry": cell(r, "소비기한"),
            })

        head = values[start]
        return {
            "found": True,
            "sheet": ws.title,
            "supplier": MFR_LABEL.get(ws.title, ws.title),
            "order_name": cell(head, "주문명") or (head[c_order].strip() if c_order >= 0 else ""),
            "bl": bl,
            "shipped": cell(head, "선적일"),
            "eta": cell(head, "예상입항일"),
            "arrived": cell(head, "실제입항일"),
            "pallet_col_index": ci.get(PALLET_COL, -1) + 1,   # 1-based
            "name_col_index": ci.get("품명", -1) + 1,
            "items": items,
        }
    return {"found": False, "bl": bl, "items": []}


def _a1_col(idx1: int) -> str:
    """1-based 열 번호 -> A1 표기(A, B, ... Z, AA)."""
    s = ""
    n = idx1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def save_order_items(sheet_title: str, items: list, name_col: int, pallet_col: int) -> dict:
    """물류입고정보 화면에서 고친 '품목명'과 '파렛트별 박스수'를 구글시트에 반영.

    items: [{"row": 시트행(1-based), "name": 품목명, "boxes_per_pallet": 값 or None}, ...]
    """
    if not items:
        return {"ok": True, "updated": 0}
    sh = gs_open()
    ws = sh.worksheet(sheet_title)

    data = []
    for it in items:
        row = int(it["row"])
        if name_col and name_col > 0 and it.get("name") is not None:
            data.append({"range": f"{_a1_col(name_col)}{row}",
                         "values": [[it["name"]]]})
        if pallet_col and pallet_col > 0:
            v = it.get("boxes_per_pallet")
            data.append({"range": f"{_a1_col(pallet_col)}{row}",
                         "values": [["" if v in (None, "") else v]]})
    if data:
        ws.batch_update(data, value_input_option="USER_ENTERED")
    return {"ok": True, "updated": len(data)}


def sync_keys(url_or_id: str) -> dict:
    cfg = load_config()
    new_keys = sync_keys_from_sheet(url_or_id)
    cfg["keys"].update(new_keys)
    cfg["sheet_url"] = url_or_id
    save_config(cfg)
    return {"key_count": len(cfg["keys"]), "loaded": len(new_keys)}
