"""The /files route: generated notes and lessons, served sandboxed (sections 12.3 and 16)."""

from pathlib import Path

LESSON_CSP = (
    "default-src 'none'; script-src 'none'; style-src 'none'; img-src data:; font-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'; "
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
)
OTHER_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"
MEDIA_TYPES = {".html": "text/html", ".md": "text/plain", ".json": "application/json"}
COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def resolve_output_file(root: Path, relative: str) -> Path | None:
    """Return the file under root named by a URL path, or None when it must not be served.

    Refuses empty, dot or dot-prefixed segments, NUL and backslashes, any symlink on the
    way (the resolved path must equal the literal one), files outside root, folders and
    extensions other than .html, .md and .json.
    """
    if not relative or "\x00" in relative or "\\" in relative:
        return None
    parts = relative.split("/")
    if any(not part or part.startswith(".") for part in parts):
        return None
    base = root.resolve()
    candidate = base.joinpath(*parts)
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if real != candidate or not real.is_relative_to(base):
        return None
    if real.suffix not in MEDIA_TYPES or not real.is_file():
        return None
    return real


def file_headers(path: Path) -> dict[str, str]:
    """Return the security headers for path: lesson CSP and same-origin framing for HTML."""
    if path.suffix == ".html":
        framing = {"Content-Security-Policy": LESSON_CSP, "X-Frame-Options": "SAMEORIGIN"}
    else:
        framing = {"Content-Security-Policy": OTHER_CSP, "X-Frame-Options": "DENY"}
    return {**COMMON_HEADERS, **framing, "Cache-Control": "no-store"}
