import asyncio
import json
import logging
import os
import pathlib
import random
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.parsers.common import detect_blocked_page
from app.parsers.http_client import USER_AGENTS, proxy_manager

logger = logging.getLogger(__name__)

_COOKIE_DIR = pathlib.Path(os.getenv("BROWSER_DATA_DIR", "/tmp/browser-cookies"))

# Локальный OCR для простых текстовых капч (без внешних API)
try:
    import ddddocr as _ddddocr
    _ocr = _ddddocr.DdddOcr(show_ad=False)
    _HAS_OCR = True
except Exception:
    _ocr = None
    _HAS_OCR = False


def solve_local_captcha(image_bytes: bytes) -> str:
    """Решает простую текстовую капчу локально через ddddocr (ONNX-модель, без интернета)."""
    if not _HAS_OCR or not image_bytes:
        return ""
    try:
        return _ocr.classification(image_bytes)
    except Exception:
        return ""


async def _load_saved_cookies(context, key: str) -> None:
    """Загружает сохранённые cookies из файла в контекст браузера."""
    try:
        path = _COOKIE_DIR / f"{key.replace(':', '_').replace('/', '_')}.json"
        if path.exists():
            cookies = json.loads(path.read_text())
            if cookies:
                await context.add_cookies(cookies)
    except Exception:
        pass


async def _save_cookies(context, key: str) -> None:
    """Сохраняет текущие cookies контекста на диск для переиспользования."""
    try:
        _COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        cookies = await context.cookies()
        if cookies:
            path = _COOKIE_DIR / f"{key.replace(':', '_').replace('/', '_')}.json"
            path.write_text(json.dumps(cookies))
    except Exception:
        pass


_playwright = None
_browser = None
_lock = asyncio.Lock()
_browser_semaphore = asyncio.Semaphore(int(os.getenv("BROWSER_CONCURRENCY", "3")))

# Persistent contexts: one BrowserContext per domain keeps cookies/localStorage alive
# between search page and product detail pages — the #1 anti-bot signal.
_contexts: dict[str, object] = {}        # domain -> BrowserContext
_context_browsers: dict[str, object] = {}  # domain -> browser instance (staleness check)
_context_locks: dict[str, asyncio.Lock] = {}
_context_warmed: set[str] = set()        # domains whose homepage was already visited

