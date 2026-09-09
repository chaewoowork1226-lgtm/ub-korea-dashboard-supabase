# -*- coding: utf-8 -*-
"""인보이스 PDF를 클로드로 읽고 정합성까지 검증한다.

`invoice_pdf.py` 의 정규식 파서는 COOSUR 한 가지 서식에만 맞아서, 서식이 조금만
달라도 품목을 하나도 못 읽는다. 이 모듈은 PDF 원본을 클로드에게 그대로 넘겨서
읽게 하고, 읽은 값끼리 숫자가 맞는지(정합성) 파이썬에서 다시 계산해 확인한다.

돌려주는 형태는 `invoice_pdf.parse()` 와 똑같다. 거기에 `findings`(검증 결과)와
`source`('ai' / 'regex')가 더 붙는다. 그래서 확인 화면 코드는 그대로 쓸 수 있다.
"""
import base64
import io
import json
import os

import invoice_pdf

# 바꾸고 싶으면 customs_config.json 의 "anthropic_model" 에 넣으면 된다.
# 추출 작업이라 sonnet 으로 내려도 품질 차이가 거의 없고 값은 절반 이하다.
DEFAULT_MODEL = "claude-opus-5"

# 인보이스 1장이 품목 수십 줄이라 넉넉히 잡는다. (생각 + 답변을 합쳐 제한된다)
MAX_TOKENS = 16000

# 딥시크(deepseek-chat)는 응답 상한이 8192로 고정이라, 이보다 크게 요청하면
# 오히려 도중에 잘린다(실제로 확인됨 — 16000으로 요청했더니 2,668토큰짜리 응답이 잘림).
DEEPSEEK_MAX_TOKENS = 8192

# 추출은 깊은 추론이 아니라서 effort 를 낮춰도 품질이 유지된다. 토큰이 가장 크게 줄어드는 손잡이.
DEFAULT_EFFORT = "medium"

# 글자가 이만큼 안 나오면 스캔본으로 보고 PDF 원본(이미지)을 보낸다.
TEXT_LAYER_MIN_CHARS = 400

PROMPT = """당신은 스페인·이탈리아산 식용유/식초를 한국으로 수입하는 회사의 통관 담당자입니다.
첨부한 수입 인보이스(패킹리스트·분석표가 같이 붙어 있을 수 있음)를 읽고, 아래를 해주세요.

1) 품목을 한 줄도 빠뜨리지 말고 모두 뽑아주세요. 합계(TOTAL) 줄은 품목이 아닙니다.
2) 각 품목의 단가가 '박스(카톤) 1개 가격'인지 '병 1개 가격'인지 판단해 price_basis 에 적어주세요.
   인보이스 단가는 보통 박스 가격입니다. 단가 × 박스수 = 금액 이면 박스 가격(carton),
   단가 × 총병수 = 금액 이면 병 가격(bottle) 입니다. 판단이 안 되면 "unknown".
3) 숫자 표기는 유럽식(1.234,56 = 천이백삼십사점오육)일 수 있습니다. 반드시 실제 수치로 바꿔서 적어주세요.
4) 문서 안에서 앞뒤가 안 맞는 곳을 findings 에 적어주세요. 예를 들면:
   - 품목 중량의 합이 문서에 적힌 총중량과 다름
   - 박스수 합이 문서 합계와 다름
   - 단가 × 수량 이 금액과 안 맞음
   - 배치번호나 소비기한이 빠진 품목
   - 같은 품목이 두 번 적힘
   확실한 것만 적고, 애매하면 level 을 "info" 로 낮춰주세요.
5) 문서에 없는 값은 지어내지 말고 비워두세요(null). 추측한 값은 findings 에 남겨주세요.
6) 같은 품목이 배치(batch)별로 패킹리스트에 여러 줄로 나뉘어 있으면(예: 인보이스엔 한 줄,
   패킹리스트엔 배치 A 372박스 + 배치 B 186박스로 558박스가 나뉨), items 에는 그 배치들을
   각각 별도 줄로 넣고 cartons 에는 **그 배치 자신의 박스수만** 넣으세요.
   인보이스에 적힌 그 품목의 전체 합계 박스수를 배치 줄에 그대로 옮기면 안 됩니다
   (실제로 이 실수로 수량이 부풀려진 적이 있습니다 — 특히 첫 번째 배치 줄에서 주의).
   cartons_total 에는 인보이스/패킹리스트 맨 아래 TOTALS 줄에 적힌 박스(카톤) 합계를 넣으세요.

배치번호는 앞자리 0이 중요합니다. 숫자로 바꾸지 말고 적힌 그대로 문자열로 주세요."""

