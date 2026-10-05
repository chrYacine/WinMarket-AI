"""Pre-import isolation checks; no database connection or application import."""
import os
from pathlib import Path
from urllib.parse import urlsplit


class ProtectedTargetError(ValueError):
    pass


def protected_paths():
    paths = [Path.home() / 'Desktop' / 'WinMarket AI']
    if os.name == 'nt':
        paths += [Path('C:/WinMarketAI-Postgres'), Path('C:/WM55bis')]
    return paths


def validate_path(path, *, forbidden=None):
    resolved = Path(path).expanduser().resolve()
    for root in forbidden if forbidden is not None else protected_paths():
        root = Path(root).resolve()
        if resolved == root or root in resolved.parents or resolved in root.parents:
            raise ProtectedTargetError('Protected or overlapping filesystem target refused')
    return resolved


def validate_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ProtectedTargetError('Invalid database target') from None
    if parsed.scheme not in ('postgresql', 'postgresql+pg8000'):
        raise ProtectedTargetError('An explicit isolated PostgreSQL target is required')
    if parsed.query or parsed.fragment or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ProtectedTargetError('Only unambiguous loopback database targets are accepted')
    if port is None or port == 5432 or not parsed.path.startswith('/wm56_'):
        raise ProtectedTargetError('Protected/default port or database namespace refused')
    if not parsed.username or not parsed.username.startswith('wm56_'):
        raise ProtectedTargetError('A dedicated wm56_ database role is required')
    return url


_LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')


def validate_deployment_url(url):
    """A real, remote PostgreSQL target (Render or equivalent) — the opposite shape of
    validate_url's local-only rules, which would incorrectly reject it (non-loopback host,
    any port, provider-assigned role/database name)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ProtectedTargetError('Invalid database target') from None
    if parsed.scheme not in ('postgresql', 'postgresql+pg8000'):
        raise ProtectedTargetError('An explicit PostgreSQL target is required for deployment')
    if not parsed.hostname or parsed.hostname in _LOOPBACK_HOSTS:
        raise ProtectedTargetError('A real, non-loopback PostgreSQL host is required for deployment')
    if not parsed.username:
        raise ProtectedTargetError('An explicit database role is required for deployment')
    if not parsed.path or parsed.path == '/':
        raise ProtectedTargetError('An explicit database name is required for deployment')
    return url


def validate_deployment_base_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https':
        raise ProtectedTargetError('A public HTTPS BASE_URL is required for deployment')
    if not parsed.hostname or parsed.hostname in _LOOPBACK_HOSTS:
        raise ProtectedTargetError('A real, non-loopback BASE_URL host is required for deployment')
    return url


def validate_deployment_secrets(values):
    secret = values.get('SESSION_SECRET') or ''
    if len(secret) < 20:
        raise ProtectedTargetError('An explicit, sufficiently random SESSION_SECRET is required for deployment')


def is_deployment_mode(values):
    return (values.get('APP_ENV') or '').strip().lower() == 'production'


def validate_environment(values, root):
    validate_path(root)
    for name in ('DATA_DIR', 'OUTPUT_DIR', 'LOCAL_STORAGE_PATH', 'LOGS_DIR', 'EMBEDDING_CACHE_DIR'):
        if values.get(name):
            validate_path(values[name])
    if is_deployment_mode(values):
        # Explicit deployment mode (APP_ENV=production): a real, remote PostgreSQL target and a
        # public HTTPS BASE_URL are REQUIRED — never silently accepted as absent, never a fallback
        # to SQLite or to the local loopback rules below, which would refuse a real Render target.
        if not values.get('DATABASE_URL'):
            raise ProtectedTargetError('DATABASE_URL is required for a production deployment')
        validate_deployment_url(values['DATABASE_URL'])
        if not values.get('BASE_URL'):
            raise ProtectedTargetError('BASE_URL is required for a production deployment')
        validate_deployment_base_url(values['BASE_URL'])
        validate_deployment_secrets(values)
        return
    if values.get('DATABASE_URL'):
        validate_url(values['DATABASE_URL'])
    if values.get('BASE_URL'):
        url = urlsplit(values['BASE_URL'])
        if url.hostname not in _LOOPBACK_HOSTS or url.port in (None,8000):
            raise ProtectedTargetError('An explicit separate loopback application port is required')
