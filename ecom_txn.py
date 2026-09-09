"""
쿠팡/컬리 "거래내역" 출고 원장(.xls, 실제로는 HTML) 파싱.

사내 ERP에서 뽑는 거래내역 파일 하나에 그 기간 출고된 주문 라인이 한 줄씩 들어있다.
컬럼: 날짜, 구분(출고만 씀), 거래처(쿠팡/컬리 판별), 적요(상품명 — ecommerce.html 의
ALL SKU 목록과 표기가 거의 그대로 같다), 수량, 공급가액, 주문번호(중복 방지 키),
장부명("...로켓배송..." 이 붙어 있으면 그 주문은 로켓배송으로 나간 것).

여러 번 겹쳐서 올려도 안전하도록 주문번호로 중복 제거해 data/ecom_txn.json 에 누적 저장한다.
같은 주문번호가 다시 들어왔는데 날짜/상품/수량/금액이 바뀐 경우에는 ERP의 최신 값을
정정본으로 보고 기존 행을 갱신한다. 월이 바뀐 정정이면 이전 월과 새 월을 모두 재집계한다.
"""
import json
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
TXN_PATH = DATA_DIR / "ecom_txn.json"
ECOM_HTML_PATH = Path(__file__).parent / "ecommerce.html"


class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None
        self._incell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._incell = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self._cell is not None:
                self._row.append("".join(self._cell).strip())
            self._incell = False
            self._cell = None
        elif tag == "tr":
            if self._row is not None:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._incell and self._cell is not None:
            self._cell.append(data)


