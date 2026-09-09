"""이커머스 판매실적 — 구글시트 저장소.

여러 사람이 동시에 고쳐도 서로 덮어쓰지 않도록, 값 하나(플랫폼·SKU·연월)를
행 단위로 찾아서 그 행만 고친다. 전체를 통째로 다시 쓰지 않는다.

구글 로그인은 통관 기능(customs.py)과 같은 토큰을 재사용한다.
시트는 처음 한 번 자동으로 새로 만들고, 그 ID를 data/ecom_sheet.json 에 적어둔다.

※ 열은 반드시 '헤더 이름'으로 찾는다. 사장님이 시트에서 열 순서를 바꿔도
   깨지지 않아야 하기 때문에 위치(A열·B열)로 하드코딩하지 않는다.
"""
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(BASE_DIR, "data", "ecom_sheet.json")

TITLE = "UB Korea 이커머스 판매실적"

WS_PERF = "실적"
WS_AD = "광고비"
WS_SET = "설정"

HDR_PERF = ["플랫폼", "SKU", "연월", "수량", "공급액", "원가"]
HDR_AD = ["플랫폼", "브랜드", "연월", "광고비"]
HDR_SET = ["키", "값"]

_lock = threading.Lock()

# 구글 API 는 분당 읽기 횟수 제한이 있어서, 접속·탭·헤더를 한 번 잡아두고 재사용한다.
_cache = {"gc": None, "sh": None, "ws": {}, "cm": {}}


def reset_cache():
    _cache["gc"] = None
    _cache["sh"] = None
    _cache["ws"] = {}
    _cache["cm"] = {}


# ── 시트 열기 ────────────────────────────────────────────
def _client():
    if _cache["gc"] is None:
        import customs
        if customs.gspread is None:
            raise RuntimeError("gspread 가 설치되어 있지 않습니다. 'pip install gspread' 후 다시 실행하세요.")
        _cache["gc"] = customs.gspread.oauth(flow=customs._select_account_flow)
    return _cache["gc"]


def _load_cfg():
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cfg(cfg):
    os.makedirs(os.path.dirname(CFG_FILE), exist_ok=True)
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _ws(sh, title, header):
    """탭을 찾고, 없으면 헤더까지 넣어서 새로 만든다. 한 번 찾은 탭은 캐시한다."""
    if title in _cache["ws"]:
        return _cache["ws"][title]
    import customs
    try:
        ws = sh.worksheet(title)
    except customs.gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=max(8, len(header)))
        ws.update([header], "A1", value_input_option="USER_ENTERED")
    _cache["ws"][title] = ws
    return ws


def open_sheet(create=True):
    if _cache["sh"] is not None:
        return _cache["sh"]
    cfg = _load_cfg()
    gc = _client()
    sid = cfg.get("spreadsheet_id")
    if sid:
        try:
            sh = gc.open_by_key(sid)
            _cache["sh"] = sh
            return sh
        except Exception:
            if not create:
                raise
    if not create:
        raise RuntimeError("이커머스 시트가 아직 없습니다.")
    sh = gc.create(TITLE)
    _save_cfg({"spreadsheet_id": sh.id, "url": sh.url})
    _cache["sh"] = sh
    for t, h in ((WS_PERF, HDR_PERF), (WS_AD, HDR_AD), (WS_SET, HDR_SET)):
        _ws(sh, t, h)
    try:                       # 기본 'Sheet1' 은 지운다
        sh.del_worksheet(sh.worksheet("Sheet1"))
    except Exception:
        pass
    return sh


def sheet_url():
    cfg = _load_cfg()
    return cfg.get("url", "")


# ── 헤더 이름으로 열 찾기 ────────────────────────────────
def _colmap(ws, header, head_row=None):
    """{헤더이름: 0-based 열번호}. 빠진 헤더는 뒤에 새로 붙인다.
    head_row 를 주면 그걸 쓰고(추가 API 호출 없음), 없으면 시트에서 읽는다."""
    key = ws.title
    if head_row is None and key in _cache["cm"]:
        return _cache["cm"][key]
    row = head_row if head_row is not None else ws.row_values(1)
    m = {name: i for i, name in enumerate(row) if name}
    missing = [h for h in header if h not in m]
    if missing:
        start = len(row)
        for j, h in enumerate(missing):
            m[h] = start + j
        ws.update([[*row, *missing]], "A1", value_input_option="USER_ENTERED")
    _cache["cm"][key] = m
    return m


def _rows(ws):
    vals = ws.get_all_values()
    return vals[0] if vals else [], vals[1:] if len(vals) > 1 else []


def _cell(row, cm, name):
    i = cm.get(name)
    return row[i].strip() if (i is not None and i < len(row)) else ""