# Full stealth init script — patches every known automation fingerprint
_STEALTH_JS = """
(function() {
  // 1. Remove webdriver flag (primary detection vector)
  try { delete navigator.__proto__.webdriver; } catch(e) {}
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });

  // 2. Realistic plugins (Chrome has 5 PDF-related plugins)
  const fakeMimeType = (type, desc, suffixes) => {
    const m = Object.create(MimeType.prototype);
    Object.defineProperties(m, {
      type: { value: type }, description: { value: desc }, suffixes: { value: suffixes }
    });
    return m;
  };
  const fakePlugin = (name, filename, desc, mimes) => {
    const p = Object.create(Plugin.prototype);
    Object.defineProperties(p, {
      name: { value: name }, filename: { value: filename },
      description: { value: desc }, length: { value: mimes.length }
    });
    mimes.forEach((m, i) => { p[i] = m; });
    return p;
  };
  const pdfMime = fakeMimeType('application/pdf', 'Portable Document Format', 'pdf');
  const plugins = [
    fakePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdfMime]),
    fakePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdfMime]),
    fakePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdfMime]),
    fakePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdfMime]),
    fakePlugin('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format', [pdfMime]),
  ];
  const pluginArr = Object.create(PluginArray.prototype);
  plugins.forEach((p, i) => { pluginArr[i] = p; });
  Object.defineProperty(pluginArr, 'length', { value: plugins.length });
  Object.defineProperty(navigator, 'plugins', { get: () => pluginArr });

  const mimeArr = Object.create(MimeTypeArray.prototype);
  mimeArr[0] = pdfMime;
  Object.defineProperty(mimeArr, 'length', { value: 1 });
  Object.defineProperty(navigator, 'mimeTypes', { get: () => mimeArr });

  // 3. Languages
  Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });

  // 4. Hardware — realistic modern laptop values
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
  Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

  // 5. Full chrome object — checked by most anti-bot systems
  window.chrome = {
    app: {
      isInstalled: false,
      InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
      RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      getDetails: () => null,
      getIsInstalled: () => false,
      installState: () => 'not_installed',
    },
    csi: function() { return { startE: Date.now(), onloadT: Date.now(), pageT: 3, tran: 15 }; },
    loadTimes: function() {
      return {
        commitLoadTime: Date.now()/1000 - 0.3,
        connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now()/1000 - 0.1,
        finishLoadTime: Date.now()/1000,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: Date.now()/1000 - 0.5,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now()/1000 - 1.0,
        startLoadTime: Date.now()/1000 - 1.0,
        wasAlternateProtocolAvailable: true,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
      };
    },
    runtime: {},
  };

  // 6. Permissions — report consistent values
  if (navigator.permissions && navigator.permissions.query) {
    const _origPermsQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = function(desc) {
      if (desc && desc.name === 'notifications') {
        return Promise.resolve({ state: 'default', onchange: null });
      }
      return _origPermsQuery(desc);
    };
  }

  // 7. WebGL — report Intel GPU (most common in laptops)
  try {
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel(R) Iris(TM) Plus Graphics';
      return _getParam.call(this, p);
    };
  } catch(e) {}

  // 8. Screen — standard 1366×768 laptop resolution
  Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
  Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

  // 9. Network connection
  try {
    Object.defineProperty(navigator, 'connection', {
      get: () => ({ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false, onchange: null })
    });
  } catch(e) {}

  // 10. Notification permission — don't look like headless
  try {
    Object.defineProperty(Notification, 'permission', { get: () => 'default' });
  } catch(e) {}

  // 11. Hide iframe contentWindow.navigator.webdriver
  try {
    const _origGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get;
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
      get: function() {
        const win = _origGetter.call(this);
        if (win && win.navigator) {
          try { Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
        }
        return win;
      }
    });
  } catch(e) {}

  // 12. Canvas fingerprint noise — randomize slightly to defeat fingerprinting
  try {
    const _getCtx = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, attrs) {
      const ctx = _getCtx.call(this, type, attrs);
      if (ctx && type === '2d') {
        const _fillText = ctx.fillText.bind(ctx);
        ctx.fillText = function(text, x, y, maxWidth) {
          return maxWidth !== undefined ? _fillText(text, x, y + 0.00001, maxWidth) : _fillText(text, x, y + 0.00001);
        };
      }
      return ctx;
    };
  } catch(e) {}

  // 13. AudioContext fingerprint noise
  try {
    const _createBuffer = AudioContext.prototype.createBuffer;
    AudioContext.prototype.createBuffer = function(numChan, length, rate) {
      const buf = _createBuffer.call(this, numChan, length, rate);
      for (let c = 0; c < buf.numberOfChannels; c++) {
        const data = buf.getChannelData(c);
        for (let i = 0; i < data.length; i++) {
          data[i] += (Math.random() * 2 - 1) * 1e-7;
        }
      }
      return buf;
    };
  } catch(e) {}

  // 14. toString() on functions should look native
  const _nativeToString = Function.prototype.toString;
  const _patchedFuncs = new WeakMap();
  Function.prototype.toString = function() {
    if (_patchedFuncs.has(this)) return _patchedFuncs.get(this);
    return _nativeToString.call(this);
  };

  // 15. Kasada: clean CDP/Playwright artifact globals
  ['__playwright_target_id__', '__playwright', 'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
   '__cdc_asdjflasutopfhvcZLmcfl_', '__webdriver_script_fn', 'callPhantom',
   '_phantom', '__nightmare', 'domAutomation', 'domAutomationController'].forEach(function(key) {
    try { if (key in window) delete window[key]; } catch(e) {}
    try { if (key in document) delete document[key]; } catch(e) {}
  });

  // 16. outerWidth/outerHeight — headless sets them to 0, Kasada checks this
  try {
    if (!window.outerHeight || window.outerHeight < 100) {
      Object.defineProperty(window, 'outerHeight', { get: function() { return 768; }, configurable: true });
    }
    if (!window.outerWidth || window.outerWidth < 100) {
      Object.defineProperty(window, 'outerWidth', { get: function() { return 1366; }, configurable: true });
    }
  } catch(e) {}

  // 17. Battery API — headless Chrome usually lacks it; real Chrome has it
  if (!navigator.getBattery) {
    navigator.getBattery = function() {
      return Promise.resolve({
        charging: true, chargingTime: 0, dischargingTime: Infinity, level: 0.97,
        onchargingchange: null, onchargingtimechange: null,
        ondischargingtimechange: null, onlevelchange: null,
        addEventListener: function() {}, removeEventListener: function() {},
      });
    };
  }

  // 18. document.hasFocus() / visibilityState — headless window is never "focused"
  try {
    document.hasFocus = function() { return true; };
    Object.defineProperty(document, 'visibilityState', { get: function() { return 'visible'; }, configurable: true });
    Object.defineProperty(document, 'hidden', { get: function() { return false; }, configurable: true });
  } catch(e) {}

  // 19. screen.orientation — sometimes absent in headless
  try {
    if (!screen.orientation || !screen.orientation.type) {
      Object.defineProperty(screen, 'orientation', {
        get: function() { return { type: 'landscape-primary', angle: 0,
          addEventListener: function() {}, removeEventListener: function() {}, dispatchEvent: function() { return true; } }; },
        configurable: true,
      });
    }
  } catch(e) {}

  // 20. CSS.paintWorklet — present in real Chrome
  try {
    if (typeof CSS !== 'undefined' && !CSS.paintWorklet) {
      Object.defineProperty(CSS, 'paintWorklet', {
        get: function() { return { addModule: function() { return Promise.resolve(); } }; },
        configurable: true,
      });
    }
  } catch(e) {}

  // 21. navigator.userActivation — real pages show interaction history
  try {
    if (!navigator.userActivation) {
      Object.defineProperty(navigator, 'userActivation', {
        get: function() { return { hasBeenActive: true, isActive: true }; },
        configurable: true,
      });
    }
  } catch(e) {}

  // 22. window.chrome.runtime — Kasada probes extension runtime
  try {
    if (window.chrome && !window.chrome.runtime) {
      window.chrome.runtime = {
        id: undefined,
        connect: function() { return {}; },
        sendMessage: function() {},
        onMessage: { addListener: function() {}, removeListener: function() {} },
      };
    }
  } catch(e) {}
})();
"""


