"""
Base class untuk capture login session per platform.

Setiap platform (FB/TikTok/IG/Twitter) inherit dan override:
  - LOGIN_URL: halaman pertama yang dibuka (biasanya /login)
  - PLATFORM: nama platform (lowercase)
  - is_logged_in(page): cek heuristik apakah user sudah login

Workflow standar:
  1. Launch Chromium HEADED dengan anti-bot args + persistent profile
  2. Inject stealth init script (patch navigator.webdriver, plugins, dll)
  3. Goto LOGIN_URL
  4. User login manual (mungkin lewat 2FA / captcha / SMS)
  5. Polling is_logged_in() setiap N detik, ATAU user klik "Saya sudah login"
  6. Save storage_state ke sessions/{platform}_{label}.json
  7. Close browser

v2 (8 Mei 2026) — STEALTH MODE
================================
Sebelumnya base.py pakai launch_temp browser context yang gampang ke-detect
sama IG/TikTok/Twitter via 5 signal:
  1. navigator.webdriver === true     → patched ke false
  2. navigator.plugins kosong          → fake 3 plugins (PDF viewer, dll)
  3. navigator.languages aneh          → set ['id-ID', 'id', 'en-US', 'en']
  4. WebGL vendor "Google SwiftShader" → spoof "Intel Inc." / "ANGLE"
  5. window.chrome incomplete          → tambah runtime/loadTimes stub
  6. Permissions API quirk             → patch denied/default mismatch

Plus: pakai launch_persistent_context dengan profile dir per-label, supaya:
  - Cookies/cache survive antar session capture (akun gak "fresh device" tiap kali)
  - Extension storage / IndexedDB persist → fingerprint lebih konsisten
  - Profile path = sessions/.profiles/<platform>_<label>/ (terpisah dari output JSON)

Trade-off:
  - Profile dir butuh ruang ~50-200MB per akun (browser cache).
  - Kalau akun banyak, total bisa beberapa GB. Ada flag CLEAR_PROFILE_ON_SAVE
    untuk auto-cleanup setelah session berhasil di-save (default: keep).

Format output: Playwright storage_state JSON, sama persis dengan fb_session.json
hasil fb_capture.py. Schema:
  {
    "cookies": [{name, value, domain, path, expires, httpOnly, secure, sameSite}, ...],
    "origins": [{origin, localStorage: [{name, value}, ...]}]
  }

Kenapa storage_state: SocialPulse Facebook scraper v2.4 sudah pakai shape ini
dan inject lengkap ke context (cookies + localStorage). TikTok / IG / Twitter
account managers (v3) juga punya parser yang accept JSON dict.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from playwright.sync_api import sync_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Args yang nutup signal otomasi paling jelas. Kombinasi dengan stealth
# init script (lihat _STEALTH_INIT_JS) menutup ~95% dari signal yang
# dipakai IG/TikTok/Twitter untuk deteksi headless/automated browser.
CHROME_ARGS = [
    # Standar anti-detect (hide AutomationControlled flag di DevTools)
    "--disable-blink-features=AutomationControlled",
    # Stability di Linux Docker / low-RAM
    "--disable-dev-shm-usage",
    # UX cleanup (jangan munculin "Chrome is being controlled" bar)
    "--no-default-browser-check",
    "--no-first-run",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    # Disable automation-related features
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
    # Better fingerprint: enable WebGL real driver (jangan SwiftShader)
    "--enable-webgl",
    "--use-gl=angle",
    # Extra stability
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
]

# Init script yang di-inject SEBELUM page navigation. Patch semua property
# JavaScript yang kebanyakan anti-bot tools (FingerprintJS, Imperva, dll)
# pakai untuk fingerprint.
#
# Sumber referensi: puppeteer-extra-plugin-stealth + playwright-stealth.
# Disalin manual karena (a) ngga mau add dependency tambahan, (b) cukup
# untuk login flow (bukan untuk scraping volume tinggi yang butuh full
# fingerprint randomization).
_STEALTH_INIT_JS = r"""
(() => {
  // ==== 1. navigator.webdriver — anti-bot signal #1 ====
  // Patch via Object.defineProperty supaya gak bisa di-override balik oleh JS lain.
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false,
      configurable: true,
    });
  } catch (e) {}

  // ==== 2. navigator.plugins — Chrome real punya minimal PDF Viewer ====
  // Build minimal PluginArray + 2-3 plugin yang umum di Chrome desktop.
  try {
    const makeMime = (type, suffixes, description) => ({
      type, suffixes, description, enabledPlugin: null,
    });
    const makePlugin = (name, filename, description, mimeTypes) => {
      const plugin = {
        name, filename, description, length: mimeTypes.length,
      };
      mimeTypes.forEach((mt, i) => {
        plugin[i] = mt;
        plugin[mt.type] = mt;
        mt.enabledPlugin = plugin;
      });
      return plugin;
    };
    const pdf = makeMime('application/pdf', 'pdf', 'Portable Document Format');
    const pdfViewer = makePlugin(
      'PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdf]
    );
    const chromePdf = makePlugin(
      'Chrome PDF Viewer', 'internal-pdf-viewer',
      'Portable Document Format', [pdf]
    );
    const chromiumPdf = makePlugin(
      'Chromium PDF Viewer', 'internal-pdf-viewer',
      'Portable Document Format', [pdf]
    );
    const arr = [pdfViewer, chromePdf, chromiumPdf];
    arr.item = (i) => arr[i] || null;
    arr.namedItem = (n) => arr.find(p => p.name === n) || null;
    arr.refresh = () => {};
    Object.setPrototypeOf(arr, PluginArray.prototype);
    Object.defineProperty(Navigator.prototype, 'plugins', {
      get: () => arr,
      configurable: true,
    });
  } catch (e) {}

  // ==== 3. navigator.languages — sesuaikan dengan locale context ====
  // Playwright set locale di context level, tapi navigator.languages kadang
  // gak ngikut. Force agar konsisten.
  try {
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: () => ['id-ID', 'id', 'en-US', 'en'],
      configurable: true,
    });
  } catch (e) {}

  // ==== 4. WebGL vendor/renderer spoof ====
  // Chromium headless / low-end Docker container default ke
  // "Google Inc. (Google)" + "Google SwiftShader". Real desktop punya
  // vendor "Google Inc. (Intel)" / "Google Inc. (NVIDIA)" + renderer
  // "ANGLE (Intel, ...)" / "ANGLE (NVIDIA, ...)".
  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
      // UNMASKED_VENDOR_WEBGL = 37445
      if (parameter === 37445) return 'Google Inc. (Intel)';
      // UNMASKED_RENDERER_WEBGL = 37446
      if (parameter === 37446) {
        return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      }
      return getParameter.apply(this, arguments);
    };
    // Same untuk WebGL2
    if (typeof WebGL2RenderingContext !== 'undefined') {
      const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Google Inc. (Intel)';
        if (parameter === 37446) {
          return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        }
        return getParameter2.apply(this, arguments);
      };
    }
  } catch (e) {}

  // ==== 5. window.chrome stub ====
  // Real Chrome punya window.chrome.runtime, .loadTimes, .csi, .app
  // Playwright Chromium default kosong / partial.
  try {
    if (!window.chrome) {
      window.chrome = {};
    }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        // Minimal — anti-bot biasa cuma cek 'runtime' exist
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', UPDATE: 'update' },
        PlatformOs: { WIN: 'win', MAC: 'mac', LINUX: 'linux' },
        connect: () => ({ onMessage: { addListener: () => {} }, postMessage: () => {} }),
        sendMessage: () => {},
      };
    }
    if (!window.chrome.loadTimes) {
      window.chrome.loadTimes = () => ({
        commitLoadTime: Date.now() / 1000,
        connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() / 1000,
        finishLoadTime: Date.now() / 1000,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: Date.now() / 1000,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000 - 1,
        startLoadTime: Date.now() / 1000 - 1,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
      });
    }
    if (!window.chrome.csi) {
      window.chrome.csi = () => ({
        onloadT: Date.now(), pageT: Date.now() - 1, startE: Date.now() - 2, tran: 15,
      });
    }
  } catch (e) {}

  // ==== 6. Permissions API quirk ====
  // Real Chrome: notifications denied → Notification.permission === 'denied'
  // Headless Chrome: notifications denied → Notification.permission === 'default'
  // Anti-bot detect mismatch ini.
  try {
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission, onchange: null })
          : origQuery(parameters)
      );
    }
  } catch (e) {}

  // ==== 7. Hide playwright-specific globals ====
  // Playwright inject __playwright__ atau __pwInitScripts__ di window kadang.
  // Kalau ada, hapus.
  try {
    delete window.__playwright__;
    delete window.__pwInitScripts__;
    delete window.__PLAYWRIGHT_GUID__;
  } catch (e) {}
})();
"""


@dataclass
class CaptureResult:
    """Hasil capture, dipakai GUI untuk display & push ke SocialPulse."""
    success: bool
    platform: str
    label: str
    session_path: Optional[Path] = None
    cookie_count: int = 0
    has_required_cookies: bool = False
    missing_cookies: list = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "platform": self.platform,
            "label": self.label,
            "session_path": str(self.session_path) if self.session_path else None,
            "cookie_count": self.cookie_count,
            "has_required_cookies": self.has_required_cookies,
            "missing_cookies": self.missing_cookies,
            "error": self.error,
        }


class LoginCapture:
    """Base — override constants & is_logged_in() per platform."""

    PLATFORM: str = "override-me"
    LOGIN_URL: str = ""
    POST_LOGIN_HINT: str = ""  # kalimat yang ditampilkan ke user setelah browser kebuka
    REQUIRED_COOKIES: tuple = ()  # cookies yang wajib ada supaya scraping jalan
    LOCALE: str = "id-ID"
    TIMEZONE: str = "Asia/Jakarta"
    VIEWPORT: dict = None  # default akan di-set di __init__

    # Kalau True, pakai launch_persistent_context dengan profile dir per-label.
    # Profile persist antar capture session (cookies/cache/IndexedDB), bikin
    # akun terasa lebih "real" ke anti-bot. Trade-off: ruang disk.
    USE_PERSISTENT_PROFILE: bool = True

    # Hapus profile dir setelah session berhasil di-save? Default: keep (False),
    # supaya next capture untuk akun yang sama bisa resume tanpa fresh device.
    CLEAR_PROFILE_AFTER_SAVE: bool = False

    def __init__(self, label: str, sessions_dir: Path,
                 status_callback: Optional[Callable[[str], None]] = None):
        self.label = label.strip() or "default"
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._status = status_callback or (lambda msg: logger.info(msg))
        if self.VIEWPORT is None:
            self.VIEWPORT = {"width": 1366, "height": 900}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def session_path(self) -> Path:
        return self.sessions_dir / f"{self.PLATFORM}_{self._safe_label()}.json"

    def profile_dir(self) -> Path:
        """Persistent profile dir per-label. Lokasi: sessions/.profiles/<plat>_<label>/"""
        return self.sessions_dir / ".profiles" / f"{self.PLATFORM}_{self._safe_label()}"

    def existing_session_path(self) -> Optional[Path]:
        p = self.session_path()
        return p if p.exists() else None

    def run(self, finished_event=None) -> CaptureResult:
        """
        Jalankan flow capture. Synchronous — caller harus jalanin di thread
        terpisah supaya GUI tidak freeze.

        finished_event: optional threading.Event() yang di-set saat user
        klik tombol "Saya Sudah Login" di GUI. Kalau None, fallback ke
        polling is_logged_in() dengan timeout 5 menit.
        """
        if self.USE_PERSISTENT_PROFILE:
            return self._run_persistent(finished_event)
        return self._run_ephemeral(finished_event)

    def _run_persistent(self, finished_event=None) -> CaptureResult:
        """
        Pakai launch_persistent_context — Chromium dibuka dengan profile dir
        yang persist di disk. Cookies, IndexedDB, localStorage, cache,
        semua tetap ada antar run.

        Bonus: anti-bot sees a "real returning user" dengan history browser
        yang konsisten, bukan fresh ephemeral session yang screams "automation".
        """
        out_path = self.session_path()
        profile = self.profile_dir()
        profile.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as pw:
            self._status(
                f"[{self.PLATFORM}] Launching Chromium (persistent profile: "
                f"{profile.name}/)"
            )
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=False,
                    args=CHROME_ARGS,
                    user_agent=DESKTOP_UA,
                    viewport=self.VIEWPORT,
                    locale=self.LOCALE,
                    timezone_id=self.TIMEZONE,
                    # Disable automation flag yang disembunyikan via context option
                    ignore_default_args=["--enable-automation"],
                )
            except Exception as e:
                # Fallback ke ephemeral kalau persistent gagal (mis. profile lock)
                self._status(
                    f"[{self.PLATFORM}] Persistent context failed ({e}), "
                    f"fallback ke ephemeral mode"
                )
                return self._run_ephemeral(finished_event)

            # Inject stealth init script — apply ke semua page (existing + future).
            try:
                context.add_init_script(_STEALTH_INIT_JS)
            except Exception as e:
                self._status(f"[{self.PLATFORM}] Stealth inject warning: {e}")

            # Persistent context biasa udah punya 1 default page. Re-use.
            pages = context.pages
            page = pages[0] if pages else context.new_page()

            try:
                return self._do_capture(context, page, out_path, finished_event)
            finally:
                try:
                    context.close()
                except Exception:
                    pass

                if self.CLEAR_PROFILE_AFTER_SAVE:
                    try:
                        shutil.rmtree(profile, ignore_errors=True)
                    except Exception:
                        pass

    def _run_ephemeral(self, finished_event=None) -> CaptureResult:
        """
        Original flow: ephemeral browser context per run. Pakai sebagai fallback
        kalau persistent context gagal launch (mis. profile lock dari prev run
        yang gak shutdown bersih).
        """
        out_path = self.session_path()
        # Resume session existing kalau ada — supaya user bisa "refresh" tanpa
        # login ulang dari nol (cookies expired tinggal di-refresh).
        storage_state = str(out_path) if out_path.exists() else None

        with sync_playwright() as pw:
            self._status(f"[{self.PLATFORM}] Launching Chromium (ephemeral)...")
            browser = pw.chromium.launch(
                headless=False,
                args=CHROME_ARGS,
                ignore_default_args=["--enable-automation"],
            )

            context = browser.new_context(
                user_agent=DESKTOP_UA,
                viewport=self.VIEWPORT,
                locale=self.LOCALE,
                timezone_id=self.TIMEZONE,
                storage_state=storage_state,
            )

            try:
                context.add_init_script(_STEALTH_INIT_JS)
            except Exception as e:
                self._status(f"[{self.PLATFORM}] Stealth inject warning: {e}")

            page = context.new_page()

            try:
                return self._do_capture(context, page, out_path, finished_event)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    def _do_capture(self, context, page, out_path, finished_event) -> CaptureResult:
        """Common capture flow — dipakai by both persistent & ephemeral mode."""
        try:
            self._status(f"[{self.PLATFORM}] Opening {self.LOGIN_URL}")
            try:
                page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                # Navigation timeout sering kejadian di TikTok karena heavy SPA.
                # Lanjut anyway — page kemungkinan sudah cukup loaded.
                self._status(f"[{self.PLATFORM}] Navigation soft-error: {e} (continuing)")

            # Hint ke user
            if self.POST_LOGIN_HINT:
                self._status(f"[{self.PLATFORM}] {self.POST_LOGIN_HINT}")

            # Wait loop: user login manual, kita poll is_logged_in
            self._wait_for_login(page, finished_event)

            # Capture context.storage_state() dan validate sebelum overwrite file.
            state = context.storage_state()
            cookie_names = [c.get("name", "") for c in (state.get("cookies") or [])]
            missing = [c for c in self.REQUIRED_COOKIES if c not in cookie_names]

            # Edge case: user trigger Playwright capture, lalu push session
            # via Chrome extension (yang lebih reliable). Extension udah save
            # ke out_path duluan, lalu set finished_event untuk wake up loop ini.
            # Di titik ini, Chromium-nya BELUM login (cookies wajib gak ada).
            # JANGAN overwrite out_path — extension session jauh lebih lengkap.
            if missing and out_path.exists():
                # Cek file existing punya required cookies — kalau ya, itu
                # pasti dari extension push, jangan timpa.
                try:
                    existing = json.loads(out_path.read_text(encoding="utf-8"))
                    existing_names = {
                        c.get("name") for c in (existing.get("cookies") or [])
                        if isinstance(c, dict) and c.get("name") and c.get("value")
                    }
                    existing_missing = [c for c in self.REQUIRED_COOKIES if c not in existing_names]
                    if not existing_missing:
                        # Existing file VALID — pakai itu, jangan overwrite.
                        self._status(
                            f"[{self.PLATFORM}] Chromium belum login (missing: "
                            f"{', '.join(missing)}), tapi file existing valid. "
                            f"Pakai existing — skip overwrite."
                        )
                        return CaptureResult(
                            success=True,
                            platform=self.PLATFORM,
                            label=self.label,
                            session_path=out_path,
                            cookie_count=len(existing.get("cookies") or []),
                            has_required_cookies=True,
                            missing_cookies=[],
                        )
                except Exception:
                    pass  # file rusak — lanjut dengan flow normal (akan overwrite)

            # Save storage state (normal path)
            self._status(f"[{self.PLATFORM}] Saving session...")
            self._post_process_state(state)  # platform-specific fixups

            out_path.write_text(
                json.dumps(state, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            result = CaptureResult(
                success=True,
                platform=self.PLATFORM,
                label=self.label,
                session_path=out_path,
                cookie_count=len(cookie_names),
                has_required_cookies=not missing,
                missing_cookies=missing,
            )

            if missing:
                self._status(
                    f"[{self.PLATFORM}] WARNING: cookies kurang: "
                    f"{', '.join(missing)}. Mungkin belum login penuh?"
                )
            else:
                self._status(
                    f"[{self.PLATFORM}] OK — saved {len(cookie_names)} cookies "
                    f"({len(self.REQUIRED_COOKIES)}/{len(self.REQUIRED_COOKIES)} required)"
                )

            return result

        except Exception as e:
            logger.exception(f"[{self.PLATFORM}] Capture failed")
            return CaptureResult(
                success=False,
                platform=self.PLATFORM,
                label=self.label,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Hooks (override per platform)
    # ------------------------------------------------------------------
    def is_logged_in(self, page: Page) -> bool:
        """
        Heuristik default: cek apakah cookie required sudah ada.
        Override kalau platform punya signal yang lebih cepat (mis. URL match,
        DOM element specific).
        """
        try:
            cookies = page.context.cookies()
            names = {c["name"] for c in cookies}
            return all(c in names for c in self.REQUIRED_COOKIES) if self.REQUIRED_COOKIES else False
        except Exception:
            return False

    def _post_process_state(self, state: dict) -> None:
        """Hook untuk filter/cleanup state sebelum di-save. Default: no-op."""
        pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _wait_for_login(self, page: Page, finished_event=None,
                        max_seconds: int = 600, poll_interval: float = 2.0):
        """
        Tunggu sampai SALAH SATU dari:
          - is_logged_in(page) returns True (auto-detect), ATAU
          - finished_event.is_set() (user klik tombol "Saya Sudah Login"), ATAU
          - timeout (default 10 menit)

        Kombinasi keduanya penting:
          - Auto-detect cepat untuk happy path (FB redirect ke /home, TikTok
            cookie sessionid muncul).
          - Manual button buat fallback kalau heuristik salah / akun butuh
            verifikasi tambahan yang gak meninggalkan jejak cookie standard.
        """
        import time
        deadline = time.time() + max_seconds
        last_status_print = 0

        while time.time() < deadline:
            # Manual override dari GUI
            if finished_event is not None and finished_event.is_set():
                self._status(f"[{self.PLATFORM}] User confirmed login manually.")
                return

            # Browser closed by user
            try:
                if page.is_closed():
                    raise RuntimeError("Browser ditutup sebelum login confirmed.")
            except Exception:
                pass

            # Auto-detect
            try:
                if self.is_logged_in(page):
                    self._status(f"[{self.PLATFORM}] Auto-detected: logged in.")
                    # Beri page 2 detik tambahan untuk settle (set additional cookies)
                    try:
                        page.wait_for_timeout(2_000)
                    except Exception:
                        pass
                    return
            except Exception as e:
                # Silent — auto-detect failure not fatal
                logger.debug(f"[{self.PLATFORM}] is_logged_in error: {e}")

            # Print waiting status setiap 15 detik
            now = time.time()
            if now - last_status_print > 15:
                remain = int(deadline - now)
                self._status(f"[{self.PLATFORM}] Waiting login... ({remain}s remaining)")
                last_status_print = now

            try:
                page.wait_for_timeout(int(poll_interval * 1000))
            except Exception:
                time.sleep(poll_interval)

        raise TimeoutError(
            f"Timeout {max_seconds}s — login belum terdeteksi. "
            f"Klik 'Saya Sudah Login' di GUI kalau actually sudah."
        )

    def _safe_label(self) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in self.label)[:40]
