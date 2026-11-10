"""Capture engine: drives Chromium with Playwright and records every HTTP exchange.

Every request (documents, scripts, workers, fetch/XHR, WASM, GLB, media...) is
routed through ``context.route``. We perform the request ourselves with
``route.fetch()``, store the raw response bytes, then fulfil the browser with the
very same bytes - so what is archived is exactly what the page consumed.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from .store import Archive, default_root
from .util import (CHROMIUM_ARGS, fulfill_headers, human_size, origin_of, short,
                   slugify, strip_fragment)

Log = Callable[[str], None]

# Conditional / partial request headers would give us 304s or 206 fragments.
STRIP_REQUEST_HEADERS = {"range", "if-range", "if-none-match", "if-modified-since"}
SKIP_LINK_EXT = (".pdf", ".zip", ".rar", ".7z", ".exe", ".msi", ".dmg", ".iso", ".mp4",
                 ".mp3", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".apk")

AUTO_SCROLL_JS = """
async () => {
  const step = Math.max(200, Math.floor(window.innerHeight * 0.8));
  let y = 0, idle = 0, lastH = 0;
  while (y < 80000 && idle < 3) {
    const h = Math.max(document.body ? document.body.scrollHeight : 0,
                       document.documentElement.scrollHeight);
    window.scrollBy(0, step);
    // also scroll any large inner scroll containers (SPA layouts)
    document.querySelectorAll('*').forEach(el => {
      if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 200) el.scrollTop += step;
    });
    y += step;
    idle = (h === lastH && y >= h) ? idle + 1 : 0;
    lastH = h;
    await new Promise(r => setTimeout(r, 90));
  }
  window.scrollTo(0, 0);
}
"""

EAGER_JS = """
() => {
  document.querySelectorAll('img[loading=lazy], iframe[loading=lazy]')
    .forEach(e => { e.loading = 'eager'; });
  // Download every declared @font-face (incl. unicode-range subsets not yet used on screen).
  try { document.fonts.forEach(f => { if (f.status === 'unloaded') f.load().catch(() => {}); }); } catch (e) {}
}
"""

# Finds every visible "clickable" thing (buttons, menus, tabs, accordions, toggles...) that has not
# been tried yet, tags it with data-rep-i and returns [{i, sig}]. Real links (<a href="/page">) are
# left to the crawler; risky buttons (submit, buy, delete, log out, accept...) are skipped.
EXPLORE_COLLECT_JS = r"""
() => {
  if (!window.__repHooked) {
    window.__repHooked = true;
    window.__repOpened = [];
    const o = window.open;
    window.open = function (u) { try { if (u) window.__repOpened.push(new URL(u, location.href).href); } catch (e) {} return null; };
  }
  const SEL = 'button, [role=button], [role=tab], [role=menuitem], [role=switch], [role=treeitem], ' +
    'summary, [aria-expanded], [aria-haspopup], [aria-controls], [data-toggle], [data-bs-toggle], ' +
    '[onclick], a[href="#"], a[href=""], a[href^="#"], a[href^="javascript:"], a:not([href]), ' +
    'label[for], input[type=checkbox], input[type=radio], li:has(> ul), [class*=dropdown], ' +
    '[class*=accordion] > *, [class*=toggle], [class*=tab]:not(table):not(tbody), [class*=menu-item]';
  const RISKY = /logg?\s*ut|log\s*out|sign\s*out|slett|delete|remove|fjern|kjøp|buy|betal|\bpay|checkout|bestill|order|\bsend|submit|abonn|subscri|kanseller|cancel|godta|aksepter|accept|agree|samtykk|allow all|tillat alle/i;
  const out = [];
  let n = window.__repN || 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (el.hasAttribute('data-rep-i') || out.length >= 400) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.pointerEvents === 'none') continue;
    if (el.closest('[disabled], [aria-disabled=true]')) continue;
    if ((el.type === 'submit' || el.type === 'image') && el.form) continue;
    const a = el.closest('a[href]');
    if (a && el.tagName !== 'A' && !/^(#|javascript:|$)/i.test(a.getAttribute('href') || '')) continue;
    const text = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
                  (el.title || '') + ' ' + (el.value || '')).trim().slice(0, 60);
    if (RISKY.test(text)) continue;
    el.setAttribute('data-rep-i', String(++n));
    out.push({ i: n, sig: el.tagName + '|' + (typeof el.className === 'string' ? el.className : '') + '|' + text });
  }
  window.__repN = n;
  return out;
}
"""

# Clicks one tagged element with synthetic hover + press events. Far faster than Playwright's
# actionability-checked clicks (no scrolling, no waiting for stability).
CLICK_JS = r"""
(i) => {
  const el = document.querySelector('[data-rep-i="' + i + '"]');
  if (!el || !el.isConnected) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  const o = { bubbles: true, cancelable: true, composed: true, view: window,
              clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, button: 0 };
  const fire = (t) => el.dispatchEvent(t.startsWith('pointer') ? new PointerEvent(t, o) : new MouseEvent(t, o));
  ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointermove', 'mousemove'].forEach(fire);
  try { el.focus({ preventScroll: true }); } catch (e) {}
  ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach(fire);
  el.click();
  return true;
}
"""

HAS_CANVAS_JS = "() => !!document.querySelector('canvas, model-viewer, video')"

# Analytics/ads/trackers: useless offline and they keep the network busy (polling, beacons),
# which makes every "wait until quiet" take its maximum time. Answered with an empty 204.
TRACKER_RE = re.compile(
    r"(^|\.)(google-analytics\.com|googletagmanager\.com|doubleclick\.net|googlesyndication\.com|"
    r"googleadservices\.com|facebook\.net|connect\.facebook\.com|hotjar\.com|hotjar\.io|clarity\.ms|"
    r"segment\.(io|com)|mixpanel\.com|siteimprove(analytics)?\.(com|net)|newrelic\.com|nr-data\.net|"
    r"sentry\.io|fullstory\.com|mouseflow\.com|quantserve\.com|scorecardresearch\.com|"
    r"adservice\.google\.[a-z.]+|bat\.bing\.com|snap\.licdn\.com|ads\.linkedin\.com|"
    r"analytics\.tiktok\.com|plausible\.io|matomo\.cloud|hs-analytics\.net|"
    r"criteo\.(com|net)|taboola\.com|outbrain\.com)$", re.I)

CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)|@import\s+['"]([^'"]+)['"]""", re.I)


