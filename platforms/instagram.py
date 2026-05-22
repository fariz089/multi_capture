"""Instagram login capture.

v3 (7 May 2026): IG sekarang cookie-based di SocialPulse (instagrapi
sudah di-drop). Tool ini = sumber utama session IG di SocialPulse.

Cookies penting:
  sessionid   — auth utama (format: <user_id>%3A<token>%3A...)
  ds_user_id  — IG user ID (numerik)
  csrftoken   — POST CSRF
  rur         — region routing (recommended, tidak strict)
  ig_did      — device ID (recommended)

is_logged_in: cek sessionid + ds_user_id ada DAN URL bukan login page lagi.
"""
from playwright.sync_api import Page

from .base import LoginCapture


class InstagramCapture(LoginCapture):
    PLATFORM = "instagram"
    LOGIN_URL = "https://www.instagram.com/accounts/login/"
    POST_LOGIN_HINT = (
        "Login pakai akun IG kamu di window Chromium. "
        "Selesaikan 2FA / challenge kalau ada. "
        "Setelah feed muncul (atau dialog 'Save your login info?'), "
        "sistem auto-detect, atau klik 'Saya Sudah Login' di GUI."
    )
    REQUIRED_COOKIES = ("sessionid", "ds_user_id")

    def is_logged_in(self, page: Page) -> bool:
        try:
            names = {c["name"] for c in page.context.cookies()}
            if "sessionid" in names and "ds_user_id" in names:
                # Pastikan bukan masih di login page
                url = page.url or ""
                if "/accounts/login" not in url and "/challenge" not in url:
                    return True
        except Exception:
            pass
        return False
