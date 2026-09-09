# -*- coding: utf-8 -*-
"""수요예측·발주 데이터를 태블로가 바로 읽을 수 있는 형태로 내보낸다.

데이터 입력·가공(계절지수, 재고 예측 등)은 계속 이 대시보드에서 하고,
태블로는 그 결과를 보여주기만 한다 — 그래서 이 모듈은 이미 계산된 값을
표 형태로 내보낸다.

두 가지 방식을 지원한다:
  - export_*()       : 구글시트에 통째로 덮어쓴다 (태블로의 Google Drive 연동용).
  - export_*_excel() : 바탕화면에 엑셀 파일로 저장한다 (구글 인증이 막힐 때의 대안 —
                        실제로 태블로 데스크톱의 Google Drive OAuth 창이 뜨지 않고
                        멈추는 걸 확인해서 만들었다. 엑셀은 그런 인증 절차가 없다).

시트/파일 다 태블로용 파생 데이터라, 사장님이 직접 손대는 시트(이커머스 등)와
달리 매번 통째로 덮어써도 안전하다.
"""
import os
from datetime import date

import customs

# 태블로 연동 전용 스프레드시트 ID를 여기(customs_config.json)에 저장해둔다.
CONFIG_KEY = "tableau_sheet_id"
SHEET_TITLE = "UB Korea 태블로 연동"

FORECAST_WS = "수요예측_월별"
FORECAST_HEADER = ["월", "상품명", "브랜드", "단위", "신상품", "실적(출고)", "월말재고", "그달입고"]

PO_WS = "구매발주_관리"
PO_HEADER = ["브랜드", "상품명", "단위",
             "입고예정월", "입고예정시점", "입고예정수량",
             "작년판매량(연간)", "올해YTD판매량", "작년동기간YTD판매량",
             "YoY증가량", "YoY증가율(%)", "최근12개월평균판매량"]


def get_sheet_id() -> str:
    return (customs.load_config().get(CONFIG_KEY) or "").strip()


def save_sheet_id(sheet_id: str) -> None:
    cfg = customs.load_config()
    cfg[CONFIG_KEY] = sheet_id
    customs.save_config(cfg)


def _open_or_create(gc):
    sheet_id = get_sheet_id()
    if sheet_id:
        try:
            return gc.open_by_key(sheet_id)
        except Exception:
            pass  # 저장된 ID가 더는 못 열리면(삭제 등) 새로 만든다
    sh = gc.create(SHEET_TITLE)
    save_sheet_id(sh.id)
    return sh


# ── 수요예측 (월 × 상품 긴 표) ──
def build_rows(store) -> list:
    """store(forecast_tool.Store) → [[월, 상품명, 브랜드, ...], ...] 긴 표."""
    rows = []
    for product in store.products():
        meta = store.meta.get(product, {})
        brand = meta.get("brand", "")
        unit = meta.get("unit", "")
        is_new = "Y" if meta.get("new") else ""

        hist = store.history.get(product, {})
        inv_series = {r["month"]: r for r in store.inventory_series(product)}

        months = sorted(set(hist) | set(inv_series))
        for ym in months:
            row = inv_series.get(ym, {})
            rows.append([
                ym, product, brand, unit, is_new,
                hist.get(ym, ""),
                row.get("qty") if row.get("qty") is not None else "",
                row.get("inbound") if row.get("inbound") is not None else "",
            ])
    return rows


# ── 구매발주 관리 (브랜드-상품 한 줄씩) ──
INBOUND_AT_LABEL = {"early": "월초", "mid": "중순", "late": "월말"}