@dataclass
class BrowserResult:
    html: str = ""
    json_payloads: list[dict | list] = field(default_factory=list)
    product_payloads: list[dict | list] = field(default_factory=list)
    status: str = "empty"
    errorReason: str = ""
    # counters for logging
    xhr_payloads: int = 0
    xhr_product_payloads: int = 0
    page_loaded: bool = False
    status_code: int = 0


async def get_browser():
    global _playwright, _browser
    async with _lock:
        if _browser and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError("Playwright is not installed in this runtime image") from exc
        _playwright = await async_playwright().start()
        _chromium_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
        _headless_env = os.getenv("PLAYWRIGHT_HEADLESS", "true").strip().lower()
        _headless = _headless_env not in ("0", "false", "no")
        _browser = await _playwright.chromium.launch(
            headless=_headless,
            executable_path=_chromium_path,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-automation",
                "--exclude-switches=enable-automation",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-popup-blocking",
                "--disable-save-password-bubble",
                "--disable-translate",
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=ru-RU",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer",
                "--enable-features=NetworkService,NetworkServiceInProcess",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1366,768",
                "--start-maximized",
            ],
        )
        return _browser


def _browser_proxy() -> dict | None:
    proxy = proxy_manager.get()
    if not proxy:
        return None
    parsed = urlparse(proxy)
    result: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


