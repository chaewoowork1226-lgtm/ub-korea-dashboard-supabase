#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UB Korea 수요예측 도구 (forecast_tool.py)
==========================================
그동안 정리한 방법론(계절지수법, 최근추세법, 시나리오)을 그대로 코드로 옮긴 도구예요.

핵심 설계:
  - history.json     : 실제 실적 (매달 add-actual 로 채워나감)
  - forecasts.json    : 예측 기록. 새 예측을 만들 때마다 "vintage"(생성시점)를 찍어서
                         계속 누적됩니다. 이전 예측은 절대 덮어쓰지 않아요.
                         → 나중에 "그때 예측한 값"과 "실제로 나온 값"을 나란히 비교 가능.

빠른 사용법:
  python3 forecast_tool.py list
  python3 forecast_tool.py history 스프레이_아보카도200
  python3 forecast_tool.py add-actual 스프레이_아보카도200 2026-07 1850
  python3 forecast_tool.py forecast 스프레이_아보카도200 2026-07 --months 6 --method seasonal --scenario base
  python3 forecast_tool.py compare 스프레이_아보카도200
  python3 forecast_tool.py export

자세한 사용법은 같은 폴더의 README.md 참고하세요.
"""

import json
import os
import re
import argparse
import statistics
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
from collections import defaultdict

try:
    import customs  # UNI-PASS 통관 백엔드 (같은 폴더의 customs.py)
except Exception:
    customs = None

try:
    import worklog  # 업무일지 (같은 폴더의 worklog.py)
except Exception:
    worklog = None

try:
    import ecom_store  # 이커머스 실적 구글시트 저장소 (같은 폴더의 ecom_store.py)
except Exception:
    ecom_store = None

try:
    import supabase_sync  # 서버 전용 Supabase 저장/스키마 조회
except Exception:
    supabase_sync = None

def friendly_error(e) -> str:
    """구글 인증 오류는 원문 대신 사장님이 알아볼 수 있는 안내로 바꾼다."""
    s = str(e)
    if "invalid_grant" in s or "expired or revoked" in s or "Token has been" in s:
        return ("구글 로그인이 만료됐어요. 바탕화면의 "
                "‘구글 다시 연결’ 을 실행해 다시 로그인한 뒤, "
                "대시보드 창을 닫았다가 다시 열어주세요.")
    if "gspread" in s and "설치" in s:
        return s
    return s


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HIST_FILE = DATA_DIR / "history.json"
FCST_FILE = DATA_DIR / "forecasts.json"
META_FILE = DATA_DIR / "products.json"
INV_FILE = DATA_DIR / "inventory.json"
PROJ_FILE = DATA_DIR / "projects.json"


# ──────────────────────────────────────────────────────────
# 파일 입출력
# ──────────────────────────────────────────────────────────
def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────
# 날짜 유틸 (YYYY-MM 문자열 기준)
# ──────────────────────────────────────────────────────────
def ym_to_tuple(ym):
    y, m = ym.split("-")
    return int(y), int(m)


def add_months(y, m, n):
    idx = (y * 12 + (m - 1)) + n
    return idx // 12, idx % 12 + 1


def month_range(start_ym, n):
    y, m = ym_to_tuple(start_ym)
    out = []
    for i in range(n):
        yy, mm = add_months(y, m, i)
        out.append(f"{yy}-{mm:02d}")
    return out


# ──────────────────────────────────────────────────────────
# 예측 엔진
#   method="seasonal" : 해당 월의 과거 실적 평균을 그대로 반복
#                        (예: 9월 예측 = 과거 모든 9월 실적의 평균)
#                        → 낙관 시나리오에서 썼던 방식, 특정 스파이크에 덜 흔들림
#   method="trend"     : 최근 N개월 평균 x 계절지수
#                        → 신중/비관 시나리오에서 썼던 방식, 최근 추세를 더 반영
# ──────────────────────────────────────────────────────────
class ForecastEngine:
    def __init__(self, history: dict):
        """history: {"YYYY-MM": qty, ...}"""
        self.history = history

    def seasonal_index(self):
        """월별(1~12) 계절지수 = 그 달 평균 ÷ 전체 평균"""
        vals = list(self.history.values())
        if not vals:
            return {m: 1.0 for m in range(1, 13)}
        overall = statistics.mean(vals)
        by_month = defaultdict(list)
        for ym, v in self.history.items():
            _, m = ym_to_tuple(ym)
            by_month[m].append(v)
        return {
            m: (statistics.mean(vs) / overall if overall else 1.0)
            for m, vs in by_month.items()
        }

    def base_level(self, window=4, exclude=None):
        """최근 window개월 평균 (exclude에 지정한 연월은 제외 — 광고 스파이크 등 이상치 제거용)"""
        exclude = set(exclude or [])
        items = sorted((ym, v) for ym, v in self.history.items() if ym not in exclude)
        recent = items[-window:]
        if not recent:
            return 0
        return statistics.mean(v for _, v in recent)

    def forecast(self, start_ym, months=6, method="seasonal", scenario="base",
                 window=4, exclude=None):
        scenario_map = {"base": 1.0, "optimistic": 1.10, "conservative": 0.90}
        try:
            scenario_mult = float(scenario)
        except (TypeError, ValueError):
            scenario_mult = scenario_map.get(scenario, 1.0)

        months_list = month_range(start_ym, months)
        results = {}

        if method == "seasonal":
            by_month = defaultdict(list)
            for ym, v in self.history.items():
                _, m = ym_to_tuple(ym)
                by_month[m].append(v)
            for ym in months_list:
                _, m = ym_to_tuple(ym)
                vs = by_month.get(m)
                val = statistics.mean(vs) if vs else self.base_level(window, exclude)
                results[ym] = round(val * scenario_mult)
        else:  # trend
            idx = self.seasonal_index()
            base = self.base_level(window, exclude)
            for ym in months_list:
                _, m = ym_to_tuple(ym)
                val = base * idx.get(m, 1.0)
                results[ym] = round(val * scenario_mult)

        return results


# ──────────────────────────────────────────────────────────
# 저장소 (history / forecasts 관리)
# ──────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.history = load_json(HIST_FILE, {})
        self.forecasts = load_json(FCST_FILE, {})
        self.meta = load_json(META_FILE, {})
        self.inventory = load_json(INV_FILE, {})
        self.projects = load_json(PROJ_FILE, {"stage_template": [], "projects": []})

    # ── 상품 등록 (신상품관리 화면에서 새 제품 추가) ──
    def save_product(self, name, brand, unit="개", code="", cost=None, is_new=True):
        """products.json 에 상품을 추가하거나 기존 항목을 갱신한다."""
        name = (name or "").strip()
        if not name:
            raise ValueError("상품명이 비어 있어요.")
        entry = dict(self.meta.get(name) or {})
        entry["brand"] = (brand or "").strip() or "미분류"
        entry["unit"] = (unit or "개").strip() or "개"
        if code is not None:
            entry["code"] = str(code).strip()
        if cost is not None and str(cost).strip() != "":
            entry["cost"] = float(cost)      # 수입원가 (개당)
        entry["new"] = bool(is_new)
        self.meta[name] = entry
        save_json(META_FILE, self.meta)
        return name

    def delete_product(self, name):
        if name in self.meta:
            del self.meta[name]
            save_json(META_FILE, self.meta)

    # ── 신제품 출시 프로젝트 ──
    def save_project(self, proj):
        """프로젝트 추가/수정. id가 있으면 덮어쓰고 없으면 새로 만든다."""
        lst = self.projects.setdefault("projects", [])
        pid = (proj.get("id") or "").strip()
        if not pid:
            pid = "p" + datetime.now().strftime("%y%m%d%H%M%S")
            proj["id"] = pid
        for i, p in enumerate(lst):
            if p.get("id") == pid:
                lst[i] = {**p, **proj}
                break
        else:
            proj.setdefault("logs", [])
            proj.setdefault("links", [])
            proj.setdefault("stages", [
                {"name": n, "start": "", "end": "", "done": False}
                for n in self.projects.get("stage_template", [])
            ])
            lst.append(proj)
        save_json(PROJ_FILE, self.projects)
        return pid

    def delete_project(self, pid):
        lst = self.projects.setdefault("projects", [])
        self.projects["projects"] = [p for p in lst if p.get("id") != pid]
        save_json(PROJ_FILE, self.projects)

    def add_project_log(self, pid, date, text):
        """일지 한 줄 추가 (최신이 위로 오도록 날짜 내림차순 정렬)."""
        for p in self.projects.get("projects", []):
            if p.get("id") == pid:
                p.setdefault("logs", []).insert(0, {"date": date, "text": text})
                p["logs"].sort(key=lambda x: x.get("date", ""), reverse=True)
                save_json(PROJ_FILE, self.projects)
                return True
        return False

    def delete_project_log(self, pid, date, text):
        for p in self.projects.get("projects", []):
            if p.get("id") == pid:
                logs = p.get("logs", [])
                for i, lg in enumerate(logs):
                    if lg.get("date") == date and lg.get("text") == text:
                        logs.pop(i)
                        save_json(PROJ_FILE, self.projects)
                        return True
        return False

    def products(self):
        return sorted(self.history.keys())

    def add_actual(self, product, ym, qty):
        if product not in self.history:
            self.history[product] = {}
        self.history[product][ym] = qty
        save_json(HIST_FILE, self.history)

    def set_stock(self, product, ym, qty, day=None, wip=None):
        """재고 실사 기준값 기록 (재고를 직접 세어봤을 때 사용, 이후 출고량은 이 값에서 차감됨).
        day: 실사한 날(1~31). 안 주면 그 달이 이번 달일 땐 오늘 날짜, 아니면 1일로 본다.
        wip: 이 값 중 '작업중'(선물세트 조립 등으로 묶여 있는) 물량. 재고 실사값(qty)에는
             포함해서 보여주지만, 재고소진 예측에서는 이 물량을 빼고 계산한다 — 조립이 끝나면
             이 상품(낱개) 재고로 돌아오지 않고 다른 상품(세트)이 되기 때문이다.
             None 이면 '모름'으로 보고 기존 기록을 지운다 (수동 입력 등 ERP 아닌 경로)."""
        inv = self.inventory.setdefault(product, {})
        inv.setdefault("stock", {})[ym] = qty
        try:
            d = int(day)
        except (TypeError, ValueError):
            today = datetime.now()
            d = today.day if ym == today.strftime("%Y-%m") else 1
        inv.setdefault("stock_day", {})[ym] = max(1, min(31, d))
        if wip is not None:
            inv.setdefault("stock_wip", {})[ym] = wip
        else:
            inv.get("stock_wip", {}).pop(ym, None)
        save_json(INV_FILE, self.inventory)

    def add_shipment(self, product, ym, qty):
        """해당 월 출고량 기록"""
        self.inventory.setdefault(product, {}).setdefault("shipments", {})[ym] = qty
        save_json(INV_FILE, self.inventory)

    INBOUND_PERIODS = ("early", "mid", "late")     # 월초 / 월중순 / 월말

    def lock_inbound(self, product, ym):
        """이 달 입고는 사람이 손으로 정한 것 — ERP 재고표가 덮어쓰지 못하게 잠근다.

        ERP 입고예정에 있는 건을 화면에서 지워도, 다음 재고표를 올리면 되살아나는
        문제가 있었다. 손으로 고친 달은 여기에 표시해두고 apply 에서 건너뛴다.
        """
        inv = self.inventory.setdefault(product, {})
        inv.setdefault("inbound_lock", {})[ym] = True
        save_json(INV_FILE, self.inventory)

    def unlock_inbound(self, product, ym):
        """잠금을 풀어 다시 ERP 값을 따라가게 한다."""
        inv = self.inventory.get(product, {})
        lock = inv.get("inbound_lock")
        if lock and lock.pop(ym, None) is not None:
            if not lock:
                inv.pop("inbound_lock", None)   # 빈 껍데기는 남기지 않는다
            save_json(INV_FILE, self.inventory)

    def inbound_locked(self, product, ym):
        return bool(self.inventory.get(product, {}).get("inbound_lock", {}).get(ym))

    def add_inbound(self, product, ym, qty, when=None, manual=False):
        """해당 월 추가 발주(입고)량 기록 — 재고에 더해짐.
        when: 'early'(월초) | 'mid'(중순) | 'late'(월말). 안 주면 기존 값 유지, 없으면 'mid'.
        manual=True 면 사람이 화면에서 넣은 값이라 ERP 가 못 덮게 잠근다."""
        inv = self.inventory.setdefault(product, {})
        inv.setdefault("inbound", {})[ym] = qty
        at = inv.setdefault("inbound_at", {})
        if when in self.INBOUND_PERIODS:
            at[ym] = when
        elif ym not in at:
            at[ym] = "mid"            # 시점을 모르면 월 중순 도착으로 본다
        if manual:
            inv.setdefault("inbound_lock", {})[ym] = True
        save_json(INV_FILE, self.inventory)

    def set_inbound_detail(self, product, ym, items):
        """그 달 입고예정의 원래 내역을 남긴다 — [{"d":"2026-09-09","q":12240}, ...]

        재고 계산은 월 단위(inbound)로 하지만, 화면에는 '언제 몇 개' 를 보여줘야
        사장님이 발주 시점을 판단할 수 있다."""
        inv = self.inventory.setdefault(product, {})
        det = inv.setdefault("inbound_detail", {})
        if items:
            det[ym] = items
            days = [int(x["d"][8:10]) for x in items if len(x.get("d") or "") >= 10]
            if days:
                inv.setdefault("inbound_day", {})[ym] = min(days)
        else:
            det.pop(ym, None)
            inv.get("inbound_day", {}).pop(ym, None)
        save_json(INV_FILE, self.inventory)

    def set_inbound_period(self, product, ym, when):
        if when not in self.INBOUND_PERIODS:
            raise ValueError("입고 시점이 올바르지 않습니다.")
        inv = self.inventory.setdefault(product, {})
        if ym not in (inv.get("inbound") or {}):
            raise ValueError("그 달에 입고 예정이 없습니다.")
        inv.setdefault("inbound_at", {})[ym] = when
        save_json(INV_FILE, self.inventory)

    def add_entry(self, product, ym, qty):
        """실적(수요)과 출고량을 같은 값으로 한 번에 기록 (두 값은 동일하게 취급)"""
        self.add_actual(product, ym, qty)
        self.add_shipment(product, ym, qty)

    def delete_month(self, product, ym):
        """해당 월의 실적·출고·입고를 모두 삭제 (그래프에서도 사라짐). 재고 실사값은 건드리지 않음."""
        if product in self.history and ym in self.history[product]:
            del self.history[product][ym]
            save_json(HIST_FILE, self.history)
        inv = self.inventory.get(product, {})
        changed = False
        for key in ("shipments", "inbound"):
            if key in inv and ym in inv[key]:
                del inv[key][ym]
                changed = True
        if changed:
            # 입고를 지운 건 사람의 결정 — 다음 재고표가 되살리지 못하게 잠근다
            inv.setdefault("inbound_lock", {})[ym] = True
            inv.get("inbound_detail", {}).pop(ym, None)
            inv.get("inbound_day", {}).pop(ym, None)
            save_json(INV_FILE, self.inventory)

    def clear_field(self, product, ym, field):
        """월별 입력칸을 비운 채로 저장했을 때 그 항목만 지운다.
        field: 'entry'(실적·출고) | 'inbound'(추가발주) | 'stock'(재고실사)"""
        if field == "entry":
            if product in self.history and ym in self.history[product]:
                del self.history[product][ym]
                save_json(HIST_FILE, self.history)
            inv = self.inventory.get(product, {})
            if "shipments" in inv and ym in inv["shipments"]:
                del inv["shipments"][ym]
                save_json(INV_FILE, self.inventory)
            return
        key = {"inbound": "inbound", "stock": "stock"}.get(field)
        if not key:
            raise ValueError("알 수 없는 항목이에요.")
        inv = self.inventory.get(product, {})
        changed = False
        if key in inv and ym in inv[key]:
            del inv[key][ym]
            changed = True
        # 딸려 있는 부가 정보(입고 시점 / 실사한 날)도 같이 지운다
        side = {"inbound": "inbound_at", "stock": "stock_day"}[field]
        if side in inv and ym in inv[side]:
            del inv[side][ym]
            changed = True
        if field == "inbound":
            # 지운 것도 사람의 결정이다 — 다음 재고표가 되살리지 못하게 잠근다
            inv.setdefault("inbound_lock", {})[ym] = True
            inv.get("inbound_detail", {}).pop(ym, None)
            inv.get("inbound_day", {}).pop(ym, None)
            changed = True
        if changed:
            save_json(INV_FILE, self.inventory)

    def inventory_series(self, product):
        """월별 재고 추이 계산: 재고실사 값에서 시작해 매달 출고량만큼 차감하고 입고량만큼 더함.
        반환: [{"month","qty","base","inbound","is_snapshot","shipment"}, ...] (연월 오름차순)
          - qty  : 그 달 말 재고(=선그래프/막대 총높이)
          - base : 그 달 입고분을 뺀 재고 (막대 아래 칸)
          - inbound : 그 달 새로 들어온 입고량 (막대 위에 다른 색으로 쌓임)"""
        inv = self.inventory.get(product, {})
        snapshots = inv.get("stock", {})
        shipments = inv.get("shipments", {})
        inbound = inv.get("inbound", {})
        if not snapshots:
            return []
        all_months = sorted(set(snapshots) | set(shipments) | set(inbound))
        start_y, start_m = ym_to_tuple(all_months[0])
        end_y, end_m = ym_to_tuple(all_months[-1])
        n = (end_y * 12 + end_m) - (start_y * 12 + start_m) + 1
        full_months = month_range(all_months[0], n)

        out = []
        running = None
        for ym in full_months:
            is_snap = ym in snapshots
            inb = inbound.get(ym, 0)
            inb_disp = None
            if is_snap:
                running = snapshots[ym]
                # 재고를 센 날보다 '나중에' 들어오는 입고는 그 실사값에 안 잡혀 있다.
                # 예전엔 실사가 있는 달의 입고를 통째로 버려서, 월말 재고가 그만큼 적게 나왔다.
                if inb:
                    snap_d = inv.get("stock_day", {}).get(ym) or 1
                    when = inv.get("inbound_at", {}).get(ym) or "mid"
                    arrive_d = {"early": 5, "mid": 15, "late": 25}.get(when, 15)
                    if arrive_d > snap_d:
                        running += inb
                        inb_disp = inb
            elif running is not None:
                running = running - shipments.get(ym, 0) + inb
                inb_disp = inb or None
            if running is not None:
                base = running - (inb if inb_disp else 0)
                out.append({
                    "month": ym,
                    "qty": running,
                    "base": max(0, base),
                    "inbound": inb_disp,
                    "inbound_at": (inv.get("inbound_at", {}).get(ym) or "mid") if inb_disp else None,
                    "is_snapshot": is_snap,
                    "snap_day": (inv.get("stock_day", {}).get(ym) or 1) if is_snap else None,
                    "wip": (inv.get("stock_wip", {}).get(ym) or 0) if is_snap else None,
                    "shipment": shipments.get(ym),
                })
        return out

    def save_forecast(self, product, method, scenario, values, start_ym):
        vintage = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = {
            "vintage": vintage,
            "method": method,
            "scenario": str(scenario),
            "start": start_ym,
            "values": values,
        }
        self.forecasts.setdefault(product, []).append(entry)
        save_json(FCST_FILE, self.forecasts)
        return entry

    def compare(self, product):
        """이 상품의 모든 예측 vintage를 실제값과 나란히 비교"""
        hist = self.history.get(product, {})
        out = []
        for entry in self.forecasts.get(product, []):
            for ym, fval in entry["values"].items():
                aval = hist.get(ym)
                err = None
                if aval is not None and fval:
                    err = (aval - fval) / fval * 100
                out.append({
                    "vintage": entry["vintage"],
                    "method": entry["method"],
                    "scenario": entry["scenario"],
                    "month": ym,
                    "forecast": fval,
                    "actual": aval,
                    "error_pct": err,
                })
        return sorted(out, key=lambda r: (r["month"], r["vintage"]))


# ──────────────────────────────────────────────────────────
# CLI 명령어들
# ──────────────────────────────────────────────────────────
DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="ko" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UB Korea 수요예측 대시보드</title>
<style>__CSS__</style>
</head>
<body>
<div class="viz-root">

  <!-- 로그인 게이트 -->
  <div id="loginGate" class="login-gate">
    <div class="login-card">
      <div class="login-logo">🔒</div>
      <h2>UB Korea 통합 대시보드</h2>
      <div class="login-sub">수요예측 · 통관관리 — 비밀번호를 입력하세요</div>
      <input type="password" id="loginPw" class="login-input" placeholder="••••" autocomplete="off" inputmode="numeric">
      <button id="loginBtn" class="login-btn" type="button">들어가기</button>
      <div id="loginErr" class="login-err" hidden>비밀번호가 올바르지 않아요.</div>
    </div>
  </div>

  <!-- 앱 본체 -->
  <div id="appShell" hidden>
    <div class="app-topbar">
      <div class="topbar-left">
        <span class="topbar-logo">UB Korea</span>
        <nav class="topnav">
          <button class="topnav-btn active" data-view="forecast" type="button">📈 수요예측</button>
          <button class="topnav-btn" data-view="newprod" type="button">🆕 신상품관리</button>
          <button class="topnav-btn" data-view="launch" type="button">🚀 신제품 출시</button>
          <button class="topnav-btn" data-view="ecom" type="button">🛒 이커머스 관리</button>
          <button class="topnav-btn" data-view="schema" type="button">🗄️ Database Schema</button>
          <button class="topnav-btn" data-view="wlog" type="button">📓 업무일지</button>
          <button class="topnav-btn" data-view="customs" type="button">🛃 통관 관리</button>
        </nav>
      </div>
      <div class="topbar-right">
        <div id="fxWidget" class="fx-widget loading">환율 불러오는 중…</div>
        <button class="theme-btn" id="themeToggle" type="button">다크 모드</button>
      </div>
    </div>

    <!-- 수요예측 뷰 -->
    <div id="viewForecast" class="app-view">
      <header class="viz-header">
        <div>
          <h1>수요예측 대시보드</h1>
          <div class="viz-sub">생성 시각: __GENERATED_AT__ · 21개 SKU 실적·예측·재고</div>
        </div>
        <div>
          <button class="cust-btn ghost" id="tableauExportBtn" type="button">📊 태블로로 내보내기(구글시트)</button>
          <button class="cust-btn ghost" id="tableauExportExcelBtn" type="button">📊 태블로로 내보내기(엑셀)</button>
          <button class="cust-btn ghost" id="poExportBtn" type="button">📦 구매발주 내보내기(구글시트)</button>
          <button class="cust-btn ghost" id="poExportExcelBtn" type="button">📦 구매발주 내보내기(엑셀)</button>
          <div class="msg" id="tableauExportMsg"></div>
          <div class="msg" id="poExportMsg"></div>
        </div>
      </header>

      <section class="card">
        <h2>📊 ERP 재고표로 출고량·재고 갱신</h2>
        <div class="card-sub">ERP에서 내려받은 <b>재고표(.xls)</b>를 끌어다 놓으면
          <b>금월 출고수량 − 홀딩재고 = 실제 출고량</b>으로 계산해 그 달의 출고량과 재고 실사를 한 번에 갱신합니다.
          바꾸기 전에 미리보기로 먼저 보여드려요.</div>
        <div class="inv-drop" id="erpDrop">
          <div class="inv-ico">📊</div>
          <div><b>재고표(.xls)를 여기로 끌어다 놓으세요</b></div>
          <div class="inv-sub">또는 <button type="button" class="inv-pick" id="erpPick">파일 선택</button></div>
          <input type="file" id="erpFile" accept=".xls,.xlsx,.htm,.html" hidden>
        </div>
        <div id="erpStatus" class="inv-status"></div>
      </section>

      <div class="filters">
        <div class="brand-filter" id="brandFilter"></div>
        <input type="search" class="search-input" id="productSearch" placeholder="상품 검색 (예: 아보카도)">
      </div>

      <section class="kpis" id="kpiRow"></section>

      <!-- 오늘의 핵심 — 사장님이 매일 보는 두 가지 -->
      <section class="today" id="todayCard">
        <div class="today-head">
          <h2 id="todayTitle">오늘의 핵심</h2>
          <span class="today-asof" id="todayAsOf"></span>
        </div>
        <div class="today-grid">
          <div class="today-col">
            <div class="today-col-head">
              <span class="today-dot up"></span>
              <span class="today-col-t">갑자기 뛴 출고</span>
              <span class="today-cnt" id="surgeCnt"></span>
            </div>
            <div class="today-list" id="surgeList"></div>
          </div>
          <div class="today-col">
            <div class="today-col-head">
              <span class="today-dot down"></span>
              <span class="today-col-t">5개월 안에 재고 바닥</span>
              <span class="today-cnt" id="shortCnt"></span>
            </div>
            <div class="today-list" id="shortList"></div>
          </div>
        </div>
      </section>

      <section class="card" id="attnCard">
        <div class="attn-head">
          <h2>⚠️ 지금 봐야 할 것</h2>
          <span class="card-sub" id="attnSub"></span>
          <span class="attn-filters" id="attnFilters"></span>
        </div>
        <div id="attnList"></div>
      </section>

      <section class="card">
        <h2>브랜드별 월간 실적 추이</h2>
        <div class="card-sub">브랜드에 속한 상품들의 월별 실적 합계예요.</div>
        <div id="brandChart"></div>
      </section>

      <section class="card">
        <h2>브랜드별 출고 비중 <span id="pieMonthLabel" class="card-sub"></span></h2>
        <div class="card-sub">조각에 마우스를 올리면 그 브랜드의 SKU별 수량·비중이 나와요.</div>
        <div class="pie-controls">
          <label for="pieMonth">기준월</label>
          <select id="pieMonth"></select>
        </div>
        <div id="brandPie"></div>
      </section>

      <section class="card">
        <h2>상품별 개요 <span id="sparkCount" class="card-sub"></span></h2>
        <div class="card-sub">카드를 클릭하면 아래 상세 차트에 표시돼요. (가로로 드래그해서 넘겨보세요)</div>
        <div class="sparkline-grid" id="sparkGrid"></div>
      </section>

      <section class="card" id="detailCard">
        <h2 id="detailTitle">상품 상세</h2>
        <div class="card-sub" id="detailSub"></div>
        <div id="stockNow"></div>
        <div id="inputSection"></div>
        <div id="editSection"></div>
        <div id="forecastSection"></div>
        <div id="detailChart"></div>
        <div class="delaybar" id="delayBar">
          <div class="delaybar-head">
            <span class="delaybar-t">입고 지연 시뮬레이션</span>
            <span class="delaybar-val" id="delayVal">지연 없음</span>
            <button type="button" class="delaybar-reset" id="delayReset">되돌리기</button>
          </div>
          <div class="delaybar-track" id="delayTrack">
            <div class="delaybar-block" id="delayBlock">📦 입고</div>
          </div>
          <div class="delaybar-note" id="delayNote"></div>
        </div>
        <div class="card-sub" id="detailLegendNote"></div>
        <button class="table-toggle-btn" id="detailTableToggle" type="button">실적 표로 보기</button>
        <div id="detailTableWrap" style="display:none"></div>
        <div id="inventorySection"></div>
        <div id="compareSection"></div>
      </section>
    </div>

    <!-- 신상품관리 뷰 -->
    <div id="viewNewProd" class="app-view" hidden>
      <header class="viz-header">
        <div>
          <h1>신상품 관리</h1>
          <div class="viz-sub">출시 초기 상품 — 실적이 짧아 수요예측 화면에서는 빼고 여기서 따로 봐요</div>
        </div>
      </header>

      <section class="kpis" id="npKpiRow"></section>

      <section class="card" id="npDailyCard">
        <div class="attn-head">
          <h2>📅 하루하루 얼마나 나갔나</h2>
          <span class="card-sub" id="npDailySub"></span>
        </div>
        <div id="npDaily"></div>
      </section>

      <section class="card">
        <h2>출시 후 월별 출고 추이</h2>
        <div class="card-sub">출시월부터의 실적이에요. 막대에 마우스를 올리면 월·수량이 나와요.</div>
        <div id="npChart"></div>
      </section>

      <section class="card">
        <div class="np-head">
          <h2>신상품 목록 <span id="npCount" class="card-sub"></span></h2>
          <button type="button" class="cust-btn" id="npAdd">➕ 새 제품 추가</button>
        </div>
        <div class="card-sub">카드를 클릭하면 아래 표에 월별 실적이 펼쳐져요.</div>
        <div class="np-tabs" id="npTabs"></div>
        <div class="np-grid" id="npGrid"></div>
        <div id="npDetail"></div>
      </section>

      <section class="card" id="npEditCard" hidden>
        <h2 id="npEditTitle">월별 값 입력 · 예측</h2>
        <div class="card-sub">수요예측 탭과 똑같이 월별 실적·입고·재고를 넣고, 예측식도 돌려볼 수 있어요.</div>
        <div id="npEditSection"></div>
        <div id="npForecastSection"></div>
        <div id="npFcChart"></div>
        <div class="legend-note" id="npFcNote"></div>
      </section>
    </div>

    <!-- 업무일지 뷰 -->
    <div id="viewLog" class="app-view" hidden>
      <header class="viz-header">
        <div>
          <h1>업무일지</h1>
          <div class="viz-sub">날짜별 기록 · 파일은 구글드라이브 <b>업무일지/날짜</b> 폴더에 저장돼요</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="cust-btn ghost" id="wlToday" type="button">오늘로</button>
          <input type="date" id="wlDate" class="wl-date">
        </div>
      </header>

      <section class="card">
        <h2 id="wlTitle">오늘의 기록</h2>
        <div class="card-sub">쓰는 대로 저장돼요. 날짜를 바꾸면 그날 기록이 나옵니다.</div>
        <textarea id="wlText" class="wl-text" placeholder="오늘 있었던 일, 통화·메일 내용, 결정사항 등을 남겨두세요."></textarea>
        <div class="wl-status" id="wlStatus"></div>
      </section>

      <section class="card">
        <h2>파일 첨부 <span id="wlFileCount" class="card-sub"></span></h2>
        <div class="card-sub">파일을 아래로 끌어다 놓으면 <b>파일명을 정한 뒤</b> 구글드라이브에 올라가요.</div>
        <div class="wl-drop" id="wlDrop">
          <div class="wl-drop-in">
            <div class="wl-drop-ico">📎</div>
            <div><b>여기로 파일을 끌어다 놓으세요</b></div>
            <div class="wl-drop-sub">또는 <button type="button" class="wl-pick" id="wlPick">파일 선택</button> · 40MB 이하</div>
          </div>
          <input type="file" id="wlFile" multiple hidden>
        </div>
        <div id="wlFiles"></div>
      </section>

      <section class="card">
        <h2>지난 기록 <span id="wlPastCount" class="card-sub"></span></h2>
        <div id="wlPast"></div>
      </section>
    </div>
    <!-- /업무일지 뷰 -->

    <!-- 이커머스 관리 뷰 -->
    <div id="viewEcom" class="app-view" hidden>
      <header class="viz-header">
        <div>
          <h1>이커머스 관리</h1>
          <div class="viz-sub">SKU별 월별·채널별 판매실적 관리</div>
        </div>
        <a class="cust-btn ghost" id="ecomOpen" href="/ecommerce.html" target="_blank" rel="noopener"
           style="text-decoration:none;line-height:34px">새 창에서 열기 ↗</a>
      </header>
      <div class="ecom-frame"><iframe id="ecomFrame" title="이커머스 관리"></iframe></div>
    </div>
    <!-- /이커머스 관리 뷰 -->

    <!-- Database Schema 뷰 -->
    <div id="viewSchema" class="app-view" hidden>
      <header class="viz-header">
        <div>
          <h1>Database Schema</h1>
          <div class="viz-sub">Supabase의 현재 테이블·컬럼·PK/FK·파티션 구조를 매번 새로 읽습니다.</div>
        </div>
        <button class="cust-btn" id="schemaRefresh" type="button">↻ 스키마 새로고침</button>
      </header>
      <div id="schemaStatus" class="schema-status">Supabase 스키마를 불러올 준비가 됐습니다.</div>
      <div class="schema-layout">
        <section class="card schema-graph-card">
          <div class="schema-card-head">
            <div>
              <h2>Relation graph</h2>
              <div class="card-sub">카드를 누르면 오른쪽에서 컬럼과 관계를 자세히 볼 수 있어요.</div>
            </div>
            <input type="search" class="search-input schema-search" id="schemaSearch" placeholder="테이블·컬럼 검색">
          </div>
          <div id="schemaGraph" class="schema-graph"></div>
        </section>
        <aside class="card schema-detail" id="schemaDetail">
          <div class="empty-note">테이블을 선택하면 상세 구조가 표시됩니다.</div>
        </aside>
      </div>
    </div>
    <!-- /Database Schema 뷰 -->

    <!-- 신제품 출시 관리 뷰 -->
    <div id="viewLaunch" class="app-view" hidden>
      <header class="viz-header">
        <div>
          <h1>신제품 출시 관리</h1>
          <div class="viz-sub">기획부터 입고까지 — 단계별 타임라인과 날짜별 기록</div>
        </div>
        <button class="cust-btn" id="lnNewBtn" type="button">+ 새 프로젝트</button>
      </header>

      <section class="card">
        <h2>전체 타임라인 <span id="lnTlRange" class="card-sub"></span></h2>
        <div class="card-sub">막대에 마우스를 올리면 단계·기간이 나와요. 세로선이 오늘이에요.</div>
        <div id="lnTimeline"></div>
      </section>

      <section class="card">
        <h2>프로젝트 <span id="lnCount" class="card-sub"></span></h2>
        <div class="ln-grid" id="lnGrid"></div>
      </section>

      <section class="card" id="lnDetailCard">
        <div id="lnDetail"></div>
      </section>
    </div>

    <!-- /신제품 출시 관리 뷰 -->

    <!-- 통관 관리 뷰 -->
    <div id="viewCustoms" class="app-view" hidden>
      <header class="viz-header">
        <div>
          <h1>통관 관리 (UNI-PASS)</h1>
          <div class="viz-sub">관세청 API 연동 · BL 조회 + 통관 트래킹 보드</div>
        </div>
      </header>

      <section class="card">
        <h2>BL / 화물관리번호 조회</h2>
        <div class="card-sub">통관진행상태 + (자동 연동) 컨테이너 상세를 관세청에서 조회해요.</div>
        <div class="cust-form">
          <div class="cust-field">
            <label>조회 방식</label>
            <div class="cust-radio">
              <label><input type="radio" name="custMode" value="cargo" checked> 화물관리번호</label>
              <label><input type="radio" name="custMode" value="mbl"> Master B/L</label>
              <label><input type="radio" name="custMode" value="hbl"> House B/L</label>
            </div>
          </div>
          <div class="cust-field"><label>번호</label><input type="text" id="custNumber" placeholder="번호 입력" style="width:220px"></div>
          <div class="cust-field"><label>BL 발급연도</label><input type="text" id="custYear" value="2026" style="width:80px" disabled></div>
          <button class="cust-btn" id="custQueryBtn" type="button">조회</button>
        </div>
        <div id="custQueryResult"></div>
      </section>

      <section class="card">
        <h2>📄 인보이스 PDF로 주문 등록</h2>
        <div class="card-sub">인보이스 PDF를 아래로 끌어다 놓으면 품목·중량·BL·선적일을 읽어 <b>확인 화면</b>을 띄웁니다. 서식이 달라 못 읽으면 <b>클로드가 대신 읽고</b>, 숫자가 앞뒤로 맞는지까지 검증해서 알려줍니다.</div>
        <div class="inv-drop" id="invDrop">
          <div class="inv-ico">📄</div>
          <div><b>인보이스 PDF를 여기로 끌어다 놓으세요</b></div>
          <div class="inv-sub">또는 <button type="button" class="inv-pick" id="invPick">파일 선택</button></div>
          <input type="file" id="invFile" accept="application/pdf,.pdf" hidden>
        </div>
        <div id="invStatus" class="inv-status"></div>
        <div class="inv-key">
          <label>클로드 키</label>
          <input type="password" id="invKey" placeholder="sk-ant-... (한 번만 넣어두면 됩니다)" autocomplete="off">
          <button type="button" class="cust-btn ghost" id="invKeySave">저장</button>
          <div class="msg" id="invKeyMsg"></div>
        </div>
        <div class="inv-key">
          <label>딥시크 키</label>
          <input type="password" id="invDsKey" placeholder="sk-... (한 번만 넣어두면 됩니다)" autocomplete="off">
          <button type="button" class="cust-btn ghost" id="invDsKeySave">저장</button>
          <div class="msg" id="invDsKeyMsg"></div>
        </div>
        <div class="card-sub">두 키를 다 넣으면 <b>딥시크를 먼저 씁니다</b>(더 저렴함). 단, 딥시크는 글자를 못 읽는 스캔본 PDF는 처리 못 해요 — 그런 파일은 클로드 키만 넣어주세요.</div>
      </section>

      <section class="card">
        <h2>통관 트래킹 보드</h2>
        <div class="card-sub">구글시트의 주문(BL)을 불러와 관세청 상태로 단계를 자동 분류해요. 결과는 시트에도 저장돼요.</div>
        <div class="customs-toolbar">
          <button class="cust-btn" id="boardSyncBtn" type="button">① 시트에서 주문 불러오기</button>
          <button class="cust-btn orange" id="boardRefreshBtn" type="button">② 관세청 상태 새로고침</button>
          <button class="cust-btn ghost" id="boardAddBtn" type="button">+ 수동 추가</button>
          <button class="cust-btn ghost" id="boardSheetBtn" type="button">구글시트 열기</button>
        </div>
        <div class="board-status" id="boardStatus">‘① 시트에서 주문 불러오기’로 시작하세요.</div>
        <div class="board" id="boardCols"></div>
      </section>
    </div>
    <!-- /통관 관리 뷰 -->
  </div>

  <div id="modalRoot"></div>
  <div id="toast" class="toast" hidden></div>
</div>

<script id="viz-data" type="application/json">__VIZ_DATA_JSON__</script>
<script>__JS__</script>
</body>
</html>
"""