SCHEMA = {
    "type": "object",
    "properties": {
        "supplier": {"type": "string", "description": "공급사명 (예: ACEITES DEL SUR, BORGES)"},
        "invoice_no": {"type": "string"},
        "invoice_date": {"type": "string", "description": "YYYY-MM-DD"},
        "container": {"type": "string"},
        "bl": {"type": "string", "description": "B/L 또는 Booking 번호"},
        "shipped": {"type": "string", "description": "선적일 YYYY-MM-DD"},
        "customer_ref": {"type": "string", "description": "CUSTOMER REF (예: 12o2026 이면 12)"},
        "net_total": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "gross_total": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "cartons_total": {"anyOf": [{"type": "number"}, {"type": "null"}],
                          "description": "문서의 TOTALS 줄에 적힌 박스(카톤) 합계"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "인보이스에 적힌 영문 품명 그대로"},
                    "code": {"type": "string"},
                    "bottles_per_carton": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "cartons": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "unit_price_eur": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "price_basis": {"type": "string", "enum": ["carton", "bottle", "unknown"]},
                    "amount_eur": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "expiry": {"type": "string", "description": "소비기한 YYYY.MM.DD"},
                    "batch": {"type": "string", "description": "배치번호 (앞자리 0 유지)"},
                    "acidity_pct": {"type": "string"},
                    "net_kg": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "gross_kg": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                },
                "required": ["description", "code", "bottles_per_carton", "cartons",
                             "unit_price_eur", "price_basis", "amount_eur", "expiry",
                             "batch", "acidity_pct", "net_kg", "gross_kg"],
                "additionalProperties": False,
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["error", "warn", "info"]},
                    "where": {"type": "string", "description": "어느 품목/항목인지"},
                    "message": {"type": "string", "description": "한국어로, 사장님이 읽을 문장"},
                },
                "required": ["level", "where", "message"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["supplier", "invoice_no", "invoice_date", "container", "bl", "shipped",
                 "customer_ref", "net_total", "gross_total", "cartons_total", "items", "findings"],
    "additionalProperties": False,
}


