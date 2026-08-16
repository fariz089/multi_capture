# Multi-Capture

Tool standalone buat capture login session multi-platform (Facebook, TikTok, Instagram, Twitter/X, Threads) lewat Chromium real, lalu push langsung ke SocialPulse atau export sebagai file JSON.

Penerus dari `fb_capture` — sekarang multi-platform dengan GUI desktop sederhana.

## v3.1 (13 Mei 2026) — Tambah platform Threads ✅

Threads (Meta) sekarang ikut bisa di-capture. Threads share auth dengan Instagram (cookie `sessionid` + `ds_user_id` sama), tapi di-capture terpisah supaya storage_state lengkap untuk kedua domain (`.threads.net` + `.instagram.com`). User flow: pilih tab Threads → login via Chromium (otomatis redirect ke IG kalau belum login) → push ke SocialPulse → scraping aktif di endpoint Threads `/api/graphql`.

## v3 (7 Mei 2026) — IG & Twitter sekarang cookie-based ✅

Sebelumnya tool ini cuma bisa auto-push session Facebook & TikTok. Instagram & Twitter cuma "info-only" karena SocialPulse pakai instagrapi/twikit (login user/pass real). Sekarang **kelima platform pakai cookie-based scraping** (Playwright + storage_state) — flow capture-to-scrape jadi konsisten end-to-end. instagrapi & twikit sudah di-drop dari SocialPulse.

## v3.1 (8 Mei 2026) — Stealth mode + persistent profile

Plain Playwright Chromium ke-detect sama IG, TikTok, dan Twitter via beberapa runtime fingerprint signal. Gejala paling umum: kamu ketik password yang BENAR tapi dapet error "Informasi login yang Anda masukkan salah" — itu bukan password salah, tapi anti-bot reject login attempt karena `navigator.webdriver === true`, WebGL renderer "SwiftShader", dan `window.chrome` incomplete.

**Yang dilakukan v3.1:**

1. **Stealth init script** di-inject sebelum page load (di `platforms/base.py`):
   - `navigator.webdriver` → `false` via `Object.defineProperty`
   - `navigator.plugins` → fake [PDF Viewer, Chrome PDF, Chromium PDF]
   - `navigator.languages` → `['id-ID', 'id', 'en-US', 'en']`
   - WebGL `UNMASKED_VENDOR/RENDERER` → spoof Intel ANGLE (bukan SwiftShader)
   - `window.chrome.{runtime, loadTimes, csi}` → minimal stub
   - Permissions API quirk patch (notifications denied/default mismatch)
   - Hide `__playwright__` globals

2. **Persistent browser profile** per-label di `sessions/.profiles/<platform>_<label>/`:
   - Cookies/cache/IndexedDB persist antar capture session → akun terasa "returning user", bukan fresh device
   - Anti-bot signal "session continuity" jadi positif
   - Trade-off: ruang disk ~50-200MB per profile. Bisa di-clear manual kalau perlu.

3. **`ignore_default_args=['--enable-automation']`** — hide automation flag di window.title bar dan internal Chrome detection.

Stealth ini juga di-apply di **scraper-side** (SocialPulse pc-scraper) supaya scraping IG/Twitter (yang headless di Docker) gak ke-block. File: `pc-scraper/scrapers/_stealth.py`.

## v3.2 (8 Mei 2026) — Chrome Extension Bridge ⭐ RECOMMENDED

Stealth Playwright cuma menangani 6 fingerprint signal yang umum. Untuk akun yang IP-nya ke-flag IG, atau akun yang baru bikin di IP lain, anti-bot kadang masih reject login walau stealth aktif. Solusi paling reliable: **pakai Chrome real (bukan Chromium) untuk login**, lalu extract cookies via Chrome extension custom.

Chrome extension bawaan (folder `chrome_extension/`) bisa:

1. Read cookies dari Chrome real lewat `chrome.cookies` API (anti-bot **tidak bisa detect** karena yang login adalah Chrome biasa user)
2. Read localStorage dari tab platform via `chrome.scripting.executeScript`
3. Build Playwright `storage_state` shape yang identik dengan output Playwright
4. POST ke `multi_capture` local server (`http://127.0.0.1:5099/extension/push`)
5. Session masuk otomatis ke tab platform GUI multi_capture — kamu tinggal klik "Push to SocialPulse"

### Setup extension (sekali aja)

1. Install dependencies: `pip install -r requirements.txt` (sekarang include Flask)
2. Jalanin `multi_capture.py` — banner di GUI atas akan nunjukin "✓ Extension server jalan di http://127.0.0.1:5099/..."
3. Buka Chrome → `chrome://extensions` → toggle **Developer mode** (kanan atas)
4. Klik **Load unpacked** → pilih folder `multi_capture/chrome_extension/`
5. Extension muncul di toolbar Chrome. Klik icon → set Server URL kalau pakai port custom (default `http://localhost:5099`)

### Cara pakai

1. Login ke platform target (FB/IG/TikTok/Twitter/Threads) di tab Chrome **biasa** (bukan tab popup extension)
2. Klik icon extension di toolbar — popup keluar, otomatis detect platform dari tab aktif
5. Isi label akun (mis. `ig_main`, `fb_alt_1`)
4. Klik tombol platform yang sesuai — extension extract cookies + localStorage → POST ke local server
5. Status "✓ Captured ..." muncul. Buka GUI multi_capture — tab platform itu otomatis switch + label ke-fill, klik **⬆ Push to SocialPulse**

### Permissions yang diminta extension

Saat install, Chrome akan tampilin warning "this extension can read your data on facebook.com, instagram.com, ...". Itu normal — extension butuh:

- **`cookies`**: read cookies dari domain target (untuk extract)
- **`scripting` + `activeTab`**: inject script untuk read localStorage di tab terbuka
- **`storage`**: simpan server URL & last labels di chrome.storage.local
- **`host_permissions`** ke FB/IG/TikTok/Twitter/Threads + localhost: scope domain yang bisa dibaca

Extension **tidak** mengirim data ke server selain `127.0.0.1:5099` (multi_capture local). Bisa di-verify di `popup.js` — semua `fetch()` call hardcoded ke server URL yang user set di popup.

### Kalau extension server gak start

GUI tetap usable dengan **Playwright fallback** (tab platform di bawah banner). Klik "Buka Browser & Login" seperti biasa. Banner extension nunjukin warning kuning dengan alasan kenapa server gak jalan (port udah dipakai, Flask gak ke-install, dll).

## Kenapa pisah dari SocialPulse?

- **Captcha & 2FA butuh interaksi mouse manusia** → harus di Chromium real (headed), bukan headless di Docker.
- **Login itu interactive (1x per akun, jarang)**. Scraping otomatis 24/7. Beda lifecycle, beda environment.
- **Cookies hasil dari Playwright real lebih lengkap** — termasuk `localStorage`, `sessionStorage`, dan attribute (`expires`, `httpOnly`, `sameSite`). Jauh lebih reliable buat anti-bot detection.

## Yang didukung

| Platform | Status di SocialPulse | Chrome Extension | Playwright Fallback |
|---|---|---|---|
| **Facebook** | Cookie-based ✅ | ✅ Recommended | ✅ Backup |
| **TikTok** | Cookie-based ✅ | ✅ Recommended | ✅ Backup |
| **Instagram** | Cookie-based ✅ (v3) | ✅ Recommended | ⚠ Often detected |
| **Twitter / X** | Cookie-based ✅ (v3) | ✅ Recommended | ⚠ Often detected |
| **YouTube** | Pakai `anonymous` di SocialPulse | (skip — gak butuh) | (skip — gak butuh) |
| **News** | Public, gak butuh login | (skip — gak butuh) | (skip — gak butuh) |

> **Recommendation**: pakai Chrome extension dulu (anti-bot bypass lebih reliable). Playwright capture sebagai fallback kalau Chrome extension belum di-install atau ada masalah dengan local server.