def _months_back(ym: str, n: int) -> list:
    """ym 을 포함해 과거로 n개월 (예: 2026-08, 3 → [2026-06,2026-07,2026-08])."""
    y, m = int(ym[:4]), int(ym[5:7])
    out = []
    for i in range(n - 1, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append("%04d-%02d" % (yy, mm))
    return out


def build_po_rows(store, today: date = None) -> list:
    """브랜드-상품 한 줄씩: 입고예정 + 작년판매량 + YoY + 최근12개월평균.

    - 입고예정: 이번 달(포함) 이후 중 가장 이른 입고 예정월/시점/수량.
      정확한 달력 날짜는 이 시스템에 없어서(월 + 초/중/말만 기록), 그 단위로 보여준다.
    - YoY: 올해 1월~이번달까지(YTD)와 작년 같은 기간을 비교한다.
      이번 달 실적은 아직 다 안 들어왔을 수 있어 참고용으로 봐야 한다.
    """
    today = today or date.today()
    cur_ym = today.strftime("%Y-%m")
    cur_year, cur_month = today.year, today.month
    last_year = cur_year - 1

    rows = []
    for product in store.products():
        meta = store.meta.get(product, {})
        brand = meta.get("brand", "")
        unit = meta.get("unit", "")
        hist = store.history.get(product, {})
        inv = store.inventory.get(product, {})
        inbound = inv.get("inbound", {})
        inbound_at = inv.get("inbound_at", {})

        future_months = sorted(ym for ym, v in inbound.items() if ym >= cur_ym and v)
        next_ym = future_months[0] if future_months else ""
        next_qty = inbound.get(next_ym, "") if next_ym else ""
        next_at = INBOUND_AT_LABEL.get(inbound_at.get(next_ym, ""), "") if next_ym else ""

        last_year_total = sum(v for ym, v in hist.items() if ym.startswith(str(last_year)))
        this_year_ytd = sum(v for ym, v in hist.items()
                             if ym.startswith(str(cur_year)) and ym <= cur_ym)
        last_year_ytd = sum(v for ym, v in hist.items()
                             if ym.startswith(str(last_year)) and int(ym[5:7]) <= cur_month)

        yoy_amount = this_year_ytd - last_year_ytd
        yoy_pct = round(yoy_amount / last_year_ytd * 100, 1) if last_year_ytd else ""

        recent_months = _months_back(cur_ym, 12)
        recent_vals = [hist[ym] for ym in recent_months if ym in hist]
        avg_12m = round(sum(recent_vals) / len(recent_vals), 1) if recent_vals else ""

        rows.append([
            brand, product, unit,
            next_ym, next_at, next_qty,
            last_year_total, this_year_ytd, last_year_ytd,
            yoy_amount, yoy_pct, avg_12m,
        ])

    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


# ── 구글시트 내보내기 (공통) ──
def _export_to_sheet(ws_title: str, header: list, rows: list) -> dict:
    gc = customs.gs_client()
    sh = _open_or_create(gc)
    try:
        ws = sh.worksheet(ws_title)
    except Exception:
        ws = sh.add_worksheet(title=ws_title, rows=100, cols=len(header))
    ws.clear()
    ws.update([header] + rows, value_input_option="USER_ENTERED")
    return {"sheet_url": sh.url, "sheet_id": sh.id, "rows": len(rows)}


def export(store) -> dict:
    return _export_to_sheet(FORECAST_WS, FORECAST_HEADER, build_rows(store))


def export_po(store) -> dict:
    return _export_to_sheet(PO_WS, PO_HEADER, build_po_rows(store))


# ── 엑셀 내보내기 (공통, 구글 인증이 막힐 때의 대안) ──
def _desktop_dir() -> str:
    """바탕화면 실제 경로. OneDrive가 바탕화면을 옮겨놓은 PC가 있어서
    고정 경로 대신 레지스트리(Shell Folders)에서 실제 위치를 읽는다."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        path, _ = winreg.QueryValueEx(key, "Desktop")
        path = os.path.expandvars(path)
        if os.path.isdir(path):
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _export_to_excel(filename: str, header: list, rows: list, out_dir: str = None) -> dict:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = filename[:31]
    ws.append(header)
    for r in rows:
        ws.append(r)

    out_dir = out_dir or _desktop_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename + ".xlsx")
    wb.save(path)
    return {"path": path, "rows": len(rows)}


def export_excel(store, out_dir: str = None) -> dict:
    return _export_to_excel("UB Korea 태블로 연동", FORECAST_HEADER, build_rows(store), out_dir)


def export_po_excel(store, out_dir: str = None) -> dict:
    return _export_to_excel("UB Korea 구매발주 관리", PO_HEADER, build_po_rows(store), out_dir)