@dataclass
class CaptureOptions:
    url: str
    name: str = ""
    depth: int = 0              # 0 = only the start page, 1 = + linked same-origin pages ...
    max_pages: int = 25
    wait_seconds: float = 8.0   # extra settle time per page (WebGL/canvas scenes)
    timeout_seconds: float = 60.0
    headless: bool = False
    scroll: bool = True
    max_asset_mb: float = 512.0
    quiet_seconds: float = 2.0  # network must be idle this long before a page is "done"
    workers: int = 8            # linked pages captured in parallel (tabs)
    fast_subpages: bool = True  # short waits on linked pages; the start page always gets full waits
    click_all: bool = True      # hover + click every button/menu/tab so content they load is archived
    block_trackers: bool = True # skip analytics/ads (not needed offline, and they slow every wait down)


def normalize_url(url: str) -> str:
    """Accepts 'igloo.inc', 'https:/igloo.inc', 'https://https://igloo.inc' etc."""
    url = url.strip().strip('"\'<>')
    m = re.match(r"^\s*((?:https?:/*)+)", url, re.I)
    scheme = "https"
    if m:
        last = re.findall(r"https?", m.group(1), re.I)[-1].lower()
        scheme = last
        url = url[m.end():]
    return f"{scheme}://{url.lstrip('/')}"


def default_name(url: str) -> str:
    host = urlsplit(url).hostname or "site"
    return f"{slugify(host)}-{time.strftime('%Y%m%d-%H%M%S')}"


