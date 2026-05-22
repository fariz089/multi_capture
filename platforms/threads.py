"""Threads login capture.

v1 (13 May 2026): Threads = child platform Meta, share auth dengan Instagram.

User flow di GUI / Chromium:
  - Buka login URL → diarahkan ke flow Instagram (kalau belum login) atau
    langsung masuk ke timeline Threads (kalau session IG sudah aktif).
  - Selesaikan 2FA / challenge IG kalau ada.
  - Setelah landing di /following atau /for-you, auto-detect.

Cookies penting (sama dengan IG karena share auth):
  sessionid   — auth utama (format: <user_id>%3A<token>%3A...)
  ds_user_id  — IG user ID (numerik, dipakai juga oleh Threads)
  csrftoken   — POST CSRF
  ig_did      — device ID (recommended)
  rur         — region routing (recommended)

Cookies tambahan yang khas Threads:
  - mostly hanya re-issue dari .threads.net domain. Storage_state akan
    capture keduanya (.instagram.com + .threads.net) karena Playwright
    record semua cookie dari context yang punya akses ke kedua domain.

is_logged_in: sessionid + ds_user_id ada DAN URL bukan /login lagi
(threads.net redirect ke /accounts/login kalau session invalid).
"""
from playwright.sync_api import Page

from .base import LoginCapture


class ThreadsCapture(LoginCapture):
    PLATFORM = "threads"
    LOGIN_URL = "https://www.threads.net/login"
    POST_LOGIN_HINT = (
        "Login pakai akun Instagram kamu di window Chromium (Threads share "
        "auth dengan IG). Threads akan redirect ke instagram.com untuk login, "
        "selesaikan 2FA / challenge kalau ada. "
        "Setelah landing di feed Threads (/following atau /for-you), "
        "sistem auto-detect, atau klik 'Saya Sudah Login' di GUI."
    )
    REQUIRED_COOKIES = ("sessionid", "ds_user_id")

    def is_logged_in(self, page: Page) -> bool:
        try:
            names = {c["name"] for c in page.context.cookies()}
            if "sessionid" in names and "ds_user_id" in names:
                url = page.url or ""
                # Threads redirect ke instagram.com/accounts/login kalau session invalid.
                # Halaman /login, /challenge, atau path IG accounts berarti belum login.
                # threads.net/@anything ATAU /following ATAU /for-you = sudah login.
                lowered = url.lower()
                if (
                    '/login' not in lowered
                    and '/challenge' not in lowered
                    and 'instagram.com/accounts' not in lowered
                ):
                    return True
        except Exception:
            pass
        return False
