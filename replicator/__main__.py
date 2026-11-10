"""Command line interface.

    python -m replicator capture https://example.com --depth 1 --wait 10
    python -m replicator serve <archive-name>          # then open the printed URL
    python -m replicator exact <archive-name>          # Playwright Chromium, original origin
    python -m replicator list
    python -m replicator har <archive-name> out.har
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser

from .store import Archive, default_root, list_archives
from .util import human_size


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="replicator")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="capture a URL into a new archive")
    c.add_argument("url")
    c.add_argument("--name", default="")
    c.add_argument("--depth", type=int, default=0)
    c.add_argument("--max-pages", type=int, default=25)
    c.add_argument("--wait", type=float, default=8.0, help="extra seconds per page for WebGL/canvas")
    c.add_argument("--timeout", type=float, default=60.0)
    c.add_argument("--headless", action="store_true")
    c.add_argument("--no-scroll", action="store_true")
    c.add_argument("--max-asset-mb", type=float, default=512.0)
    c.add_argument("--workers", type=int, default=8, help="linked pages captured in parallel")
    c.add_argument("--slow-subpages", action="store_true", help="full waits on every page (slower)")
    c.add_argument("--no-click", action="store_true", help="don't click buttons/menus/tabs")

    s = sub.add_parser("serve", help="serve an archive on localhost")
    s.add_argument("archive")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-browser", action="store_true")

    e = sub.add_parser("exact", help="replay in Playwright Chromium on the original origin")
    e.add_argument("archive")

    sub.add_parser("list", help="list archives")

    h = sub.add_parser("har", help="export an archive as a HAR file")
    h.add_argument("archive")
    h.add_argument("out")

    a = p.parse_args(argv)

    if a.cmd == "capture":
        from .capture import CaptureOptions, capture_blocking
        capture_blocking(CaptureOptions(
            url=a.url, name=a.name, depth=a.depth, max_pages=a.max_pages, wait_seconds=a.wait,
            timeout_seconds=a.timeout, headless=a.headless, scroll=not a.no_scroll,
            max_asset_mb=a.max_asset_mb, workers=a.workers, fast_subpages=not a.slow_subpages,
            click_all=not a.no_click))
    elif a.cmd == "serve":
        from .server import ReplayServer
        srv = ReplayServer(Archive.open(a.archive), port=a.port)
        url = srv.start()
        print(f"Open: {url}\nCtrl+C to stop.")
        if not a.no_browser:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
    elif a.cmd == "exact":
        from .replay import replay_exact_blocking
        replay_exact_blocking(Archive.open(a.archive))
    elif a.cmd == "list":
        print(f"Archives in {default_root()}:")
        for arc in list_archives():
            m, st = arc.meta(), arc.stats()
            print(f"  {arc.name:45} {st['files']:6} files {human_size(st['bytes']):>10}  {m.get('start_url', '?')}")
    elif a.cmd == "har":
        print(Archive.open(a.archive).export_har(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
