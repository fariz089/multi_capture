// popup.js — handler tombol platform, extract cookies + localStorage, push ke local server.
//
// Flow per klik tombol:
//   1. Resolve URL pattern berdasarkan platform (mis. .instagram.com, .x.com)
//   2. chrome.cookies.getAll({ domain }) — semua cookies untuk domain itu
//   3. Find tab yang lagi load domain itu (atau open new tab kalau gak ada)
//   4. chrome.scripting.executeScript di tab itu untuk read localStorage
//   5. Build storage_state shape (cookies array + origins array)
//   6. POST ke {server-url}/extension/push dengan body { platform, label, state }
//   7. Show status berdasarkan response

const PLATFORM_CONFIG = {
  facebook: {
    cookieDomains: ['.facebook.com'],
    storageOrigins: ['https://www.facebook.com', 'https://m.facebook.com'],
    requiredCookies: ['c_user', 'xs'],
    sampleUrl: 'https://www.facebook.com',
  },
  instagram: {
    cookieDomains: ['.instagram.com'],
    storageOrigins: ['https://www.instagram.com'],
    requiredCookies: ['sessionid', 'ds_user_id'],
    sampleUrl: 'https://www.instagram.com',
  },
  tiktok: {
    cookieDomains: ['.tiktok.com'],
    storageOrigins: ['https://www.tiktok.com'],
    // msToken muncul di multiple sub-domain; sessionid yg bener-bener wajib
    requiredCookies: ['sessionid'],
    sampleUrl: 'https://www.tiktok.com',
  },
  twitter: {
    // Twitter pindah ke .x.com tapi sebagian akun masih bawa .twitter.com.
    // Ambil dari dua-duanya, dedup di backend.
    cookieDomains: ['.x.com', '.twitter.com'],
    storageOrigins: ['https://x.com', 'https://twitter.com'],
    requiredCookies: ['auth_token', 'ct0'],
    sampleUrl: 'https://x.com',
  },
  threads: {
    // Threads share auth dengan Instagram via SSO. Cookie auth utama
    // (sessionid, ds_user_id) di-set di .instagram.com, sementara .threads.net
    // punya cookie tambahan untuk client-side state. Capture dari KEDUANYA
    // supaya storage_state lengkap dan scrape Playwright bisa hit kedua domain.
    cookieDomains: ['.threads.net', '.instagram.com'],
    storageOrigins: ['https://www.threads.net', 'https://www.instagram.com'],
    requiredCookies: ['sessionid', 'ds_user_id'],
    sampleUrl: 'https://www.threads.net',
  },
};

// ============================================================================
// State persistence (server URL & last label)
// ============================================================================

async function loadState() {
  const stored = await chrome.storage.local.get(['serverUrl', 'lastLabels']);
  return {
    serverUrl: stored.serverUrl || 'http://localhost:5099',
    lastLabels: stored.lastLabels || {},
  };
}

async function saveServerUrl(url) {
  await chrome.storage.local.set({ serverUrl: url });
}

async function saveLastLabel(platform, label) {
  const stored = await chrome.storage.local.get(['lastLabels']);
  const lastLabels = stored.lastLabels || {};
  lastLabels[platform] = label;
  await chrome.storage.local.set({ lastLabels });
}

// ============================================================================
// UI helpers
// ============================================================================

function setStatus(kind, msg) {
  const el = document.getElementById('status');
  el.className = `status ${kind}`;
  el.innerHTML = msg;
}

function disableButtons(disabled) {
  document.querySelectorAll('.plat-btn').forEach(b => {
    b.disabled = disabled;
  });
}

// ============================================================================
// Cookie extraction
// ============================================================================

async function getCookiesForDomains(domains) {
  // chrome.cookies.getAll({ domain: '.facebook.com' }) returns ALL cookies
  // for that domain AND its subdomains. Same call for x.com plus twitter.com
  // covers both auth_token (now at .x.com) and any leftover cookies (still at
  // .twitter.com from older sessions).
  const all = [];
  const seenKey = new Set();
  for (const domain of domains) {
    let batch;
    try {
      batch = await chrome.cookies.getAll({ domain });
    } catch (e) {
      console.warn(`[cookies] getAll failed for ${domain}:`, e);
      continue;
    }
    for (const c of batch) {
      // Dedup by (name, domain, path) — avoid double-counting cookies that
      // appear in multiple getAll queries (e.g. host-only x.com vs parent .x.com)
      const key = `${c.name}\x00${c.domain}\x00${c.path}`;
      if (seenKey.has(key)) continue;
      seenKey.add(key);
      all.push(c);
    }
  }
  return all;
}

