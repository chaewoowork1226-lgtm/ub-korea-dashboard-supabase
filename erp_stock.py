# -*- coding: utf-8 -*-
"""ERP 재고표(.xls) 읽어서 수요예측 탭의 출고량·재고·입고예정을 갱신한다.

파일 이름은 .xls 지만 실제로는 HTML 표다. 그리고 셀 안에 '홀딩 상세',
'입고예정 상세' 같은 작은 표가 또 들어있다. 이 중첩 표를 걸러내지 않으면
행이 쪼개지고 홀딩이 전부 0으로 읽힌다. (실제로 그 함정에 한 번 빠졌다)

계산 규칙
---------
실제 출고량 = 금월 출고수량 − (출고예정일이 '그 달'인 홀딩만)
    홀딩은 잡혀만 있고 아직 안 나간 물량이다. 다만 다음 달 출고예정 홀딩까지
    빼면 이번 달 출고가 실제보다 적게(때론 음수로) 나온다. 그래서 그 달 것만 뺀다.

전월 말 재고 = 당일재고 + 금월 출고수량 − 금월 입고수량
    당일재고는 홀딩을 이미 포함하고 있어서 홀딩 항은 넣지 않는다.
    (검증: 산체스_해바라기유500 역산 43,556 vs 기존 실사값 43,553)

입고예정은 '확정'만 재고에 반영하고 '협의중'은 뺀다. 날짜별 내역은
inbound_detail 로 남겨서 화면에 '9/9 12,240개' 처럼 보여준다.
"""
import datetime
import json
import os
import re
from html.parser import HTMLParser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(BASE_DIR, "data", "erp_map.json")
IGNORE_PATH = os.path.join(BASE_DIR, "data", "erp_ignore.json")
DAILY_PATH = os.path.join(BASE_DIR, "data", "erp_daily.json")

# 이 열들만 쓴다 (ERP 헤더 이름 그대로)
C_NAME = "품목"
C_CODE = "부품코드"
C_SHIP_M = "금월 출고수량"
C_HOLD = "홀딩재고"
C_STOCK = "당일 재고"
C_WIP = "작업중 재고"      # 실제 재고 = 당일 재고 + 작업중 재고
C_AVAIL = "가용 재고"
C_SHIP_PREV = "전월 출고수량"
C_SHIP_DAY = "당일 출고"
C_IN_M = "금월 입고수량"
C_IN_PLAN = "입고예정"
C_COST = "단가"              # ERP에 표시되는 현재 원가 (landed cost와 별도 스냅샷)