def build_dashboard_payload(store, live=False):
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "history": store.history,
        "forecasts": store.forecasts,
        "meta": store.meta,
        "compare": {p: store.compare(p) for p in store.products()},
        "inventory": {p: store.inventory_series(p) for p in store.inventory
                      if store.inventory_series(p)},
        "inventory_raw": store.inventory,
        "projects": store.projects,
        "worklog": (worklog.load() if worklog else {"days": {}}),
        "daily": _load_daily_safe(),
        "live": live,
    }


def _load_daily_safe():
    """ERP 일별 스냅샷 (월 안에서의 출고 변화). 없으면 빈 값."""
    try:
        import erp_stock
        return erp_stock.load_daily()
    except Exception:
        return {}


def dashboard_template():
    """화면 코드(CSS/JS)를 끼워 넣어 완성된 템플릿을 만든다.

    CSS 와 JS 는 `static/` 안의 진짜 .css / .js 파일에 있다. 파이썬 문자열 안에
    두던 걸 빼낸 것이라, 이제 JS 정규식에 `\\s` 를 두 번 쓰지 않아도 되고
    `node --check static/dashboard.js` 로 문법 검사도 바로 된다.
    끼워 넣은 결과는 예전 한 덩어리 템플릿과 글자 하나까지 똑같다.
    """
    css = (BASE_DIR / "static" / "dashboard.css").read_text(encoding="utf-8")
    js = (BASE_DIR / "static" / "dashboard.js").read_text(encoding="utf-8")
    return DASHBOARD_TEMPLATE.replace("__CSS__", css).replace("__JS__", js)