function chromeCookiesToPlaywrightFormat(cookies) {
  // chrome.cookies.Cookie → Playwright storage_state cookie shape.
  // Field mapping:
  //   chrome: { name, value, domain, hostOnly, path, secure, httpOnly,
  //             sameSite, session, expirationDate }
  //   playwright: { name, value, domain, path, expires, httpOnly, secure,
  //                 sameSite }
  // Note: chrome.cookies.sameSite values: 'no_restriction'|'lax'|'strict'|'unspecified'
  //       playwright accepts: 'Strict'|'Lax'|'None'
  const sameSiteMap = {
    'no_restriction': 'None',
    'lax': 'Lax',
    'strict': 'Strict',
    'unspecified': 'Lax',
  };
  return cookies
    .filter(c => c.name && c.value !== undefined && c.value !== null)
    .map(c => {
      const out = {
        name: c.name,
        value: String(c.value),
        domain: c.domain,
        path: c.path || '/',
        httpOnly: !!c.httpOnly,
        secure: !!c.secure,
        sameSite: sameSiteMap[c.sameSite] || 'Lax',
      };
      // expirationDate: epoch detik (float). Session cookies = null/undefined.
      if (typeof c.expirationDate === 'number') {
        out.expires = c.expirationDate;
      }
      return out;
    });
}

// ============================================================================
// localStorage extraction (via injected script di tab target)
// ============================================================================

async function findTabForOrigin(origins) {
  // Cari tab yang URL-nya match salah satu origin. Prefer tab aktif kalau ada.
  // Returns tab object atau null.
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (!tab.url) continue;
    for (const origin of origins) {
      if (tab.url.startsWith(origin)) {
        return tab;
      }
    }
  }
  return null;
}

async function readLocalStorageInTab(tabId, origin) {
  // Eksekusi script kecil di tab target untuk dump localStorage.
  // Pakai chrome.scripting.executeScript (MV3 way), bukan
  // chrome.tabs.executeScript yang sudah deprecated.
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const items = [];
        try {
          for (let i = 0; i < localStorage.length; i++) {
            const name = localStorage.key(i);
            if (name == null) continue;
            const value = localStorage.getItem(name);
            if (value == null) continue;
            items.push({ name, value });
          }
        } catch (e) {
          // Some sites have CSP that blocks localStorage access from injected
          // scripts; just return what we have (or nothing).
          return { error: String(e), items: [] };
        }
        return { error: null, items };
      },
    });
    if (!results || !results[0]) return [];
    const result = results[0].result;
    if (!result) return [];
    if (result.error) {
      console.warn(`[localStorage] error in tab: ${result.error}`);
    }
    return result.items || [];
  } catch (e) {
    console.warn(`[localStorage] executeScript failed:`, e);
    return [];
  }
}

async function getLocalStorageForOrigins(origins) {
  // Untuk tiap origin, coba extract localStorage dari tab yang lagi load.
  // Kalau gak ada tab → skip (localStorage cuma bisa diakses dari context tab).
  // Build origins array sesuai shape Playwright storage_state.
  const result = [];
  for (const origin of origins) {
    const tab = await findTabForOrigin([origin]);
    if (!tab) {
      console.log(`[localStorage] no tab open for ${origin}, skipping`);
      continue;
    }
    const items = await readLocalStorageInTab(tab.id, origin);
    if (items.length === 0) {
      // Tetap include origin dengan empty array — kasih sinyal ke server bahwa
      // tab ada tapi storage kosong (vs gak ada tab sama sekali).
      console.log(`[localStorage] tab found for ${origin} but storage empty`);
      continue;
    }
    result.push({ origin, localStorage: items });
  }
  return result;
}

// ============================================================================
// Validation
// ============================================================================

function validateRequiredCookies(cookies, requiredNames) {
  const names = new Set(cookies.map(c => c.name));
  const missing = requiredNames.filter(r => !names.has(r));
  return { ok: missing.length === 0, missing };
}

// ============================================================================
// Main capture handler
// ============================================================================