class _Table(HTMLParser):
    """맨 바깥 표의 행을 모은다.

    셀 안에 중첩된 표(홀딩 상세 팝업)는 바깥 값에 섞이지 않게 분리하되,
    버리지 않고 `details` 에 따로 담아둔다. 홀딩의 출고예정일이 거기 들어있다.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []        # [[셀값, ...], ...]
        self.details = []     # rows 와 같은 길이. [[중첩행, ...] 또는 None, ...]
        self._tdepth = 0
        self._row = None
        self._det = None
        self._cell = None
        self._cell_depth = None
        self._nest = None      # 현재 셀의 중첩표 행들
        self._nrow = None
        self._ncell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tdepth += 1
        elif tag == "tr":
            if self._tdepth == 1:
                self._row, self._det = [], []
            elif self._cell is not None:
                self._nrow = []
        elif tag in ("td", "th"):
            if self._tdepth == 1 and self._row is not None:
                self._cell, self._cell_depth, self._nest = [], self._tdepth, []
            elif self._nrow is not None:
                self._ncell = []
        elif tag == "br":
            if self._ncell is not None:
                self._ncell.append(" ")
            elif self._cell is not None:
                self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            self._tdepth -= 1
        elif tag == "tr":
            if self._tdepth == 1 and self._row is not None:
                self.rows.append(self._row)
                self.details.append(self._det)
                self._row = self._det = None
            elif self._nrow is not None:
                if self._nest is not None:
                    self._nest.append(self._nrow)
                self._nrow = None
        elif tag in ("td", "th"):
            if self._ncell is not None and self._nrow is not None:
                self._nrow.append(" ".join("".join(self._ncell).split()))
                self._ncell = None
            elif self._cell is not None and self._tdepth == self._cell_depth:
                self._row.append(" ".join("".join(self._cell).split()))
                self._det.append(self._nest or None)
                self._cell = self._cell_depth = self._nest = None

    def handle_data(self, data):
        if self._ncell is not None:
            self._ncell.append(data)
        elif self._cell is not None and self._tdepth == self._cell_depth:
            self._cell.append(data)


def parse_holdings(detail) -> list:
    """홀딩 상세 팝업 → [{'qty':수량, 'when':'2026-09-30', 'kind':'진행중'|'확정'}, ...]

    표는 4칸짜리 행이 [라벨, 진행중값, 라벨, 확정값] 으로 반복된다.
    수량 줄과 출고예정 줄이 짝을 이룬다.
    """
    if not detail:
        return []
    out = []
    pend = {1: None, 3: None}      # 열별로 대기중인 수량
    for row in detail:
        if len(row) < 4:
            continue
        label = (row[0] or "").strip()
        for ci, kind in ((1, "진행중"), (3, "확정")):
            val = (row[ci] or "").strip()
            if label == "수량":
                pend[ci] = _num(val)
            elif label == "출고예정":
                q = pend[ci]
                if q:
                    out.append({"qty": q, "when": val or "", "kind": kind})
                pend[ci] = None
    # 출고예정 줄이 아예 없던 수량은 날짜 없는 홀딩으로 남긴다
    for ci, kind in ((1, "진행중"), (3, "확정")):
        if pend[ci]:
            out.append({"qty": pend[ci], "when": "", "kind": kind})
    return out


def _num(s):
    s = (s or "").replace(",", "").strip()
    if not s:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def parse_inbound(detail) -> list:
    """입고예정 팝업 → [{'qty':주문수량, 'when':'2026-09-09', 'kind':'확정'|'협의중'}, ...]

    4칸 행이 [라벨(협의중), 값, 라벨(입고예정), 값] 으로 반복된다.
    왼쪽 칸은 아직 협의중이라 날짜가 '발주일'이고, 오른쪽이 확정 '입고예정일'이다.
    """
    if not detail:
        return []
    out = []
    pend = {1: None, 3: None}      # 열별 대기중 날짜
    for row in detail:
        if len(row) < 4:
            continue
        l0, l2 = (row[0] or "").strip(), (row[2] or "").strip()
        if l0 in ("발주일", "입고예정일"):
            pend[1] = (row[1] or "").strip()
        if l2 in ("발주일", "입고예정일"):
            pend[3] = (row[3] or "").strip()
        if l0 == "주문수량":
            q = _num(row[1])
            if q:
                out.append({"qty": q, "when": pend[1] or "", "kind": "협의중"})
            pend[1] = None
        if l2 == "주문수량":
            q = _num(row[3])
            if q:
                out.append({"qty": q, "when": pend[3] or "", "kind": "확정"})
            pend[3] = None
    return out


def month_period(day: int) -> str:
    """1~10 월초 / 11~20 중순 / 21~ 월말 (대시보드 규칙과 동일)"""
    if day <= 10:
        return "early"
    if day <= 20:
        return "mid"
    return "late"


def read_rows(src, ym: str = "") -> list:
    """ERP 파일 → 품목별 값. ym('2026-08')을 주면 그 달 홀딩만 출고에서 뺀다.

    홀딩은 '언제 나갈 물량인지'가 출고예정일로 붙어 있다. 다음 달에 나갈 홀딩을
    이번 달 출고에서 빼면 안 된다 (그래서 예전엔 실제출고가 음수로 나왔다).
    """
    if isinstance(src, bytes):
        text = src.decode("utf-8", errors="replace")
    elif os.path.exists(str(src)):
        text = open(src, encoding="utf-8", errors="replace").read()
    else:
        text = str(src)

    p = _Table()
    p.feed(text)
    pairs = [(r, d) for r, d in zip(p.rows, p.details) if r]
    rows = [r for r, _ in pairs]
    if not rows:
        raise RuntimeError("표를 찾지 못했어요. ERP 재고표가 맞는지 확인해주세요.")

    # 헤더 = 필요한 열 이름을 모두 가진 첫 행
    header = None
    for r in rows:
        if C_NAME in r and C_SHIP_M in r and C_HOLD in r:
            header = r
            break
    if header is None:
        raise RuntimeError("'품목' '금월 출고수량' '홀딩재고' 열을 찾지 못했어요. "
                           "ERP에서 그 열들을 켜고 다시 내려받아주세요.")

    col = {n: i for i, n in enumerate(header)}
    hold_i = col.get(C_HOLD)
    inb_i = col.get(C_IN_PLAN)
    out = []
    for r, det in pairs:
        if r is header or len(r) != len(header):
            continue
        name = (r[col[C_NAME]] or "").strip()
        if not name or name == C_NAME:
            continue
        ship = _num(r[col[C_SHIP_M]]) or 0
        hold_total = _num(r[hold_i]) or 0

        holds = parse_holdings(det[hold_i] if det and hold_i is not None and hold_i < len(det) else None)
        hold_this = hold_other = hold_undated = 0
        for h in holds:
            when = (h.get("when") or "").strip()
            if not when:
                hold_undated += h["qty"]
            elif ym and when[:7] == ym:
                hold_this += h["qty"]
            elif ym:
                hold_other += h["qty"]
            else:
                hold_this += h["qty"]

        # 금월 출고수량(raw)에는 이번 달·다른 달 홀딩이 전부 합쳐져 있다.
        # 이번 달 예정 홀딩은 이번 달 안에 실제로 나갈 물량이라 이미 출고로 잡혀도 맞으므로
        # 빼지 않는다. 다음 달 이후 예정 홀딩만 "아직 안 나갔는데 출고로 잡힌 것"이라 뺀다.
        if holds:
            deduct = hold_other
        else:
            deduct = 0     # 상세를 못 읽으면 뭐가 다른 달 것인지 몰라 안전하게 안 뺀다

        # 실제 재고 = 당일 재고 + 작업중 재고 (작업중도 창고에 있는 물량이다)
        day_stock = _num(r[col[C_STOCK]]) if C_STOCK in col else None
        wip = (_num(r[col[C_WIP]]) if C_WIP in col else None) or 0
        real_stock = None if day_stock is None else day_stock + wip

        out.append({
            "품목": name,
            "부품코드": (r[col[C_CODE]] or "").strip() if C_CODE in col else "",
            "금월출고": ship,
            "홀딩": hold_total,
            "홀딩_이번달": hold_this,
            "홀딩_다른달": hold_other,
            "홀딩_날짜없음": hold_undated,
            "홀딩상세": holds,
            "차감액": deduct,
            "실제출고": ship - deduct,
            "당일재고": day_stock,
            "작업중재고": wip,
            "실제재고": real_stock,
            "가용재고": _num(r[col[C_AVAIL]]) if C_AVAIL in col else None,
            "원가": _num(r[col[C_COST]]) if C_COST in col else None,
            "전월출고": _num(r[col[C_SHIP_PREV]]) if C_SHIP_PREV in col else None,
            "당일출고": _num(r[col[C_SHIP_DAY]]) if C_SHIP_DAY in col else None,
            "금월입고": _num(r[col[C_IN_M]]) if C_IN_M in col else None,
            "입고예정계": _num(r[inb_i]) if inb_i is not None else None,
            "입고예정": parse_inbound(det[inb_i] if det and inb_i is not None and inb_i < len(det) else None),
        })
    return out


# ── 파일 이름에서 기준일 뽑기: 20260812101950_재고.xls → 2026-08-12 ──
def date_from_name(filename: str):
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", os.path.basename(filename or ""))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# ── 품목 연결 ──
def _norm(s: str) -> str:
    """이름 비교용으로 다듬는다. 괄호설명·공백·단위표기를 걷어낸다."""
    s = (s or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)          # (벌크) (스페인직수입) 등
    s = s.replace("ml", "").replace("l ", " ")
    for junk in ("직수입", "스페인no.1", "벌크", "세트", "캔"):
        s = s.replace(junk, " ")
    return re.sub(r"[^0-9a-z가-힣]", "", s)


def load_map() -> dict:
    if os.path.exists(MAP_PATH):
        try:
            with open(MAP_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_map(m: dict) -> None:
    os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2, sort_keys=True)


def load_ignore() -> dict:
    """대시보드에서 안 쓰는 품목. '확인 필요' 목록에 그만 뜨게 한다.

    {"codes": ["034355", ...], "names": ["...", ...]} 형태.
    """
    if os.path.exists(IGNORE_PATH):
        try:
            with open(IGNORE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return {"codes": [str(x).upper() for x in (d.get("codes") or [])],
                    "names": list(d.get("names") or [])}
        except Exception:
            pass
    return {"codes": [], "names": []}


def save_ignore(codes=None, names=None) -> dict:
    d = {"codes": sorted({str(x).upper() for x in (codes or [])}),
         "names": sorted(set(names or []))}
    os.makedirs(os.path.dirname(IGNORE_PATH), exist_ok=True)
    with open(IGNORE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return d


def is_ignored(row: dict, ig: dict) -> bool:
    code = (row.get("부품코드") or "").upper()
    if code and code in ig.get("codes", []):
        return True
    return row.get("품목") in ig.get("names", [])


def match(rows: list, products: dict) -> dict:
    """ERP 행 → 대시보드 품목. 코드 → 저장된 수동연결 → 이름 순으로 찾는다."""
    by_code, by_name = {}, {}
    for k, v in (products or {}).items():
        c = (v.get("code") or "").strip().upper()
        if c and c != "-":
            by_code.setdefault(c, k)
        by_name.setdefault(_norm(k), k)

    saved = load_map()
    ig = load_ignore()
    hit, miss, skipped = [], [], []
    for r in rows:
        if is_ignored(r, ig):
            skipped.append(r)
            continue
        key = None
        how = ""
        code = (r.get("부품코드") or "").upper()
        if code and code in by_code:
            key, how = by_code[code], "코드"
        if key is None and r["품목"] in saved:
            key, how = saved[r["품목"]], "수동연결"
        # 이름 짐작(부분일치)은 일부러 안 한다. 실제로 '프리미엄올리브유500ml(그린,유기농)' 을
        # '에스파뇰라_프리미엄올리브유500_블랙' 에 잘못 붙인 적이 있다. 코드나 수동연결만 믿는다.
        if key and key in (products or {}):
            hit.append(dict(r, 품목키=key, 연결=how))
        else:
            miss.append(r)

    # 한 품목에 두 줄이 붙으면 둘 다 보류한다 (합쳐야 하는지 사람이 판단)
    seen = {}
    for h in hit:
        seen.setdefault(h["품목키"], []).append(h)
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    if dups:
        hit = [h for h in hit if h["품목키"] not in dups]
        for v in dups.values():
            miss.extend(v)
    return {"matched": hit, "unmatched": miss, "dups": list(dups), "ignored": skipped}


def plan(src, products: dict, filename: str = "") -> dict:
    """무엇을 바꿀지 계산만 한다. 저장은 하지 않는다 (사람이 확인한 뒤에)."""
    d = date_from_name(filename or (src if isinstance(src, str) else "")) or datetime.date.today()
    month = "%04d-%02d" % (d.year, d.month)
    rows = read_rows(src, ym=month)
    m = match(rows, products)

    changes, notes = [], []
    for r in m["matched"]:
        real, stock = r["실제출고"], r["실제재고"]
        if real < 0:
            notes.append({"level": "warn", "품목": r["품목키"],
                          "message": "홀딩(%s)이 금월 출고(%s)보다 커서 실제 출고가 음수예요. "
                                     "0으로 넣습니다." % (r["홀딩"], r["금월출고"])})
            real = 0
        if stock is not None and stock < 0:
            notes.append({"level": "warn", "품목": r["품목키"],
                          "message": "ERP 재고가 음수(%s)예요. 재고 실사는 건너뜁니다." % stock})
            stock = None
        if r.get("홀딩_다른달"):
            notes.append({"level": "info", "품목": r["품목키"],
                          "message": "홀딩 %s개는 출고예정이 다음 달 이후예요. 금월 출고수량에 이미 잡혀 있어서 뺐습니다."
                                     % r["홀딩_다른달"]})
        # 입고예정을 월별로 묶는다. 같은 달에 여러 건이면 더하되, 날짜별 내역도 남긴다.
        inb_by_month, inb_day, inb_detail = {}, {}, {}
        for h in (r.get("입고예정") or []):
            w = (h.get("when") or "").strip()
            if len(w) < 7 or h.get("kind") != "확정":
                continue          # 협의중이거나 날짜 없으면 재고 계산에 넣지 않는다
            k = w[:7]
            inb_by_month[k] = inb_by_month.get(k, 0) + h["qty"]
            inb_detail.setdefault(k, []).append({"d": w, "q": h["qty"]})
            try:
                dd = int(w[8:10])
                inb_day[k] = min(inb_day.get(k, 99), dd)   # 그 달 가장 이른 도착일
            except ValueError:
                pass
        for k in inb_detail:
            inb_detail[k].sort(key=lambda x: x["d"])
        tentative = sum(h["qty"] for h in (r.get("입고예정") or []) if h.get("kind") != "확정")
        detail_sum = sum(h["qty"] for h in (r.get("입고예정") or []))
        if r.get("입고예정계") and detail_sum and abs(detail_sum - r["입고예정계"]) > 1:
            notes.append({"level": "warn", "품목": r["품목키"],
                          "message": "입고예정 상세 합(%s)이 표의 합계(%s)와 달라요."
                                     % (detail_sum, r["입고예정계"])})
        if tentative:
            notes.append({"level": "info", "품목": r["품목키"],
                          "message": "협의중 입고 %s개는 아직 확정이 아니라 재고 계산에서 뺐어요." % tentative})

        changes.append({
            "전월말재고": r.get("전월말재고"),
            "입고예정": [{"월": k, "수량": v, "시점": month_period(inb_day.get(k, 15)),
                      "내역": inb_detail.get(k, [])}
                     for k, v in sorted(inb_by_month.items())],
            "입고예정계": r.get("입고예정계"),
            "품목키": r["품목키"], "품목코드": r.get("부품코드") or "",
            "ERP품목": r["품목"], "연결": r["연결"], "월": month,
            "금월출고": r["금월출고"], "홀딩": r["홀딩"],
            "홀딩_이번달": r.get("홀딩_이번달", 0), "홀딩_다른달": r.get("홀딩_다른달", 0),
            "차감액": r.get("차감액", r["홀딩"]), "실제출고": real, "재고실사": stock,
            "작업중": r.get("작업중재고") or 0,
            "당일재고": r.get("당일재고"), "가용재고": r.get("가용재고"),
            "원가": r.get("원가"),
            "홀딩상세": r.get("홀딩상세") or [],
            "전월출고": r.get("전월출고"), "당일출고": r.get("당일출고"),
            "금월입고": r.get("금월입고"),
            # 전월 마지막날 재고 역산 — 여기엔 '당일재고'를 쓴다. 실제재고가 아니다.
            #   작업중 재고는 금월 출고수량에 이미 잡힌 물량이라(48개 중 43개가 작업중<=금월출고,
            #   예: 탑셰프 페트 작업중 94,890 vs 금월출고 97,242) 둘 다 더하면 이중 계산이 된다.
            #   식을 풀면: (당일+작업중) + (금월출고−작업중) − 금월입고 = 당일 + 금월출고 − 금월입고
            "전월말재고": ((r["당일재고"] or 0) + (r["금월출고"] or 0) - (r.get("금월입고") or 0))
                        if r["당일재고"] is not None else None,
        })
    for k in m.get("dups") or []:
        notes.append({"level": "warn", "품목": k,
                      "message": "ERP에 이 품목으로 붙는 줄이 둘 이상이라 건너뜁니다. 연결을 정리해주세요."})
    changes.sort(key=lambda c: -(c["실제출고"] or 0))
    # 전월과 그 달의 마지막 날 (역산한 재고를 그 날짜로 박아둔다)
    first = datetime.date(d.year, d.month, 1)
    prev_last_date = first - datetime.timedelta(days=1)
    return {
        "기준일": d.isoformat(), "월": month,
        "전월": prev_last_date.strftime("%Y-%m"),
        "전월마지막날": prev_last_date.day,
        "ERP행수": len(rows),
        "연결됨": len(changes), "연결안됨": len(m["unmatched"]),
        "제외됨": len(m.get("ignored") or []),
        "changes": changes,
        "notes": notes,
        "unmatched": [{"품목": r["품목"], "부품코드": r["부품코드"],
                       "실제출고": r["실제출고"], "당일재고": r["당일재고"]}
                      for r in m["unmatched"]],
        "홀딩있음": [c for c in changes if c["홀딩"]],
    }


def load_daily() -> dict:
    """{품목: {"2026-08": {"12": {"ship":금월출고, "real":실제출고, "hold":홀딩, "stock":재고}}}}

    재고표를 줄 때마다 그날 값을 쌓아둔다. 월 안에서 출고가 어떻게 늘어가는지 보기 위한 것.
    """
    if os.path.exists(DAILY_PATH):
        try:
            return json.load(open(DAILY_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_daily(d: dict) -> None:
    os.makedirs(os.path.dirname(DAILY_PATH), exist_ok=True)
    with open(DAILY_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)


def record_daily(pl: dict) -> int:
    """이번 파일의 값들을 '그날의 기록'으로 남긴다. 같은 날 다시 주면 덮어쓴다."""
    daily = load_daily()
    ym = pl["월"]
    day = str(int(pl["기준일"][8:10]))
    n = 0
    for c in pl["changes"]:
        slot = daily.setdefault(c["품목키"], {}).setdefault(ym, {})
        slot[day] = {"ship": c["금월출고"], "real": c["실제출고"],
                     "hold": c["홀딩"], "stock": c["재고실사"],
                     "hold_cut": c.get("차감액", 0),          # 이번 달 출고에서 실제로 뺀 양
                     "holds": [{"q": h["qty"], "d": h.get("when") or "", "k": h.get("kind") or ""}
                               for h in (c.get("홀딩상세") or [])]}
        n += 1
    save_daily(daily)
    return n


def backup(tag: str = "erp") -> str:
    """data/*.json 을 통째로 복사해둔다. 되돌릴 일이 생기면 여기서 꺼낸다."""
    import shutil
    src = os.path.join(BASE_DIR, "data")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BASE_DIR, "data", "_backup", "%s_%s" % (tag, stamp))
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src):
        if f.endswith(".json"):
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return dst


def apply(store, pl: dict, do_stock: bool = True) -> dict:
    """계산된 plan 을 실제로 반영한다. 반드시 백업을 먼저 뜬다.

    출고량  → add_entry (실적·출고를 같은 값으로 기록)
    재고실사 → set_stock (파일 기준일의 '일'까지 같이 넣어 월 중간 계산이 맞도록)
    """
    saved = backup("erp_" + pl["월"])
    n_daily = record_daily(pl)   # 월 안에서의 변화를 보려면 매일 값이 필요하다
    day = int(pl["기준일"][8:10])
    ym = pl["월"]
    prev_ym = pl["전월"]
    prev_last = pl["전월마지막날"]
    n_ship = n_stock = n_prev = n_inb = 0
    n_locked = 0
    for c in pl["changes"]:
        for e in (c.get("입고예정") or []):
            # 화면에서 손으로 고치거나 지운 달은 건드리지 않는다.
            # (예전엔 여기서 덮어써서, 지운 입고예정이 재고표 올릴 때마다 되살아났다)
            if store.inbound_locked(c["품목키"], e["월"]):
                n_locked += 1
                continue
            store.add_inbound(c["품목키"], e["월"], int(e["수량"]), when=e["시점"])
            store.set_inbound_detail(c["품목키"], e["월"], e.get("내역") or [])
            n_inb += 1
        store.add_entry(c["품목키"], ym, int(c["실제출고"] or 0))
        n_ship += 1
        if do_stock and c["재고실사"] is not None:
            # 이번 달 = 오늘 실제로 세어본 재고 (파일 줄 때마다 갱신됨).
            # 작업중(선물세트 조립 등) 물량도 같이 넘긴다 — 화면 표시는 그대로 두되,
            # 재고소진 예측에서는 그만큼 빼고 계산하게.
            store.set_stock(c["품목키"], ym, int(c["재고실사"]), day=day, wip=int(c.get("작업중") or 0))
            n_stock += 1
        if do_stock and c["전월말재고"] is not None:
            # 지난달 = 역산한 그 달 마지막날 재고. 그 시점의 작업중 물량은 알 수 없으니
            # wip 는 안 넘긴다 (역산값 자체가 이미 당일재고 기준이라 작업중이 안 섞여 있다).
            store.set_stock(c["품목키"], prev_ym, int(c["전월말재고"]), day=prev_last)
            n_prev += 1
    return {"백업": saved, "출고반영": n_ship, "재고실사반영": n_stock,
            "전월말재고반영": n_prev, "전월": prev_ym, "입고예정반영": n_inb,
            "입고건너뜀": n_locked,
            "일별기록": n_daily, "월": ym, "기준일": pl["기준일"]}
