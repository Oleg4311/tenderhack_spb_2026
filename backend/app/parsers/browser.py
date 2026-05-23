import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.parsers.common import detect_blocked_page
from app.parsers.http_client import USER_AGENTS

logger = logging.getLogger(__name__)

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
})();
"""


@dataclass
class BrowserResult:
    html: str = ""
    json_payloads: list[dict | list] = field(default_factory=list)
    product_payloads: list[dict | list] = field(default_factory=list)
    status: str = "empty"
    errorReason: str = ""


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
        _browser = await _playwright.chromium.launch(
            headless=True,
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
    proxy = os.getenv("PROXY_URL")
    if not proxy:
        proxies = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]
        proxy = random.choice(proxies) if proxies else ""
    return {"server": proxy} if proxy else None


async def _get_context(domain: str):
    """Get or create a persistent BrowserContext for domain.

    Cookies and localStorage survive across calls, so the site sees a returning
    user instead of a new bot fingerprint on every request.
    """
    if domain not in _context_locks:
        _context_locks[domain] = asyncio.Lock()
    async with _context_locks[domain]:
        browser = await get_browser()
        # Reuse existing context if the browser hasn't restarted
        if domain in _contexts and _context_browsers.get(domain) is browser:
            return _contexts[domain]
        proxy = _browser_proxy()
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
        _contexts[domain] = context
        _context_browsers[domain] = browser
        return context


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
) -> BrowserResult:
    async with _browser_semaphore:
        domain = urlparse(url).netloc
        page = None
        on_response = None
        payloads: list[dict | list] = []
        product_payloads: list[dict | list] = []
        try:
            context = await _get_context(domain)
            page = await context.new_page()

            # Referer changes per-call so set it at page level (overrides context header)
            if referer:
                await page.set_extra_http_headers({"Referer": referer})

            # Warmup: visit homepage once per domain lifetime so cookies/session are established.
            # With persistent context this only runs on the very first request to each domain.
            if warmup_url and domain not in _context_warmed:
                _context_warmed.add(domain)
                try:
                    await page.goto(warmup_url, wait_until="commit", timeout=7_000)
                    await page.wait_for_timeout(random.randint(800, 1500))
                    try:
                        await page.wait_for_load_state("networkidle", timeout=3_000)
                    except Exception:
                        pass
                except Exception:
                    pass

            async def route_handler(route):
                try:
                    if route.request.resource_type in {"image", "media"}:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    return

            await page.route("**/*", route_handler)

            async def on_response(response):
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype:
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

            for i in range(max(0, scroll_steps)):
                try:
                    scroll_amount = random.randint(800, 1400)
                    await page.mouse.wheel(0, scroll_amount)
                    await page.wait_for_timeout(random.randint(400, 900))
                except Exception:
                    break

            try:
                await page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                pass

            try:
                html = await page.content()
            except Exception:
                html = ""
            status_code = response.status if response else 0

            if detect_blocked_page(html, status_code):
                if product_payloads:
                    return BrowserResult(
                        html=html,
                        json_payloads=payloads,
                        product_payloads=product_payloads,
                        status="ok",
                        errorReason="challenge page but XHR product payloads captured",
                    )
                try:
                    await page.wait_for_timeout(3_000)
                    await page.wait_for_load_state("networkidle", timeout=4_000)
                except Exception:
                    pass
                html2 = await page.content()
                if not detect_blocked_page(html2, status_code):
                    return BrowserResult(html=html2, json_payloads=payloads, product_payloads=product_payloads, status="ok")
                if product_payloads:
                    return BrowserResult(
                        html=html2,
                        json_payloads=payloads,
                        product_payloads=product_payloads,
                        status="ok",
                        errorReason="challenge page but XHR product payloads captured",
                    )
                return BrowserResult(
                    html=html2,
                    json_payloads=payloads,
                    product_payloads=product_payloads,
                    status="blocked",
                    errorReason="CAPTCHA or access restriction after JS challenge wait",
                )

            return BrowserResult(
                html=html,
                json_payloads=payloads,
                product_payloads=product_payloads,
                status="ok" if html else "empty",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("[Browser] fallback failed for %s: %s", url, exc)
            return BrowserResult(status="error", errorReason=str(exc))
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
    _context_warmed.clear()
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