# ── 인증키 ──
def get_api_key() -> str:
    """환경변수 우선, 없으면 customs_config.json 의 anthropic_api_key."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    try:
        import customs
        return (customs.load_config().get("anthropic_api_key") or "").strip()
    except Exception:
        return ""


def save_api_key(key: str) -> None:
    import customs
    cfg = customs.load_config()
    cfg["anthropic_api_key"] = (key or "").strip()
    customs.save_config(cfg)


def get_deepseek_key() -> str:
    """환경변수 우선, 없으면 customs_config.json 의 deepseek_api_key."""
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    try:
        import customs
        return (customs.load_config().get("deepseek_api_key") or "").strip()
    except Exception:
        return ""


def save_deepseek_key(key: str) -> None:
    import customs
    cfg = customs.load_config()
    cfg["deepseek_api_key"] = (key or "").strip()
    customs.save_config(cfg)


def _setting(name: str, default):
    try:
        import customs
        v = customs.load_config().get(name)
        return v if v else default
    except Exception:
        return default


def _provider() -> str:
    """어느 AI를 쓸지 고른다. 딥시크 키가 있으면 딥시크를 우선한다(더 저렴함).
    둘 다 없으면 빈 문자열."""
    if get_deepseek_key():
        return "deepseek"
    if get_api_key():
        return "anthropic"
    return ""


def available() -> tuple:
    """(쓸 수 있나, 안내문). 화면에 그대로 띄운다."""
    prov = _provider()
    if prov == "deepseek":
        return True, ""
    if prov == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, ("클로드 검증을 쓰려면 anthropic 패키지가 필요해요. "
                           "바탕화면 '클로드 검증 설치.bat' 을 한 번 실행해주세요.")
        return True, ""
    return False, ("AI 키가 없어요. 통관관리 탭 위쪽 '클로드 키' 또는 '딥시크 키' 칸에 "
                   "넣고 저장해주세요.")


# ── 숫자 도우미 ──
def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else round(f, 4)


def _close(a, b, tol=0.5):
    return a is not None and b is not None and abs(a - b) <= tol


def _sum(vals):
    got = [v for v in vals if v is not None]
    return round(sum(got), 3) if got else None


# ── 클로드 호출 ──
def _as_bytes(data) -> bytes:
    if isinstance(data, str):
        return base64.b64decode(data)
    return data


def pdf_text(raw: bytes) -> str:
    """PDF 안의 글자층을 뽑는다. 스캔본이면 거의 빈 문자열이 나온다."""
    try:
        from pypdf import PdfReader
        pages = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages]
    except Exception:
        return ""
    return "\n\n".join("[%d쪽]\n%s" % (i + 1, t.strip()) for i, t in enumerate(pages) if t.strip())


def read_pdf(data) -> dict:
    """PDF(base64 문자열 또는 bytes) → AI가 읽은 원자료. 저장된 키에 따라
    딥시크 또는 클로드로 보낸다 (`_provider()` 참고).

    딥시크가 (스캔본이라 못 읽거나, 잔액 부족 등으로) 실패했는데 클로드 키도
    있으면 조용히 클로드로 넘어간다 — 대신 넘어갔다는 사실을 `_fallback_note`에
    남겨서 `parse()`가 화면에 알려주게 한다."""
    raw = _as_bytes(data)
    text = pdf_text(raw)
    used_text = len(text) >= TEXT_LAYER_MIN_CHARS

    prov = _provider()
    if prov == "deepseek":
        if used_text:
            try:
                return _read_deepseek(text)
            except Exception as e:
                if not get_api_key():
                    raise
                result = _read_anthropic(data, text, used_text)
                result["_fallback_note"] = "딥시크 호출이 실패해서 클로드로 대신 읽었어요: %s" % e
                return result
        # 스캔본(글자층 없음) — 딥시크는 이미지를 못 읽는다.
        if get_api_key():
            result = _read_anthropic(data, text, used_text)
            result["_fallback_note"] = "스캔본이라 딥시크 대신 클로드로 읽었어요."
            return result
        raise RuntimeError(
            "이 PDF는 글자를 못 읽는 스캔본이에요. 딥시크는 글자만 읽을 수 있어서 "
            "이미지로 된 페이지는 못 읽어요. '클로드 키'를 넣고 다시 시도해주세요.")
    return _read_anthropic(data, text, used_text)


def _read_anthropic(data, text: str, used_text: bool) -> dict:
    """토큰을 아끼려고 두 가지를 한다:
      1. PDF 에 글자층이 있으면 이미지 대신 글자만 보낸다 (보통 1/5 수준).
      2. 지시문·스키마는 system 에 넣고 캐시한다. 두 번째 파일부터 그 부분은 1/10 값이다.
    """
    import anthropic

    if used_text:
        content = [{"type": "text", "text": "인보이스 원문:\n\n" + text}]
    else:
        b64 = data if isinstance(data, str) else base64.b64encode(_as_bytes(data)).decode("ascii")
        content = [{"type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf",
                               "data": b64.replace("\n", "").replace("\r", "")}}]

    client = anthropic.Anthropic(api_key=get_api_key())
    resp = client.messages.create(
        model=_setting("anthropic_model", DEFAULT_MODEL),
        max_tokens=MAX_TOKENS,
        # 지시문은 매번 똑같으니 캐시한다 (PDF 는 매번 달라서 뒤에 둔다)
        system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": _setting("anthropic_effort", DEFAULT_EFFORT),
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("클로드가 이 문서 처리를 거절했어요. 다른 파일로 시도해주세요.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("인보이스가 너무 길어서 다 읽지 못했어요. 페이지를 나눠서 올려주세요.")
    out = next((b.text for b in resp.content if b.type == "text"), "")
    if not out:
        raise RuntimeError("클로드 응답이 비어 있어요.")

    parsed = json.loads(out)
    u = resp.usage
    parsed["_usage"] = {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "mode": "글자" if used_text else "이미지(스캔본)",
    }
    return parsed


# ── 딥시크 호출 ──
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# 딥시크는 클로드처럼 스키마를 강제하는 기능이 없어서(json_object 모드만 지원),
# 스키마를 프롬프트 안에 통째로 넣어 형식을 맞춰달라고 부탁한다.
_DEEPSEEK_SUFFIX = ("\n\n반드시 아래 JSON 스키마와 같은 모양의 JSON 객체 '하나만' 답하세요. "
                    "설명 문장이나 코드블록 표시(```) 없이 JSON만 출력하세요.\n\n스키마:\n")


def _read_deepseek(text: str) -> dict:
    import urllib.request
    import urllib.error

    sys_prompt = PROMPT + _DEEPSEEK_SUFFIX + json.dumps(SCHEMA, ensure_ascii=False)
    body = json.dumps({
        "model": _setting("deepseek_model", DEEPSEEK_MODEL),
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "인보이스 원문:\n\n" + text},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": DEEPSEEK_MAX_TOKENS,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + get_deepseek_key()})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError("딥시크 호출 실패(%s): %s" % (e.code, detail)) from e
    except urllib.error.URLError as e:
        raise RuntimeError("딥시크 네트워크 오류: %s" % e) from e

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("딥시크 응답이 비어 있어요: %s" % json.dumps(payload, ensure_ascii=False)[:300])
    out = (choices[0].get("message") or {}).get("content") or ""
    finish = choices[0].get("finish_reason")
    if finish == "length":
        raise RuntimeError("인보이스가 너무 길어서 다 읽지 못했어요. 페이지를 나눠서 올려주세요.")
    if not out:
        raise RuntimeError("딥시크 응답이 비어 있어요.")

    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError("딥시크 응답을 JSON으로 읽지 못했어요: %s" % e) from e

    parsed = _normalize_ai_json(parsed)
    u = payload.get("usage") or {}
    parsed["_usage"] = {
        "input": u.get("prompt_tokens", 0),
        "output": u.get("completion_tokens", 0),
        "cache_write": 0,
        "cache_read": u.get("prompt_cache_hit_tokens", 0),
        "mode": "글자(딥시크)",
    }
    return parsed


def _normalize_ai_json(parsed) -> dict:
    """딥시크는 스키마를 강제로 지키지 않으므로, 빠진 값을 기본값으로 채워
    뒤 코드가 KeyError 없이 그대로 동작하게 한다."""
    parsed = dict(parsed) if isinstance(parsed, dict) else {}
    top_defaults = {
        "supplier": "", "invoice_no": "", "invoice_date": "", "container": "",
        "bl": "", "shipped": "", "customer_ref": "", "net_total": None,
        "gross_total": None, "cartons_total": None, "items": [], "findings": [],
    }
    for k, v in top_defaults.items():
        parsed.setdefault(k, v)

    item_defaults = {
        "description": "", "code": "", "bottles_per_carton": None, "cartons": None,
        "unit_price_eur": None, "price_basis": "unknown", "amount_eur": None,
        "expiry": "", "batch": "", "acidity_pct": "", "net_kg": None, "gross_kg": None,
    }
    items = []
    for it in (parsed.get("items") or []):
        if not isinstance(it, dict):
            continue
        d = dict(item_defaults)
        d.update({k: v for k, v in it.items() if k in item_defaults})
        items.append(d)
    parsed["items"] = items

    finding_defaults = {"level": "info", "where": "", "message": ""}
    findings = []
    for f in (parsed.get("findings") or []):
        if not isinstance(f, dict):
            continue
        d = dict(finding_defaults)
        d.update({k: v for k, v in f.items() if k in finding_defaults})
        findings.append(d)
    parsed["findings"] = findings
    return parsed


# ── 정합성 검산 (파이썬이 직접 계산 — 여기가 근거가 된다) ──
def cross_check(raw: dict, items: list) -> list:
    """클로드가 읽은 값끼리 숫자가 맞는지 파이썬으로 다시 계산한다."""
    out = []

    def add(level, where, msg):
        out.append({"level": level, "where": where, "message": msg})

    # 1) 품목별 — 총수량, 단가 기준, 금액
    for it in items:
        nm = it.get("품명") or it.get("_영문") or "(품명 없음)"
        cartons, bottles = it.get("카톤수"), it.get("병입수")
        if cartons and bottles:
            want = cartons * bottles
            if it.get("총수량") != want:
                add("warn", nm, "총수량이 %s인데 카톤수×병입수는 %s예요." % (it.get("총수량"), want))
        if it.get("_price_basis") == "unknown" and bottles:
            add("warn", nm, "단가가 박스 가격인지 병 가격인지 확실하지 않아요. "
                            "박스가라면 %s병으로 나눠야 합니다. 확인해주세요." % bottles)
        amt, unit = it.get("_amount_eur"), it.get("개당가격(EUR)")
        if amt and unit and cartons and bottles:
            calc = unit * cartons * bottles
            if abs(calc - amt) > max(1.0, amt * 0.02):
                add("warn", nm, "개당가격×총수량이 %s인데 인보이스 금액은 %s예요." % (round(calc, 2), amt))
        if not it.get("배치넘버"):
            add("info", nm, "배치번호가 비어 있어요.")
        if not it.get("소비기한"):
            add("info", nm, "소비기한이 비어 있어요.")

    # 2) 합계 대조
    pairs = [("박스수 합계", _sum([i.get("카톤수") for i in items]), _num(raw.get("cartons_total"))),
             ("넷중량 합계", _sum([i.get("넷중량(KG)") for i in items]), _num(raw.get("net_total"))),
             ("총중량 합계", _sum([i.get("총중량(KG)") for i in items]), _num(raw.get("gross_total")))]
    for label, calc, doc in pairs:
        if calc is not None and doc is not None and not _close(calc, doc):
            add("error", label, "품목을 더하면 %s인데 문서 합계는 %s예요." % (calc, doc))

    # 3) 넷중량이 총중량보다 크면 뒤바뀐 것
    for it in items:
        n, g = it.get("넷중량(KG)"), it.get("총중량(KG)")
        if n is not None and g is not None and n > g:
            add("error", it.get("품명") or "", "넷중량(%s)이 총중량(%s)보다 커요. 두 값이 바뀐 것 같아요." % (n, g))

    # 4) 중복 배치
    seen = {}
    for it in items:
        b = (it.get("배치넘버") or "").strip()
        if not b:
            continue
        if b in seen:
            add("warn", b, "배치번호가 '%s'와 '%s' 두 줄에 똑같이 있어요." % (seen[b], it.get("품명")))
        else:
            seen[b] = it.get("품명")

    return out


def _to_sheet_items(raw: dict) -> list:
    """클로드가 읽은 원자료 → 시트에 넣을 형태 (invoice_pdf 와 같은 열 이름)."""
    items = []
    for r in raw.get("items") or []:
        desc = (r.get("description") or "").strip()
        bottles = r.get("bottles_per_carton")
        cartons = _num(r.get("cartons"))
        unit = _num(r.get("unit_price_eur"))

        # 개당가격은 '1병' 가격이어야 한다. 박스가면 병입수로 나눈다.
        per_bottle = unit
        if unit is not None and r.get("price_basis") == "carton" and bottles:
            per_bottle = round(unit / bottles, 2)

        ml = invoice_pdf._ml(desc)
        items.append({
            "품명": invoice_pdf._korean(desc),
            "병입수": bottles,
            "카톤수": cartons,
            "총수량": (cartons * bottles) if (cartons and bottles) else None,
            "파렛트별박스수": invoice_pdf.PALLET_DEFAULT.get(ml, ""),
            "개당가격(EUR)": per_bottle,
            # 문서마다 "2029/06" 처럼 슬래시를 쓰기도 해서, 정규식 파서와 똑같이 점으로 통일한다.
            "소비기한": (r.get("expiry") or "").strip().replace("-", ".").replace("/", "."),
            "산도(%)": (r.get("acidity_pct") or "").strip(),
            "배치넘버": (r.get("batch") or "").strip(),
            "넷중량(KG)": _num(r.get("net_kg")),
            "총중량(KG)": _num(r.get("gross_kg")),
            "_영문": desc,
            "_price_basis": r.get("price_basis") or "unknown",
            "_amount_eur": _num(r.get("amount_eur")),
        })
    return items


def parse(data) -> dict:
    """PDF → 확인 화면에 띄울 값 + 정합성 검증 결과.

    `invoice_pdf.parse()` 와 같은 형태를 돌려준다.
    """
    ok, why = available()
    if not ok:
        raise RuntimeError(why)

    raw = read_pdf(data)
    fallback_note = raw.pop("_fallback_note", None)
    items = _to_sheet_items(raw)

    supplier = (raw.get("supplier") or "").upper()
    tab, prefix = "", ""
    for key, (t, p) in invoice_pdf.SUPPLIERS.items():
        if key in supplier:
            tab, prefix = t, p
            break

    ref = (raw.get("customer_ref") or "").strip()
    order_no = None
    for tok in ref.replace("o", " ").replace("º", " ").replace("°", " ").split():
        if tok.isdigit() and len(tok) <= 3:
            order_no = int(tok)
            break

    out = {
        "sheet_tab": tab,
        "invoice_no": (raw.get("invoice_no") or "").strip(),
        "invoice_date": (raw.get("invoice_date") or "").strip(),
        "container": (raw.get("container") or "").strip(),
        "bl": (raw.get("bl") or "").strip(),
        "shipped": (raw.get("shipped") or "").strip(),
        "order_no": order_no,
        "order_name": ("%s %d번" % (prefix, order_no)) if (prefix and order_no) else "",
        "net_total": _num(raw.get("net_total")),
        "gross_total": _num(raw.get("gross_total")),
        "cartons_total": _num(raw.get("cartons_total")),
        "items": items,
        "invoice_lines": [],
        "source": "ai",
    }

    # 예상입항일 = 선적일 + 60일 (기존 규칙 그대로)
    out["eta"] = ""
    if out["shipped"]:
        import datetime
        try:
            d = datetime.date.fromisoformat(out["shipped"]) + datetime.timedelta(days=60)
            out["eta"] = d.isoformat()
        except ValueError:
            pass

    # 검산 표 (기존 확인 화면이 이 모양을 그린다)
    box_sum = _sum([i.get("카톤수") for i in items])
    net_sum = _sum([i.get("넷중량(KG)") for i in items])
    gro_sum = _sum([i.get("총중량(KG)") for i in items])
    out["checks"] = [
        {"항목": "박스수 합계", "계산": box_sum, "문서": out["cartons_total"],
         "일치": _close(box_sum, out["cartons_total"], tol=0.5)},
        {"항목": "넷중량 합계", "계산": net_sum, "문서": out["net_total"],
         "일치": _close(net_sum, out["net_total"])},
        {"항목": "총중량 합계", "계산": gro_sum, "문서": out["gross_total"],
         "일치": _close(gro_sum, out["gross_total"])},
        {"항목": "품목 수", "계산": len(items), "문서": len(items), "일치": bool(items)},
    ]

    # 클로드가 짚은 것 + 파이썬이 계산해서 짚은 것
    findings = list(raw.get("findings") or [])
    findings.extend(cross_check(raw, items))
    out["findings"] = findings
    out["usage"] = raw.get("_usage") or {}

    warnings = []
    if fallback_note:
        warnings.append(fallback_note)
    if not items:
        warnings.append("품목을 하나도 읽지 못했어요. 스캔본이라면 글자가 선명한 파일로 다시 올려주세요.")
    if not tab:
        warnings.append("공급사를 알아내지 못했어요. 시트 탭을 직접 골라주세요.")
    if not out["order_name"]:
        warnings.append("주문 번호(CUSTOMER REF)를 찾지 못했어요. 주문명을 직접 입력해주세요.")
    if not out["bl"]:
        warnings.append("BL번호를 찾지 못했어요. 직접 입력해주세요.")
    for f in findings:
        if f.get("level") == "error":
            warnings.append("%s — %s" % (f.get("where") or "", f.get("message") or ""))
    out["warnings"] = warnings
    return out


def parse_best(data, force_ai: bool = False) -> dict:
    """정규식으로 먼저 읽어보고, 못 읽었거나 자체 검산이 안 맞으면 AI로 넘어간다.

    토큰을 아끼려고, 정규식이 제대로 읽었으면 AI를 부르지 않는다.
    그 경우 정합성 검증은 파이썬이 직접 계산해서 붙인다 (돈이 안 든다).

    정규식은 인보이스 서식이 살짝만 달라도(줄바꿈 위치, 소비기한 구분자 등) 품목을
    하나 통째로 놓치고도 '성공'으로 보일 수 있다 — 그럴 때 자체 검산(박스수·중량 합계)이
    어긋나므로, 검산이 안 맞으면 AI 키가 있는 한 자동으로 AI 결과로 바꿔친다.
    """
    if force_ai:
        return parse(data)

    try:
        reg = invoice_pdf.parse(data)
    except Exception:
        reg = None

    if reg and reg.get("items"):
        reg["source"] = "regex"
        reg.setdefault("findings", [])
        # 무료 검산 — AI를 부르지 않는다
        reg["findings"].extend(cross_check(reg, reg["items"]))
        reg["usage"] = {}

        checks_ok = all(c.get("일치") for c in (reg.get("checks") or []))
        if checks_ok:
            return reg

        # 검산이 안 맞는다 — 품목을 놓쳤거나 잘못 읽었을 가능성이 높다.
        ok, _why = available()
        if not ok:
            reg["warnings"] = (reg.get("warnings") or []) + [
                "자동 검산이 안 맞았지만 AI 키가 없어서 정규식 결과를 그대로 보여드려요. "
                "표를 직접 확인해주세요."]
            return reg
        try:
            ai = parse(data)
            ai["warnings"] = (ai.get("warnings") or []) + [
                "정규식으로 읽은 값의 검산이 안 맞아서 AI로 다시 읽었어요."]
            return ai
        except Exception as e:
            reg["warnings"] = (reg.get("warnings") or []) + [
                "자동 검산이 안 맞았고 AI 재확인도 실패해서(%s) 정규식 결과를 그대로 "
                "보여드려요. 표를 직접 확인해주세요." % e]
            return reg

    return parse(data)