# 화면에서 감출 탭: (data-view 값, 버튼 글자, 주석 표시 이름)
# 비우면 [] 로 두면 전부 다시 보인다.
HIDDEN_VIEWS = [
    ("wlog", "📓 업무일지", "업무일지"),
    ("customs", "🛃 통관 관리", "통관 관리"),
]


def render_dashboard_html(payload, forecast_only=False):
    """대시보드 HTML 생성.

    forecast_only=True 면 로그인 화면과 통관 탭을 빼고 수요예측 화면만 남긴다.
    (공유·보기용 스냅샷. 서버 없이 파일만 열어도 바로 보이게)
    """
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = dashboard_template().replace("__GENERATED_AT__", payload["generated_at"]).replace("__VIZ_DATA_JSON__", data_json)

    # 화면에서 감출 탭 (2026-09: 수요예측에 집중하려고 업무일지·통관관리를 뺐다).
    # 코드(customs.py / worklog.py)와 구글시트 데이터는 그대로 두므로,
    # 이 목록을 비우면 그대로 다시 나타난다.
    for key, label, mark in HIDDEN_VIEWS:
        html = html.replace(
            '<button class="topnav-btn" data-view="%s" type="button">%s</button>' % (key, label), '')
        html = re.sub(r'<!-- %s 뷰 -->.*?<!-- /%s 뷰 -->' % (mark, mark), '', html, flags=re.S)

    if forecast_only:
        # 로그인 게이트 제거 + 앱 본체 바로 노출
        html = html.replace('<div id="loginGate" class="login-gate">', '<div id="loginGate" class="login-gate" hidden style="display:none">')
        html = html.replace('<div id="appShell" hidden>', '<div id="appShell">')
        # 통관 탭 버튼과 통관 화면 제거
        html = html.replace('<button class="topnav-btn" data-view="customs" type="button">🛃 통관 관리</button>', '')
        html = re.sub(r'<!-- 통관 관리 뷰 -->.*?<!-- /통관 관리 뷰 -->', '', html, flags=re.S)
        html = html.replace('<button class="topnav-btn" data-view="launch" type="button">🚀 신제품 출시</button>', '')
        html = html.replace('<button class="topnav-btn" data-view="ecom" type="button">🛒 이커머스 관리</button>', '')
        html = re.sub(r'<!-- 이커머스 관리 뷰 -->.*?<!-- /이커머스 관리 뷰 -->', '', html, flags=re.S)
        html = html.replace('<button class="topnav-btn" data-view="schema" type="button">🗄️ Database Schema</button>', '')
        html = re.sub(r'<!-- Database Schema 뷰 -->.*?<!-- /Database Schema 뷰 -->', '', html, flags=re.S)
        html = html.replace('<button class="topnav-btn" data-view="wlog" type="button">📓 업무일지</button>', '')
        html = re.sub(r'<!-- 업무일지 뷰 -->.*?<!-- /업무일지 뷰 -->', '', html, flags=re.S)
        html = re.sub(r'<!-- 신제품 출시 관리 뷰 -->.*?<!-- /신제품 출시 관리 뷰 -->', '', html, flags=re.S)
        # 로그인 없이 바로 시계/환율이 돌도록
        html = html.replace("if (sessionStorage.getItem('ub_auth')==='1'){ startFx(); }", "startFx();")
    return html


