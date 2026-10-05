"""Single shared Jinja2Templates instance for every route module."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")


def asset_version(path: str) -> str:
    """Cache-busting token for a static asset — its own mtime.

    Browsers otherwise keep serving a stale cached copy of a CSS/JS file
    after it changes on disk, even though the server sends the new content
    on the next request. Templates append this as a `?v=` query string so
    an edited asset always gets a fresh URL.
    """
    try:
        return str(int((Path("static") / path).stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = asset_version
