# -*- coding: utf-8 -*-
"""파일을 구글 드라이브에 올린다.

    python upload_to_drive.py                     # 추석 엑셀을 내 드라이브 최상위로
    python upload_to_drive.py "D:\\어떤파일.xlsx"    # 다른 파일
    python upload_to_drive.py "파일" "폴더명"        # 그 이름의 폴더 안으로 (없으면 만듦)
"""
import mimetypes
import os
import sys

GS_TOKEN = os.path.join(os.getenv("APPDATA", ""), "gspread", "authorized_user.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

DEFAULT = os.path.join(os.path.expanduser("~"), "Downloads",
                       "추석_재고부족_SKU_2026.xlsx")


def service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not os.path.exists(GS_TOKEN):
        sys.exit("구글 로그인이 필요합니다. 먼저 google_login.py 를 실행하세요.")
    creds = Credentials.from_authorized_user_file(GS_TOKEN, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def folder_id(svc, name):
    q = ("name = '%s' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false and 'root' in parents" % name.replace("'", "\\'"))
    got = svc.files().list(q=q, spaces="drive", fields="files(id)").execute().get("files", [])
    if got:
        return got[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    return svc.files().create(body=meta, fields="id").execute()["id"]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    folder = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(path):
        sys.exit("파일을 찾지 못했습니다: %s" % path)

    from googleapiclient.http import MediaFileUpload
    svc = service()

    body = {"name": os.path.basename(path)}
    if folder:
        body["parents"] = [folder_id(svc, folder)]

    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    media = MediaFileUpload(path, mimetype=mime, resumable=False)

    print("올리는 중:", os.path.basename(path),
          "(%.1f KB)" % (os.path.getsize(path) / 1024))
    f = svc.files().create(body=body, media_body=media,
                           fields="id,name,size,webViewLink").execute()

    print("\n업로드 완료")
    print("  파일명:", f.get("name"))
    print("  위치  :", folder or "내 드라이브 (최상위)")
    print("  링크  :", f.get("webViewLink"))


if __name__ == "__main__":
    main()