async def capture(opts: CaptureOptions, log: Log = print,
                  stop_event: threading.Event | None = None, root=None) -> Archive:
    start_url = normalize_url(opts.url)
    name = slugify(opts.name) if opts.name.strip() else default_name(start_url)
    archive = Archive.create(root or default_root(), name)
    conn = archive.connect()
    max_bytes = int(opts.max_asset_mb * 1024 * 1024)
    st = {"inflight": 0, "last": time.monotonic(), "requests": 0, "stored": 0,
          "bytes": 0, "failed": 0, "too_big": 0}
    stopped = lambda: stop_event is not None and stop_event.is_set()
    timeout_ms = opts.timeout_seconds * 1000

    log(f"Archive '{name}' -> {archive.dir}")

    async def on_route(route):
        req = route.request
        url = req.url
        if not url.startswith(("http://", "https://")):
            with suppress(Exception):
                await route.continue_()
            return
        if opts.block_trackers and TRACKER_RE.search(urlsplit(url).hostname or ""):
            with suppress(Exception):
                if route.request.method == "GET":  # archive the empty answer so replay has no misses
                    archive.put(conn, method="GET", url=url, status=204, status_text="No Content",
                                req_headers=[], resp_headers=[("content-type", "text/javascript")],
                                body=b"", resource_type="blocked-tracker")
                await route.fulfill(status=204, body=b"")
            return
        st["inflight"] += 1
        st["requests"] += 1
        st["last"] = time.monotonic()
        ps = None  # per-tab network state, so parallel tabs can each detect "quiet"
        with suppress(Exception):
            ps = page_state.get(req.frame.page)
        if ps:
            ps["inflight"] += 1
            ps["last"] = time.monotonic()
        try:
            if ps and ps.get("exploring") and req.is_navigation_request() and req.frame == req.frame.page.main_frame:
                # A clicked button tried to leave the page: remember where it goes (crawler picks it up)
                # and answer 204 No Content, which makes the browser stay on the current page.
                ps["nav"].append(url)
                await route.fulfill(status=204, body=b"")
                return
            headers = await req.all_headers()
            override = None
            if STRIP_REQUEST_HEADERS & headers.keys():
                override = {k: v for k, v in headers.items()
                            if k not in STRIP_REQUEST_HEADERS and not k.startswith(":")}
            resp = body = None
            err = ""
            for attempt in range(3):  # retry hiccups and rate limiting (429/503)
                try:
                    resp = await route.fetch(headers=override, timeout=timeout_ms)
                    body = await resp.body()          # raw bytes - never decoded as text
                    if resp.status in (429, 502, 503, 504) and attempt < 2:
                        err = f"HTTP {resp.status}"
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    break
                except Exception as e:  # DNS failure, timeout, connection reset...
                    resp, err = None, str(e).splitlines()[0][:80]
                    if attempt < 2:
                        await asyncio.sleep(1.0 * (attempt + 1))
            if resp is None:
                # Last resort: let Chromium fetch it itself and record the response when it arrives.
                log(f"  ~ retrying via browser: {short(url)} ({err})")
                passive[url] = headers
                with suppress(Exception):
                    await route.continue_()
                return

            pairs = [(h["name"], h["value"]) for h in resp.headers_array]
            if resp.status != 304:
                too_big = len(body) > max_bytes
                st["too_big"] += too_big
                common = dict(status=resp.status, status_text=resp.status_text,
                              req_headers=list(headers.items()), resp_headers=pairs,
                              body=None if too_big else body, resource_type=req.resource_type,
                              truncated=too_big)
                post = None
                with suppress(Exception):
                    post = req.post_data_buffer
                archive.put(conn, method=req.method, url=url, post_data=post, **common)
                # After redirects, also index the final URL.
                if req.method == "GET" and resp.url and strip_fragment(resp.url) != strip_fragment(url):
                    archive.put(conn, method="GET", url=resp.url, **common)
                st["stored"] += 1
                st["bytes"] += len(body)
                if st["stored"] % 25 == 0:
                    log(f"  .. {st['stored']} responses stored ({human_size(st['bytes'])})")
            await route.fulfill(status=resp.status, headers=fulfill_headers(pairs), body=body)
        except Exception as e:
            # Typically the page/frame went away while we were fetching.
            if "Target" not in str(e) and "closed" not in str(e):
                log(f"  ! route error {short(url)}: {str(e).splitlines()[0][:100]}")
        finally:
            st["inflight"] -= 1
            st["last"] = time.monotonic()
            if ps:
                ps["inflight"] -= 1
                ps["last"] = time.monotonic()

    passive: dict[str, dict] = {}  # url -> request headers, for requests the browser fetched itself
    page_state: dict = {}           # page -> {"inflight", "last"} for per-tab quiet detection

    async def on_response(resp):
        req = resp.request
        if req.url not in passive:
            return
        headers = passive.pop(req.url)
        try:
            body = await resp.body()
            pairs = [(h["name"], h["value"]) for h in await resp.headers_array()]
        except Exception as e:
            if req.method == "GET":
                failed_urls.append(req.url)
            log(f"  ! failed {short(req.url)} ({str(e).splitlines()[0][:80]}) - will retry at the end")
            return
        post = None
        with suppress(Exception):
            post = req.post_data_buffer
        archive.put(conn, method=req.method, url=req.url, post_data=post, status=resp.status,
                    status_text=resp.status_text, req_headers=list(headers.items()), resp_headers=pairs,
                    body=body if len(body) <= max_bytes else None, resource_type=req.resource_type,
                    truncated=len(body) > max_bytes)
        st["stored"] += 1
        st["bytes"] += len(body)
        log(f"  + recovered via browser: {short(req.url)}")

    failed_urls: list[str] = []

    def on_request_failed(req):
        if passive.pop(req.url, None) is not None and req.method == "GET":
            failed_urls.append(req.url)
            log(f"  ! failed {short(req.url)} ({req.failure}) - will retry at the end")

    async def retry_failed(context) -> None:
        """Final repair pass: fetch anything that still failed, a few seconds later."""
        if not failed_urls:
            return
        log(f"  retrying {len(failed_urls)} failed file(s)...")
        await asyncio.sleep(1.5)
        for u in dict.fromkeys(failed_urls):
            for attempt in range(3):
                try:
                    resp = await context.request.get(u, timeout=timeout_ms, fail_on_status_code=False)
                    body = await resp.body()
                    if resp.status in (429, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    archive.put(conn, method="GET", url=u, status=resp.status, status_text=resp.status_text,
                                req_headers=[], resp_headers=list(resp.headers.items()),
                                body=body if len(body) <= max_bytes else None, resource_type="retry",
                                truncated=len(body) > max_bytes)
                    st["stored"] += 1
                    st["bytes"] += len(body)
                    break
                except Exception as e:
                    if attempt == 2:
                        st["failed"] += 1
                        log(f"  ! gave up on {short(u)} ({str(e).splitlines()[0][:80]})")
                    else:
                        await asyncio.sleep(2 * (attempt + 1))

    async def wait_quiet(ps: dict, quiet: float, max_s: float = 30.0) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_s and not stopped():
            if ps["inflight"] <= 0 and time.monotonic() - ps["last"] >= quiet:
                return True
            await asyncio.sleep(0.2)
        return False

    async def interact(page) -> None:
        """Wiggle the mouse and wheel - many WebGL scenes lazy-load on interaction."""
        with suppress(Exception):
            vp = page.viewport_size or {"width": 1440, "height": 900}
            w, h = vp["width"], vp["height"]
            for fx, fy in ((0.5, 0.5), (0.25, 0.3), (0.75, 0.7), (0.5, 0.5)):
                await page.mouse.move(w * fx, h * fy, steps=8)
            for _ in range(6):
                await page.mouse.wheel(0, 500)
                await page.wait_for_timeout(250)
            for _ in range(6):
                await page.mouse.wheel(0, -500)
                await page.wait_for_timeout(120)

    clicked_sigs: dict[str, int] = {}  # shared across pages: the same header menu is clicked only once

    async def explore(page, ps: dict, fast: bool) -> list[str]:
        """Hover and click every button, menu, tab, accordion... so whatever they load (XHR, images,
        scripts, fonts) gets archived. Navigations are blocked and returned as extra links."""
        budget, max_clicks = (10.0, 60) if fast else (45.0, 300)
        t0 = time.monotonic()
        clicks, extra = 0, []
        ps["exploring"], ps["nav"] = True, []
        try:
            for _round in range(4):  # later rounds find things that only appeared after earlier clicks
                try:
                    items = await page.evaluate(EXPLORE_COLLECT_JS)
                except PWError:
                    break
                if not items:
                    break
                for it in items:
                    if clicks >= max_clicks or time.monotonic() - t0 > budget or stopped():
                        break
                    if clicked_sigs.get(it["sig"], 0) >= 2:
                        continue
                    clicked_sigs[it["sig"]] = clicked_sigs.get(it["sig"], 0) + 1
                    t_click = time.monotonic()
                    try:
                        if not await page.evaluate(CLICK_JS, it["i"]):
                            continue
                        clicks += 1
                    except Exception:
                        continue
                    # Only wait when the click actually started a download.
                    await asyncio.sleep(0.12)
                    if ps["inflight"] > 0 or ps["last"] > t_click:
                        await wait_quiet(ps, 0.25, 1.5 if fast else 3.0)
                    if strip_fragment(page.url) not in extra:
                        extra.append(strip_fragment(page.url))  # SPA route change via pushState
                    with suppress(Exception):
                        await page.keyboard.press("Escape")  # close modals/menus before the next click
        finally:
            ps["exploring"] = False
        with suppress(PWError):
            extra += await page.evaluate("() => window.__repOpened || []")
        extra += ps["nav"]
        if clicks:
            log(f"  clicked {clicks} button(s)/menu(s) on {short(page.url, 50)} in {time.monotonic() - t0:.0f}s")
        return extra

    async def process_page(context, url: str, depth: int, full: bool = True):
        """full=True: the start page gets every wait/interaction. Sub-pages (full=False, fast mode)
        use short waits so many can be captured in parallel."""
        fast = not full and opts.fast_subpages
        page = await context.new_page()
        ps = page_state[page] = {"inflight": 0, "last": time.monotonic()}
        page.on("websocket", lambda ws: log(f"  ! WebSocket {short(ws.url)} - live sockets cannot be archived"))
        title, links = "", []
        try:
            try:
                await page.goto(url, wait_until="load", timeout=timeout_ms)
            except PWTimeout:
                log(f"  ! 'load' timed out on {short(url, 60)} - continuing with what has arrived")
            # Our own per-tab "quiet" check instead of networkidle (ends as soon as the tab is idle).
            await wait_quiet(ps, 0.5, 4 if fast else 10)
            with suppress(PWError):
                await page.evaluate(EAGER_JS)
            heavy = False
            with suppress(PWError):
                heavy = await page.evaluate(HAS_CANVAS_JS)
            if opts.scroll and not stopped():
                if full:
                    log("  scrolling to trigger lazy content...")
                with suppress(PWError):
                    await page.evaluate(AUTO_SCROLL_JS)
                if heavy and not fast:
                    await interact(page)
            if heavy:
                # canvas/WebGL/video: give the scene its full time to stream models, textures, WASM.
                wait = min(opts.wait_seconds, 2.0) if fast else opts.wait_seconds
                if wait > 0 and not stopped():
                    if full:
                        log(f"  canvas/WebGL found - waiting {wait:g}s for 3D/WASM assets...")
                    await page.wait_for_timeout(wait * 1000)
            clicked_links = []
            if opts.click_all and not stopped():
                if full:
                    log("  clicking every button, menu and tab...")
                clicked_links = await explore(page, ps, fast)
            if not await wait_quiet(ps, 0.6 if fast else (opts.quiet_seconds if heavy else 1.0),
                                    8 if fast else 30):
                log(f"  ! network never went fully quiet on {short(url, 60)} (streaming/polling?) - moving on")
            with suppress(PWError):
                title = await page.title()
            if depth < opts.depth:
                with suppress(PWError):
                    links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                links += clicked_links
        except PWError as e:
            log(f"  ! page error: {str(e).splitlines()[0]}")
        finally:
            with suppress(Exception):
                await page.close()
            page_state.pop(page, None)
        return title, links

    async def sweep_css(context) -> None:
        """Fetch everything referenced by captured CSS (fonts, images, @imports) that the
        page never requested - e.g. font subsets or hover/mobile backgrounds."""
        tried: set[str] = set()
        for _round in range(3):
            refs: set[str] = set()
            for r in conn.execute("SELECT url, body FROM responses WHERE method='GET' AND status=200 "
                                  "AND mime LIKE '%css%' AND body IS NOT NULL"):
                text = r["body"].decode("utf-8", "replace")  # parse-only copy; stored bytes untouched
                for m in CSS_URL_RE.finditer(text):
                    ref = (m.group(2) or m.group(3) or "").strip()
                    if not ref or ref.startswith(("data:", "blob:", "#", "about:", "javascript:")):
                        continue
                    u = strip_fragment(urljoin(r["url"], ref))
                    if u.startswith(("http://", "https://")) and u not in tried:
                        refs.add(u)
            missing = [u for u in refs if archive.lookup(conn, "GET", u)[1] not in ("exact", "same-url")]
            tried |= refs
            if not missing or stopped():
                return
            log(f"  fetching {len(missing)} extra asset(s) referenced by CSS...")
            sem = asyncio.Semaphore(8)

            async def get(u):
                async with sem:
                    try:
                        resp = await context.request.get(u, timeout=timeout_ms, fail_on_status_code=False)
                        body = await resp.body()
                        if resp.status == 200 and len(body) <= max_bytes:
                            archive.put(conn, method="GET", url=u, status=200, status_text=resp.status_text,
                                        req_headers=[], resp_headers=list(resp.headers.items()), body=body,
                                        resource_type="css-ref")
                            st["stored"] += 1
                            st["bytes"] += len(body)
                    except Exception as e:
                        log(f"  ! css asset failed {short(u)} ({str(e).splitlines()[0][:60]})")

            await asyncio.gather(*(get(u) for u in missing[:1000]))

    pages_done: list[str] = []
    title = ""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=opts.headless, args=CHROMIUM_ARGS)
        context = await browser.new_context(
            service_workers="block",          # the site's own SW would hide requests from us
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
            accept_downloads=False,
        )
        await context.route("**/*", on_route)
        context.on("response", on_response)
        context.on("requestfailed", on_request_failed)
        popup_links: list[str] = []

        async def on_popup(p):
            """A clicked button opened a new tab: let it load (so it is archived), note it, close it."""
            with suppress(Exception):
                if await p.opener() is None:
                    return
                with suppress(PWError, PWTimeout):
                    await p.wait_for_load_state("load", timeout=15000)
                popup_links.append(p.url)
                await p.close()

        context.on("page", on_popup)

        start_host = (urlsplit(start_url).hostname or "").removeprefix("www.")
        seen = {strip_fragment(start_url)}
        queue: asyncio.Queue = asyncio.Queue()

        def enqueue(links, depth):
            for link in links:
                link = strip_fragment(link)
                host = (urlsplit(link).hostname or "").removeprefix("www.")
                if (link.startswith(("http://", "https://")) and host == start_host and link not in seen
                        and not link.lower().endswith(SKIP_LINK_EXT) and len(seen) < opts.max_pages):
                    seen.add(link)
                    queue.put_nowait((link, depth))

        try:
            # 1) Start page alone, with every wait and interaction.
            log(f"[page 1] {short(start_url)} (start page)")
            title, links = await process_page(context, start_url, 0, full=True)
            pages_done.append(start_url)
            if opts.depth > 0:
                enqueue(links + popup_links, 1)

            # 2) Linked pages: several tabs at the same time.
            active = 0
            t0 = time.monotonic()
            if not queue.empty():
                log(f"Crawling linked pages with {opts.workers} tabs in parallel...")

            async def worker():
                nonlocal active
                while not stopped():
                    try:
                        url, depth = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        if active == 0:
                            return
                        await asyncio.sleep(0.15)
                        continue
                    active += 1
                    try:
                        _, found = await process_page(context, url, depth, full=False)
                        pages_done.append(url)
                        log(f"[page {len(pages_done)}/{len(seen)}] {short(url)} (depth {depth})")
                        if depth < opts.depth:
                            enqueue(found, depth + 1)
                    finally:
                        active -= 1

            await asyncio.gather(*(worker() for _ in range(max(1, opts.workers))))
            if len(pages_done) > 1:
                log(f"  {len(pages_done) - 1} linked page(s) in {time.monotonic() - t0:.1f}s")
            if not stopped():
                await sweep_css(context)
                await retry_failed(context)
        finally:
            with suppress(Exception):
                await context.close()
            with suppress(Exception):
                await browser.close()

    archive.set_meta(conn, start_url=start_url, origin=origin_of(start_url), title=title,
                     created_at=time.strftime("%Y-%m-%d %H:%M:%S"), pages=pages_done,
                     capture_stats={k: v for k, v in st.items() if k not in ("inflight", "last")})
    archive.write_manifest(conn)
    archive.export_files(conn, log)
    s = archive.stats(conn)
    conn.close()
    log(f"Done{' (stopped)' if stopped() else ''}: {len(pages_done)} page(s), {s['files']} responses, "
        f"{human_size(s['bytes'])}, {st['failed']} failed, {st['too_big']} over size limit.")
    return archive


def capture_blocking(opts: CaptureOptions, log: Log = print,
                     stop_event: threading.Event | None = None) -> Archive:
    return asyncio.run(capture(opts, log, stop_event))
