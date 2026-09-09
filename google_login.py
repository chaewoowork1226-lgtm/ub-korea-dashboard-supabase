# -*- coding: utf-8 -*-
"""구글 재로그인 - 크롬으로 동의 화면을 연다.

    python google_login.py

통관관리 · 업무일지 · 이커머스 시트가 모두 이 토큰 하나를 같이 쓴다.
한 번 승인하면 세 기능이 전부 다시 동작한다.
"""
import json
import os
import sys
import traceback
import webbrowser

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.getenv("LOCALAPPDATA", ""),
                 r"Google\Chrome\Application\chrome.exe"),
]

GS_DIR = os.path.join(os.getenv("APPDATA", ""), "gspread")
CREDS = os.path.join(GS_DIR, "credentials.json")      # 앱(클라이언트) 정보
TOKEN = os.path.join(GS_DIR, "authorized_user.json")  # 로그인 결과
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]


def use_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p):
            webbrowser.register("chrome", None,
                                webbrowser.BackgroundBrowser(p), preferred=True)
            print("크롬으로 엽니다:", p)
            return
    print("! 크롬을 찾지 못해 기본 브라우저로 엽니다.")


def save(creds):
    os.makedirs(GS_DIR, exist_ok=True)
    with open(TOKEN, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print("\n토큰 저장 완료:", TOKEN)


def main():
    if not os.path.exists(CREDS):
        sys.exit("앱 정보 파일이 없습니다: %s" % CREDS)

    use_chrome()
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(CREDS, SCOPES)
    print("\n브라우저에서 계정을 고르고 [허용]을 눌러주세요.")
    print('("이 앱은 확인되지 않았습니다" → 고급 → 안전하지 않은 페이지로 이동)\n')

    try:
        creds = flow.run_local_server(
            port=0, open_browser=True,
            authorization_prompt_message="",
            success_message="승인됐습니다. 이 창을 닫고 터미널을 확인하세요.")
    except Exception:
        print("\n=== 승인 후 토큰 교환에서 실패했습니다 ===")
        traceback.print_exc()
        print("\n위 메시지를 그대로 복사해서 알려주세요.")
        sys.exit(1)

    save(creds)          # 받자마자 먼저 저장 (뒤에서 무슨 일이 나도 로그인은 유지)

    try:
        from googleapiclient.discovery import build
        me = build("drive", "v3", credentials=creds).about().get(
            fields="user(emailAddress)").execute()
        print("연결된 계정:", me["user"]["emailAddress"])
        print("\n로그인 완료 - 이제 대시보드의 구글 기능이 다시 동작합니다.")
    except Exception:
        print("\n토큰은 저장됐지만 연결 확인에서 오류가 났습니다:")
        traceback.print_exc()


if __name__ == "__main__":
    main()
