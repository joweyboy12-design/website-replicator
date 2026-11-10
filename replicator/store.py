"""SQLite asset cache: one row per captured HTTP exchange, bodies stored as raw BLOBs.

Bodies are never decoded - they go in and come out as the exact byte stream the
browser received (after transfer decompression), so .glb/.wasm/.bin/audio/video
stay bit-identical.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .util import (header_value, origin_of, path_only, path_query, request_key,
                   strip_fragment)

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT UNIQUE NOT NULL,
    method        TEXT NOT NULL,
    url           TEXT NOT NULL,
    origin        TEXT NOT NULL,
    path          TEXT NOT NULL,
    pathq         TEXT NOT NULL,
    status        INTEGER NOT NULL,
    status_text   TEXT,
    req_headers   TEXT,
    resp_headers  TEXT NOT NULL,
    mime          TEXT,
    resource_type TEXT,
    post_data     BLOB,
    body          BLOB,
    body_size     INTEGER NOT NULL DEFAULT 0,
    truncated     INTEGER NOT NULL DEFAULT 0,
    captured_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_url      ON responses(method, url);
CREATE INDEX IF NOT EXISTS ix_path     ON responses(method, origin, path);
CREATE INDEX IF NOT EXISTS ix_pathq    ON responses(method, pathq);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


APP_DIR = Path(__file__).resolve().parent.parent


def default_root() -> Path:
    """Archives live in a visible 'archives' folder next to the app (override: REPLICATOR_HOME)."""
    env = os.environ.get("REPLICATOR_HOME")
    root = Path(env) / "archives" if env else APP_DIR / "archives"
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_archives(root: Path | None = None) -> list["Archive"]:
    root = root or default_root()
    items = [Archive(p) for p in root.iterdir() if (p / "archive.db").exists()]
    return sorted(items, key=lambda a: a.db_path.stat().st_mtime, reverse=True)


class Archive:
    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.db_path = self.dir / "archive.db"
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.dir.name

    @classmethod
    def create(cls, root: Path, name: str) -> "Archive":
        d = Path(root) / name
        d.mkdir(parents=True, exist_ok=True)
        a = cls(d)
        a.connect().close()
        return a

    @classmethod
    def open(cls, name_or_path: str, root: Path | None = None) -> "Archive":
        p = Path(name_or_path)
        if not (p / "archive.db").exists():
            p = (root or default_root()) / name_or_path
        if not (p / "archive.db").exists():
            raise FileNotFoundError(f"No archive at {p}")
        return cls(p)

    # ------------------------------------------------------------------ db
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        return conn

    def put(self, conn, *, method: str, url: str, status: int, status_text: str,
            req_headers, resp_headers, body: bytes | None, resource_type: str = "",
            post_data: bytes | None = None, truncated: bool = False) -> None:
        url = strip_fragment(url)
        method = method.upper()
        mime = header_value(resp_headers, "content-type") or ""
        with self._lock:
            conn.execute(
                """INSERT OR REPLACE INTO responses(key, method, url, origin, path, pathq, status,
                   status_text, req_headers, resp_headers, mime, resource_type, post_data, body,
                   body_size, truncated, captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_key(method, url, post_data), method, url, origin_of(url), path_only(url),
                 path_query(url), int(status), status_text or "", json.dumps(list(req_headers or [])),
                 json.dumps([list(p) for p in resp_headers]), mime, resource_type or "",
                 sqlite3.Binary(post_data) if post_data else None,
                 sqlite3.Binary(body) if body is not None else None,
                 len(body) if body is not None else 0, int(truncated), time.time()))
            conn.commit()

    def set_meta(self, conn, **kv) -> None:
        with self._lock:
            for k, v in kv.items():
                conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            conn.commit()

    def meta(self, conn=None) -> dict:
        own = conn is None
        conn = conn or self.connect()
        try:
            return {r["k"]: json.loads(r["v"]) for r in conn.execute("SELECT k, v FROM meta")}
        finally:
            if own:
                conn.close()

    def stats(self, conn=None) -> dict:
        own = conn is None
        conn = conn or self.connect()
        try:
            r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(body_size),0) b FROM responses").fetchone()
            return {"files": r["n"], "bytes": r["b"]}
        finally:
            if own:
                conn.close()

    def write_manifest(self, conn) -> None:
        data = self.meta(conn)
        data.update(self.stats(conn))
        (self.dir / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    # -------------------------------------------------------------- lookup
    def lookup(self, conn, method: str, url: str, body: bytes | None = None,
               prefer_origins: tuple[str, ...] = ()):
        """Find the best cached response. Returns (row | None, how)."""
        m = "GET" if method.upper() == "HEAD" else method.upper()
        url = strip_fragment(url)
        q = lambda sql, args: conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

        row = conn.execute("SELECT * FROM responses WHERE key=?", (request_key(m, url, body),)).fetchone()
        if row:
            return row, "exact"
        row = q("SELECT * FROM responses WHERE method=? AND url=?", (m, url))
        if row:
            return row, "same-url"
        # Cache-busting query strings (?t=171234...) differ every run: ignore the query.
        row = q("SELECT * FROM responses WHERE method=? AND origin=? AND path=?",
                (m, origin_of(url), path_only(url)))
        if row:
            return row, "query-ignored"
        # Root-relative URL resolved against the wrong origin (e.g. CDN css -> /fonts/x).
        pq = path_query(url)
        for o in prefer_origins:
            row = q("SELECT * FROM responses WHERE method=? AND origin=? AND pathq=?", (m, o, pq))
            if row:
                return row, "referer-origin"
        row = q("SELECT * FROM responses WHERE method=? AND pathq=?", (m, pq))
        if row:
            return row, "other-origin"
        return None, "miss"

    def start_document(self, conn):
        start = self.meta(conn).get("start_url")
        if not start:
            return None
        row, _ = self.lookup(conn, "GET", start)
        return row

    # ----------------------------------------------------------------- HAR
    def export_har(self, out_path: Path) -> Path:
        """Stream an HTTP Archive 1.2 file (bodies base64, so binaries survive)."""
        conn = self.connect()
        out_path = Path(out_path)
        try:
            with out_path.open("w", encoding="utf-8") as f:
                f.write('{"log":{"version":"1.2","creator":{"name":"Website Replicator","version":"1.0"},'
                        '"pages":[],"entries":[')
                first = True
                for r in conn.execute("SELECT * FROM responses ORDER BY id"):
                    req_h = [{"name": k, "value": v} for k, v in json.loads(r["req_headers"] or "[]")]
                    resp_h = [{"name": k, "value": v} for k, v in json.loads(r["resp_headers"])]
                    body = r["body"] or b""
                    entry = {
                        "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(r["captured_at"] or 0)),
                        "time": 0,
                        "request": {"method": r["method"], "url": r["url"], "httpVersion": "HTTP/1.1",
                                    "cookies": [], "headers": req_h, "queryString": [],
                                    "headersSize": -1, "bodySize": len(r["post_data"] or b"")},
                        "response": {"status": r["status"], "statusText": r["status_text"] or "",
                                     "httpVersion": "HTTP/1.1", "cookies": [], "headers": resp_h,
                                     "content": {"size": len(body), "mimeType": r["mime"] or "",
                                                 "text": base64.b64encode(body).decode("ascii"),
                                                 "encoding": "base64"},
                                     "redirectURL": "", "headersSize": -1, "bodySize": len(body)},
                        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
                    }
                    if r["post_data"]:
                        entry["request"]["postData"] = {
                            "mimeType": header_value(json.loads(r["req_headers"] or "[]"), "content-type") or "",
                            "text": base64.b64encode(r["post_data"]).decode("ascii"),
                            "encoding": "base64"}
                    f.write(("" if first else ",") + json.dumps(entry))
                    first = False
                f.write("]}}")
        finally:
            conn.close()
        return out_path

    # ------------------------------------------------------------ delete
    def delete(self) -> str:
        """Move the archive folder to the Windows Recycle Bin (restorable).
        Falls back to permanent removal on other systems. Returns 'recycled' or 'deleted'."""
        path = str(self.dir.resolve())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

            FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
            op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,
                                 FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                                 False, None, None)
            if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not self.dir.exists():
                return "recycled"
        import shutil
        shutil.rmtree(path)
        return "deleted"

    # ------------------------------------------------------ visible files
    def export_files(self, conn=None, log=print) -> Path:
        """Write every captured file (HTML, JS, CSS, GLB, WASM, images...) as a real file:
        <archive>/files/<host>/<path>. Bytes are copied 1:1 from the database."""
        own = conn is None
        conn = conn or self.connect()
        out = (self.dir / "files").resolve()
        # Keep every full path under Windows' 260-char limit so Explorer/editors can open it.
        budget = max(40, 245 - len(str(out)))
        index_lines, failed, n = [], [], 0
        try:
            for r in conn.execute("SELECT url, body FROM responses WHERE method='GET' AND status=200 "
                                  "AND body IS NOT NULL ORDER BY id"):
                url = r["url"]
                for rel in _candidate_paths(url, budget):
                    try:
                        _write(out / rel, r["body"])
                        index_lines.append(f"{rel.as_posix()}\t{url}")
                        n += 1
                        break
                    except OSError:  # e.g. 'a' is a file but 'a/b' needs it as a folder
                        continue
                else:
                    failed.append(url)
                    log(f"  ! could not write file for {url[:100]}")
            _write(out / "_index.tsv", ("local file\toriginal url\n" + "\n".join(index_lines) + "\n").encode("utf-8"))
        finally:
            if own:
                conn.close()
        self.write_launcher()
        log(f"Wrote {n} files to {out}" + (f" ({len(failed)} failed)" if failed else " - all files exported"))
        return out

    def write_launcher(self) -> None:
        """Make the archive folder a self-contained package that works without the app:
        a copy of the (stdlib-only) replay engine, 'Open website.bat' and an index.html."""
        import shutil
        from .util import path_query, preferred_port

        engine = self.dir / "_engine" / "replicator"
        engine.mkdir(parents=True, exist_ok=True)
        for src in Path(__file__).resolve().parent.glob("*.py"):
            shutil.copy2(src, engine / src.name)

        bat = """@echo off
chcp 65001 >nul
title Offline website - close this window to stop it
cd /d "%~dp0"
set "PYTHONPATH=%~dp0_engine"
set "PY="
if exist "%LOCALAPPDATA%\\WebsiteReplicator\\venv\\Scripts\\python.exe" set "PY=%LOCALAPPDATA%\\WebsiteReplicator\\venv\\Scripts\\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
    echo Python is needed to show this offline website.
    echo Install it from https://www.python.org/downloads/ and run this file again.
    pause
    exit /b 1
)
echo Starting the offline website... (keep this window open while you browse)
"%PY%" -m replicator serve "%~dp0."
pause
"""
        (self.dir / "Open website.bat").write_bytes(bat.replace("\n", "\r\n").encode("utf-8"))
        old = self.dir / "PLAY.bat"
        if old.exists():
            old.unlink()

        meta = self.meta()
        start = meta.get("start_url", "")
        port = preferred_port(self.name)
        title = (meta.get("title") or self.name).replace("<", "&lt;")
        html = INDEX_HTML.replace("__TITLE__", title).replace("__NAME__", json.dumps(self.name)) \
            .replace("__PORT__", str(port)).replace("__TO__", json.dumps(path_query(start) if start else "/")) \
            .replace("__URL__", start.replace("<", "&lt;"))
        (self.dir / "index.html").write_text(html, encoding="utf-8")
        (self.dir / "README.txt").write_text(
            "Open website.bat - double-click to open this website offline (keep its window open)\n"
            "index.html       - opens the website if 'Open website.bat' is running, otherwise explains what to do\n"
            "files/           - every captured file (HTML, JS, CSS, 3D models, WASM, images...)\n"
            "files/_index.tsv - which original URL each file came from\n"
            "archive.db       - the full archive (bodies + HTTP headers) that the offline server uses\n"
            "_engine/         - the small offline server (Python standard library only)\n\n"
            "This whole folder is the package: zip it, move it or copy it to another PC.\n"
            "Only Python is needed there (python.org).\n\n"
            "Why not just open files/.../index.html? Browsers block JavaScript modules, WebAssembly\n"
            "and fetch() on file:// pages, so modern sites must be served over http://.\n",
            encoding="utf-8")

    def export_package(self, zip_path: Path, log=print) -> Path:
        """Zip the whole self-contained package (everything in the archive folder)."""
        import zipfile
        self.write_launcher()
        c = self.connect()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # make archive.db complete on its own
        c.close()
        zip_path = Path(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(self.dir.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(("-wal", "-shm")):
                    z.write("\\\\?\\" + str(p.resolve()) if os.name == "nt" else p,
                            Path(self.name) / p.relative_to(self.dir))
        log(f"Package saved: {zip_path} ({zip_path.stat().st_size / 1048576:.1f} MB)")
        return zip_path


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ (offline copy)</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e6e6e6;
       font:16px/1.5 system-ui,Segoe UI,sans-serif}
  main{max-width:560px;padding:32px}
  h1{font-size:24px;margin:0 0 4px} .url{color:#8a93a6;font-size:13px;word-break:break-all}
  .card{margin-top:24px;padding:20px;border:1px solid #2a2f3a;border-radius:12px;background:#161a21}
  a.btn{display:inline-block;margin-top:12px;padding:10px 18px;border-radius:8px;background:#3d7bfd;color:#fff;
        text-decoration:none;font-weight:600} #st{color:#8a93a6}
  code{background:#232834;padding:2px 6px;border-radius:4px}
</style></head><body><main>
<h1>__TITLE__</h1><div class="url">Offline copy of __URL__</div>
<div class="card">
  <div id="st">Looking for the offline server...</div>
  <p>1. Double-click <code>Open website.bat</code> in this folder (keep its window open).<br>
     2. The website opens by itself. This page also jumps there as soon as the server is running.</p>
  <a class="btn" id="go" href="#">Open the website</a>
</div></main>
<script>
const BASE = 'http://127.0.0.1:__PORT__', NAME = __NAME__, TO = __TO__;
const target = BASE + '/__boot?to=' + encodeURIComponent(TO);
document.getElementById('go').href = target;
window.__replicator_pong = n => { if (n === NAME) location.replace(target); };
function ping() {   // a <script> tag works from file:// where fetch() is blocked
  const s = document.createElement('script');
  s.src = BASE + '/__replicator/ping.js?t=' + Date.now();
  s.onerror = () => { document.getElementById('st').textContent = 'The offline server is not running yet.'; s.remove(); };
  s.onload = () => s.remove();
  document.head.appendChild(s);
}
ping(); setInterval(ping, 1500);
</script></body></html>
"""

_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _write(path: Path, data: bytes) -> None:
    p = Path("\\\\?\\" + str(path)) if os.name == "nt" else path  # tolerate long paths
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _shorten(seg: str, keep: int) -> str:
    if len(seg) <= keep:
        return seg
    stem, ext = os.path.splitext(seg)
    h = hashlib.sha1(seg.encode()).hexdigest()[:6]
    return f"{stem[:max(1, keep - len(ext) - 7)]}~{h}{ext}"


def _candidate_paths(url: str, budget: int):
    """Readable path first, then progressively shorter ones that always fit `budget` chars."""
    full = _local_path(url)
    if len(str(full)) <= budget:
        yield full
    parts = list(full.parts)
    short = Path(*[_shorten(p, 24) for p in parts[:-1]], _shorten(parts[-1], 40))
    if len(str(short)) <= budget:
        yield short
    name = parts[-1]
    stem, ext = os.path.splitext(name)
    h = hashlib.sha1(url.encode()).hexdigest()[:10]
    room = max(0, budget - len("_long/") - len(h) - len(ext) - 1)
    yield Path("_long", f"{h}-{stem[:room]}{ext}" if room else f"{h}{ext}")


def _local_path(url: str) -> Path:
    s = urlsplit(url)
    host = _BAD_CHARS.sub("_", s.netloc) or "_"
    path = unquote(s.path or "/")
    if path.endswith("/"):
        path += "index.html"
    parts = [_BAD_CHARS.sub("_", p)[:120].rstrip(". ") or "_" for p in path.split("/")
             if p not in ("", ".", "..")]
    parts = [f"_{p}" if p.split(".")[0].lower() in _RESERVED else p for p in parts]
    if not parts:
        parts = ["index.html"]
    if s.query:
        stem, ext = os.path.splitext(parts[-1])
        parts[-1] = f"{stem}~{hashlib.sha1(s.query.encode()).hexdigest()[:8]}{ext}"
    return Path(host, *parts)