async function captureAndPush(platform) {
  const config = PLATFORM_CONFIG[platform];
  if (!config) {
    setStatus('err', `Platform '${platform}' tidak dikenali.`);
    return;
  }

  const labelInput = document.getElementById('account-label');
  const label = (labelInput.value || '').trim();
  if (!label) {
    setStatus('err', 'Isi label akun dulu (mis. <code>ig_main</code>).');
    labelInput.focus();
    return;
  }

  const serverUrlInput = document.getElementById('server-url');
  const serverUrl = (serverUrlInput.value || '').trim().replace(/\/+$/, '');
  if (!serverUrl) {
    setStatus('err', 'Isi server URL multi_capture (default <code>http://localhost:5099</code>).');
    serverUrlInput.focus();
    return;
  }

  await saveServerUrl(serverUrl);
  await saveLastLabel(platform, label);

  disableButtons(true);
  setStatus('info', `<i>Capturing ${platform}/${label}…</i>`);

  try {
    // 1. Cookies dari semua domain target
    const rawCookies = await getCookiesForDomains(config.cookieDomains);
    if (rawCookies.length === 0) {
      setStatus(
        'err',
        `Cookies kosong untuk ${platform}. Login dulu di tab Chrome biasa, ` +
        `lalu klik tombol ini lagi.`
      );
      return;
    }

    const pwCookies = chromeCookiesToPlaywrightFormat(rawCookies);

    // 2. Validate required cookies
    const { ok, missing } = validateRequiredCookies(pwCookies, config.requiredCookies);
    if (!ok) {
      setStatus(
        'err',
        `Cookies wajib hilang: <code>${missing.join(', ')}</code>. ` +
        `Pastikan kamu sudah login penuh di tab Chrome — kalau baru di halaman ` +
        `login / 2FA / checkpoint, cookies belum lengkap.`
      );
      return;
    }

    // 3. localStorage dari origin yang ada tab-nya
    const origins = await getLocalStorageForOrigins(config.storageOrigins);
    const nLs = origins.reduce((sum, o) => sum + (o.localStorage?.length || 0), 0);

    // 4. Build storage_state shape (sama dengan output Playwright context.storage_state())
    const state = {
      cookies: pwCookies,
      origins: origins,
    };

    // 5. POST ke local server
    setStatus('info', `<i>Posting ke ${serverUrl}…</i>`);
    let resp;
    try {
      resp = await fetch(`${serverUrl}/extension/push`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          platform,
          label,
          state,
          source: 'chrome_extension_v1',
        }),
      });
    } catch (e) {
      setStatus(
        'err',
        `Tidak bisa konek ke <code>${serverUrl}</code>. Pastikan multi_capture ` +
        `jalan dan extension server aktif (default port 5099).<br><br>` +
        `Error: ${e.message}`
      );
      return;
    }

    if (!resp.ok) {
      let errText = `HTTP ${resp.status}`;
      try {
        const j = await resp.json();
        if (j.error) errText = j.error;
      } catch (_) {}
      setStatus('err', `Server tolak: ${errText}`);
      return;
    }

    const data = await resp.json().catch(() => ({}));
    setStatus(
      'ok',
      `✓ Captured ${platform}/${label}: ${pwCookies.length} cookies, ${nLs} ` +
      `localStorage items.<br>` +
      `Buka GUI multi_capture untuk klik <b>Push to SocialPulse</b>.` +
      (data.session_path ? `<br><small>${data.session_path}</small>` : '')
    );
  } catch (e) {
    console.error('Capture failed:', e);
    setStatus('err', `Error: ${e.message}`);
  } finally {
    disableButtons(false);
  }
}

// ============================================================================
// Init
// ============================================================================

async function init() {
  const state = await loadState();
  document.getElementById('server-url').value = state.serverUrl;

  // Detect platform dari tab aktif → preset label dari last-used
  // (untuk platform yang ke-detect)
  let detectedPlatform = null;
  try {
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (activeTab && activeTab.url) {
      for (const [plat, cfg] of Object.entries(PLATFORM_CONFIG)) {
        for (const origin of cfg.storageOrigins) {
          if (activeTab.url.startsWith(origin)) {
            detectedPlatform = plat;
            break;
          }
        }
        if (detectedPlatform) break;
      }
    }
  } catch (_) {}

  // Preload label dari last-used (kalau ada tab yang ke-detect, pakai itu;
  // kalau ngga, pakai label yang paling baru dipakai)
  if (detectedPlatform && state.lastLabels[detectedPlatform]) {
    document.getElementById('account-label').value = state.lastLabels[detectedPlatform];
  } else {
    const anyLast = Object.values(state.lastLabels).filter(Boolean).pop();
    if (anyLast) {
      document.getElementById('account-label').value = anyLast;
    }
  }

  // Highlight button platform yang ke-detect
  if (detectedPlatform) {
    const btn = document.querySelector(`.plat-btn[data-platform="${detectedPlatform}"]`);
    if (btn) {
      btn.style.background = '#2a3a4a';
      btn.style.borderColor = '#4a90e2';
      setStatus('info', `Tab aktif: <b>${detectedPlatform}</b>. Klik tombol di bawah untuk capture.`);
    }
  }

  // Wire button handlers
  document.querySelectorAll('.plat-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const plat = btn.getAttribute('data-platform');
      captureAndPush(plat);
    });
  });
}

document.addEventListener('DOMContentLoaded', init);