def _num(s):
    s = (s or "").replace(",", "").strip()
    if s == "":
        return None
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def _a1(col_idx):
    """0-based 열번호 → A1 표기 (26열 넘어가도 동작)."""
    n, s = col_idx + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ── 읽기 ─────────────────────────────────────────────────
def load():
    """세 탭을 한 번의 API 호출로 읽는다 (분당 읽기 제한에 걸리지 않게)."""
    sh = open_sheet()
    out = {"perf": {}, "ad": {}, "settings": {}, "url": sh.url}
    for t, h in ((WS_PERF, HDR_PERF), (WS_AD, HDR_AD), (WS_SET, HDR_SET)):
        _ws(sh, t, h)

    got = sh.values_batch_get(["'%s'!A1:Z5000" % t for t in (WS_PERF, WS_AD, WS_SET)])
    ranges = got.get("valueRanges", [])
    grids = []
    for vr in ranges:
        vals = vr.get("values", []) or []
        grids.append((vals[0] if vals else [], vals[1:] if len(vals) > 1 else []))
    while len(grids) < 3:
        grids.append(([], []))

    head, rows = grids[0]
    cm = _colmap(_cache["ws"][WS_PERF], HDR_PERF, head or None)
    for r in rows:
        p, k, ym = _cell(r, cm, "플랫폼"), _cell(r, cm, "SKU"), _cell(r, cm, "연월")
        if not (p and k and ym):
            continue
        q, a, c = _num(_cell(r, cm, "수량")), _num(_cell(r, cm, "공급액")), _num(_cell(r, cm, "원가"))
        if q is None and a is None:
            continue
        out["perf"].setdefault(p, {}).setdefault(k, {})[ym] = {
            "q": q or 0, "a": a or 0, "c": c}

    head, rows = grids[1]
    cm = _colmap(_cache["ws"][WS_AD], HDR_AD, head or None)
    for r in rows:
        p, b, ym = _cell(r, cm, "플랫폼"), _cell(r, cm, "브랜드"), _cell(r, cm, "연월")
        v = _num(_cell(r, cm, "광고비"))
        if not (p and b and ym) or not v:
            continue
        out["ad"].setdefault(p, {}).setdefault(b, {})[ym] = v

    head, rows = grids[2]
    cm = _colmap(_cache["ws"][WS_SET], HDR_SET, head or None)
    for r in rows:
        k = _cell(r, cm, "키")
        if k:
            out["settings"][k] = _cell(r, cm, "값")
    return out


# ── 쓰기 (행 단위 upsert) ────────────────────────────────
def _find_row(rows, cm, keys):
    """keys = [(헤더이름, 값), ...] 이 모두 일치하는 행의 시트 행번호(2부터)."""
    for i, r in enumerate(rows, start=2):
        if all(_cell(r, cm, h) == v for h, v in keys):
            return i
    return None


def _write_row(ws, cm, header, row_num, values):
    """헤더 이름 기준으로 값을 배치해 그 행만 갱신."""
    width = max(cm.values()) + 1
    row = [""] * width
    for name, v in values.items():
        row[cm[name]] = "" if v is None else v
    ws.update([row], "A%d:%s%d" % (row_num, _a1(width - 1), row_num),
              value_input_option="USER_ENTERED")


def save_entry(plat, sku, ym, q, a, c):
    with _lock:
        sh = open_sheet()
        ws = _ws(sh, WS_PERF, HDR_PERF)
        cm = _colmap(ws, HDR_PERF)
        _, rows = _rows(ws)
        keys = [("플랫폼", plat), ("SKU", sku), ("연월", ym)]
        vals = {"플랫폼": plat, "SKU": sku, "연월": ym,
                "수량": q, "공급액": a, "원가": c}
        n = _find_row(rows, cm, keys)
        if n:
            _write_row(ws, cm, HDR_PERF, n, vals)
        else:
            width = max(cm.values()) + 1
            row = [""] * width
            for name, v in vals.items():
                row[cm[name]] = "" if v is None else v
            ws.append_row(row, value_input_option="USER_ENTERED")
    return True


def delete_entry(plat, sku, ym):
    with _lock:
        sh = open_sheet()
        ws = _ws(sh, WS_PERF, HDR_PERF)
        cm = _colmap(ws, HDR_PERF)
        _, rows = _rows(ws)
        n = _find_row(rows, cm, [("플랫폼", plat), ("SKU", sku), ("연월", ym)])
        if n:
            ws.delete_rows(n)
    return True


