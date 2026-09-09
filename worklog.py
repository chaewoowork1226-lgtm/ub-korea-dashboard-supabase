"""업무일지 — 날짜별 텍스트 기록 + 구글드라이브 파일 업로드.

- 텍스트는 data/worklog.json 에 날짜별로 저장.
- 파일은 구글드라이브 '업무일지/YYYY-MM-DD/' 아래에 지정한 이름으로 올라간다.
  (구글 로그인은 gspread 와 같은 토큰을 재사용 — 통관 기능과 동일)
"""
import base64
import io
import json
import os
import re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "data", "worklog.json")

DRIVE_ROOT = "업무일지"
GS_TOKEN = os.path.join(os.getenv("APPDATA", ""), "gspread", "authorized_user.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

MAX_BYTES = 40 * 1024 * 1024   # 파일 1개당 40MB 제한


# ── 텍스트 기록 ──────────────────────────────────────────
def load() -> dict:
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
    else:
        d = {}
    d.setdefault("days", {})     # {"YYYY-MM-DD": {"text": "...", "files": [...]}}
    return d


def save(d: dict) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _day(d: dict, date: str) -> dict:
    return d["days"].setdefault(date, {"text": "", "files": []})


def set_text(date: str, text: str) -> dict:
    _check_date(date)
    d = load()
    day = _day(d, date)
    day["text"] = text
    day["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save(d)
    return d


def _check_date(date: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise RuntimeError("날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).")


# ── 구글드라이브 ─────────────────────────────────────────
def _service():
    if not os.path.exists(GS_TOKEN):
        raise RuntimeError("구글 로그인이 필요합니다. 통관 관리 탭에서 한 번 연동해주세요.")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(GS_TOKEN, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _folder(svc, name: str, parent: str = None) -> str:
    safe = name.replace("'", "\\'")
    q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
         f"and trashed = false")
    q += f" and '{parent}' in parents" if parent else " and 'root' in parents"
    got = svc.files().list(q=q, spaces="drive", fields="files(id)").execute().get("files", [])
    if got:
        return got[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        meta["parents"] = [parent]
    return svc.files().create(body=meta, fields="id").execute()["id"]


def _safe_name(name: str) -> str:
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r'[:*?"<>|]', "_", name)
    return name or "무제"


def upload(date: str, filename: str, content_b64: str, mime: str = None) -> dict:
    """업무일지/{날짜}/{파일명} 으로 업로드."""
    _check_date(date)
    filename = _safe_name(filename)
    try:
        raw = base64.b64decode(content_b64 or "", validate=False)
    except Exception:
        raise RuntimeError("파일 데이터를 읽지 못했습니다.")
    if not raw:
        raise RuntimeError("빈 파일입니다.")
    if len(raw) > MAX_BYTES:
        raise RuntimeError(f"파일이 너무 큽니다 ({len(raw)/1048576:.1f}MB). 40MB 이하만 올릴 수 있어요.")

    from googleapiclient.http import MediaIoBaseUpload
    svc = _service()
    root = _folder(svc, DRIVE_ROOT)
    day_id = _folder(svc, date, root)

    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype=mime or "application/octet-stream",
                              resumable=False)
    f = svc.files().create(body={"name": filename, "parents": [day_id]},
                           media_body=media,
                           fields="id,name,size,webViewLink").execute()

    d = load()
    day = _day(d, date)
    day.setdefault("files", []).append({
        "name": f.get("name"),
        "id": f.get("id"),
        "url": f.get("webViewLink"),
        "size": int(f.get("size") or len(raw)),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save(d)
    return {"ok": True, "file": day["files"][-1],
            "folder": f"https://drive.google.com/drive/folders/{day_id}"}


def remove_file(date: str, file_id: str, trash: bool = True) -> dict:
    """일지 목록에서 제거. trash=True 면 드라이브에서도 휴지통으로 보냄."""
    _check_date(date)
    d = load()
    day = _day(d, date)
    day["files"] = [x for x in day.get("files", []) if x.get("id") != file_id]
    save(d)
    if trash and file_id:
        try:
            _service().files().update(fileId=file_id, body={"trashed": True}).execute()
        except Exception:
            pass          # 드라이브에서 이미 지워졌어도 목록 정리는 유지
    return {"ok": True}


def folder_url(date: str) -> dict:
    """그 날짜 폴더 링크 (없으면 만들어서 반환)."""
    _check_date(date)
    svc = _service()
    root = _folder(svc, DRIVE_ROOT)
    day_id = _folder(svc, date, root)
    return {"url": f"https://drive.google.com/drive/folders/{day_id}"}
