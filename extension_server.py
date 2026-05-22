"""
Extension Server — local Flask server yang nerima POST dari Chrome extension.
==============================================================================

Jalan di port 5099 (default), terima POST /extension/push dengan body:
  {
    "platform": "facebook" | "instagram" | "tiktok" | "twitter" | "threads",
    "label": "<identifier akun bebas>",
    "state": { "cookies": [...], "origins": [...] },  // Playwright storage_state shape
    "source": "chrome_extension_v1"
  }

Save state ke sessions/{platform}_{label}.json — sama persis dengan output
flow Playwright multi_capture, jadi tombol Push di GUI bisa dipakai apa
adanya.

CORS: enable cross-origin dari chrome-extension://*  dan  http://localhost.
Extension Chrome punya origin chrome-extension://<id>, jadi tanpa CORS
header browser bakal block.

Threading: dijalanin sebagai daemon thread dari multi_capture.py main.
GUI process owns Tk root; server thread cuma write file + emit callback
ke GUI biar list refresh.

Security:
  - Bind ke 127.0.0.1 doang (localhost), bukan 0.0.0.0 — gak ke-ekspos
    ke jaringan, cuma extension di mesin yang sama yang bisa hit.
  - No auth (gak butuh — origin check via CORS sudah cukup buat use
    case lokal). Kalau user concern, tutup port 5099 di firewall.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

try:
    from flask import Flask, request, jsonify, make_response
except ImportError as e:
    raise ImportError(
        "flask not installed. Add 'flask>=3.0' to requirements.txt: "
        f"{e}"
    )

logger = logging.getLogger(__name__)

# Required cookies per platform — server-side validation supaya gak
# kepush session yang setengah jadi.
REQUIRED_COOKIES = {
    "facebook":  ("c_user", "xs"),
    "instagram": ("sessionid", "ds_user_id"),
    "tiktok":    ("sessionid",),  # msToken juga penting tapi gak strict di server
    "twitter":   ("auth_token", "ct0"),
    "threads":   ("sessionid", "ds_user_id"),  # share auth dengan IG
}

VALID_PLATFORMS = set(REQUIRED_COOKIES.keys())


def _safe_label(label: str) -> str:
    """Sanitize label supaya safe jadi filename. Sama dengan _safe_label di
    LoginCapture base class."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]


def _validate_state(state: dict, platform: str) -> Optional[str]:
    """
    Pre-flight: cek struktur storage_state + cookies wajib.
    Return error message kalau invalid, None kalau OK.
    """
    if not isinstance(state, dict):
        return "state harus object (dict)."
    cookies = state.get("cookies")
    if not isinstance(cookies, list):
        return "state.cookies harus array."
    if not cookies:
        return "state.cookies kosong — gak ada cookies sama sekali."

    # Cek required cookies ada (di domain mana pun)
    names = set()
    for c in cookies:
        if isinstance(c, dict) and c.get("name") and c.get("value"):
            names.add(c["name"])

    required = REQUIRED_COOKIES.get(platform, ())
    missing = [r for r in required if r not in names]
    if missing:
        return (
            f"Cookies wajib untuk {platform} tidak ditemukan: "
            f"{', '.join(missing)}. Login mungkin belum complete."
        )

    return None


