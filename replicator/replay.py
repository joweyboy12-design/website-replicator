"""'Exact' replay: open the ORIGINAL URL in Playwright Chromium, answer every request from the archive.

Because the page runs on its real origin (https://site.com), code that inspects
``location``, origin-scoped storage or hostnames behaves exactly as it did online.
Nothing ever reaches the network: every request is fulfilled from SQLite or
answered with a 404.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import suppress
from typing import Callable

from playwright.async_api import async_playwright

from .store import Archive
from .util import CHROMIUM_ARGS, fulfill_headers, origin_of, replay_headers, short


async def replay_exact(archive: Archive, log: Callable[[str], None] = print,
                       stop_event: threading.Event | None = None) -> None:
    conn = archive.connect()
    meta = archive.meta(conn)
    start_url = meta["start_url"]
    primary = meta.get("origin") or origin_of(start_url)
    misses = 0

    async def on_route(route):
        nonlocal misses
        req = route.request
        url = req.url
        if not url.startswith(("http://", "https://")):
            with suppress(Exception):
                await route.continue_()
            return
        post = None
        with suppress(Exception):
            post = req.post_data_buffer
        prefer = [primary]
        with suppress(Exception):
            prefer.insert(0, origin_of(req.frame.url))
        row, _ = archive.lookup(conn, req.method, url, post, tuple(dict.fromkeys(prefer)))
        if row is None and req.is_navigation_request():
            row = archive.start_document(conn)
        if row is None:
            misses += 1
            log(f"  miss: {req.method} {short(url)}")
            with suppress(Exception):
                await route.fulfill(status=404, body=b"", headers={"access-control-allow-origin": "*"})
            return
        status = row["status"]
        if 300 <= status < 400:
            status = 200
        headers = fulfill_headers(replay_headers(json.loads(row["resp_headers"]), row["url"]))
        with suppress(Exception):
            await route.fulfill(status=status, headers=headers, body=row["body"] or b"")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=CHROMIUM_ARGS + ["--start-maximized"])
        context = await browser.new_context(service_workers="block", no_viewport=True,
                                            ignore_https_errors=True)
        await context.route("**/*", on_route)
        done = asyncio.Event()
        browser.on("disconnected", lambda *_: done.set())

        def on_close(*_):
            if not context.pages:
                done.set()

        context.on("page", lambda p: p.on("close", on_close))
        page = await context.new_page()
        page.on("close", on_close)
        log(f"Exact replay of '{archive.name}' -> {start_url} (offline, served from archive)")
        with suppress(Exception):
            await page.goto(start_url, wait_until="commit")
        while not done.is_set():
            if stop_event is not None and stop_event.is_set():
                break
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), 0.5)
        with suppress(Exception):
            await browser.close()
    conn.close()
    log(f"Exact replay closed ({misses} request(s) were not in the archive).")


def replay_exact_blocking(archive: Archive, log=print, stop_event=None) -> None:
    asyncio.run(replay_exact(archive, log, stop_event))