## Setup

```bash
# Sekali aja
pip install -r requirements.txt
playwright install chromium
```

## Cara pakai

### Lewat GUI (recommended)

Windows: double-click `run.bat`
Atau dari terminal:
```bash
python multi_capture.py
```

Flow:
1. Set **API URL** SocialPulse di atas (default `http://localhost:3001`).
2. Isi **Username / Email** + **Password**, lalu klik **Login to SocialPulse**. Tool akan mengambil JWT otomatis dari `/api/auth/login`. Password tidak disimpan ke config. JWT yang masih valid di-cache dan dicek lagi ke `/api/auth/me` saat app dibuka.
3. Kalau JWT expire saat push dan field password masih terisi, Multi-Capture otomatis login ulang dan retry push satu kali.
4. Pilih tab platform.
5. Input **label akun** (bebas, buat identifier, mis. `fb_alt_1`, `tiktok_main`, `ig_brand`, `tw_news`).
6. Klik **🌐 Buka Browser & Login** — Chromium kebuka headed.
7. Login manual di window itu (selesaikan 2FA / captcha kalau ada).
8. Sistem **auto-detect** saat cookies penting terbaca, ATAU klik **✓ Saya Sudah Login** kalau auto-detect telat.
9. Cookies disimpan ke `sessions/{platform}_{label}.json` (Playwright storage_state format).
10. Klik **⬆ Push to SocialPulse** untuk auto-add via `POST /api/accounts`. Atau **💾 Save As...** untuk dapet file copy.

### Output format

File JSON di `sessions/` adalah Playwright `storage_state` dict:

```json
{
  "cookies": [
    {"name": "c_user", "value": "...", "domain": ".facebook.com",
     "path": "/", "expires": 1812629381.04, "httpOnly": false,
     "secure": true, "sameSite": "None"},
    ...
  ],
  "origins": [
    {"origin": "https://www.facebook.com",
     "localStorage": [{"name": "...", "value": "..."}, ...]}
  ]
}
```

Format ini identik dengan `fb_session.json` hasil `fb_capture.py` lama, dan SocialPulse account managers (Facebook, TikTok, Instagram v3, Twitter v3) nerima native.

### Push payload strategy

Helper push (`push_to_socialpulse.py`) kirim payload dalam dua skenario:

1. **Default**: kirim full storage_state (cookies + localStorage + atribut). SocialPulse auto-extract dengan parser, simpan SEMUA cookie kecuali Akamai/analytics noise.

2. **Backward-compat fallback** (TikTok only): kalau response 400 dengan keyword parser-error (mis. "ms_token wajib ada"), helper otomatis retry dengan flat dict (legacy format). Jadi tool ini works di SocialPulse versi lama JUGA, gak perlu update dua-duanya barengan. IG & Twitter gak ada legacy yang perlu fallback (parser baru sejak hari pertama support).

Result dict berisi field `payload_format` yang nunjukin mana yang akhirnya berhasil:
- `"storage_state"` — full payload diterima
- `"flat_dict_legacy"` — TikTok backward-compat retry success

### Pre-flight checks

Sebelum POST ke SocialPulse, helper cek cookies wajib per platform:

| Platform | Required |
|---|---|
| Facebook | `c_user`, `xs` |
| TikTok | `msToken` (atau `ms_token`) |
| Instagram | `sessionid`, `ds_user_id` |
| Twitter | `auth_token`, `ct0` |

Kalau ada yang kurang, helper return error actionable: "Cookies wajib untuk X tidak ditemukan: …" — re-capture dulu.

### Manual paste (kalau push gagal)

Buka file `sessions/{platform}_{label}.json`, copy isinya, paste ke field **Cookie** di SocialPulse > Akun Scraper. Klik **Format Otomatis** kalau perlu.

## Catatan per platform

