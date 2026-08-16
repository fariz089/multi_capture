"""
multi_capture.py — GUI sederhana untuk capture login session multi-platform
=============================================================================

Tool standalone yang launch Chromium real (Playwright headed), user login
manual, lalu cookies + localStorage di-save sebagai Playwright storage_state
JSON. File hasil bisa di-paste ke SocialPulse via UI Akun Scraper, ATAU
auto-push lewat tombol "Push to SocialPulse" di tool ini.

Platform yang didukung (semua cookie-based per v3, 7 May 2026):
  - Facebook   (cookie-based ✅)
  - TikTok     (cookie-based ✅)
  - Instagram  (cookie-based ✅ — instagrapi dropped)
  - Twitter/X  (cookie-based ✅ — twikit dropped)
  - Threads    (cookie-based ✅ — v3.1 13 May 2026, share auth dengan IG)

Tidak include:
  - YouTube — pakai 'anonymous' di SocialPulse (sudah built-in)
  - News    — gak butuh login (semua publik)

Cara pakai:
  pip install playwright requests
  playwright install chromium
  python multi_capture.py

GUI flow:
  1. Pilih platform di tab atas
  2. Input label akun (bebas, buat identifier)
  3. Klik "Buka Browser & Login"
  4. Login manual di Chromium yang terbuka
  5. Klik "Saya Sudah Login" di GUI → cookies di-save
  6. (Optional) Klik "Push to SocialPulse" untuk auto-add ke backend
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

from platforms import PLATFORMS, CaptureResult
from push_to_socialpulse import (
    push_to_socialpulse, login_socialpulse, validate_socialpulse_token, PUSHABLE,
)

# Extension server — optional. Kalau Flask gak ke-install, mode degraded
# (Playwright capture tetap jalan, tombol extension status nunjukkin disabled).
try:
    from extension_server import run_server_in_thread as run_extension_server
    EXTENSION_SERVER_IMPORT_ERROR = None
except Exception as _ext_err:
    run_extension_server = None
    EXTENSION_SERVER_IMPORT_ERROR = str(_ext_err)

EXTENSION_SERVER_PORT = 5099  # default — match dengan popup.js default

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
CONFIG_PATH = BASE_DIR / "multi_capture_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# Config persistence (remember SocialPulse URL, username & cached JWT)
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "socialpulse_url": "http://localhost:3001",
        "socialpulse_username": "admin",
        # JWT boleh di-cache supaya restart app tidak perlu login lagi selama
        # token masih valid. Password TIDAK PERNAH disimpan ke config.
        "socialpulse_jwt": "",
        "socialpulse_user": None,
        "last_labels": {},  # platform -> last label used
    }


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logging.warning(f"Save config failed: {e}")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class MultiCaptureApp:
    PLATFORM_DISPLAY = {
        "facebook":  "Facebook",
        "tiktok":    "TikTok",
        "instagram": "Instagram",
        "twitter":   "Twitter / X",
        "threads":   "Threads",
    }
    PLATFORM_NOTES = {
        "facebook":  "Cookie-based ✅  Push ke SocialPulse → langsung scraping.",
        "tiktok":    "Cookie-based ✅  Push ke SocialPulse → langsung scraping.",
        "instagram": "Cookie-based ✅  Push ke SocialPulse → langsung scraping. (v3 — instagrapi dropped)",
        "twitter":   "Cookie-based ✅  Push ke SocialPulse → langsung scraping. (v3 — twikit dropped)",
        "threads":   "Cookie-based ✅  Share auth dengan Instagram (sessionid + ds_user_id sama). Login Threads = login IG. Push → langsung scraping.",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.current_capture_thread = None
        self.current_finished_event = None
        self.current_result: CaptureResult | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self._ext_server_status = "disabled"  # "running" / "disabled" / "error"

        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Spawn extension server di daemon thread SEBELUM build UI, supaya
        # status-nya bisa langsung di-display.
        self._start_extension_server()

        self._build_ui()
        self._poll_log_queue()
        self._poll_session_files()  # auto-refresh badge tiap 2s

    def _poll_session_files(self):
        """
        Periodic refresh badge session di semua tab platform. Dipanggil
        tiap 2 detik, biar:
          - Session yang ke-push via extension Chrome auto-update badge
            tanpa user perlu klik apa-apa
          - Kalau user edit / hapus file session manual di disk, GUI
            ke-sync.

        Read disk berkali-kali murah (Tk app idle, file kecil).
        """
        try:
            for platform_key, widgets in self.platform_widgets.items():
                self._refresh_session_info(platform_key, widgets)
        except Exception:
            # Defensif — kalau ada widget yg di-destroy mid-cycle
            pass
        finally:
            self.root.after(2000, self._poll_session_files)

    def _start_extension_server(self):
        """
        Spawn local Flask server (port 5099 default) untuk nerima POST dari
        Chrome extension. Daemon thread — auto-shutdown saat GUI keluar.

        Kalau Flask gak ke-install atau port udah dipakai, server gak jalan
        tapi GUI tetap usable (Playwright fallback masih bisa). Status
        ke-display di status bar bawah GUI.
        """
        if run_extension_server is None:
            self._ext_server_status = "error"
            self._ext_server_error = (
                f"Flask gak ke-install ({EXTENSION_SERVER_IMPORT_ERROR}). "
                f"Install: pip install flask"
            )
            return

        try:
            run_extension_server(
                sessions_dir=SESSIONS_DIR,
                port=EXTENSION_SERVER_PORT,
                host="127.0.0.1",
                on_session_saved=self._on_extension_session_saved,
            )
            self._ext_server_status = "running"
            self._ext_server_error = None
        except OSError as e:
            # Port udah dipakai
            self._ext_server_status = "error"
            self._ext_server_error = (
                f"Port {EXTENSION_SERVER_PORT} sudah dipakai aplikasi lain "
                f"({e}). Tutup aplikasi yang pakai port itu, atau ubah "
                f"EXTENSION_SERVER_PORT di multi_capture.py."
            )
        except Exception as e:
            self._ext_server_status = "error"
            self._ext_server_error = f"Server gagal start: {e}"

    def _on_extension_session_saved(self, platform: str, label: str, path: Path):
        """
        Callback dipanggil dari Flask thread saat session masuk dari extension.
        WAJIB marshal ke main thread via root.after — Tkinter NOT thread-safe.

        Side-effects:
          1. Update GUI tab platform — switch + isi label + refresh badge.
          2. Kalau ada Playwright capture session aktif (user buka Chromium
             dulu lalu inget pakai extension), auto-finish loop-nya supaya
             Chromium tidak loop "Waiting login..." selamanya. Session dari
             extension lebih trustworthy karena pakai Chrome real.
        """
        # Build closure yang capture data, dispatch ke main thread
        def _refresh():
            self._enqueue_log(
                f"[ext] ✓ Captured {platform}/{label} from Chrome extension → {path.name}"
            )

            # Kalau ada Playwright capture lagi jalan — auto-finish-in.
            # Background: user kadang klik "Buka Browser & Login" dulu, lalu
            # inget pakai extension yang lebih reliable. Tanpa fix ini,
            # Chromium loop di base.py akan terus poll is_logged_in() di
            # context Playwright (yang BELUM login) sambil ke-block tombol
            # "Buka Browser & Login" GUI. Set finished_event = unblock.
            if (self.current_capture_thread
                and self.current_capture_thread.is_alive()
                and self.current_finished_event is not None
                and not self.current_finished_event.is_set()):
                self._enqueue_log(
                    f"[ext] Auto-stopping Playwright capture — extension session "
                    f"sudah dapat. Browser akan close otomatis."
                )
                self.current_finished_event.set()
                # Note: kita TIDAK overwrite session file dengan hasil Playwright
                # karena extension session lebih lengkap. Behavior ini di-handle
                # via _on_capture_done yang panggil _refresh_session_info — yang
                # akan re-read file dari disk (= file dari extension).

            # Update label input GUI tab platform itu, lalu refresh info
            widgets = self.platform_widgets.get(platform)
            if widgets:
                widgets["label_var"].set(label)
                self._refresh_session_info(platform, widgets)
                # Switch tab biar user lihat
                try:
                    plat_idx = list(self.PLATFORM_DISPLAY.keys()).index(platform)
                    self.notebook.select(plat_idx)
                except (ValueError, tk.TclError):
                    pass

        try:
            self.root.after(0, _refresh)
        except Exception:
            # GUI mungkin udah di-destroy
            pass

    # ---------------- UI construction ----------------
    def _build_ui(self):
        self.root.title("Multi-Capture — Login Session Capturer")
        self.root.geometry("780x780")
        self.root.minsize(700, 700)

        # Style
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # ==== Top: SocialPulse auth/config ====
        cfg_frame = ttk.LabelFrame(self.root, text="SocialPulse Backend (login untuk auto-push)", padding=8)
        cfg_frame.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(cfg_frame, text="API URL:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.url_var = tk.StringVar(value=self.cfg.get("socialpulse_url", "http://localhost:3001"))
        ttk.Entry(cfg_frame, textvariable=self.url_var, width=42).grid(row=0, column=1, sticky="we")

        ttk.Label(cfg_frame, text="Username / Email:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(4, 0))
        self.sp_username_var = tk.StringVar(value=self.cfg.get("socialpulse_username", "admin"))
        ttk.Entry(cfg_frame, textvariable=self.sp_username_var, width=42).grid(row=1, column=1, sticky="we", pady=(4, 0))

        ttk.Label(cfg_frame, text="Password:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(4, 0))
        self.sp_password_var = tk.StringVar(value="")
        self.sp_password_entry = ttk.Entry(cfg_frame, textvariable=self.sp_password_var, width=42, show="•")
        self.sp_password_entry.grid(row=2, column=1, sticky="we", pady=(4, 0))
        self.sp_password_entry.bind("<Return>", lambda _e: self._login_socialpulse())

        auth_buttons = ttk.Frame(cfg_frame)
        auth_buttons.grid(row=0, column=2, rowspan=3, sticky="ns", padx=(8, 0))
        self.sp_login_btn = ttk.Button(auth_buttons, text="Login to SocialPulse", command=self._login_socialpulse)
        self.sp_login_btn.pack(fill="x")
        ttk.Button(auth_buttons, text="Logout", command=self._logout_socialpulse).pack(fill="x", pady=(4, 0))
        ttk.Button(auth_buttons, text="Save URL/User", command=self._save_config_now).pack(fill="x", pady=(4, 0))

        self.sp_auth_status_var = tk.StringVar(value="○ Not connected")
        self.sp_auth_status_label = tk.Label(
            cfg_frame, textvariable=self.sp_auth_status_var, anchor="w",
            foreground="#888", font=("Segoe UI", 9, "bold"),
        )
        self.sp_auth_status_label.grid(row=3, column=0, columnspan=3, sticky="we", pady=(7, 0))
        ttk.Label(
            cfg_frame,
            text="Password hanya dipakai saat login dan tidak disimpan ke multi_capture_config.json.",
            foreground="#888", font=("Segoe UI", 8, "italic"),
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        cfg_frame.columnconfigure(1, weight=1)

        # Kalau ada JWT dari run sebelumnya, validasi ke /api/auth/me.
        self.root.after(250, self._validate_cached_socialpulse_auth)

        # ==== Extension server status banner ====
        ext_frame = ttk.LabelFrame(self.root, text="Chrome Extension Bridge (recommended)", padding=8)
        ext_frame.pack(fill="x", padx=10, pady=4)

        if self._ext_server_status == "running":
            status_text = (
                f"✓ Extension server jalan di http://127.0.0.1:{EXTENSION_SERVER_PORT}/extension/push  "
                f"— pakai Chrome extension (lihat README.md) untuk capture pakai Chrome real (anti-bot bypass)."
            )
            status_color = "#52c41a"
        elif self._ext_server_status == "error":
            status_text = (
                f"⚠ Extension server gagal start: {self._ext_server_error}\n"
                f"GUI tetap bisa dipakai dengan Playwright capture (tab di bawah). "
                f"Tapi extension Chrome gak bisa di-pakai sampai server jalan."
            )
            status_color = "#fadb14"
        else:
            status_text = "ℹ Extension server disabled."
            status_color = "#888"

        ttk.Label(
            ext_frame, text=status_text, foreground=status_color, wraplength=720,
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            ext_frame,
            text=(
                "Cara pakai: install extension dari folder chrome_extension/ "
                "(chrome://extensions → Developer Mode → Load unpacked) → login "
                "ke platform target di Chrome biasa → klik icon extension → "
                "klik tombol platform. Session masuk otomatis ke tab platform "
                "di bawah, klik 'Push to SocialPulse' untuk submit."
            ),
            foreground="#888", wraplength=720, justify="left",
            font=("Segoe UI", 8, "italic"),
        ).pack(anchor="w", pady=(4, 0))

        # ==== Middle: Platform tabs ====
        notebook_frame = ttk.LabelFrame(self.root, text="Capture Session (Playwright fallback / extension landing)", padding=8)
        notebook_frame.pack(fill="both", expand=False, padx=10, pady=4)

        self.notebook = ttk.Notebook(notebook_frame)
        self.notebook.pack(fill="both", expand=True)

        self.platform_widgets: dict[str, dict] = {}
        for platform_key, display_name in self.PLATFORM_DISPLAY.items():
            tab = ttk.Frame(self.notebook, padding=10)
            self.notebook.add(tab, text=display_name)
            self.platform_widgets[platform_key] = self._build_platform_tab(tab, platform_key)

        # ==== Bottom: Log ====
        log_frame = ttk.LabelFrame(self.root, text="Status / Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        log_inner = ttk.Frame(log_frame)
        log_inner.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_inner, height=12, wrap="word",
            background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="#fff", font=("Consolas", 9),
        )
        scrollbar = ttk.Scrollbar(log_inner, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # Tag styles
        self.log_text.tag_config("info", foreground="#d4d4d4")
        self.log_text.tag_config("success", foreground="#73d13d")
        self.log_text.tag_config("warn", foreground="#fadb14")
        self.log_text.tag_config("error", foreground="#ff7875")
        self.log_text.tag_config("muted", foreground="#888")

        self._log("Tool ready. Login ke SocialPulse di atas, lalu pilih platform → Capture → Push.", "muted")

    def _build_platform_tab(self, parent: ttk.Frame, platform_key: str) -> dict:
        """Build content per tab. Returns dict of widgets buat referensi."""
        # Note about platform
        note_text = self.PLATFORM_NOTES[platform_key]
        note_color = "#888"
        if platform_key in PUSHABLE:
            note_color = "#52c41a"
        ttk.Label(parent, text=note_text, foreground=note_color, wraplength=700).pack(anchor="w", pady=(0, 8))

        # Status badge — VISUAL INDICATOR PROMINENT (the answer untuk "mana indikator?")
        # Pakai tk.Label (bukan ttk) supaya bisa set background warna langsung.
        badge_frame = ttk.Frame(parent)
        badge_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(badge_frame, text="Status: ", font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 4))
        status_badge = tk.Label(
            badge_frame,
            text="✗ NO SESSION",
            foreground="#888",
            background="#2a2a2a",
            font=("Segoe UI", 10, "bold"),
            padx=10, pady=3,
        )
        status_badge.pack(side="left")

        # Label input
        label_frame = ttk.Frame(parent)
        label_frame.pack(fill="x", pady=4)
        ttk.Label(label_frame, text="Label akun (identifier bebas):").pack(side="left", padx=(0, 8))
        last_label = self.cfg.get("last_labels", {}).get(platform_key, "")
        label_var = tk.StringVar(value=last_label)
        label_entry = ttk.Entry(label_frame, textvariable=label_var, width=30)
        label_entry.pack(side="left", fill="x", expand=True)

        # Existing session info — verbose detail
        info_var = tk.StringVar(value="(belum ada session)")
        info_label = ttk.Label(parent, textvariable=info_var, foreground="#aaa",
                               font=("Segoe UI", 9, "italic"), wraplength=700, justify="left")
        info_label.pack(anchor="w", pady=(6, 4))

        # Buttons row
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill="x", pady=8)

        login_btn = ttk.Button(
            btn_frame, text="🌐 Buka Browser & Login",
            command=lambda: self._start_capture(platform_key),
        )
        login_btn.pack(side="left", padx=(0, 6))

        confirm_btn = ttk.Button(
            btn_frame, text="✓ Saya Sudah Login",
            command=self._confirm_login,
            state="disabled",
        )
        confirm_btn.pack(side="left", padx=6)

        push_btn = ttk.Button(
            btn_frame, text="⬆ Push to SocialPulse",
            command=lambda: self._push_session(platform_key),
            state="disabled",
        )
        push_btn.pack(side="left", padx=6)

        export_btn = ttk.Button(
            btn_frame, text="💾 Save As...",
            command=lambda: self._export_session(platform_key),
            state="disabled",
        )
        export_btn.pack(side="left", padx=6)

        widgets = {
            "label_var": label_var,
            "label_entry": label_entry,
            "info_var": info_var,
            "status_badge": status_badge,
            "login_btn": login_btn,
            "confirm_btn": confirm_btn,
            "push_btn": push_btn,
            "export_btn": export_btn,
        }
        # Refresh info awal
        self._refresh_session_info(platform_key, widgets)

        # Update info saat label berubah
        label_var.trace_add("write", lambda *_: self._refresh_session_info(platform_key, widgets))

        return widgets

    # ---------------- Session info / button enabling ----------------
    # Required cookies per platform — sama dengan extension_server.REQUIRED_COOKIES
    # & pc-scraper account managers. Kalau cookies wajib ada → status "VALID".
    REQUIRED_COOKIES_BY_PLATFORM = {
        "facebook":  ("c_user", "xs"),
        "instagram": ("sessionid", "ds_user_id"),
        "tiktok":    ("sessionid",),  # plus msToken — akan di-cek terpisah
        "twitter":   ("auth_token", "ct0"),
    }

    def _inspect_session_file(self, platform_key: str, path: Path) -> dict:
        """
        Read & validate session file. Returns dict:
          {
            "exists": bool,
            "valid": bool,           # required cookies semua ada
            "n_cookies": int,
            "n_localstorage": int,
            "cookie_names": set,
            "missing_required": list,   # required yg gak ada
            "found_required": list,
            "error": str | None,
          }
        """
        result = {
            "exists": path.exists(),
            "valid": False,
            "n_cookies": 0,
            "n_localstorage": 0,
            "cookie_names": set(),
            "missing_required": [],
            "found_required": [],
            "error": None,
        }
        if not result["exists"]:
            return result

        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            result["error"] = f"file rusak: {e}"
            return result

        cookies = state.get("cookies") or []
        origins = state.get("origins") or []
        names = set()
        for c in cookies:
            if isinstance(c, dict) and c.get("name") and c.get("value"):
                names.add(c["name"])

        result["n_cookies"] = len(cookies)
        result["n_localstorage"] = sum(
            len(o.get("localStorage") or []) for o in origins if isinstance(o, dict)
        )
        result["cookie_names"] = names

        required = self.REQUIRED_COOKIES_BY_PLATFORM.get(platform_key, ())
        # TikTok: msToken juga penting (alias ms_token). Kalau dua-duanya gak ada,
        # masuk ke missing-required list.
        for r in required:
            if r in names:
                result["found_required"].append(r)
            else:
                result["missing_required"].append(r)

        # TikTok extra check: msToken (atau ms_token alias)
        if platform_key == "tiktok":
            if "msToken" in names or "ms_token" in names:
                result["found_required"].append("msToken")
            else:
                result["missing_required"].append("msToken")

        result["valid"] = (len(result["missing_required"]) == 0)
        return result

    def _refresh_session_info(self, platform_key: str, widgets: dict):
        label = widgets["label_var"].get().strip() or "default"
        capture_cls = PLATFORMS[platform_key]
        cap = capture_cls(label, SESSIONS_DIR)
        path = cap.session_path()

        info = self._inspect_session_file(platform_key, path)

        # Set badge — visual indicator yang jelas
        badge = widgets["status_badge"]
        if not info["exists"]:
            badge.configure(text="✗ NO SESSION", foreground="#888", background="#2a2a2a")
            widgets["info_var"].set(
                "Belum ada session — pakai extension Chrome (recommended) atau "
                "klik 'Buka Browser & Login' untuk Playwright fallback."
            )
            push_state = "disabled"
            export_state = "disabled"

        elif info["error"]:
            badge.configure(text="✗ ERROR", foreground="#ff7875", background="#3a1f1f")
            widgets["info_var"].set(f"⚠ {info['error']} ({path.name})")
            push_state = "disabled"
            export_state = "normal"

        elif info["valid"]:
            badge.configure(text="✓ VALID — LOGIN OK", foreground="#73d13d", background="#1f3a1f")
            found_str = ", ".join(info["found_required"])
            widgets["info_var"].set(
                f"Session OK — {info['n_cookies']} cookies, "
                f"{info['n_localstorage']} localStorage items. "
                f"Wajib found: {found_str}. ({path.name})"
            )
            push_state = "normal" if platform_key in PUSHABLE else "disabled"
            export_state = "normal"

        else:
            # File ada tapi missing required cookies — login belum complete
            badge.configure(text="⚠ INCOMPLETE", foreground="#fadb14", background="#3a3a1f")
            missing_str = ", ".join(info["missing_required"])
            widgets["info_var"].set(
                f"⚠ Session ada tapi cookies wajib hilang: {missing_str}. "
                f"Login mungkin belum selesai (masih di halaman /login atau /challenge?). "
                f"({info['n_cookies']} cookies, {info['n_localstorage']} ls — {path.name})"
            )
            push_state = "disabled"  # jangan biarin user push session yg incomplete
            export_state = "normal"

        widgets["push_btn"].configure(state=push_state)
        widgets["export_btn"].configure(state=export_state)

    # ---------------- Capture flow ----------------
    def _start_capture(self, platform_key: str):
        if self.current_capture_thread and self.current_capture_thread.is_alive():
            messagebox.showwarning(
                "Sedang berjalan",
                "Ada capture lain yang masih aktif. Tutup browser atau klik 'Saya Sudah Login' dulu."
            )
            return

        widgets = self.platform_widgets[platform_key]
        label = widgets["label_var"].get().strip() or "default"

        # Save last label
        self.cfg.setdefault("last_labels", {})[platform_key] = label
        save_config(self.cfg)

        # Disable login button, enable confirm button
        widgets["login_btn"].configure(state="disabled")
        widgets["confirm_btn"].configure(state="normal")

        # Switch tab supaya jelas user lagi capture platform mana
        self.notebook.select(list(self.PLATFORM_DISPLAY.keys()).index(platform_key))

        capture_cls = PLATFORMS[platform_key]
        capture = capture_cls(
            label=label,
            sessions_dir=SESSIONS_DIR,
            status_callback=self._enqueue_log,
        )

        self.current_finished_event = threading.Event()
        self.current_capture_thread = threading.Thread(
            target=self._run_capture_thread,
            args=(capture, platform_key, widgets),
            daemon=True,
        )
        self.current_capture_thread.start()

        self._log(f"Starting capture untuk {platform_key} dengan label '{label}'", "info")

    def _run_capture_thread(self, capture, platform_key: str, widgets: dict):
        try:
            result = capture.run(finished_event=self.current_finished_event)
            self.current_result = result

            # Update UI di main thread
            self.root.after(0, self._on_capture_done, result, platform_key, widgets)
        except Exception as e:
            logging.exception("Capture thread crashed")
            self.root.after(0, self._on_capture_error, str(e), widgets)

    def _on_capture_done(self, result: CaptureResult, platform_key: str, widgets: dict):
        widgets["login_btn"].configure(state="normal")
        widgets["confirm_btn"].configure(state="disabled")

        if result.success:
            tag = "success" if result.has_required_cookies else "warn"
            msg = (
                f"✓ {result.platform} '{result.label}' saved → {result.session_path.name} "
                f"({result.cookie_count} cookies)"
            )
            if not result.has_required_cookies:
                msg += f" ⚠ MISSING: {', '.join(result.missing_cookies)}"
            self._log(msg, tag)
        else:
            self._log(f"✗ Capture gagal: {result.error}", "error")

        self._refresh_session_info(platform_key, widgets)

    def _on_capture_error(self, err: str, widgets: dict):
        widgets["login_btn"].configure(state="normal")
        widgets["confirm_btn"].configure(state="disabled")
        self._log(f"✗ Error: {err}", "error")

    def _confirm_login(self):
        if self.current_finished_event:
            self.current_finished_event.set()
            self._log("User confirmed login — saving session...", "info")
            # Disable confirm sementara save berjalan
            for widgets in self.platform_widgets.values():
                widgets["confirm_btn"].configure(state="disabled")

    # ---------------- Push & Export ----------------
    def _push_session(self, platform_key: str):
        widgets = self.platform_widgets[platform_key]
        label = widgets["label_var"].get().strip() or "default"
        capture_cls = PLATFORMS[platform_key]
        path = capture_cls(label, SESSIONS_DIR).session_path()

        if not path.exists():
            messagebox.showerror("Error", "Session belum ada. Login dulu.")
            return

        if platform_key not in PUSHABLE:
            messagebox.showinfo(
                "Info",
                f"Platform '{platform_key}' belum di-support untuk auto-push. "
                f"Gunakan tombol 'Save As...' untuk export, lalu paste manual "
                f"di UI Akun Scraper SocialPulse."
            )
            return

        # Pre-check: validate session content sebelum push (jangan kirim
        # session incomplete ke pc-scraper biar gak dapet error misleading)
        info = self._inspect_session_file(platform_key, path)
        if not info["valid"]:
            ok = messagebox.askyesno(
                "Session Incomplete",
                f"Session untuk {platform_key}/{label} kekurangan cookies wajib: "
                f"{', '.join(info['missing_required'])}.\n\n"
                f"Push tetap akan kemungkinan besar gagal di server. "
                f"Yakin lanjut?",
            )
            if not ok:
                return

        url = self.url_var.get().strip()
        username = self.sp_username_var.get().strip()
        password = self.sp_password_var.get()
        jwt = str(self.cfg.get("socialpulse_jwt") or "").strip()
        if not url:
            messagebox.showerror("Error", "API URL kosong.")
            return

        # Auto-stop Playwright capture kalau lagi jalan (sama dengan logic
        # di _on_extension_session_saved). Background: user buka Chromium
        # untuk login, tapi sebelum login selesai user inget session udah
        # ada dari run sebelumnya / extension. Klik Push langsung — kita
        # auto-close Chromium loop supaya gak stuck di "Waiting login..."
        if (self.current_capture_thread
            and self.current_capture_thread.is_alive()
            and self.current_finished_event is not None
            and not self.current_finished_event.is_set()):
            self._log(
                f"Auto-stopping Playwright capture — push session existing.",
                "info"
            )
            self.current_finished_event.set()

        # Save URL + username sebelum push. Password tidak pernah disimpan.
        self._save_config_now(silent=True)

        self._log(f"Pushing {platform_key}/{label} → {url} ...", "info")

        def worker():
            token = jwt
            auth_user = None

            # Kalau belum punya cached token tetapi credentials diisi, login
            # otomatis dulu. Jadi user tidak perlu klik Login secara eksplisit.
            if not token:
                if not username or not password:
                    res = {
                        "ok": False,
                        "status_code": 401,
                        "error": "Login SocialPulse diperlukan. Isi username/email + password lalu klik Login (atau langsung Push).",
                        "response_json": {"error": "SocialPulse login required"},
                        "auth_required": True,
                    }
                    self.root.after(0, self._on_push_result, res, platform_key, label)
                    return

                login_res = login_socialpulse(url, username, password)
                if not login_res.get("ok"):
                    login_res["auth_required"] = True
                    self.root.after(0, self._on_push_result, login_res, platform_key, label)
                    return
                token = login_res.get("token") or ""
                auth_user = login_res.get("user")

            res = push_to_socialpulse(
                session_path=path,
                platform=platform_key,
                label=label,
                api_url=url,
                jwt_token=token or None,
            )

            # Cached JWT mungkin expired/revoked. Kalau credentials sedang
            # tersedia, re-login dan retry tepat satu kali.
            if (
                not res.get("ok")
                and res.get("status_code") in (401, 403)
                and username
                and password
            ):
                self._enqueue_log("SocialPulse token expired/invalid — re-login otomatis lalu retry push...")
                login_res = login_socialpulse(url, username, password)
                if login_res.get("ok"):
                    token = login_res.get("token") or ""
                    auth_user = login_res.get("user")
                    res = push_to_socialpulse(
                        session_path=path,
                        platform=platform_key,
                        label=label,
                        api_url=url,
                        jwt_token=token or None,
                    )
                else:
                    res = login_res
                    res["auth_required"] = True

            if token and auth_user:
                res["_new_auth_token"] = token
                res["_new_auth_user"] = auth_user

            self.root.after(0, self._on_push_result, res, platform_key, label)

        threading.Thread(target=worker, daemon=True).start()

    def _on_push_result(self, res: dict, platform_key: str, label: str):
        # Login otomatis saat push bisa menghasilkan token baru. Cache token,
        # tapi jangan pernah cache password.
        new_token = res.pop("_new_auth_token", None)
        new_user = res.pop("_new_auth_user", None)
        if new_token:
            self._store_socialpulse_auth(new_token, new_user)

        if res.get("status_code") in (401, 403) and not res.get("ok"):
            self._clear_socialpulse_token(keep_username=True)
            self._set_socialpulse_auth_status(
                "✗ Session SocialPulse expired / login diperlukan", "error"
            )

        if res.get("ok"):
            self._log(
                f"✓ Pushed {platform_key}/{label} ke SocialPulse — "
                f"response: {res.get('response_json')}",
                "success",
            )
            messagebox.showinfo(
                "Push Sukses",
                f"{platform_key} '{label}' berhasil ditambahkan ke SocialPulse.\n\n"
                f"Cek di UI Akun Scraper untuk verify."
            )
        else:
            self._log(
                f"✗ Push gagal: {res.get('error')} (status: {res.get('status_code')})",
                "error",
            )
            messagebox.showerror(
                "Push Gagal",
                f"Error: {res.get('error')}\n\n"
                f"Status: {res.get('status_code')}\n"
                f"Response: {json.dumps(res.get('response_json'), indent=2)[:400]}"
            )

    def _export_session(self, platform_key: str):
        widgets = self.platform_widgets[platform_key]
        label = widgets["label_var"].get().strip() or "default"
        capture_cls = PLATFORMS[platform_key]
        path = capture_cls(label, SESSIONS_DIR).session_path()

        if not path.exists():
            messagebox.showerror("Error", "Session belum ada.")
            return

        dst = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialfile=path.name,
            title=f"Export {platform_key} session",
        )
        if not dst:
            return

        try:
            Path(dst).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            self._log(f"✓ Exported → {dst}", "success")
        except Exception as e:
            self._log(f"✗ Export gagal: {e}", "error")

    # ---------------- SocialPulse auth ----------------
    def _set_socialpulse_auth_status(self, text: str, kind: str = "muted"):
        self.sp_auth_status_var.set(text)
        colors = {
            "success": "#52c41a",
            "error": "#ff4d4f",
            "warn": "#d4a017",
            "muted": "#888",
            "info": "#1677ff",
        }
        try:
            self.sp_auth_status_label.configure(foreground=colors.get(kind, "#888"))
        except Exception:
            pass

    def _store_socialpulse_auth(self, token: str, user: dict | None):
        self.cfg["socialpulse_url"] = self.url_var.get().strip()
        self.cfg["socialpulse_username"] = self.sp_username_var.get().strip()
        self.cfg["socialpulse_jwt"] = token or ""
        self.cfg["socialpulse_user"] = user if isinstance(user, dict) else None
        save_config(self.cfg)

        if isinstance(user, dict):
            username = user.get("username") or user.get("email") or "user"
            role = user.get("role") or "user"
            self._set_socialpulse_auth_status(
                f"✓ Connected as {username} ({role})", "success"
            )
        else:
            self._set_socialpulse_auth_status("✓ Connected to SocialPulse", "success")

    def _clear_socialpulse_token(self, keep_username: bool = True):
        self.cfg["socialpulse_jwt"] = ""
        self.cfg["socialpulse_user"] = None
        if not keep_username:
            self.cfg["socialpulse_username"] = ""
            self.sp_username_var.set("")
        save_config(self.cfg)

    def _login_socialpulse(self):
        url = self.url_var.get().strip()
        username = self.sp_username_var.get().strip()
        password = self.sp_password_var.get()

        if not url:
            messagebox.showerror("Login SocialPulse", "API URL kosong.")
            return
        if not username or not password:
            messagebox.showerror("Login SocialPulse", "Isi username/email dan password.")
            return

        self._save_config_now(silent=True)
        self._set_socialpulse_auth_status("… Logging in to SocialPulse", "info")
        self.sp_login_btn.configure(state="disabled")
        self._log(f"Login SocialPulse as {username} → {url} ...", "info")

        def worker():
            res = login_socialpulse(url, username, password)
            self.root.after(0, self._on_socialpulse_login_result, res)

        threading.Thread(target=worker, daemon=True).start()

    def _on_socialpulse_login_result(self, res: dict):
        self.sp_login_btn.configure(state="normal")
        if res.get("ok"):
            self._store_socialpulse_auth(res.get("token") or "", res.get("user"))
            user = res.get("user") or {}
            who = user.get("username") or user.get("email") or self.sp_username_var.get().strip()
            self._log(f"✓ SocialPulse login OK: {who}", "success")
            # Password sengaja tidak disimpan. Biarkan field selama app hidup
            # supaya auto re-login bisa bekerja bila JWT expire di tengah sesi.
            return

        self._clear_socialpulse_token(keep_username=True)
        err = res.get("error") or "Login failed"
        self._set_socialpulse_auth_status(f"✗ Login failed: {err}", "error")
        self._log(f"✗ SocialPulse login gagal: {err}", "error")
        messagebox.showerror(
            "Login SocialPulse Gagal",
            f"Error: {err}\n\nStatus: {res.get('status_code')}",
        )

    def _validate_cached_socialpulse_auth(self):
        token = str(self.cfg.get("socialpulse_jwt") or "").strip()
        url = self.url_var.get().strip()
        if not token or not url:
            self._set_socialpulse_auth_status("○ Not connected — login diperlukan sebelum push", "muted")
            return

        self._set_socialpulse_auth_status("… Checking saved SocialPulse session", "info")

        def worker():
            res = validate_socialpulse_token(url, token)
            self.root.after(0, self._on_cached_auth_validation, res, token)

        threading.Thread(target=worker, daemon=True).start()

    def _on_cached_auth_validation(self, res: dict, token: str):
        if res.get("ok"):
            user = res.get("user") or {}
            # Sync username field ke server identity kalau ada.
            if user.get("username"):
                self.sp_username_var.set(user["username"])
            self._store_socialpulse_auth(token, user)
            self._log("✓ Saved SocialPulse session masih valid.", "success")
            return

        if res.get("status_code") is None:
            # Backend sedang offline/tidak reachable: jangan buang token yang
            # mungkin sebenarnya masih valid. Nanti push akan mencoba lagi.
            self._set_socialpulse_auth_status("⚠ Backend tidak reachable — saved session dipertahankan", "warn")
            self._log(f"Tidak bisa validasi saved SocialPulse session: {res.get('error')}", "warn")
            return

        self._clear_socialpulse_token(keep_username=True)
        self._set_socialpulse_auth_status("○ Saved session expired — silakan login", "warn")
        self._log("Saved SocialPulse JWT sudah invalid/expired; login ulang diperlukan.", "warn")

    def _logout_socialpulse(self):
        self._clear_socialpulse_token(keep_username=True)
        self.sp_password_var.set("")
        self._set_socialpulse_auth_status("○ Logged out from SocialPulse", "muted")
        self._log("SocialPulse token dihapus dari Multi-Capture.", "muted")

    # ---------------- Config save ----------------
    def _save_config_now(self, silent: bool = False):
        self.cfg["socialpulse_url"] = self.url_var.get().strip()
        self.cfg["socialpulse_username"] = self.sp_username_var.get().strip()
        # socialpulse_jwt hanya diubah oleh login/logout/validation; password
        # tidak pernah dimasukkan ke cfg.
        save_config(self.cfg)
        if not silent:
            self._log("Config URL/username saved (password tidak disimpan).", "muted")

    # ---------------- Log helpers ----------------
    def _enqueue_log(self, msg: str):
        # Dipanggil dari worker thread — masuk queue
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                # Detect tag dari content
                lower = msg.lower()
                tag = "info"
                if "error" in lower or "fail" in lower or "✗" in msg or "exception" in lower:
                    tag = "error"
                elif "warning" in lower or "warn" in lower or "missing" in lower or "⚠" in msg:
                    tag = "warn"
                elif "ok" in lower or "saved" in lower or "✓" in msg or "success" in lower:
                    tag = "success"
                self._log(msg, tag)
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self._poll_log_queue)

    def _log(self, msg: str, tag: str = "info"):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] ", "muted")
        self.log_text.insert("end", f"{msg}\n", tag)
        self.log_text.see("end")


def main():
    root = tk.Tk()
    app = MultiCaptureApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
