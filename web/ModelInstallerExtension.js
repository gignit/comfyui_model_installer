// Try to import app/api if the environment supports import maps; otherwise fall back to globals
let appRef, apiRef;
try {
  // dynamic import can fail on older bundles; we guard it
  ({ app: appRef } = await import('/scripts/app.js'));
  ({ api: apiRef } = await import('/scripts/api.js'));
} catch (e) {
  appRef = (typeof window !== 'undefined' ? window.app : undefined);
  apiRef = (typeof window !== 'undefined' ? window.api : undefined);
}

// Check if Model Installer server support is available
async function isModelInstallerAvailable() {
  try {
    const response = await fetch('/model_installer/health');
    const data = await response.json();
    return response.ok && data.ok;
  } catch {
    return false;
  }
}

// Minimal frontend extension that augments the Missing Models dialog entries
// Uses existing UI components, styles and fetch to call backend endpoints

// global app is provided by the ComfyUI frontend
if (appRef && typeof appRef.registerExtension === 'function') appRef.registerExtension({
  name: "comfyui_model_installer",
  async setup() {},
  async beforeRegisterNodeDef() {},
  async nodeCreated() {},
  async loadedGraph() {},
  async init() {
    // Check if server supports model installer
    const available = await isModelInstallerAvailable();
    if (!available) {
      console.log('[Model Installer] Server support not detected');
      return;
    }
    
    console.log('[Model Installer] Extension initialized');
    
    // augment list items after UI loads. Observe DOM changes to attach buttons
    const observer = new MutationObserver(() => {
      document.querySelectorAll('.comfy-missing-models li.p-listbox-option').forEach((li) => {
        const row = li.querySelector('div.flex.flex-row.items-center.gap-2');
        if (!row) return;
        if (row.querySelector('.model-install-buttons')) return;

        const labelSpan = row.querySelector('span[title]');
        const labelText = (labelSpan?.textContent || '').trim();
        // Expect formats like: "text_encoders / clip_l.safetensors"
        let directory = undefined;
        let filename = undefined;
        const m = labelText.match(/^(.*?)\s*\/\s*([^\s]+\.[a-z0-9]+)\b/i);
        if (m) {
          directory = (m[1] || '').trim();
          filename = (m[2] || '').trim();
        }

        // Prefer URL from the Download button's title; avoid client-side CORS fetches
        const downloadBtn = row.querySelector('button[title]');
        let url = downloadBtn?.getAttribute('title') || '';
        // As a last resort, fallback to span title (may be a blob URL); we'll normalize it
        if (!url && labelSpan?.getAttribute('title')) url = labelSpan.getAttribute('title');
        url = normalizeDownloadUrl(url);

        const container = document.createElement('div');
        container.className = 'model-install-buttons flex gap-2 flex-col';
        
        // Path selector dropdown (hidden by default)
        const pathSelector = document.createElement('select');
        pathSelector.className = 'p-dropdown p-component p-inputtext p-inputtext-sm';
        pathSelector.style.display = 'none';
        
        // Install button
        const btn = document.createElement('button');
        btn.className = 'p-button p-component p-button-outlined p-button-sm';
        btn.textContent = '...';
        
        container.appendChild(pathSelector);
        container.appendChild(btn);
        row.appendChild(container);
        // button injected

        const setBusy = (busy) => {
          btn.disabled = busy;
          btn.classList.toggle('p-disabled', busy);
        };

        const setLabel = (installed) => {
          // Simple approach: show Uninstall button, let server handle the 403 error
          btn.textContent = installed ? 'Uninstall' : 'Install';
        };

        const query = async () => {
          try {
            // Initialize path selector if we have model info
            let qs = '';
            if (directory && filename) {
              qs = `directory=${encodeURIComponent(directory)}&filename=${encodeURIComponent(filename)}`;
            } else if (url) {
              qs = `url=${encodeURIComponent(url)}`;
            }
            const res = await fetch(`/models/status?${qs}`);
            const js = await res.json();
            
            // Update path selector with storage info from status response (when model not found)
            if (directory && filename) {
              updatePathSelector(pathSelector, directory, js.storage_info);
            }
            
            if (js.state === 'downloading') {
              btn.textContent = 'Downloading…';
              btn.disabled = true;
              pathSelector.disabled = true; // Disable during download
            } else {
              setLabel(!!js.present);
              btn.disabled = false;
              pathSelector.disabled = false; // Re-enable after download
            }
            btn.onclick = async () => {
              // Decide intent from current label to avoid stale js.present when file exists but is downloading
              const installing = (btn.textContent || '').trim().toLowerCase() !== 'uninstall';
              console.debug('[Model Installer] click', { installing, directory, filename, url });
              try {
                const ep = js.present ? '/models/uninstall' : '/models/install';
                let body = {};
                if (js.present) {
                  // Uninstall: use existing format
                  body = { directory: js.folder, name: (js.path || '').split(/[\\/]/).pop() };
                } else if (url && directory && filename) {
                  // Install: use new structured format
                  body = { 
                    name: filename,
                    directory: directory, 
                    url: normalizeDownloadUrl(url)
                  };
                  
                  // Add user-selected path if dropdown is visible and has selection
                  if (pathSelector.style.display !== 'none' && pathSelector.value) {
                    body.path = pathSelector.value;
                  }
                } else {
                  throw new Error('Insufficient information to install');
                }

                if (installing) {
                  // Disable immediately; only show progress after server accepts
                  btn.textContent = 'Downloading…';
                  btn.disabled = true;
                  pathSelector.disabled = true; // Disable path selector during install
                } else {
                  setBusy(true);
                }

                const r = await fetch(ep, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
                const jr = await r.json().catch(() => ({}));
                console.debug('[Model Installer] POST result', { status: r.status, jr });

                if (!r.ok) {
                  // Handle HF auth flow
                  if (r.status === 401 && jr && jr.error_code === 'auth_required') {
                    const token = await openHfTokenDialog();
                    if (token && token.trim().length > 0) {
                      const lr = await fetch('/auth/hf_login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token.trim() }) });
                      const lj = await lr.json().catch(() => ({}));
                      if (lr.ok) {
                        // Retry install once after successful login
                        const rr = await fetch(ep, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
                        const rj = await rr.json().catch(() => ({}));
                        console.debug('[Model Installer] POST retry result', { status: rr.status, rj });
                        if (!rr.ok) {
                          throw new Error(rj.error || 'operation failed');
                        }
                        // Server accepted: set downloading state and start progress immediately
                        btn.textContent = 'Downloading…';
                        btn.disabled = true;
                        const key = `${directory}/${filename}`;
                        console.debug('[Model Installer] progress start (after login)', { key });
                        startDownloadProgress({ directory, filename, url, expected: 0, onComplete: async () => { console.debug('[Model Installer] onComplete', { key }); await query(); } });
                        (async () => {
                          const exp = url ? await fetchExpected(url) : 0;
                          console.debug('[Model Installer] expected updated (after login)', { key, exp });
                          updateDownloadExpected(key, exp);
                        })();
                        return;
                      } else {
                        alert(lj.error || 'Login failed. Please run: hf auth login --token <token> --add-to-git-credential');
                      }
                    } else {
                      alert('A Hugging Face access token is required. Visit https://huggingface.co/settings/tokens');
                    }
                  } else {
                    alert(jr.error || 'operation failed');
                  }
                  // Reset UI on failure
                  if (installing) {
                    btn.textContent = 'Install';
                    btn.disabled = false;
                    pathSelector.disabled = false; // Re-enable path selector on error
                  } else {
                    setBusy(false);
                  }
                  return;
                }

                // Server accepted: start progress for install
                if (installing) {
                  // Create panel immediately, then fetch expected asynchronously and update
                  const key = `${directory}/${filename}`;
                  console.debug('[Model Installer] progress start', { key });
                  startDownloadProgress({ directory, filename, url, expected: 0, onComplete: async () => { console.debug('[Model Installer] onComplete', { key }); await query(); } });
                  (async () => {
                    const exp = url ? await fetchExpected(url) : 0;
                    console.debug('[Model Installer] expected updated', { key, exp });
                    updateDownloadExpected(key, exp);
                  })();
                } else {
                  await query();
                }
              } catch (e) {
                alert(e.message || String(e));
                if (installing) {
                  btn.textContent = 'Install';
                  btn.disabled = false;
                  pathSelector.disabled = false; // Re-enable path selector on error
                } else {
                  setBusy(false);
                }
              }
            };
          } catch (e) {
            setLabel(false);
          }
        };

        query();
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  },
});

// Helper functions for path selection
function parseModelInfo(labelText) {
  // Parse format: "text_encoders / clip_l.safetensors"
  const match = labelText.match(/^(.*?)\s*\/\s*([^\s]+\.[a-z0-9]+)\b/i);
  if (match) {
    return {
      directory: match[1].trim(),  // "text_encoders"
      name: match[2].trim()        // "clip_l.safetensors"
    };
  }
  return null;
}

// Get storage info for path selection
async function getStorageInfo() {
  try {
    const response = await fetch('/model_installer/health');
    const data = await response.json();
    return data.storage_info || {};
  } catch {
    return {};
  }
}

// Format bytes to GB
function formatGB(bytes) {
  return (bytes / (1024**3)).toFixed(1);
}

// Update path selector with available paths for specific folder type
function updatePathSelector(pathSelector, directory, storageInfo) {
  // Use storage info from status response (when model not found) or from health check
  const storage = storageInfo || {};
  const paths = storage[directory] || [];  // Use directory as folder_name
  
  if (paths.length > 1) {
    pathSelector.innerHTML = '';
    paths.forEach((pathInfo, index) => {
      const option = document.createElement('option');
      option.value = pathInfo.path;
      option.textContent = `${pathInfo.path} (${formatGB(pathInfo.available_bytes)} GB free)`;
      if (index === 0) option.selected = true; // Auto-select best (most space)
      pathSelector.appendChild(option);
    });
    pathSelector.style.display = 'block';
  } else {
    pathSelector.style.display = 'none';
  }
}

function openHfTokenDialog() {
  return new Promise((resolve) => {
    // Overlay
    const overlay = document.createElement('div');
    overlay.style.position = 'fixed';
    overlay.style.inset = '0';
    overlay.style.background = 'rgba(0,0,0,0.35)';
    overlay.style.zIndex = '1000000';

    // Dialog
    const dlg = document.createElement('div');
    dlg.className = 'p-dialog p-component';
    dlg.style.position = 'fixed';
    dlg.style.top = '50%';
    dlg.style.left = '50%';
    dlg.style.transform = 'translate(-50%, -50%)';
    dlg.style.minWidth = '420px';
    dlg.style.background = 'var(--surface-card, #1e1e1e)';
    dlg.style.borderRadius = '8px';
    dlg.style.boxShadow = '0 10px 30px rgba(0,0,0,0.4)';
    dlg.style.zIndex = '1000001';
    dlg.setAttribute('role', 'dialog');
    dlg.setAttribute('aria-modal', 'true');

    dlg.innerHTML = `
      <div class="p-dialog-header p-3">
        <span class="p-dialog-title">Hugging Face Authentication</span>
      </div>
      <div class="p-dialog-content p-3" style="display:flex;flex-direction:column;gap:.75rem;">
        <div class="text-sm">Enter your Hugging Face access token. You can create one at
          <a href="https://huggingface.co/settings/tokens" target="_blank">huggingface.co/settings/tokens</a>.
        </div>
        <input type="password" class="p-inputtext p-component" placeholder="hf_xxx token" style="width:100%;" />
        <div class="p-message p-message-error" style="display:none"><span class="p-message-text"></span></div>
      </div>
      <div class="p-dialog-footer p-3" style="display:flex;justify-content:flex-end;gap:.5rem;">
        <button class="p-button p-component p-button-text" data-action="cancel">Cancel</button>
        <button class="p-button p-component" data-action="ok">Authenticate</button>
      </div>
    `;

    const input = dlg.querySelector('input');
    const msg = dlg.querySelector('.p-message');
    const msgText = dlg.querySelector('.p-message-text');
    const destroy = () => { document.body.removeChild(overlay); document.body.removeChild(dlg); };
    const submit = async () => {
      const val = (input.value || '').trim();
      if (!val) {
        msg.style.display = '';
        msgText.textContent = 'Token is required.';
        return;
      }
      destroy();
      resolve(val);
    };
    dlg.querySelector('[data-action="cancel"]').onclick = () => { destroy(); resolve(null); };
    dlg.querySelector('[data-action="ok"]').onclick = submit;
    input.onkeydown = (e) => { if (e.key === 'Enter') submit(); };

    document.body.appendChild(overlay);
    document.body.appendChild(dlg);
    input.focus();
  });
}

async function fetchExpected(url) {
  if (!url) return 0;
  try {
    const r = await fetch(`/models/expected_size?url=${encodeURIComponent(url)}`);
    const j = await r.json();
    return j.expected || 0;
  } catch {
    return 0;
  }
}

function startDownloadProgress({ directory, filename, url, expected, onComplete }) {
  try {
    createOrUpdateProgress({ directory, filename, url, expected, onComplete });
  } catch (e) {
    console.warn('[Model Installer] progress error', e);
  }
}

function updateDownloadExpected(key, expected) {
  try {
    const panel = document.getElementById('model-install-progress-panel');
    if (!panel) return;
    const row = panel.querySelector(`[data-key="${key}"]`);
    if (!row) return;
    row.dataset.expected = String(expected || 0);
  } catch {}
}

function createOrUpdateProgress({ directory, filename, url, expected, onComplete }) {
  let panel = document.getElementById('model-install-progress-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'model-install-progress-panel';
    panel.style.position = 'fixed';
    panel.style.top = '12px';
    panel.style.right = '12px';
    panel.style.width = '320px';
    panel.style.zIndex = '100000';
    panel.className = 'p-card p-component p-3 flex flex-col gap-2';
    document.body.appendChild(panel);
  }

  const key = `${directory}/${filename}`;
  let row = panel.querySelector(`[data-key="${key}"]`);
  if (!row) {
    row = document.createElement('div');
    row.dataset.key = key;
    row.className = 'p-card p-component p-2';
    row.innerHTML = `
      <div class="flex justify-between text-sm mb-1">
        <span>${directory} / ${filename}</span>
        <button class="p-button p-component p-button-text p-button-sm" data-action="close">×</button>
      </div>
      <div class="p-progressbar p-component" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" style="height:8px">
        <div class="p-progressbar-value" style="width:0%"></div>
      </div>
      <div class="flex justify-between text-xs mt-1">
        <span class="sizes">0 MB / ? MB</span>
        <span class="percent">0%</span>
      </div>
      <div class="flex justify-between text-xs mt-1">
        <span class="eta">ETA —</span>
        <span class="rate">0 MB/s</span>
      </div>
    `;
    panel.appendChild(row);
    row.querySelector('button[data-action="close"]').onclick = () => row.remove();
  }

  const progressValue = row.querySelector('.p-progressbar-value');
  const progressBar = row.querySelector('.p-progressbar');
  const rateEl = row.querySelector('.rate');
  const percentEl = row.querySelector('.percent');
  const sizesEl = row.querySelector('.sizes');
  const etaEl = row.querySelector('.eta');

  let lastBytes = 0;
  let lastTime = Date.now();

  const timer = setInterval(async () => {
    try {
      const qs = `directory=${encodeURIComponent(directory)}&filename=${encodeURIComponent(filename)}`;
      const r = await fetch(`/models/status?${qs}`);
      const j = await r.json();
      const bytes = j.size || 0;
      const now = Date.now();
      const dt = Math.max(1, now - lastTime) / 1000;
      const db = Math.max(0, bytes - lastBytes);
      const mbps = db / (1024 * 1024) / dt;
      lastBytes = bytes;
      lastTime = now;

      const dynamicExpected = Number(row.dataset.expected || '0') || 0;
      const total = dynamicExpected || expected || 0;
      const pct = total > 0 ? Math.min(100, Math.round((bytes / total) * 100)) : 0;
      progressValue.style.width = `${pct}%`;
      if (progressBar) progressBar.setAttribute('aria-valuenow', String(pct));

      rateEl.textContent = `${mbps.toFixed(2)} MB/s`;
      percentEl.textContent = `${pct}%`;

      // Sizes and ETA
      const toMB = (n) => (n / (1024 * 1024));
      const downloadedMB = toMB(bytes);
      const totalMB = total > 0 ? toMB(total) : 0;
      sizesEl.textContent = total > 0
        ? `${downloadedMB.toFixed(2)} MB / ${totalMB.toFixed(2)} MB`
        : `${downloadedMB.toFixed(2)} MB / ? MB`;

      if (total > 0 && mbps > 0) {
        const remainingBytes = Math.max(0, total - bytes);
        const remainingSec = remainingBytes / (mbps * 1024 * 1024);
        const mm = Math.floor(remainingSec / 60);
        const ss = Math.max(0, Math.floor(remainingSec % 60));
        etaEl.textContent = `ETA ${String(mm).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
      } else {
        etaEl.textContent = 'ETA —';
      }

      if (j.present && total > 0 && bytes >= total) {
        clearInterval(timer);
        progressValue.style.width = '100%';
        percentEl.textContent = '100%';
        setTimeout(() => row.remove(), 2500);
        try { if (typeof onComplete === 'function') onComplete(); } catch {}
      }
    } catch (e) {
      console.warn('[Model Installer] progress poll error', e);
    }
  }, 2000);
}

function normalizeDownloadUrl(u) {
  if (!u) return '';
  try {
    const url = new URL(u);
    if (url.hostname.endsWith('huggingface.co')) {
      // Convert blob/tree links to resolve links
      if (url.pathname.includes('/blob/')) url.pathname = url.pathname.replace('/blob/', '/resolve/');
      if (url.pathname.includes('/tree/')) url.pathname = url.pathname.replace('/tree/', '/resolve/');
      // Add download hint when missing
      if (!url.searchParams.has('download')) url.searchParams.set('download', 'true');
      return url.toString();
    }
    return u;
  } catch {
    return u;
  }
}