def _to_int(s):
    s = (s or "").replace(",", "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _platform(gujaeche):
    if "쿠팡" in gujaeche:
        return "쿠팡"
    if "컬리" in gujaeche:
        return "컬리"
    return None


def parse_bytes(raw):
    """거래내역 xls(HTML) bytes -> row dict 리스트."""
    html = raw.decode("utf-8", errors="replace")
    t = _Table()
    t.feed(html)
    rows = t.rows
    if not rows:
        return []
    hdr = rows[0]
    idx = {name: i for i, name in enumerate(hdr)}
    need = ["날짜", "구분", "거래처", "적요", "수량", "공급가액", "주문번호", "장부명"]
    if any(k not in idx for k in need):
        raise ValueError("거래내역 파일 형식을 알아볼 수 없어요 (열 이름이 달라요).")
    out = []
    for r in rows[1:]:
        if len(r) < len(hdr):
            continue
        if r[idx["구분"]] != "출고":
            continue
        date = r[idx["날짜"]].strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            continue
        qty = _to_int(r[idx["수량"]])
        supply = _to_int(r[idx["공급가액"]])
        if qty is None or supply is None:
            continue
        plat = _platform(r[idx["거래처"]])
        order_no = r[idx["주문번호"]].strip() or None
        out.append({
            "date": date,
            "plat": plat,
            "name": r[idx["적요"]].strip(),
            "qty": qty,
            "supply": supply,
            "order_no": order_no,
            "rocket": "로켓배송" in r[idx["장부명"]],
        })
    return out


def load_txn():
    if TXN_PATH.exists():
        return json.loads(TXN_PATH.read_text(encoding="utf-8"))
    return {}


def save_txn(store):
    DATA_DIR.mkdir(exist_ok=True)
    TXN_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")


def _txn_key(row):
    """ERP 주문번호가 있으면 그것을, 없으면 안정적인 fallback 키를 사용한다."""
    return row.get("order_no") or ("noord:%s:%s:%s:%s" % (
        row.get("plat"), row.get("date"), row.get("name"), row.get("qty")
    ))


def _materially_changed(old, new):
    """재집계가 필요한 실제 거래 정정인지 확인한다. rocket 플래그만 바뀐 것은 제외."""
    return any(old.get(k) != new.get(k) for k in ("date", "plat", "name", "qty", "supply"))


def ingest(files):
    """files: [(filename, raw_bytes), ...].

    주문번호 기준으로 중복 제거하되, 같은 주문번호의 날짜/상품/수량/금액이 달라졌으면
    단순 중복으로 버리지 않고 최신 ERP 행으로 교체한다.
    """
    store = load_txn()
    added, skipped_no_plat, skipped_dup = 0, 0, 0
    for filename, raw in files:
        for row in parse_bytes(raw):
            if not row["plat"]:
                skipped_no_plat += 1
                continue
            key = _txn_key(row)
            old = store.get(key)
            if old is not None:
                if _materially_changed(old, row):
                    merged = dict(row)
                    merged["rocket"] = bool(old.get("rocket") or row.get("rocket"))
                    store[key] = merged
                else:
                    skipped_dup += 1
                    if row["rocket"] and not old.get("rocket"):
                        store[key]["rocket"] = True
                continue
            store[key] = row
            added += 1
    save_txn(store)
    return store, added, skipped_dup


_NORM_RE = re.compile(r"\s+")


def _norm(s):
    return _NORM_RE.sub("", s or "")


def all_skus():
    """ecommerce.html 안의 var ALL={...} 에서 플랫폼별 SKU 목록을 뽑아온다."""
    html = ECOM_HTML_PATH.read_text(encoding="utf-8")
    m = re.search(r"var ALL=(\{.*?\});", html, re.S)
    if not m:
        return {}
    data = json.loads(m.group(1))
    return {p: v.get("skus", []) for p, v in data.items()}


ALIASES = {
    "컬리": {
        "베르데 에스메랄다 프리미엄올리브유500ml(그린,피쿠알)1P(세트)":
            "베르데 에스메랄다 프리미엄올리브유500ml(그린,피쿠알)1P",
        "베르데 에스메랄다 프리미엄올리브유500ml(레드,로얄)1P(세트)":
            "베르데 에스메랄다 프리미엄올리브유500ml(레드,로얄)1P",
        "베르데 에스메랄다 프리미엄유기농올리브유500ml(블루,유기농피쿠알)1P(세트)":
            "베르데 에스메랄다 프리미엄유기농올리브유500ml(블루,피쿠알)1P",
    },
}
_ALIAS_NORM = {p: {_norm(k): v for k, v in m.items()} for p, m in ALIASES.items()}


def plan(files):
    """files: [(filename, raw_bytes), ...] -> 미리보기 계획."""
    before = load_txn()
    uploaded_rows = [row for _, raw in files for row in parse_bytes(raw)]
    touched = set()
    updated_keys = set()

    for row in uploaded_rows:
        if not row.get("plat"):
            continue
        touched.add((row["plat"], row["date"][:7]))
        key = _txn_key(row)
        old = before.get(key)
        if old is None:
            continue
        old_plat = old.get("plat")
        old_date = str(old.get("date") or "")
        if old_plat and len(old_date) >= 7:
            touched.add((old_plat, old_date[:7]))
        if _materially_changed(old, row):
            updated_keys.add(key)

    store, added, skipped_dup = ingest(files)
    skus = all_skus()
    norm_map = {p: {_norm(s): s for s in lst} for p, lst in skus.items()}

    by_group = {}
    for row in store.values():
        plat = row.get("plat")
        if not plat or (plat, row["date"][:7]) not in touched:
            continue
        ym = row["date"][:7]
        nm = norm_map.get(plat, {})
        norm_name = _norm(row["name"])
        alias_target = _ALIAS_NORM.get(plat, {}).get(norm_name)
        matched = nm.get(_norm(alias_target)) if alias_target else nm.get(norm_name)
        key = (plat, matched or row["name"], ym)
        g = by_group.setdefault(key, {"qty": 0, "supply": 0, "rocket": False,
                                       "matched": matched, "raw": set()})
        g["qty"] += row["qty"]
        g["supply"] += row["supply"]
        g["raw"].add(row["name"])
        if row.get("rocket"):
            g["rocket"] = True

    changes, unmatched = [], []
    for (plat, key, ym), g in sorted(by_group.items(), key=lambda x: -x[1]["qty"]):
        item = {
            "플랫폼": plat, "SKU": key, "연월": ym,
            "수량": g["qty"], "공급가액": g["supply"],
            "개당": round(g["supply"] / g["qty"]) if g["qty"] else 0,
            "로켓배송": g["rocket"],
        }
        if g["matched"]:
            changes.append(item)
        else:
            item["원본명"] = sorted(g["raw"])
            unmatched.append(item)

    daily = {}
    for row in store.values():
        plat = row.get("plat")
        if not plat or (plat, row["date"][:7]) not in touched:
            continue
        d = daily.setdefault(plat, {}).setdefault(row["date"], {"qty": 0, "supply": 0})
        d["qty"] += row["qty"]
        d["supply"] += row["supply"]

    return {
        "source_files": [
            {"name": name, "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in files
        ],
        "새로_담김": added,
        "수정_반영": len(updated_keys),
        "이미_있던_라인": skipped_dup,
        "대상월": sorted({ym for _, ym in touched}),
        "changes": changes,
        "unmatched": unmatched,
        "daily": daily,
    }


def month_totals(ym):
    """그 달 플랫폼별 총 출고 수량. {"쿠팡":n, "컬리":n, "전체":n}"""
    store = load_txn()
    out = {"쿠팡": 0, "컬리": 0}
    for row in store.values():
        if row["date"][:7] != ym:
            continue
        plat = row.get("plat")
        if plat in out:
            out[plat] += row["qty"]
    out["전체"] = out["쿠팡"] + out["컬리"]
    return out


def daily_trend(plat, days=60):
    store = load_txn()
    by_date = {}
    for row in store.values():
        if row.get("plat") != plat:
            continue
        d = by_date.setdefault(row["date"], {"qty": 0, "supply": 0, "items": {}})
        d["qty"] += row["qty"]
        d["supply"] += row["supply"]
        it = d["items"].setdefault(row["name"], {"qty": 0, "supply": 0})
        it["qty"] += row["qty"]
        it["supply"] += row["supply"]

    out = []
    for date in sorted(by_date)[-days:]:
        d = by_date[date]
        items = [{"name": n, "qty": v["qty"], "supply": v["supply"]}
                 for n, v in d["items"].items()]
        items.sort(key=lambda x: -x["qty"])
        out.append({"date": date, "qty": d["qty"], "supply": d["supply"], "items": items})
    return out


def sku_daily_series(plat, sku):
    store = load_txn()
    nm = _norm(sku)
    out = {}
    for row in store.values():
        if row.get("plat") != plat:
            continue
        if _norm(row["name"]) != nm:
            continue
        d = out.setdefault(row["date"], {"qty": 0, "supply": 0})
        d["qty"] += row["qty"]
        d["supply"] += row["supply"]
    return [{"date": k, "qty": v["qty"], "supply": v["supply"]} for k, v in sorted(out.items())]