def run_server(store, port=8787, open_browser=True):
    """대시보드를 로컬 서버로 띄워서 브라우저에서 실적/출고량/재고를 바로 입력할 수 있게 함"""

    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            pass

        def _handle_worklog(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if worklog is None:
                self._json({"error": "worklog 모듈을 불러오지 못했어요."}, 500)
                return
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                self._json({"error": "요청 형식이 잘못됐어요."}, 400)
                return
            try:
                path = self.path
                if path == "/api/worklog/text":
                    worklog.set_text(body.get("date", ""), body.get("text", ""))
                    result = worklog.load()
                elif path == "/api/worklog/upload":
                    result = worklog.upload(body.get("date", ""), body.get("name", ""),
                                            body.get("data", ""), body.get("mime"))
                    result["log"] = worklog.load()
                elif path == "/api/worklog/delete":
                    worklog.remove_file(body.get("date", ""), body.get("id", ""))
                    result = worklog.load()
                elif path == "/api/worklog/folder":
                    result = worklog.folder_url(body.get("date", ""))
                else:
                    self._json({"error": "알 수 없는 요청이에요."}, 404)
                    return
                self._json(result)
            except Exception as e:
                self._json({"error": friendly_error(e)}, 400)

        def _handle_ecom(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if ecom_store is None:
                self._json({"error": "ecom_store 모듈을 불러오지 못했어요."}, 500)
                return
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                self._json({"error": "요청 형식이 잘못됐어요."}, 400)
                return
            try:
                path = self.path
                if path == "/api/ecom/load":
                    result = ecom_store.load()
                elif path == "/api/ecom/entry":
                    ecom_store.save_entry(body["plat"], body["sku"], body["ym"],
                                          body.get("q"), body.get("a"), body.get("c"))
                    result = {"ok": True}
                elif path == "/api/ecom/entry-delete":
                    ecom_store.delete_entry(body["plat"], body["sku"], body["ym"])
                    result = {"ok": True}
                elif path == "/api/ecom/ad":
                    ecom_store.save_ad(body["plat"], body["brand"], body["ym"], body.get("v"))
                    result = {"ok": True}
                elif path == "/api/ecom/setting":
                    ecom_store.save_setting(body["key"], body.get("value", ""))
                    result = {"ok": True}
                elif path == "/api/ecom/push":
                    n = ecom_store.push_all(body.get("perf"), body.get("ad"),
                                            body.get("settings"))
                    result = {"ok": True, "n": n, "url": ecom_store.sheet_url()}
                elif path == "/api/ecom/txn/plan":
                    import base64
                    import ecom_txn
                    files = [(f.get("name", ""), base64.b64decode(f.get("data", "")))
                             for f in (body.get("files") or [])]
                    result = ecom_txn.plan(files)
                elif path == "/api/ecom/txn/apply":
                    if supabase_sync is None:
                        result = {"configured": False, "ok": False,
                                  "message": "supabase_sync 모듈을 불러오지 못했습니다."}
                    else:
                        source_files = body.get("source_files") or []
                        source_name = ", ".join(str(x.get("name") or "") for x in source_files) or "ecommerce-upload"
                        result = supabase_sync.sync_ecommerce(
                            body.get("changes") or [], body.get("unmatched") or [], source_name)
                elif path == "/api/ecom/txn/daily":
                    import ecom_txn
                    result = {"series": ecom_txn.sku_daily_series(body.get("plat", ""), body.get("sku", ""))}
                elif path == "/api/ecom/txn/trend":
                    import ecom_txn
                    result = {"series": ecom_txn.daily_trend(body.get("plat", ""),
                                                             int(body.get("days") or 60))}
                elif path == "/api/ecom/share":
                    import ecom_txn
                    ym = body.get("ym") or ""
                    ecom = ecom_txn.month_totals(ym)
                    total = sum((v.get(ym) or 0) for v in store.history.values())
                    result = {"ym": ym, "total_qty": total, "ecom_qty": ecom["전체"],
                              "coupang_qty": ecom["쿠팡"], "kurly_qty": ecom["컬리"],
                              "other_qty": max(total - ecom["전체"], 0)}
                else:
                    self._json({"error": "알 수 없는 요청이에요."}, 404)
                    return
                self._json(result)
            except Exception as e:
                self._json({"error": friendly_error(e)}, 400)

        def _handle_project(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                self._json({"error": "요청 형식이 잘못됐어요."}, 400)
                return
            try:
                if self.path == "/api/project/save":
                    store.save_project(body.get("project") or {})
                elif self.path == "/api/project/delete":
                    store.delete_project(body.get("id", ""))
                elif self.path == "/api/project/log":
                    txt = (body.get("text") or "").strip()
                    if not txt:
                        self._json({"error": "내용을 입력해주세요."}, 400)
                        return
                    d = body.get("date") or datetime.now().strftime("%Y-%m-%d")
                    ym_to_tuple(d[:7])
                    store.add_project_log(body.get("id", ""), d, txt)
                elif self.path == "/api/project/log/delete":
                    store.delete_project_log(body.get("id", ""), body.get("date", ""),
                                             body.get("text", ""))
                else:
                    self._json({"error": "알 수 없는 요청이에요."}, 404)
                    return
            except (KeyError, ValueError, TypeError) as e:
                self._json({"error": "입력값을 확인해주세요. (" + str(e) + ")"}, 400)
                return
            self._json(build_dashboard_payload(store, live=True))

        def _handle_customs(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if customs is None:
                self._json({"error": "customs 모듈을 불러오지 못했어요. customs.py 를 확인해주세요."}, 500)
                return
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                self._json({"error": "요청 형식이 잘못됐어요."}, 400)
                return
            try:
                path = self.path
                if path == "/api/customs/query":
                    result = customs.query(body.get("mode", "cargo"),
                                           body.get("number", ""), body.get("year", ""))
                elif path == "/api/customs/sync":
                    result = customs.board_sync()
                elif path == "/api/customs/refresh":
                    result = customs.board_refresh()
                elif path == "/api/customs/add":
                    result = customs.board_add(body.get("bl", ""), body.get("mfr", ""),
                                               body.get("order", ""), body.get("shipped", ""))
                elif path == "/api/customs/edit":
                    result = customs.board_edit(int(body["sheet_row"]), body.get("values", {}))
                elif path == "/api/customs/delete":
                    result = customs.board_delete(int(body["sheet_row"]))
                elif path == "/api/customs/synckeys":
                    result = customs.sync_keys(body.get("url", ""))
                elif path == "/api/customs/order":
                    result = customs.order_detail(body.get("bl", ""))
                elif path == "/api/customs/invoice/parse":
                    # 정규식으로 먼저 읽고, 못 읽으면 클로드가 읽는다. 어느 쪽이든 정합성 검증은 붙는다.
                    import invoice_ai
                    result = invoice_ai.parse_best(body.get("data", ""),
                                                   force_ai=bool(body.get("force_ai")))
                elif path == "/api/customs/invoice/aikey":
                    import invoice_ai
                    invoice_ai.save_api_key(body.get("key", ""))
                    ok, why = invoice_ai.available()
                    result = {"ok": ok, "message": why or "클로드 검증을 쓸 수 있어요."}
                elif path == "/api/customs/invoice/dskey":
                    import invoice_ai
                    invoice_ai.save_deepseek_key(body.get("key", ""))
                    ok, why = invoice_ai.available()
                    result = {"ok": ok, "message": why or "딥시크 검증을 쓸 수 있어요."}
                elif path == "/api/customs/invoice/save":
                    result = customs.save_invoice_order(
                        body.get("tab", ""), body.get("order_name", ""),
                        body.get("bl", ""), body.get("shipped", ""),
                        body.get("eta", ""), body.get("items") or [],
                        body.get("pi", ""))
                elif path == "/api/customs/order/save":
                    result = customs.save_order_items(body["sheet"], body.get("items", []),
                                                      int(body.get("name_col") or 0),
                                                      int(body.get("pallet_col") or 0))
                else:
                    self._json({"error": "알 수 없는 통관 요청이에요."}, 404)
                    return
                self._json(result)
            except Exception as e:
                self._json({"error": friendly_error(e)}, 400)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._html(render_dashboard_html(build_dashboard_payload(store, live=True)))
            elif self.path == "/api/data":
                self._json(build_dashboard_payload(store, live=True))
            elif self.path == "/api/database/schema":
                if supabase_sync is None:
                    self._json({"configured": False, "tables": [],
                                "error": "supabase_sync 모듈을 불러오지 못했습니다."}, 500)
                else:
                    try:
                        self._json(supabase_sync.get_schema())
                    except Exception as e:
                        self._json({"configured": True, "tables": [],
                                    "error": friendly_error(e)}, 400)
            elif self.path == "/api/supabase/status":
                if supabase_sync is None:
                    self._json({"configured": False, "connected": False,
                                "message": "supabase_sync 모듈을 불러오지 못했습니다."})
                else:
                    self._json(supabase_sync.get_status())
            elif self.path == "/ecommerce.html":
                # 이커머스 관리 도구 (자체 CSS/JS를 가진 독립 문서 → iframe 으로 격리해 제공)
                f = BASE_DIR / "ecommerce.html"
                if f.exists():
                    self._html(f.read_text(encoding="utf-8"))
                else:
                    self._html("<p style='font-family:sans-serif;padding:24px'>"
                               "ecommerce.html 파일이 없어요.</p>")
            elif self.path == "/api/customs/status":
                if customs is None:
                    self._json({"available": False, "error": "customs 모듈을 불러오지 못했어요."})
                else:
                    st = customs.status()
                    st["available"] = True
                    try:
                        import invoice_ai
                        ai_ok, ai_why = invoice_ai.available()
                        st["ai_ok"] = ai_ok
                        st["ai_message"] = ai_why
                        st["ai_provider"] = invoice_ai._provider()
                    except Exception as e:
                        st["ai_ok"] = False
                        st["ai_message"] = "클로드 검증 모듈을 불러오지 못했어요: %s" % e
                    self._json(st)
            else:
                self.send_response(404)
                self.end_headers()

        def _handle_erp(self):
            import base64
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
                import erp_stock
                path = self.path.split("?")[0]
                if path == "/api/erp/stock/plan":
                    data = base64.b64decode(body.get("data", ""))
                    result = erp_stock.plan(data, store.meta, body.get("filename", ""))
                elif path == "/api/erp/stock/apply":
                    data = base64.b64decode(body.get("data", ""))
                    pl = erp_stock.plan(data, store.meta, body.get("filename", ""))
                    result = erp_stock.apply(store, pl, do_stock=bool(body.get("do_stock", True)))
                    result["plan"] = pl
                    if supabase_sync is None:
                        result["supabase"] = {"configured": False, "ok": False,
                                              "message": "supabase_sync 모듈을 불러오지 못했습니다."}
                    else:
                        result["supabase"] = supabase_sync.sync_erp_plan(
                            pl, body.get("filename", "inventory-upload.xls"), store.meta)
                elif path == "/api/erp/ignore":
                    ig = erp_stock.load_ignore()
                    codes = set(ig["codes"]) | {str(c).upper() for c in (body.get("add_codes") or [])}
                    names = set(ig["names"]) | set(body.get("add_names") or [])
                    codes -= {str(c).upper() for c in (body.get("remove_codes") or [])}
                    names -= set(body.get("remove_names") or [])
                    result = erp_stock.save_ignore(codes, names)
                elif path == "/api/erp/map":
                    m = erp_stock.load_map()
                    for k, v in (body.get("map") or {}).items():
                        if v:
                            m[k] = v
                        else:
                            m.pop(k, None)
                    erp_stock.save_map(m)
                    result = {"saved": len(m)}
                else:
                    self._json({"error": "알 수 없는 ERP 요청이에요."}, 404)
                    return
                self._json(result)
            except Exception as e:
                self._json({"error": friendly_error(e)}, 400)

        def do_POST(self):
            # ── 통관(UNI-PASS) 엔드포인트: 상품 기반 파싱 전에 먼저 처리 ──
            if self.path.startswith("/api/customs/"):
                self._handle_customs()
                return
            # ── ERP 재고표(.xls) → 출고량·재고 실사 갱신 ──
            if self.path.startswith("/api/erp/"):
                self._handle_erp()
                return
            # ── 신제품 출시 프로젝트 ──
            if self.path.startswith("/api/project"):
                self._handle_project()
                return
            # ── 업무일지 ──
            if self.path.startswith("/api/ecom"):
                self._handle_ecom()
                return
            if self.path.startswith("/api/worklog"):
                self._handle_worklog()
                return
            # ── 태블로 연동: 계산된 값을 구글시트로 통째로 내보낸다 ──
            if self.path == "/api/tableau/export":
                try:
                    import tableau_export
                    self._json(tableau_export.export(store))
                except Exception as e:
                    self._json({"error": friendly_error(e)}, 400)
                return
            # ── 태블로 연동(엑셀): 구글 인증 없이 바탕화면에 파일로 저장 ──
            if self.path == "/api/tableau/export-excel":
                try:
                    import tableau_export
                    self._json(tableau_export.export_excel(store))
                except Exception as e:
                    self._json({"error": friendly_error(e)}, 400)
                return
            # ── 구매발주 관리표: 브랜드-상품별 입고예정 + YoY + 12개월평균 ──
            if self.path == "/api/tableau/export-po":
                try:
                    import tableau_export
                    self._json(tableau_export.export_po(store))
                except Exception as e:
                    self._json({"error": friendly_error(e)}, 400)
                return
            if self.path == "/api/tableau/export-po-excel":
                try:
                    import tableau_export
                    self._json(tableau_export.export_po_excel(store))
                except Exception as e:
                    self._json({"error": friendly_error(e)}, 400)
                return

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
                product = str(body["product"]).strip()
                if not product:
                    raise ValueError("empty product")
                if self.path == "/api/actual":
                    ym_to_tuple(body["month"])
                    store.add_actual(product, body["month"], int(body["qty"]))
                elif self.path == "/api/shipment":
                    ym_to_tuple(body["month"])
                    store.add_shipment(product, body["month"], int(body["qty"]))
                elif self.path == "/api/entry":
                    ym_to_tuple(body["month"])
                    store.add_entry(product, body["month"], int(body["qty"]))
                elif self.path == "/api/inbound":
                    ym_to_tuple(body["month"])
                    # 화면에서 넣은 값이므로 ERP 가 덮어쓰지 못하게 잠근다
                    store.add_inbound(product, body["month"], int(body["qty"]),
                                      body.get("when"), manual=True)
                elif self.path == "/api/inbound/unlock":
                    ym_to_tuple(body["month"])
                    store.unlock_inbound(product, body["month"])
                elif self.path == "/api/delete":
                    ym_to_tuple(body["month"])
                    store.delete_month(product, body["month"])
                elif self.path == "/api/stock":
                    asof = body.get("asof") or datetime.now().strftime("%Y-%m")
                    ym_to_tuple(asof)
                    store.set_stock(product, asof, int(body["qty"]), body.get("day"))
                elif self.path == "/api/clear":
                    ym_to_tuple(body["month"])
                    store.clear_field(product, body["month"], body.get("field", "entry"))
                elif self.path == "/api/product":
                    store.save_product(product,
                                       body.get("brand", ""),
                                       body.get("unit", "개"),
                                       body.get("code", ""),
                                       body.get("cost"),
                                       bool(body.get("new", True)))
                elif self.path == "/api/product-delete":
                    store.delete_product(product)
                elif self.path == "/api/forecast":
                    hist = store.history.get(product)
                    if not hist:
                        self._json({"error": "실적 데이터가 없는 상품이에요."}, 400)
                        return
                    start = body["start"]
                    ym_to_tuple(start)
                    months = int(body.get("months") or 6)
                    method = body.get("method") or "seasonal"
                    scenario = str(body.get("scenario") or "base")
                    window = int(body.get("window") or 4)
                    exclude = body.get("exclude")
                    if isinstance(exclude, str):
                        exclude = [x.strip() for x in exclude.split(",") if x.strip()]
                    eng = ForecastEngine(hist)
                    values = eng.forecast(start, months, method, scenario,
                                          window=window, exclude=exclude or None)
                    # 그래프에서 직접 끌어 고친 달은 계산값 대신 그 값으로 덮어쓴다
                    override = body.get("values")
                    if isinstance(override, dict):
                        for ym, v in override.items():
                            if ym in values:
                                values[ym] = int(v)
                    store.save_forecast(product, method, scenario, values, start)
                else:
                    self._json({"error": "알 수 없는 요청이에요."}, 404)
                    return
            except (KeyError, ValueError, TypeError):
                self._json({"error": "입력값을 확인해주세요 (연월은 YYYY-MM 형식이어야 해요)."}, 400)
                return
            self._json(build_dashboard_payload(store, live=True))

    httpd = None
    port_try = port
    for _ in range(10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port_try), Handler)
            break
        except OSError:
            port_try += 1
    if httpd is None:
        print("사용 가능한 포트를 찾지 못했어요. --port 로 다른 포트를 지정해보세요.")
        return

    url = f"http://127.0.0.1:{port_try}/"
    print(f"대시보드 서버 시작 → {url}")
    print("실적·출고량·재고를 브라우저에서 바로 입력할 수 있어요. 종료하려면 이 터미널에서 Ctrl+C 를 누르세요.")
    if open_browser:
        try:
            webbrowser.get('chrome').open(url)
        except:
            webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료했어요.")
    finally:
        httpd.server_close()


def cmd_list(args, store):
    print(f"{'상품명':<28}{'브랜드':<24}{'실적개월수':>10}")
    print("-" * 64)
    for p in store.products():
        brand = store.meta.get(p, {}).get("brand", "-")
        n = len(store.history[p])
        print(f"  {p:<26}{brand:<24}{n:>8}개월")


def cmd_history(args, store):
    hist = store.history.get(args.product)
    if not hist:
        print("해당 상품 실적 데이터가 없어요:", args.product)
        return
    for ym in sorted(hist):
        print(f"  {ym}: {hist[ym]:,}")


def cmd_add(args, store):
    store.add_actual(args.product, args.month, args.qty)
    print(f"저장 완료 → {args.product} {args.month} = {args.qty:,}")


def cmd_set_stock(args, store):
    asof = args.asof or datetime.now().strftime("%Y-%m")
    store.set_stock(args.product, asof, args.qty)
    print(f"재고 실사값 저장 완료 → {args.product} {asof} 기준 {args.qty:,}개")


def cmd_add_shipment(args, store):
    store.add_shipment(args.product, args.month, args.qty)
    print(f"출고량 저장 완료 → {args.product} {args.month} 출고 {args.qty:,}개 (재고에서 자동 차감됨)")


def cmd_stock(args, store):
    series = store.inventory_series(args.product)
    if not series:
        print("재고 실사값이 없어요. 먼저 set-stock 으로 현재 재고를 입력하세요:", args.product)
        return
    for row in series:
        note = " (재고실사)" if row["is_snapshot"] else ""
        ship = f"  [출고 {row['shipment']:,}]" if row["shipment"] else ""
        print(f"  {row['month']}: {row['qty']:,}{note}{ship}")


def cmd_forecast(args, store):
    hist = store.history.get(args.product)
    if not hist:
        print("해당 상품 실적 데이터가 없어요:", args.product)
        return
    eng = ForecastEngine(hist)
    exclude = args.exclude.split(",") if args.exclude else None
    values = eng.forecast(
        args.start, args.months, args.method, args.scenario,
        window=args.window, exclude=exclude,
    )
    entry = store.save_forecast(args.product, args.method, args.scenario, values, args.start)
    print(f"[{args.product}] 새 예측 저장됨 (vintage={entry['vintage']}, method={args.method}, scenario={args.scenario})")
    for ym, v in values.items():
        print(f"  {ym}: {v:,}")


def cmd_compare(args, store):
    rows = store.compare(args.product)
    if not rows:
        print("비교할 예측 데이터가 없어요. 먼저 forecast 명령으로 예측을 생성하세요.")
        return
    print(f"{'월':<9}{'vintage':<18}{'방식':<10}{'시나리오':<13}{'예측':>10}{'실제':>10}{'오차%':>9}")
    print("-" * 79)
    for r in rows:
        err = f"{r['error_pct']:+.1f}%" if r["error_pct"] is not None else "-"
        act = f"{r['actual']:,}" if r["actual"] is not None else "(대기중)"
        print(f"{r['month']:<9}{r['vintage']:<18}{r['method']:<10}{r['scenario']:<13}{r['forecast']:>10,}{act:>10}{err:>9}")


def cmd_export(args, store):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    navy = "1A3A5C"

    def style_header(ws, row, ncols):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=row, column=c)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=navy)
            cell.alignment = Alignment(horizontal="center")

    ws1 = wb.active
    ws1.title = "실적"
    ws1.append(["상품", "브랜드", "연월", "수량"])
    style_header(ws1, 1, 4)
    for p, hist in store.history.items():
        brand = store.meta.get(p, {}).get("brand", "-")
        for ym, v in sorted(hist.items()):
            ws1.append([p, brand, ym, v])

    ws2 = wb.create_sheet("예측기록")
    ws2.append(["상품", "vintage", "방식", "시나리오", "연월", "예측치"])
    style_header(ws2, 1, 6)
    for p, entries in store.forecasts.items():
        for e in entries:
            for ym, v in sorted(e["values"].items()):
                ws2.append([p, e["vintage"], e["method"], e["scenario"], ym, v])

    ws3 = wb.create_sheet("예측vs실제")
    ws3.append(["상품", "연월", "vintage", "방식", "시나리오", "예측치", "실제치", "오차%"])
    style_header(ws3, 1, 8)
    for p in store.products():
        for r in store.compare(p):
            err = round(r["error_pct"], 1) if r["error_pct"] is not None else None
            ws3.append([p, r["month"], r["vintage"], r["method"], r["scenario"],
                        r["forecast"], r["actual"], err])

    for ws in (ws1, ws2, ws3):
        for col in ws.columns:
            width = max(len(str(c.value)) if c.value else 0 for c in col) + 2
            ws.column_dimensions[col[0].column_letter].width = min(width, 30)

    out = args.out or str(BASE_DIR / "forecast_export.xlsx")
    wb.save(out)
    print("엑셀로 내보냈어요 →", out)


def cmd_visualize(args, store):
    if not store.products():
        print("실적 데이터가 없어요. 먼저 add-actual 로 실적을 입력하세요.")
        return
    payload = build_dashboard_payload(store)
    html = render_dashboard_html(payload, forecast_only=getattr(args, "forecast_only", False))
    out = args.out or str(BASE_DIR / "forecast_dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("대시보드 생성 완료 →", out)
    if not args.no_open:
        try:
            webbrowser.get('chrome').open(Path(out).resolve().as_uri())
        except:
            webbrowser.open(Path(out).resolve().as_uri())


def cmd_serve(args, store):
    run_server(store, port=args.port, open_browser=not args.no_open)


def main():
    parser = argparse.ArgumentParser(description="UB Korea 수요예측 도구")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="등록된 상품 목록과 실적 보유 개월수")

    p_hist = sub.add_parser("history", help="특정 상품의 실적 이력 보기")
    p_hist.add_argument("product")

    p_add = sub.add_parser("add-actual", help="이번 달 실제 실적 입력")
    p_add.add_argument("product")
    p_add.add_argument("month", help="YYYY-MM 형식, 예: 2026-07")
    p_add.add_argument("qty", type=int)

    p_setstock = sub.add_parser("set-stock", help="재고를 직접 세어본 실사값 입력 (이후 출고량이 여기서 차감됨)")
    p_setstock.add_argument("product")
    p_setstock.add_argument("qty", type=int)
    p_setstock.add_argument("--asof", help="기준 연월 YYYY-MM (생략시 이번 달)")

    p_ship = sub.add_parser("add-shipment", help="이번 달 출고량 입력 (재고에서 자동 차감)")
    p_ship.add_argument("product")
    p_ship.add_argument("month", help="YYYY-MM 형식, 예: 2026-07")
    p_ship.add_argument("qty", type=int)

    p_stock = sub.add_parser("stock", help="특정 상품의 월별 재고 추이 보기")
    p_stock.add_argument("product")

    p_fc = sub.add_parser("forecast", help="새 예측 생성 (기존 예측은 보존됨)")
    p_fc.add_argument("product")
    p_fc.add_argument("start", help="예측 시작월 YYYY-MM")
    p_fc.add_argument("--months", type=int, default=6, help="몇 개월치 예측할지 (기본 6)")
    p_fc.add_argument("--method", choices=["seasonal", "trend"], default="seasonal",
                       help="seasonal=해당월 과거평균 반복 / trend=최근N개월평균×계절지수")
    p_fc.add_argument("--scenario", default="base",
                       help="base|optimistic|conservative 또는 숫자 배율 (예: 1.15)")
    p_fc.add_argument("--window", type=int, default=4, help="trend 방식에서 쓸 최근 개월수 (기본 4)")
    p_fc.add_argument("--exclude", help="제외할 연월, 콤마로 구분 (예: 2026-03,2025-09) — 광고 스파이크 등 이상치 제거용")

    p_cmp = sub.add_parser("compare", help="과거 예측들과 실제값을 나란히 비교")
    p_cmp.add_argument("product")

    p_exp = sub.add_parser("export", help="실적/예측/비교 데이터를 엑셀 파일로 저장")
    p_exp.add_argument("--out", help="저장할 파일 경로 (생략시 forecast_export.xlsx)")

    p_viz = sub.add_parser("visualize", help="실적/예측 데이터를 인터랙티브 대시보드(HTML)로 시각화 (정적 스냅샷)")
    p_viz.add_argument("--out", help="저장할 HTML 파일 경로 (생략시 forecast_dashboard.html)")
    p_viz.add_argument("--no-open", action="store_true", help="생성 후 브라우저에서 자동으로 열지 않기")
    p_viz.add_argument("--forecast-only", action="store_true",
                       help="로그인·통관 탭을 빼고 수요예측 화면만 담기 (공유·보기용)")

    p_serve = sub.add_parser("serve", help="입력 가능한 실시간 대시보드를 로컬 서버로 실행 (브라우저에서 바로 저장됨)")
    # PORT 환경변수가 있으면 그 값을 기본값으로 쓴다 (미리보기 도구가 포트를 지정할 때 사용)
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8787),
                         help="사용할 포트 (기본 8787, PORT 환경변수로도 지정 가능)")
    p_serve.add_argument("--no-open", action="store_true", help="자동으로 브라우저를 열지 않기")

    args = parser.parse_args()
    store = Store()

    if args.cmd == "list":
        cmd_list(args, store)
    elif args.cmd == "history":
        cmd_history(args, store)
    elif args.cmd == "add-actual":
        cmd_add(args, store)
    elif args.cmd == "set-stock":
        cmd_set_stock(args, store)
    elif args.cmd == "add-shipment":
        cmd_add_shipment(args, store)
    elif args.cmd == "stock":
        cmd_stock(args, store)
    elif args.cmd == "forecast":
        cmd_forecast(args, store)
    elif args.cmd == "compare":
        cmd_compare(args, store)
    elif args.cmd == "export":
        cmd_export(args, store)
    elif args.cmd == "visualize":
        cmd_visualize(args, store)
    elif args.cmd == "serve":
        cmd_serve(args, store)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