### Facebook
- Cookie penting: `c_user`, `xs`. Sisanya recommended.
- Kalau ke-checkpoint: selesaikan di window Chromium, lanjut sampai feed muncul. Tool tunggu sampai 10 menit.
- LocalStorage (mis. `screen_time_period_logging_facebook`) otomatis ke-capture — bantu fingerprint kelihatan lebih natural.

### TikTok
- Cookie penting: `sessionid`, `msToken`. Plus `sid_guard`, `ttwid`, `tt_csrf_token`, `passport_csrf_token`, `tea_web_id`, `cmpl_token` recommended (anti-bot fingerprint).
- **msToken muncul 2x di context.cookies()** (parent `.tiktok.com` + host-only `www.tiktok.com`). Tool simpan keduanya, push helper otomatis pilih yang `.tiktok.com` (parent — yang lebih general dipakai TikTokApi).
- **SocialPulse versi terbaru** (parser updated 2026-05) simpan SEMUA cookie kecuali Akamai/analytics noise — jauh lebih lengkap dari sebelumnya yang cuma simpan ~16 cookie hardcoded. Lebih banyak cookie = lebih sedikit captcha challenge.
- Captcha geser muncul agak sering — selesaikan, retry kalau gagal. Scroll-scroll dulu di feed sebelum klik "Saya Sudah Login" supaya msToken regenerate dengan value yang fresh.

### Instagram (v3 — cookie-based)
- Cookie penting: `sessionid`, `ds_user_id`. Plus `csrftoken`, `rur`, `ig_did` recommended.
- **Format `sessionid`**: `<user_id>%3A<token>%3A<…>` — kalau ke-paste ke SocialPulse `sessionid`-nya cuma string angka tanpa `%3A`, hampir pasti keliru copy field lain. Pakai multi_capture supaya format-nya 100% benar.
- IG sensitif ke "device baru". Login pertama mungkin trigger email verification — selesaikan dulu di Chromium sebelum klik "Saya Sudah Login".
- Scraping hashtag: SocialPulse navigate ke `/explore/tags/{keyword}/` lalu intercept GraphQL response (`/api/v1/tags/`, `/graphql/query`).

### Twitter / X (v3 — cookie-based)
- Cookie penting: `auth_token`, `ct0`. Plus `twid` (user ID dgn prefix `u%3D`), `kdt` (known device token), `guest_id` recommended.
- `kdt` (kalau ada) bantu ngurangi challenge "device baru" di scrape berikutnya.
- Login dengan email + password (atau via Google SSO) di Chromium — sama seperti normal browsing.
- Scraping search: SocialPulse navigate ke `x.com/search?q={keyword}&f=live` (Latest tab) lalu intercept GraphQL response (`/i/api/graphql/.../SearchTimeline`).

## Troubleshooting

**"playwright belum ke-install"** → `pip install playwright && playwright install chromium`

**Window kebuka tapi cuma blank/loading lama** → TikTok khusus kadang load lambat. Tunggu 10-30 detik.

**Auto-detect login gak jalan padahal udah login** → klik tombol **✓ Saya Sudah Login** manual di GUI. Cookies tetap ke-save.

**Push gagal: "Access token required" / HTTP 401** → login ke SocialPulse pada panel paling atas. Jangan copy-paste JWT manual lagi; Multi-Capture akan mengambil token sendiri.

**Push gagal: "no_active_accounts"** → backend SocialPulse belum reachable di URL itu. Cek `docker ps` dan API URL di GUI.

**Push gagal: "c_user format invalid"** → session yang ke-save belum login penuh. Re-login (hapus session file dulu, atau pakai label baru).

**Push gagal: "Cookies wajib untuk instagram tidak ditemukan: sessionid, ds_user_id"** → IG masih di halaman login / 2FA. Selesaikan dulu sampai feed muncul, baru klik "Saya Sudah Login".

**Push gagal: "auth_token terlalu pendek"** → Twitter login belum complete (misal masih di OAuth flow). Tunggu sampai redirect ke `/home`, baru save.

