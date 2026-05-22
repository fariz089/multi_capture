"""Facebook login capture.

Cookies penting:
  c_user — FB user ID (numerik)
  xs     — session token utama
  fr     — friendly token (recommended)
  datr   — device fingerprint (optional tapi biasanya muncul)

is_logged_in heuristik: redirect ke domain facebook.com tanpa /login,
ATAU c_user + xs cookie ada. Cukup robust.
"""
from playwright.sync_api import Page

from .base import LoginCapture


class FacebookCapture(LoginCapture):
    PLATFORM = "facebook"
    LOGIN_URL = "https://www.facebook.com/login"
    POST_LOGIN_HINT = (
        "Login pakai akun FB kamu di window Chromium. "
        "Selesaikan 2FA / checkpoint kalau ada. "
        "Sistem auto-detect saat feed muncul, atau klik 'Saya Sudah Login' di GUI."
    )
    REQUIRED_COOKIES = ("c_user", "xs")

    def is_logged_in(self, page: Page) -> bool:
        # Cara 1: cek cookie c_user + xs
        try:
            names = {c["name"] for c in page.context.cookies()}
            if "c_user" in names and "xs" in names:
                # Pastikan juga URL bukan login page lagi
                url = page.url or ""
                if "/login" not in url and "checkpoint" not in url:
                    return True
        except Exception:
            pass
        return False
