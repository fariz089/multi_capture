"""Twitter / X login capture.

v3 (7 May 2026): Twitter sekarang cookie-based di SocialPulse (twikit sudah
di-drop). Tool ini = sumber utama session Twitter di SocialPulse.

Cookies penting di X:
  auth_token  — auth utama (40-char hex)
  ct0         — CSRF token (32-160 hex)
  twid        — user ID dengan prefix 'u%3D' (URL-encoded "u=")
  guest_id    — fallback identifier (recommended)
  kdt         — known device token (recommended, ngurangi challenge)

is_logged_in: auth_token + ct0 ada DAN URL bukan /login lagi.
"""
from playwright.sync_api import Page

from .base import LoginCapture


class TwitterCapture(LoginCapture):
    PLATFORM = "twitter"
    LOGIN_URL = "https://x.com/login"
    POST_LOGIN_HINT = (
        "Login pakai akun X (Twitter) kamu di window Chromium. "
        "Selesaikan 2FA / kode email kalau ada. "
        "Sistem auto-detect setelah ke timeline (/home), "
        "atau klik 'Saya Sudah Login' di GUI."
    )
    REQUIRED_COOKIES = ("auth_token", "ct0")

    def is_logged_in(self, page: Page) -> bool:
        try:
            names = {c["name"] for c in page.context.cookies()}
            if "auth_token" in names and "ct0" in names:
                url = page.url or ""
                # X redirect ke /home, /i/flow/login dianggap belum
                if "/login" not in url and "/i/flow" not in url:
                    return True
        except Exception:
            pass
        return False