**msToken kurang / scraping TikTok rate-limited** → re-capture sambil scroll-scroll dulu di TikTok feed sebelum klik "Saya Sudah Login". msToken regenerate per page navigation.

**"Informasi login yang Anda masukkan salah" di IG padahal password BENAR** → ini anti-bot detection. Pastikan kamu pakai v3.1 (stealth mode aktif). Cek: di Chromium yang kebuka, buka DevTools (F12) → Console → ketik `navigator.webdriver` — kalau return `false`, stealth aktif. Kalau return `true` atau `undefined`, ada yang salah dengan stealth inject.

Workaround tambahan kalau IG masih reject:
- Tutup browser, hapus profile dir-nya (`rm -rf sessions/.profiles/instagram_<label>/`), retry
- Coba dari IP rumah (bukan VPN/datacenter IP) — IG sangat aware ke ASN
- Login via app dulu (HP) untuk warm up akun, baru capture di multi_capture
- Kalau pakai akun yang baru bikin: tunggu 24-48 jam sebelum login dari device baru, IG-nya treat sebagai akun fresh

**TikTok captcha geser muncul terus** → ini normal, selesaikan secara manual. TikTok punya captcha bahkan untuk login real user. Setelah selesai 1-2x, biasanya akun di-trust dan captcha berkurang.

**Twitter redirect ke /i/flow/login terus padahal udah login** → sama dengan IG: anti-bot. v3.1 stealth biasanya cukup. Kalau masih: clear profile dir, coba pakai akun yang sudah aktif >7 hari.

## File structure

```
multi_capture/
├── multi_capture.py           # Main GUI (Tkinter) + spawn extension server
├── extension_server.py        # Local Flask server, port 5099 (v3.2)
├── push_to_socialpulse.py     # Helper buat POST cookie ke SocialPulse
├── platforms/                 # Playwright capture fallback
│   ├── __init__.py            # PLATFORMS registry
│   ├── base.py                # LoginCapture base + stealth init script
│   ├── facebook.py
│   ├── tiktok.py
│   ├── instagram.py           # v3: cookie-based ✅
│   └── twitter.py             # v3: cookie-based ✅
├── chrome_extension/          # v3.2: Chrome MV3 extension (load unpacked)
│   ├── manifest.json
│   ├── popup.html
│   ├── popup.js               # Extract cookies + localStorage, POST ke 5099
│   ├── background.js          # Service worker (minimal MV3)
│   └── icons/                 # 16/48/128 px
├── sessions/                  # Output: {platform}_{label}.json (gitignored)
│   └── .profiles/             # Persistent Chromium profiles per-label (gitignored)
├── requirements.txt
├── run.bat                    # Windows double-click launcher
├── multi_capture_config.json  # Remember URL/username/cached JWT (password tidak disimpan)
├── .gitignore
└── README.md
```

## Konfigurasi tersimpan

`multi_capture_config.json` (gitignored) menyimpan:
- `socialpulse_url` — last API URL
- `socialpulse_username` — username/email terakhir (aman disimpan)
- `socialpulse_jwt` — cached JWT; divalidasi ke `/api/auth/me` saat startup
- **Password tidak pernah disimpan** ke config.
- `last_labels` — last label per platform (autofill saat next run)

Hapus file ini buat reset.

## Migrasi dari versi lama (akun IG/Twitter via instagrapi/twikit)

Akun IG & Twitter yang sebelumnya di-add via SocialPulse UI dengan user/pass (versi pre-v3) **tidak akan jalan lagi** — instagrapi/twikit udah di-drop. Migrasi:

1. Di SocialPulse UI → Akun Scraper: hapus akun IG & Twitter lama (tombol Delete).
2. Buka multi_capture, login pakai akun-akun itu lewat tab IG dan Twitter.
3. Push ke SocialPulse — sekarang scraping pakai cookie-based.

Akun Facebook & TikTok yang udah di-add sebelumnya **tetap jalan tanpa perubahan** (mereka udah cookie-based dari awal).
