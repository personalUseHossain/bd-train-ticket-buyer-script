"""
browser_session.py - drives a REAL Chrome/Edge window for login + Turnstile + API calls.

Design (so the browser and the app stay in sync in real time):
  * We start your normal Chrome/Edge ourselves (remote-debugging port + own profile
    folder) and only ATTACH to it - a genuine browser, nothing spoofed.
  * Every API call is executed INSIDE the logged-in page with fetch(). The auth token
    is re-read from the browser on every single call, so a refreshed/renewed token is
    used automatically. No copying, nothing goes stale.
  * We also listen to the site's own requests to shohoz.com and remember the
    Authorization / x-device-id / x-device-key headers it sends, as a fallback and for
    device credentials.
  * "Logged in" means: a non-expired token exists AND the page has left /login. A stale
    token left in the saved profile is ignored.
  * Turnstile (cft) tokens are generated in the same page on demand.

Requires: pip install playwright   (uses your installed Chrome/Edge)
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

LOGIN_URL = "https://eticket.railway.gov.bd/login"
SITE_HOST = "eticket.railway.gov.bd"
API_HOST = "shohoz.com"
SITEKEY = "0x4AAAAAACNkZ_TxQr_zpcZW"
DEBUG_PORT = 9222
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile")

_CFT_JS = """
async ({sitekey, timeoutMs}) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // Same as the manual console script: wait for the SITE's own Turnstile (up to 30s).
  // Never load a second copy while the site's is present or still loading (-> error 400020).
  const hasTag = () => !!document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]');
  for (let i = 0; i < 300 && !window.turnstile; i++) await sleep(100);
  if (!window.turnstile && !hasTag()) {
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      s.onload = res;
      s.onerror = () => rej(new Error('could not load turnstile script'));
      document.head.appendChild(s);
    });
    for (let i = 0; i < 100 && !window.turnstile; i++) await sleep(100);
  }
  if (!window.turnstile) throw new Error('window.turnstile not available (open a page of the site that uses it)');

  document.querySelectorAll('#cft-helper').forEach(n => n.remove());
  const box = document.createElement('div');
  box.id = 'cft-helper';
  box.style.cssText = 'position:fixed;bottom:10px;right:10px;z-index:999999;background:#fff;' +
    'padding:10px;border:2px solid #006747;border-radius:8px;font:12px monospace;' +
    'box-shadow:0 4px 12px rgba(0,0,0,.25)';
  const label = document.createElement('div');
  label.textContent = 'Getting verification token (click the box if asked)...';
  const widgetDiv = document.createElement('div');
  box.appendChild(label);
  box.appendChild(widgetDiv);
  document.body.appendChild(box);

  return await new Promise((resolve, reject) => {
    let id;
    const timer = setTimeout(() => {
      box.remove();
      reject(new Error('Turnstile timed out - no token within the time limit'));
    }, timeoutMs);
    id = window.turnstile.render(widgetDiv, {
      sitekey,
      theme: 'light',
      size: 'normal',
      callback: token => {
        clearTimeout(timer);
        window.__cft = token;
        try { sessionStorage.setItem('cft', token); } catch (e) {}
        try { window.turnstile.remove(id); } catch (e) {}
        box.remove();
        resolve(token);
      },
      'error-callback': e => {
        clearTimeout(timer);
        try { window.turnstile.remove(id); } catch (x) {}
        box.remove();
        reject(new Error('Turnstile error: ' + e));
      },
    });
  });
}
"""

_FETCH_JS = """
async ({url, method, headers, body}) => {
  const attempt = async (cred) => {
    const opts = {method, headers, credentials: cred};
    if (body !== null && body !== undefined) opts.body = body;
    const r = await fetch(url, opts);
    const h = {};
    r.headers.forEach((v, k) => { h[k] = v; });
    return {status: r.status, text: await r.text(), headers: h};
  };
  try {
    return await attempt('include');          // same as the console helper
  } catch (e1) {
    if (method !== 'GET') return {status: 0, text: 'fetch failed: ' + e1, headers: {}};
    try { return await attempt('omit'); }     // GET is safe to retry without credentials
    catch (e2) { return {status: 0, text: 'fetch failed: ' + e2, headers: {}}; }
  }
}
"""

_DEVICE_JS = """
() => {
  const out = {id: null, key: null};
  const scan = st => {
    for (let i = 0; i < st.length; i++) {
      const k = st.key(i), v = st.getItem(k) || '';
      if (!/device/i.test(k) && !/device/i.test(v.slice(0, 200))) continue;
      const m128 = v.match(/[0-9a-f]{128}/i);
      const m32 = v.match(/(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])/i);
      if (m128 && !out.key) out.key = m128[0];
      if (m32 && !out.id) out.id = m32[0];
    }
  };
  try { scan(localStorage); } catch (e) {}
  try { scan(sessionStorage); } catch (e) {}
  return out;
}
"""


# ----------------------------------------------------------------- helpers
def _find_browser():
    cands = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            cands += [
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
    elif sys.platform == "darwin":
        cands += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    for c in cands:
        if os.path.exists(c):
            return c
    raise RuntimeError("Could not find Chrome or Edge. Set BROWSER_PATH env var to its executable.")


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _clean_token(tok):
    if not tok:
        return None
    tok = tok.strip()
    if len(tok) > 1 and tok[0] == tok[-1] == '"':
        tok = tok[1:-1]
    if tok.lower().startswith("bearer "):
        tok = tok[7:]
    return tok or None


def _jwt_exp(tok):
    """exp (unix seconds) of a JWT, or None if not a JWT / no exp."""
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp")
    except Exception:  # noqa: BLE001
        return None


def _remaining(tok):
    """Seconds until expiry (None = unknown/opaque token, treated as valid)."""
    exp = _jwt_exp(tok)
    return None if exp is None else int(exp - time.time())


def _usable(tok):
    r = _remaining(tok)
    return r is None or r > 10


def _is_placeholder(v):
    return (not v) or str(v).upper().startswith("PASTE")


# ------------------------------------------------------------------ session
class BrowserSession:
    """All Playwright calls run on ONE dedicated thread (Playwright's sync API is thread-bound)."""

    def __init__(self):
        self._ex = ThreadPoolExecutor(max_workers=1)
        self._pw = None
        self._browser = None
        self._hooked = set()
        self.captured = {}   # headers seen on the site's own API requests
        self.started = False

    # ---- public (thread-safe) API -------------------------------------
    def login_and_get_token(self, timeout=900):
        """Open the login page, wait until you are really logged in, return the token."""
        return self._ex.submit(self._login, timeout).result()

    def api_request(self, method, url, params=None, body=None, extra_headers=None,
                    fallback_device_id=None, fallback_device_key=None):
        """Send an API request from inside the logged-in page.
        Returns (status, body_text, response_headers_lowercased, debug_info)."""
        return self._ex.submit(self._api_request, method, url, params, body, extra_headers,
                               fallback_device_id, fallback_device_key).result()

    def get_cft(self, timeout=120):
        """Generate a fresh Turnstile token in the logged-in page."""
        return self._ex.submit(self._cft, timeout).result()

    def close(self):
        try:
            self._ex.submit(self._close).result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._ex.shutdown(wait=False)

    # ---- internals (run on the worker thread) -------------------------
    def _connect(self):
        if self._browser and self._browser.is_connected():
            self._hook_contexts()
            return
        if not _port_open(DEBUG_PORT):
            exe = os.environ.get("BROWSER_PATH") or _find_browser()
            os.makedirs(PROFILE_DIR, exist_ok=True)
            subprocess.Popen([
                exe,
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                LOGIN_URL,
            ])
        for _ in range(60):
            if _port_open(DEBUG_PORT):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Browser did not open its debugging port.")
        if self._pw is None:
            self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        self._hooked.clear()
        self._hook_contexts()
        self.started = True

    def _hook_contexts(self):
        for ctx in self._browser.contexts:
            if id(ctx) not in self._hooked:
                ctx.on("request", self._on_request)
                self._hooked.add(id(ctx))

    def _on_request(self, req):
        """Remember the credentials the site itself sends to its API."""
        try:
            if API_HOST not in req.url:
                return
            h = req.headers
            auth = h.get("authorization", "")
            if auth.lower().startswith("bearer ") and len(auth) > 30:
                self.captured["authorization"] = auth[7:].strip()
            for k in ("x-device-id", "x-device-key"):
                if h.get(k):
                    self.captured[k] = h[k]
        except Exception:  # noqa: BLE001
            pass

    def _pump(self, ms=100):
        """Playwright's sync API only delivers events while we call into it."""
        pages = [p for c in self._browser.contexts for p in c.pages]
        if pages:
            try:
                pages[0].wait_for_timeout(ms)
            except Exception:  # noqa: BLE001
                time.sleep(ms / 1000)
        else:
            time.sleep(ms / 1000)

    def _site_pages(self):
        return [p for c in self._browser.contexts for p in c.pages if SITE_HOST in p.url]

    def _site_page(self):
        pages = self._site_pages()
        if pages:
            return pages[0]
        allp = [p for c in self._browser.contexts for p in c.pages]
        if allp:
            allp[0].goto(LOGIN_URL)
            return allp[0]
        ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        page = ctx.new_page()
        page.goto(LOGIN_URL)
        return page

    def _best_token(self):
        """Pick the usable token with the latest expiry. Returns (token, source, debug_list)."""
        cands = []  # (source, token)
        for p in self._site_pages():
            try:
                t = _clean_token(p.evaluate("localStorage.getItem('token')"))
                if t:
                    cands.append(("localStorage", t))
            except Exception:  # noqa: BLE001  (page mid-navigation)
                continue
        if self.captured.get("authorization"):
            cands.append(("site-request", self.captured["authorization"]))

        debug, best = [], None
        for src, t in cands:
            rem = _remaining(t)
            ok = _usable(t)
            debug.append({"source": src, "usable": ok, "remaining_s": rem})
            if ok:
                score = rem if rem is not None else 10 ** 9
                if best is None or score > best[0]:
                    best = (score, t, src)
        if best:
            return best[1], best[2], debug
        return None, None, debug

    def _login(self, timeout):
        self._connect()
        self._site_page()
        end = time.time() + timeout
        while time.time() < end:
            self._pump(1000)
            pages = self._site_pages()
            tok, _src, _dbg = self._best_token()
            on_login_page = (not pages) or any("/login" in p.url for p in pages)
            if tok and not on_login_page:
                return tok
        raise RuntimeError("Timed out waiting for login (no valid token / still on the login page).")

    def _api_request(self, method, url, params, body, extra_headers, fb_id, fb_key):
        self._connect()
        self._pump(5)
        page = self._site_page()

        token, src, cand_dbg = self._best_token()
        scan = {}
        if not (self.captured.get("x-device-id") and self.captured.get("x-device-key")):
            try:
                scan = page.evaluate(_DEVICE_JS) or {}
            except Exception:  # noqa: BLE001
                pass

        def pick(captured_key, scanned, fallback):
            if self.captured.get(captured_key):
                return self.captured[captured_key], "site-request"
            if scanned:
                return scanned, "storage"
            if not _is_placeholder(fallback):
                return fallback, "app-constant"
            return None, "missing"

        dev_id, id_src = pick("x-device-id", scan.get("id"), fb_id)
        dev_key, key_src = pick("x-device-key", scan.get("key"), fb_key)

        info = {
            "page_url": page.url,
            "token_source": src,
            "token_remaining_s": _remaining(token) if token else None,
            "token_candidates": cand_dbg,
            "device_id_source": id_src,
            "device_key_source": key_src,
        }
        if not token:
            return 401, "No valid (non-expired) login token found in the browser.", {}, info

        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "x-requested-with": "XMLHttpRequest",
        }
        if dev_id:
            headers["x-device-id"] = dev_id
        if dev_key:
            headers["x-device-key"] = dev_key
        if extra_headers:
            headers.update(extra_headers)

        full = f"{url}?{urlencode(params)}" if params else url
        payload = json.dumps(body) if body is not None else None
        res = page.evaluate(_FETCH_JS, {"url": full, "method": method.upper(),
                                        "headers": headers, "body": payload})
        rh = {str(k).lower(): v for k, v in (res.get("headers") or {}).items()}
        return int(res["status"]), res["text"], rh, info

    def _cft(self, timeout):
        self._connect()
        page = self._site_page()
        last = None
        for _ in range(4):  # retry on navigation or transient Turnstile errors
            try:
                return page.evaluate(_CFT_JS, {"sitekey": SITEKEY, "timeoutMs": int(timeout * 1000)})
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).lower()
                if "context was destroyed" in msg or "navigation" in msg:
                    time.sleep(1)
                    page = self._site_page()
                    continue
                if "turnstile error" in msg:
                    time.sleep(2)
                    page = self._site_page()
                    continue
                raise
        raise last

    def _close(self):
        # Leave the browser window open; just detach.
        try:
            if self._browser:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass