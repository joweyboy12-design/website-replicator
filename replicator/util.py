"""Small, dependency-free helpers shared by capture, storage and replay."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import socket
import zlib
from urllib.parse import urlsplit, urlunsplit

# Make sure binary/3D/WASM types are served with the MIME types browsers demand
# (WebAssembly.instantiateStreaming refuses anything but application/wasm).
for _ext, _type in {
    ".wasm": "application/wasm",
    ".mjs": "text/javascript",
    ".js": "text/javascript",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".bin": "application/octet-stream",
    ".ktx2": "image/ktx2",
    ".basis": "application/octet-stream",
    ".hdr": "application/octet-stream",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}.items():
    mimetypes.add_type(_type, _ext)

CHROMIUM_ARGS = [
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--enable-unsafe-swiftshader",  # WebGL fallback when no GPU (headless)
    "--autoplay-policy=no-user-gesture-required",
]

# Headers that must never be replayed verbatim.
#  * content-encoding/length: Playwright hands us *decoded* bytes, we recompute length.
#  * CSP / XFO / HSTS / cookies / reporting: would break or pollute localhost replay.
DROP_RESPONSE_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "content-security-policy", "content-security-policy-report-only",
    "strict-transport-security", "x-frame-options", "set-cookie", "alt-svc",
    "report-to", "nel", "public-key-pins", "expect-ct", "accept-ranges",
    "access-control-allow-origin", "access-control-allow-credentials",
    "cross-origin-resource-policy", "etag", "last-modified", "cache-control",
    "expires", "age", "pragma", "vary", "date", "server", "content-range",
}


def strip_fragment(url: str) -> str:
    s = urlsplit(url)
    return urlunsplit((s.scheme, s.netloc, s.path, s.query, ""))


def origin_of(url: str) -> str:
    s = urlsplit(url)
    return f"{s.scheme}://{s.netloc}"


def path_only(url: str) -> str:
    return urlsplit(url).path or "/"


def path_query(url: str) -> str:
    s = urlsplit(url)
    return (s.path or "/") + (f"?{s.query}" if s.query else "")


def request_key(method: str, url: str, body: bytes | None = None) -> str:
    """Stable key: METHOD + URL (+ hash of body for non-GET requests)."""
    method = method.upper()
    h = hashlib.sha1()
    h.update(method.encode())
    h.update(b" ")
    h.update(strip_fragment(url).encode("utf-8", "surrogatepass"))
    if body and method not in ("GET", "HEAD"):
        h.update(b"\n")
        h.update(hashlib.sha1(body).digest())
    return h.hexdigest()


def slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-.")
    return text[:80] or "site"


def header_value(pairs, name: str) -> str | None:
    name = name.lower()
    for k, v in pairs:
        if k.lower() == name:
            return v
    return None


def guess_mime(url: str) -> str:
    t, _ = mimetypes.guess_type(path_only(url))
    return t or "application/octet-stream"


def fulfill_headers(pairs) -> dict[str, str]:
    """Collapse a header list into a dict suitable for Playwright's route.fulfill."""
    out: dict[str, str] = {}
    for k, v in pairs:
        lk = k.lower()
        if lk in ("content-encoding", "content-length", "transfer-encoding", "connection"):
            continue
        out[lk] = f"{out[lk]}\n{v}" if lk == "set-cookie" and lk in out else (
            f"{out[lk]}, {v}" if lk in out else v)
    return out


def replay_headers(pairs, url: str) -> list[tuple[str, str]]:
    """Headers for offline serving: original minus hostile ones, plus permissive CORS."""
    out = [(k, v) for k, v in pairs if k.lower() not in DROP_RESPONSE_HEADERS and not k.startswith(":")]
    if not header_value(out, "content-type"):
        out.append(("Content-Type", guess_mime(url)))
    out += [
        ("Access-Control-Allow-Origin", "*"),
        ("Cross-Origin-Resource-Policy", "cross-origin"),
        ("Cache-Control", "no-store"),
        ("Accept-Ranges", "bytes"),
    ]
    return out


def preferred_port(name: str) -> int:
    """Deterministic port per archive, so each clone keeps its own storage origin."""
    return 20000 + zlib.crc32(name.encode()) % 20000


def pick_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("No free port available")


def human_size(n: int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def short(url: str, n: int = 90) -> str:
    return url if len(url) <= n else url[: n - 3] + "..."
