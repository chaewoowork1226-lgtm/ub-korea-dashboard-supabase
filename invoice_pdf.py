# -*- coding: utf-8 -*-
"""수입 인보이스 PDF 읽기 (COOSUR/아세수르 서식).

인보이스 + 패킹리스트 + B/L 을 묶어 시트에 넣을 형태로 바꾼다.
읽기만 하고 저장은 하지 않는다 — 사람이 화면에서 확인한 뒤 저장한다.
"""
import base64
import io
import re

# 공급사 코드 → 시트 탭 / 주문명 접두어
SUPPLIERS = {
    "ACEITES DEL SUR": ("COOSUR", "아세수르"),
    "COOSUR": ("COOSUR", "아세수르"),
    "BORGES": ("BORGES", "보르헤스"),
    "ZENAT": ("ZENAT", "제나트"),
}

# 영문 품명 → 한글 품명 (시트에 쓰던 이름 그대로)
NAME_RULES = [
    (("PURE AVOCADO", "SPRAY"), "에스파뇰라 아보카도오일 스프레이 {ml}ml"),
    (("AVOCADO", "SPRAY"), "에스파뇰라 아보카도오일 스프레이 {ml}ml"),
    # TRUFFLE 은 설명에 "EXTRA VIRGIN OLIVE OIL"도 같이 들어있어서, 아래 일반 올리브유
    # 규칙보다 먼저 와야 한다. 순서가 바뀌면 트러플이 일반 올리브유로 잘못 표시된다.
    (("TRUFFLE", "SPRAY"), "에스파뇰라 트러플올리브유 스프레이 {ml}ml"),
    (("EXTRA VIRGIN OLIVE OIL", "SPRAY"), "에스파뇰라 스프레이 올리브유 {ml}ml"),
    (("BALSAMIC VINEGAR", "1840"), "1840 발사믹식초 {ml}ml"),
    (("BALSAMIC",), "에스파뇰라 발사믹식초 {ml}ml"),
    (("ORGANIC EXTRA VIRGIN OLIVE OIL",), "에스파뇰라 유기농 올리브유 {ml}ml"),
    (("CENTENNIAL",), "에스파뇰라 센테니얼 올리브유 {ml}ml"),
    (("HOJIBLANCA",), "에스파뇰라 호히블랑카 올리브유 {ml}ml"),
    (("MANZANILLA",), "에스파뇰라 만자니야 올리브유 {ml}ml"),
    (("GRAPE SEED",), "에스파뇰라 포도씨유 {ml}ml"),
    (("SUNFLOWER",), "에스파뇰라 해바라기유 {ml}ml"),
    (("RICE",), "에스파뇰라 현미유 {ml}ml"),
    (("EXTRA VIRGIN OLIVE OIL",), "에스파뇰라 올리브유 {ml}ml"),
    (("OLIVE OIL",), "에스파뇰라 올리브유 {ml}ml"),
]

# 소포장 기준 파렛트별 박스수 (PDF 에 없어 관례값을 미리 채워둔다 — 화면에서 고칠 수 있음)
PALLET_DEFAULT = {"200": 186, "250": 150, "500": 125, "1000": 56, "1L": 56}