def make_app(
    sessions_dir: Path,
    on_session_saved: Optional[Callable[[str, str, Path], None]] = None,
) -> Flask:
    """
    Build Flask app. Caller pass:
      - sessions_dir: folder untuk save {platform}_{label}.json
      - on_session_saved: optional callback dipanggil setelah save sukses;
        signature (platform, label, path) — biasanya dipakai GUI untuk
        refresh list yg display.

    Returns Flask app, ready to be run by caller.
    """
    app = Flask(__name__)

    def _cors_headers(resp):
        # Allow chrome-extension://* dan localhost:* — extension origin
        # bentuknya chrome-extension://<extension-id>, gak bisa di-whitelist
        # by ID karena ID di-generate per install kalau load unpacked.
        # Solusinya: allow semua origin chrome-extension dan localhost.
        origin = request.headers.get("Origin", "")
        if origin.startswith("chrome-extension://") or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1"):
            resp.headers["Access-Control-Allow-Origin"] = origin
        else:
            # Default: kasih * tapi tanpa credentials supaya browser tetap
            # accept (GET/POST tanpa cookie cross-origin OK).
            resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "3600"
        return resp

    @app.after_request
    def after(resp):
        return _cors_headers(resp)

    @app.route("/extension/push", methods=["OPTIONS"])
    def push_options():
        # Preflight CORS
        return _cors_headers(make_response("", 204))

    @app.route("/extension/push", methods=["POST"])
    def push_session():
        try:
            payload = request.get_json(silent=True) or {}
        except Exception as e:
            return jsonify({"error": f"invalid json: {e}"}), 400

        platform = (payload.get("platform") or "").strip().lower()
        label = (payload.get("label") or "").strip()
        state = payload.get("state")
        source = payload.get("source", "unknown")

        # ----- validate -----
        if platform not in VALID_PLATFORMS:
            return jsonify({
                "error": f"platform '{platform}' invalid. "
                         f"Valid: {sorted(VALID_PLATFORMS)}"
            }), 400

        if not label:
            return jsonify({"error": "label kosong"}), 400

        err = _validate_state(state, platform)
        if err:
            return jsonify({"error": err}), 400

        # ----- normalize storage_state defensively -----
        # Extension udah kirim shape yg bener, tapi kita tetap defensif.
        # Drop entries tanpa name/value, coerce types, dll.
        clean_cookies = []
        for c in state.get("cookies") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            value = c.get("value")
            if not name or value in (None, ""):
                continue
            entry = {
                "name": str(name),
                "value": str(value),
                "domain": c.get("domain") or "",
                "path": c.get("path") or "/",
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": c.get("sameSite") or "Lax",
            }
            if "expires" in c and c["expires"] is not None:
                try:
                    entry["expires"] = float(c["expires"])
                except (TypeError, ValueError):
                    pass
            clean_cookies.append(entry)

        clean_origins = []
        for o in state.get("origins") or []:
            if not isinstance(o, dict):
                continue
            origin = o.get("origin")
            ls = o.get("localStorage") or []
            if not origin:
                continue
            valid_items = [
                {"name": str(it.get("name")), "value": str(it.get("value"))}
                for it in ls
                if isinstance(it, dict) and it.get("name") and it.get("value") is not None
            ]
            clean_origins.append({"origin": origin, "localStorage": valid_items})

        normalized = {"cookies": clean_cookies, "origins": clean_origins}

        # ----- save -----
        safe = _safe_label(label)
        out_path = sessions_dir / f"{platform}_{safe}.json"
        try:
            sessions_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(normalized, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.exception("Save failed")
            return jsonify({"error": f"save failed: {e}"}), 500

        n_cookies = len(clean_cookies)
        n_ls = sum(len(o["localStorage"]) for o in clean_origins)
        logger.info(
            f"[ext] Saved {platform}/{label} (source={source}): "
            f"{n_cookies} cookies, {n_ls} localStorage items → {out_path.name}"
        )

        # Notify GUI
        if on_session_saved is not None:
            try:
                on_session_saved(platform, label, out_path)
            except Exception as e:
                logger.warning(f"on_session_saved callback failed: {e}")

        return jsonify({
            "ok": True,
            "platform": platform,
            "label": label,
            "session_path": str(out_path),
            "cookie_count": n_cookies,
            "localstorage_count": n_ls,
        })

    @app.route("/extension/health", methods=["GET"])
    def health():
        return jsonify({
            "ok": True,
            "service": "multi_capture_extension_server",
            "sessions_dir": str(sessions_dir),
            "supported_platforms": sorted(VALID_PLATFORMS),
        })

    @app.route("/", methods=["GET"])
    def root():
        return jsonify({
            "service": "multi_capture extension server",
            "endpoints": ["GET /extension/health", "POST /extension/push"],
        })

    return app


def run_server_in_thread(
    sessions_dir: Path,
    port: int = 5099,
    host: str = "127.0.0.1",
    on_session_saved: Optional[Callable[[str, str, Path], None]] = None,
) -> threading.Thread:
    """
    Spawn Flask server di daemon thread. Returns Thread object — caller
    boleh ignore, daemon thread akan auto-shutdown saat main proses keluar.

    NOTE: bind ke 127.0.0.1 default, BUKAN 0.0.0.0. Server ini cuma untuk
    Chrome extension di mesin yang sama. Kalau bind ke 0.0.0.0 user lain
    di jaringan bisa hit endpoint dan inject session palsu.
    """
    app = make_app(sessions_dir, on_session_saved)

    def _run():
        try:
            # Suppress werkzeug request log noise — kita pakai logger sendiri
            import logging as _log
            _log.getLogger("werkzeug").setLevel(_log.WARNING)
            app.run(
                host=host, port=port,
                debug=False, use_reloader=False, threaded=True,
            )
        except Exception as e:
            logger.error(f"Extension server crashed: {e}")

    thread = threading.Thread(
        target=_run, daemon=True, name="ext-server",
    )
    thread.start()
    logger.info(f"Extension server listening on http://{host}:{port}/extension/push")
    return thread


if __name__ == "__main__":
    # Standalone mode untuk debugging — jalan tanpa GUI
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    base = Path(__file__).parent
    sessions = base / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    print(f"Standalone mode — sessions dir: {sessions}")
    print(f"Listening on http://127.0.0.1:5099/extension/push")
    app = make_app(sessions)
    app.run(host="127.0.0.1", port=5099, debug=False)
