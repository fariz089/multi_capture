// background.js — service worker MV3
//
// Untuk extension ini, semua logic ada di popup.js (user-driven via
// klik popup). Background cuma butuh ada supaya MV3 manifest valid
// dan bisa handle install/update event di masa depan.

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === 'install') {
    console.log('[multi-capture] Extension installed.');
    // Set default server URL kalau belum ada
    chrome.storage.local.get(['serverUrl'], (stored) => {
      if (!stored.serverUrl) {
        chrome.storage.local.set({ serverUrl: 'http://localhost:5099' });
      }
    });
  } else if (details.reason === 'update') {
    console.log(`[multi-capture] Updated to ${chrome.runtime.getManifest().version}`);
  }
});
