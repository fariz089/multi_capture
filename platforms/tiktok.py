"""TikTok login capture.

Cookies penting (lihat tiktok_accounts.py di SocialPulse):
  msToken    — paling penting; di-rename ke ms_token oleh SocialPulse parser
  sessionid  — auth utama
  sid_guard  — wrapper sessionid + expire
  ttwid      — device fingerprint kuat
  tt_csrf_token — untuk POST endpoint

Note penting tentang msToken:
  msToken muncul di TIGA tempat yang berbeda:
    - domain ".tiktok.com"     ← yang KITA MAU (parent, valid utk semua subdomain)
    - domain "www.tiktok.com"  ← host-only, kurang general
    - dalam URL params signature TikTokApi
  
  Playwright context.cookies() return SEMUA. SocialPulse parser udah handle
  prioritas (.tiktok.com menang), tapi storage_state kita simpan APA ADANYA —
  biar parser tujuan punya pilihan.

is_logged_in heuristik:
  sessionid muncul = user authenticated. msToken sendiri muncul tanpa login
  (anonymous browsing), jadi harus pakai sessionid sebagai signal.
"""
from playwright.sync_api import Page

from .base import LoginCapture


class TikTokCapture(LoginCapture):
    PLATFORM = "tiktok"
    # Login page TikTok ada beberapa varian. /login langsung paling stabil.
    # Jangan pakai /foryou langsung — TikTok kadang serve splash anonymous
    # pertama tanpa kasih login prompt.
    LOGIN_URL = "https://www.tiktok.com/login/phone-or-email/email"
    POST_LOGIN_HINT = (
        "Pilih metode login yang biasa kamu pakai (Email/Telp/Google). "
        "Setelah masuk ke 'For You' page, sistem auto-detect. "
        "Kalau kena captcha geser, selesaikan dulu — captcha TikTok "
        "biasanya butuh 2-3 percobaan."
    )
    # ms_token KETAT-nya wajib, tapi SocialPulse simpan storage_state full.
    # Cookie names di Playwright = nama asli ('msToken', bukan 'ms_token'
    # yang itu naming SocialPulse internal).
    REQUIRED_COOKIES = ("sessionid", "msToken")
    # Setelah login, buka halaman search sekali. Ini memicu TikTok menulis
    # signing keys ke localStorage dari konteks search — kunci yang divalidasi
    # endpoint /api/search/item/full/ (yang selama ini balas size=0 di scraper).
    POST_LOGIN_VISIT_URL = "https://www.tiktok.com/search/video?q=bus"
    # PENTING: locale HARUS sama dengan pc-scraper/tiktok_scraper.py yang
    # sekarang aktif (id-ID / Asia/Jakarta). Sebelumnya di-set en-US biar
    # cocok dengan TikTokApi lama (tiktok-pc), tapi TikTokApi sudah tidak
    # dipakai. Kalau cookie di-capture sebagai sesi en-US lalu dipakai ulang
    # oleh browser id-ID, TikTok anggap itu session pindah negara → captcha
    # terus + endpoint search dimatikan (size=0). Samakan supaya konsisten.
    LOCALE = "id-ID"
    TIMEZONE = "Asia/Jakarta"

    def is_logged_in(self, page: Page) -> bool:
        try:
            names = {c["name"] for c in page.context.cookies()}
            # sessionid = signal terkuat. msToken muncul anonymous juga.
            if "sessionid" in names and "msToken" in names:
                return True
        except Exception:
            pass
        return False

    def _post_process_state(self, state: dict) -> None:
        """
        Untuk TikTok, kalau msToken muncul DUA KALI (parent + host-only domain),
        Playwright simpan keduanya. SocialPulse parser udah pintar handle ini,
        tapi kita log info supaya user tahu.
        """
        cookies = state.get("cookies") or []
        ms_tokens = [c for c in cookies if c.get("name") == "msToken"]
        if len(ms_tokens) > 1:
            domains = ", ".join(c.get("domain", "?") for c in ms_tokens)
            self._status(
                f"[tiktok] msToken muncul {len(ms_tokens)}x (domains: {domains}). "
                f"SocialPulse parser akan pilih yang '.tiktok.com'."
            )