async def _get_context(domain: str, use_proxy: bool = True):
    """Get or create a persistent BrowserContext for domain.

    Cookies and localStorage survive across calls, so the site sees a returning
    user instead of a new bot fingerprint on every request.
    """
    # Key includes proxy mode so direct and proxy contexts are separate
    ctx_key = f"{domain}:{'proxy' if use_proxy else 'direct'}"
    if ctx_key not in _context_locks:
        _context_locks[ctx_key] = asyncio.Lock()
    async with _context_locks[ctx_key]:
        browser = await get_browser()
        # Reuse existing context if the browser hasn't restarted
        if ctx_key in _contexts and _context_browsers.get(ctx_key) is browser:
            return _contexts[ctx_key]
        proxy = _browser_proxy() if use_proxy else None
        ua = random.choice(USER_AGENTS)
        context_kwargs: dict = {
            "user_agent": ua,
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "viewport": {"width": 1366, "height": 768},
            "screen": {"width": 1366, "height": 768},
            "color_scheme": "light",
            "java_script_enabled": True,
            "permissions": ["geolocation"],
            "extra_http_headers": {
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
        }
        if proxy:
            context_kwargs["proxy"] = proxy
        context = await browser.new_context(**context_kwargs)
        context.set_default_timeout(10_000)
        context.set_default_navigation_timeout(20_000)
        await context.add_init_script(_STEALTH_JS)
        await _load_saved_cookies(context, ctx_key)
        _contexts[ctx_key] = context
        _context_browsers[ctx_key] = browser
        return context


async def reset_context(domain: str, use_proxy: bool = True) -> None:
    """Close and delete the cached browser context for domain.

    Call this after detecting a block so the next request starts with a clean
    session instead of re-using cookies that were flagged by the antibot system.
    """
    ctx_key = f"{domain}:{'proxy' if use_proxy else 'direct'}"
    lock = _context_locks.get(ctx_key)
    if lock is None:
        return
    async with lock:
        ctx = _contexts.pop(ctx_key, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        _context_browsers.pop(ctx_key, None)
        _context_warmed.discard(ctx_key)
        try:
            path = _COOKIE_DIR / f"{ctx_key.replace(':', '_').replace('/', '_')}.json"
            if path.exists():
                path.unlink()
        except Exception:
            pass
    logger.info("[Browser] context reset for %s (was_blocked=True)", ctx_key)


def _looks_like_product_payload(data: dict | list) -> bool:
    text = json.dumps(data, ensure_ascii=False)[:250_000].lower()
    markers = ("product", "sku", "offer", "price", "товар", "model", "wareid", "cardprice", "nm_id", "market")
    return sum(1 for marker in markers if marker in text) >= 2


async def fetch_rendered_html(
    url: str,
    *,
    referer: str = "",
    warmup_url: str = "",
    region: str = "",
    wait_selectors: list[str] | None = None,
    scroll_steps: int = 3,
    use_proxy: bool = True,
    block_assets: bool = True,
    after_load_evaluate: str = "",
) -> BrowserResult:
    async with _browser_semaphore:
        domain = urlparse(url).netloc
        ctx_key = f"{domain}:{'proxy' if use_proxy else 'direct'}"
        page = None
        on_response = None
        payloads: list[dict | list] = []
        product_payloads: list[dict | list] = []
        try:
            context = await _get_context(domain, use_proxy=use_proxy)
            page = await context.new_page()

            # Referer changes per-call so set it at page level (overrides context header)
            if referer:
                await page.set_extra_http_headers({"Referer": referer})

            # Warmup: visit homepage once per domain+proxy_mode lifetime so cookies/session are established.
            # With persistent context this only runs on the very first request to each domain.
            if warmup_url and ctx_key not in _context_warmed:
                _context_warmed.add(ctx_key)
                try:
                    await page.goto(warmup_url, wait_until="domcontentloaded", timeout=10_000)
                    # Human-like: wait for page to render, then move mouse as if reading
                    await page.wait_for_timeout(random.randint(1500, 2500))
                    await page.mouse.move(
                        random.randint(150, 600), random.randint(100, 350),
                        steps=random.randint(12, 25),
                    )
                    await page.wait_for_timeout(random.randint(400, 900))
                    await page.mouse.move(
                        random.randint(400, 900), random.randint(200, 500),
                        steps=random.randint(8, 18),
                    )
                    await page.wait_for_timeout(random.randint(300, 700))
                    # Scroll down a bit then back, like a human checking the homepage
                    await page.mouse.wheel(0, random.randint(200, 500))
                    await page.wait_for_timeout(random.randint(300, 600))
                    await page.mouse.wheel(0, -random.randint(100, 300))
                    await page.wait_for_timeout(random.randint(200, 500))
                except Exception:
                    pass

            async def route_handler(route):
                try:
                    if block_assets and route.request.resource_type in {"image", "media"}:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    return

            await page.route("**/*", route_handler)

            _API_URL_MARKERS = (
                "/search", "/catalog", "/products", "/cards", "/api/",
                "json", "ajax", "graphql", "search.wb.ru", "/composer",
            )

            async def on_response(response):
                ctype = response.headers.get("content-type", "")
                resp_url = response.url
                is_api = any(m in resp_url for m in _API_URL_MARKERS)
                if "json" not in ctype and not is_api:
                    return
                try:
                    data = await response.json()
                    if isinstance(data, (dict, list)):
                        payloads.append(data)
                        if _looks_like_product_payload(data):
                            product_payloads.append(data)
                except Exception:
                    return

            page.on("response", on_response)

            try:
                response = await page.goto(url, wait_until="commit", timeout=20_000)
            except Exception as nav_exc:
                err_lower = str(nav_exc).lower()
                if any(k in err_lower for k in ("err_aborted", "aborted", "frame was detached", "net::")):
                    response = None
                else:
                    raise

            try:
                await page.mouse.move(
                    random.randint(200, 800),
                    random.randint(100, 400),
                    steps=random.randint(5, 15),
                )
            except Exception:
                pass

            selectors = wait_selectors or [
                'a[href*="/product/"]',
                'a[href*="/catalog/"]',
                '[data-zone-name*="product" i]',
                '[data-widget*="searchResults" i]',
                "article",
                ".product-card",
                ".product",
            ]
            for selector in selectors:
                try:
                    await page.wait_for_selector(selector, timeout=3_000)
                    break
                except Exception:
                    continue

            # Умный scroll: остановиться если 2 прохода без новых product XHR
            no_new_rounds = 0
            for i in range(max(0, scroll_steps)):
                prev_count = len(product_payloads)
                try:
                    await page.mouse.wheel(0, random.randint(800, 1400))
                    await page.wait_for_timeout(1_200)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2_000)
                    except Exception:
                        pass
                except Exception:
                    break
                if len(product_payloads) > prev_count:
                    no_new_rounds = 0
                else:
                    no_new_rounds += 1
                    if no_new_rounds >= 2:
                        break

            try:
                await page.wait_for_load_state("networkidle", timeout=3_000)
            except Exception:
                pass

            try:
                html = await page.content()
            except Exception:
                html = ""
            status_code = response.status if response else 0

            _counters = dict(
                xhr_payloads=len(payloads),
                xhr_product_payloads=len(product_payloads),
                page_loaded=bool(html),
                status_code=status_code,
            )

            if detect_blocked_page(html, status_code):
                if product_payloads:
                    return BrowserResult(
                        html=html, json_payloads=payloads, product_payloads=product_payloads,
                        status="ok", errorReason="challenge page but XHR product payloads captured",
                        **_counters,
                    )
                # JS challenge: give up to ~12s for the page to auto-resolve.
                # Some sites (Ozon, YM) run a proof-of-work JS challenge that
                # completes in 3–10 s and then redirects to the real page.
                html_latest = html
                for _wait_round in range(3):
                    try:
                        await page.wait_for_timeout(4_000)
                        await page.wait_for_load_state("networkidle", timeout=3_000)
                    except Exception:
                        pass
                    # XHR product data captured during challenge resolution counts as success
                    if product_payloads:
                        html_latest = await page.content()
                        _counters["page_loaded"] = bool(html_latest)
                        return BrowserResult(
                            html=html_latest, json_payloads=payloads, product_payloads=product_payloads,
                            status="ok", errorReason="challenge resolved, XHR products captured",
                            **_counters,
                        )
                    html_latest = await page.content()
                    _counters["page_loaded"] = bool(html_latest)
                    if not detect_blocked_page(html_latest, status_code):
                        return BrowserResult(html=html_latest, json_payloads=payloads, product_payloads=product_payloads, status="ok", **_counters)
                if product_payloads:
                    return BrowserResult(
                        html=html_latest, json_payloads=payloads, product_payloads=product_payloads,
                        status="ok", errorReason="challenge page but XHR product payloads captured",
                        **_counters,
                    )
                # Confirmed block — reset context so next call starts with clean session
                asyncio.ensure_future(reset_context(domain, use_proxy))
                return BrowserResult(
                    html=html_latest, json_payloads=payloads, product_payloads=product_payloads,
                    status="blocked", errorReason="CAPTCHA or access restriction after JS challenge wait",
                    **_counters,
                )

            # after_load_evaluate: run caller-supplied JS and capture result as product payload
            if after_load_evaluate:
                try:
                    eval_result = await page.evaluate(after_load_evaluate)
                    if isinstance(eval_result, (dict, list)) and eval_result:
                        payloads.append(eval_result)
                        if _looks_like_product_payload(eval_result):
                            product_payloads.append(eval_result)
                            _counters["xhr_product_payloads"] = len(product_payloads)
                        _counters["xhr_payloads"] = len(payloads)
                except Exception as eval_exc:
                    logger.debug("[Browser] after_load_evaluate failed: %s", eval_exc)

            # Сохраняем cookies после успешной загрузки (не заблокированной)
            asyncio.ensure_future(_save_cookies(page.context, ctx_key))
            return BrowserResult(
                html=html, json_payloads=payloads, product_payloads=product_payloads,
                status="ok" if html else "empty", **_counters,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("[Browser] fallback failed for %s: %s", url, exc)
            return BrowserResult(status="error", errorReason=str(exc), page_loaded=False)
        finally:
            if page:
                if on_response:
                    try:
                        page.remove_listener("response", on_response)
                    except Exception:
                        pass
                try:
                    await page.close()
                except Exception:
                    pass
            # Context is NOT closed here — it persists to keep cookies/session alive


async def new_page(context_options: dict | None = None):
    browser = await get_browser()
    context = await browser.new_context(**(context_options or {}))
    return await context.new_page()


async def close_browser() -> None:
    global _playwright, _browser
    for ctx in list(_contexts.values()):
        try:
            await ctx.close()
        except Exception:
            pass
    _contexts.clear()
    _context_browsers.clear()
    _context_locks.clear()
    _context_warmed.clear()
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