def _num(s):
    """'2.232' → 2232, '31,58' → 31.58, '4.493,016' → 4493.016 (유럽식 표기)"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def _text(data):
    from pypdf import PdfReader
    raw = base64.b64decode(data) if isinstance(data, str) else data
    reader = PdfReader(io.BytesIO(raw))
    return [(p.extract_text() or "") for p in reader.pages]


def _ml(desc):
    m = re.search(r"X\s*([\d.,]+)\s*(ML|L)\b", desc, re.I)
    if not m:
        return ""
    v, unit = m.group(1).replace(",", "."), m.group(2).upper()
    if unit == "L":
        try:
            return str(int(float(v) * 1000))
        except ValueError:
            return v
    return v.split(".")[0]


def _bottles(desc):
    m = re.match(r"\s*(\d+)\s+", desc)
    return int(m.group(1)) if m else None


def _korean(desc):
    up = desc.upper()
    ml = _ml(desc)
    for keys, tpl in NAME_RULES:
        if all(k in up for k in keys):
            return tpl.format(ml=ml or "")
    return desc.strip()[:60]


def parse(data):
    """PDF(base64 또는 bytes) → 시트에 넣을 주문 정보."""
    pages = _text(data)
    full = "\n".join(pages)
    flat = re.sub(r"[ \t]+", " ", full)

    out = {"warnings": [], "items": []}

    # ── 공급사 / 탭 ──
    tab, prefix = "", ""
    for key, (t, p) in SUPPLIERS.items():
        if key in full.upper():
            tab, prefix = t, p
            break
    if not tab:
        out["warnings"].append("공급사를 알아내지 못했습니다. 탭을 직접 골라주세요.")
    out["sheet_tab"] = tab

    # ── 인보이스 번호 / 발행일 ──
    m = re.search(r"INVOICE\s*NO\s*:?\s*(\d+)", flat, re.I)
    out["invoice_no"] = m.group(1) if m else ""
    m = re.search(r"\bDATE\s*:?\s*(\d{2})/(\d{2})/(\d{4})", flat)
    out["invoice_date"] = "%s-%s-%s" % (m.group(3), m.group(2), m.group(1)) if m else ""

    # ── 컨테이너 / 주문번호(CUSTOMER REF: 12º2026) ──
    m = re.search(r"CONTAINER\s*(?:NO)?\s*:?\s*([A-Z]{4}\d{6,7})", flat, re.I)
    out["container"] = m.group(1) if m else ""
    m = re.search(r"CUSTOMER\s*REF\s*:?\s*(\d+)\s*[º°o]?\s*(\d{4})", flat, re.I)
    if m:
        out["order_no"] = int(m.group(1))
        out["order_name"] = "%s %d번" % (prefix, out["order_no"]) if prefix else ""
    else:
        out["order_no"] = None
        out["order_name"] = ""
        out["warnings"].append("주문 번호(CUSTOMER REF)를 찾지 못했습니다. 직접 입력해주세요.")

    # ── B/L (Maersk waybill 은 Booking No 와 같은 번호) ──
    m = re.search(r"B/L\s*:?\s*(\d{6,})", flat)
    if not m:
        m = re.search(r"Booking!?No\.?!?\s*(\d{6,})", flat)
    if not m:
        m = re.search(r"Booking\s*No\.?\s*\n?\s*(\d{6,})", full)
    out["bl"] = m.group(1) if m else ""
    if not out["bl"]:
        out["warnings"].append("BL번호를 찾지 못했습니다. 직접 입력해주세요.")

    # ── 선적일 (Shipped on Board) ──
    m = re.search(r"Shipped!?on!?Board!?Date[^\n]*\n\s*(\d{4})-(\d{2})-(\d{2})", full)
    if not m:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", full)
    out["shipped"] = "-".join(m.groups()) if m else ""

    # ── 총 중량 ──
    m = re.search(r"NET\s*WEIGHT\s*:?\s*([\d.,]+)\s*KG", flat, re.I)
    out["net_total"] = _num(m.group(1)) if m else None
    m = re.search(r"GROSS\s*WEIGHT\s*:?\s*([\d.,]+)\s*KG", flat, re.I)
    out["gross_total"] = _num(m.group(1)) if m else None

    # ── 인보이스 품목: 코드 + 설명 … 박스수 단가 금액 ──
    inv = {}
    inv_page = pages[0] if pages else ""
    blocks = re.split(r"\n(?=\d{6}\s)", inv_page)
    numpat = re.compile(r"^([\d.]+)\s+([\d.,]+)\s+([\d.,]+)\s*$")
    for b in blocks:
        mcode = re.match(r"(\d{6})\s+(.*)", b, re.S)
        if not mcode:
            continue
        code, rest = mcode.group(1), mcode.group(2)
        # 수량·단가·금액 줄을 위에서부터 찾는다. 마지막 품목은 블록에 TOTALS 줄까지
        # 딸려 들어오므로, 끝에서 찾으면 합계를 품목값으로 잘못 읽는다.
        desc_lines, mnum = [], None
        for ln in rest.split("\n"):
            t = ln.strip()
            if not t:
                continue
            if re.match(r"^(TOTALS|PAYMENT|IBAN|BIC)\b", t, re.I):
                break
            m2 = numpat.match(t)
            if m2:
                mnum = m2
                break
            desc_lines.append(t)
        if not mnum:
            continue
        inv[code] = {"code": code,
                     "desc": re.sub(r"\s+", " ", " ".join(desc_lines)).strip(),
                     "boxes": _num(mnum.group(1)),
                     "unit_price": _num(mnum.group(2)),
                     "amount": _num(mnum.group(3))}
    out["invoice_lines"] = list(inv.values())

    # ── 패킹리스트: 배치 / 소비기한 / 박스 / 중량 ──
    # 품목이 많으면 패킹리스트가 2페이지 이상으로 넘어간다(예: 8개 배치 → 2쪽).
    # ANALYSIS(분석표) 페이지가 나오기 전까지를 전부 패킹리스트로 본다.
    pk_pages = []
    for p in pages[1:]:
        if p.strip()[:20].upper().startswith("ANALYSIS"):
            break
        pk_pages.append(p)
    pk_page = "\n".join(pk_pages) if pk_pages else (pages[1] if len(pages) > 1 else "")
    rowpat = re.compile(
        r"([A-Z]?\.?[\d]{6,}|L\.\d+(?:\(\w\))?)\s+"          # 배치
        r"(\d{4}[.\-/]\d{2}(?:[.\-/]\d{2})?|\d{2}\.\d{2}\.\d{4})\s+"  # 소비기한 (구분자: . - /)
        r"([\d.]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,\s]+)")
    lines = pk_page.split("\n")
    desc_buf = []
    for ln in lines:
        m = rowpat.search(ln)
        if not m:
            if ln.strip() and not re.match(r"^\s*(TOTALS|UNITS|BRAND)", ln, re.I):
                desc_buf.append(ln.strip())
            continue
        desc = re.sub(r"\s+", " ", " ".join(desc_buf[-4:])).strip()
        desc_buf = []
        batch, exp = m.group(1), m.group(2)
        boxes = _num(m.group(3))
        net_tot = _num(m.group(5))
        gross_tot = _num(re.sub(r"\s+", "", m.group(7)))
        # 소비기한 표기 통일: 23.06.2029 → 2029.06.23
        me = re.match(r"(\d{2})\.(\d{2})\.(\d{4})$", exp)
        if me:
            exp = "%s.%s.%s" % (me.group(3), me.group(2), me.group(1))
        exp = exp.replace("-", ".").replace("/", ".")

        # 인보이스에서 단가 찾기 (설명이 가장 많이 겹치는 줄)
        best, score = None, 0
        words = set(re.findall(r"[A-Z]{3,}", desc.upper()))
        for it in inv.values():
            s = len(words & set(re.findall(r"[A-Z]{3,}", it["desc"].upper())))
            if s > score:
                best, score = it, s
        unit_price = (best["unit_price"] / (_bottles(best["desc"]) or 1)) if best else None
        bottles = _bottles(best["desc"]) if best else _bottles(desc)
        src_desc = best["desc"] if best else desc
        ml = _ml(src_desc)

        out["items"].append({
            "품명": _korean(src_desc),
            "병입수": bottles,
            "카톤수": boxes,
            "총수량": (boxes * bottles) if (boxes and bottles) else None,
            "파렛트별박스수": PALLET_DEFAULT.get(ml, ""),
            "개당가격(EUR)": round(unit_price, 2) if unit_price else None,
            "소비기한": exp,
            "산도(%)": "",
            "배치넘버": batch,
            "넷중량(KG)": net_tot,
            "총중량(KG)": gross_tot,
            "_영문": src_desc,
        })

    # ── 산도: 분석표에서 배치별로 뽑아 붙인다 ──
    for blk in re.split(r"(?=Analysis:)", full):
        mb = re.search(r"BATCH\s+([A-Z]?\.?[\w.]+)", blk)
        ma = re.search(r"Acidity\s*\(%\s*oleic acid\)\s*([\d,\.]+)", blk)
        if mb and ma:
            b = mb.group(1).strip()
            for it in out["items"]:
                if it["배치넘버"].replace(" ", "") == b.replace(" ", ""):
                    it["산도(%)"] = str(_num(ma.group(1)))

    # ── 검산 ──
    chk = []
    box_sum = sum(i["카톤수"] or 0 for i in out["items"])
    inv_box = sum(i["boxes"] or 0 for i in inv.values())
    chk.append(("박스수 합계", box_sum, inv_box, box_sum == inv_box))
    net_sum = round(sum(i["넷중량(KG)"] or 0 for i in out["items"]), 3)
    chk.append(("넷중량 합계", net_sum, out["net_total"],
                out["net_total"] is not None and abs(net_sum - out["net_total"]) < 0.5))
    gro_sum = round(sum(i["총중량(KG)"] or 0 for i in out["items"]), 3)
    chk.append(("총중량 합계", gro_sum, out["gross_total"],
                out["gross_total"] is not None and abs(gro_sum - out["gross_total"]) < 0.5))
    out["checks"] = [{"항목": a, "계산": b, "문서": c, "일치": bool(d)} for a, b, c, d in chk]
    if not all(c["일치"] for c in out["checks"]):
        out["warnings"].append("검산이 맞지 않는 항목이 있습니다. 표를 확인하고 고쳐주세요.")

    # ── 예상입항일 = 선적일 + 60일 ──
    if out["shipped"]:
        import datetime
        try:
            d = datetime.date.fromisoformat(out["shipped"]) + datetime.timedelta(days=60)
            out["eta"] = d.isoformat()
        except ValueError:
            out["eta"] = ""
    else:
        out["eta"] = ""

    return out