def save_ad(plat, brand, ym, value):
    with _lock:
        sh = open_sheet()
        ws = _ws(sh, WS_AD, HDR_AD)
        cm = _colmap(ws, HDR_AD)
        _, rows = _rows(ws)
        n = _find_row(rows, cm, [("플랫폼", plat), ("브랜드", brand), ("연월", ym)])
        if not value:
            if n:
                ws.delete_rows(n)
            return True
        vals = {"플랫폼": plat, "브랜드": brand, "연월": ym, "광고비": value}
        if n:
            _write_row(ws, cm, HDR_AD, n, vals)
        else:
            width = max(cm.values()) + 1
            row = [""] * width
            for name, v in vals.items():
                row[cm[name]] = v
            ws.append_row(row, value_input_option="USER_ENTERED")
    return True


def save_setting(key, value):
    with _lock:
        sh = open_sheet()
        ws = _ws(sh, WS_SET, HDR_SET)
        cm = _colmap(ws, HDR_SET)
        _, rows = _rows(ws)
        n = _find_row(rows, cm, [("키", key)])
        vals = {"키": key, "값": value}
        if n:
            _write_row(ws, cm, HDR_SET, n, vals)
        else:
            width = max(cm.values()) + 1
            row = [""] * width
            for name, v in vals.items():
                row[cm[name]] = v
            ws.append_row(row, value_input_option="USER_ENTERED")
    return True


def push_all(perf, ad, settings):
    """브라우저에 쌓여 있던 값을 시트로 한 번에 올린다 (최초 이전용).

    한 건씩 올리면 구글 API 분당 제한에 걸리므로, 기존 값과 합친 뒤
    탭마다 딱 한 번씩만 통째로 쓴다."""
    with _lock:
        sh = open_sheet()
        cur = load()          # 읽기 1회
        n = 0

        # ── 실적 ──
        merged = cur["perf"]
        for plat, skus in (perf or {}).items():
            for sku, months in (skus or {}).items():
                for ym, v in (months or {}).items():
                    merged.setdefault(plat, {}).setdefault(sku, {})[ym] = v
                    n += 1
        ws = _ws(sh, WS_PERF, HDR_PERF)
        cm = _colmap(ws, HDR_PERF)
        width = max(cm.values()) + 1
        head = [""] * width
        for name, i in cm.items():
            head[i] = name
        body = []
        for plat in sorted(merged):
            for sku in sorted(merged[plat]):
                for ym in sorted(merged[plat][sku]):
                    v = merged[plat][sku][ym]
                    row = [""] * width
                    row[cm["플랫폼"]] = plat
                    row[cm["SKU"]] = sku
                    row[cm["연월"]] = ym
                    row[cm["수량"]] = v.get("q") if v.get("q") is not None else ""
                    row[cm["공급액"]] = v.get("a") if v.get("a") is not None else ""
                    row[cm["원가"]] = v.get("c") if v.get("c") is not None else ""
                    body.append(row)
        _replace(ws, head, body, width)

        # ── 광고비 ──
        merged_ad = cur["ad"]
        for plat, brands in (ad or {}).items():
            for b, months in (brands or {}).items():
                for ym, v in (months or {}).items():
                    if v:
                        merged_ad.setdefault(plat, {}).setdefault(b, {})[ym] = v
                        n += 1
        ws = _ws(sh, WS_AD, HDR_AD)
        cm = _colmap(ws, HDR_AD)
        width = max(cm.values()) + 1
        head = [""] * width
        for name, i in cm.items():
            head[i] = name
        body = []
        for plat in sorted(merged_ad):
            for b in sorted(merged_ad[plat]):
                for ym in sorted(merged_ad[plat][b]):
                    row = [""] * width
                    row[cm["플랫폼"]] = plat
                    row[cm["브랜드"]] = b
                    row[cm["연월"]] = ym
                    row[cm["광고비"]] = merged_ad[plat][b][ym]
                    body.append(row)
        _replace(ws, head, body, width)

        # ── 설정 ──
        merged_set = cur["settings"]
        for k, v in (settings or {}).items():
            merged_set[k] = v
            n += 1
        ws = _ws(sh, WS_SET, HDR_SET)
        cm = _colmap(ws, HDR_SET)
        width = max(cm.values()) + 1
        head = [""] * width
        for name, i in cm.items():
            head[i] = name
        body = []
        for k in sorted(merged_set):
            row = [""] * width
            row[cm["키"]] = k
            row[cm["값"]] = merged_set[k]
            body.append(row)
        _replace(ws, head, body, width)
    return n


def _replace(ws, head, body, width):
    """탭 내용을 헤더+본문으로 통째로 교체 (남는 옛 행은 빈칸으로 덮음)."""
    data = [head] + body
    try:
        prev = ws.row_count
    except Exception:
        prev = len(data)
    blank = [""] * width
    while len(data) < min(prev, len(body) + 400):
        data.append(blank[:])
    ws.update(data, "A1:%s%d" % (_a1(width - 1), len(data)),
              value_input_option="USER_ENTERED")
