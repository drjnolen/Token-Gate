"""Validated URL construction for the hosted wallet-verification flow."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from verification_security import is_valid_verification_session_id


DEFAULT_WALLET_CONNECT_URL = "https://alphacity.tech/verify/"
DEFAULT_PUBLIC_API_BASE_URL = "https://token-gate-bot-production.up.railway.app"
LOCAL_DEVELOPMENT_HOSTS = frozenset({"localhost", "127.0.0.1"})
PUBLIC_API_ALLOWED_HOSTS = frozenset(
    {
        "token-gate-bot-production.up.railway.app",
        "token-gate-bot.onrender.com",
    }
)


def _validate_http_url(value: str, *, label: str):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    is_local_http = parsed.scheme == "http" and host in LOCAL_DEVELOPMENT_HOSTS
    if parsed.scheme != "https" and not is_local_http:
        raise ValueError(f"{label} must use HTTPS (HTTP is allowed only for local development)")
    if not host or parsed.username or parsed.password:
        raise ValueError(f"{label} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain a query string or fragment")
    if parsed.port and not is_local_http and parsed.port != 443:
        raise ValueError(f"{label} must use the default HTTPS port")
    return parsed, host, is_local_http


def normalize_wallet_connect_url(value: str, allowed_hosts: set[str] | frozenset[str]) -> str:
    """Return a canonical, allowlisted ``/verify/`` page URL."""
    raw_value = (value or "").strip() or DEFAULT_WALLET_CONNECT_URL
    parsed, host, is_local_http = _validate_http_url(raw_value, label="WALLET_CONNECT_URL")
    normalized_allowed_hosts = {item.strip().lower() for item in allowed_hosts if item.strip()}
    if not is_local_http and host not in normalized_allowed_hosts:
        raise ValueError("WALLET_CONNECT_URL host is not allowlisted")
    if parsed.path.rstrip("/") != "/verify":
        raise ValueError("WALLET_CONNECT_URL path must be /verify/")
    if host == "www.alphacity.tech":
        host = "alphacity.tech"
    netloc = host
    if parsed.port and (is_local_http or parsed.port != 443):
        netloc = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "/verify/", "", ""))


def normalize_public_api_base_url(value: str) -> str:
    """Return a canonical backend origin used to construct API URLs."""
    parsed, host, is_local_http = _validate_http_url(value, label="PUBLIC_API_BASE_URL")
    if not is_local_http and host not in PUBLIC_API_ALLOWED_HOSTS:
        raise ValueError("PUBLIC_API_BASE_URL host is not supported by the hosted verifier")
    if parsed.path not in {"", "/"}:
        raise ValueError("PUBLIC_API_BASE_URL must be an origin without a path")
    netloc = host
    if parsed.port and (is_local_http or parsed.port != 443):
        netloc = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def normalize_api_verify_url(value: str) -> str:
    """Validate the absolute backend endpoint embedded in the page fragment."""
    parsed, host, is_local_http = _validate_http_url(value, label="api_verify_url")
    if not is_local_http and host not in PUBLIC_API_ALLOWED_HOSTS:
        raise ValueError("api_verify_url host is not supported by the hosted verifier")
    if parsed.path.rstrip("/") != "/api/verify":
        raise ValueError("api_verify_url path must be /api/verify")
    netloc = host
    if parsed.port and (is_local_http or parsed.port != 443):
        netloc = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "/api/verify", "", ""))


def build_hosted_verification_url(
    page_url: str,
    verification_session: str,
    api_verify_url: str,
) -> str:
    """Put the single-use session and API endpoint in a URL fragment."""
    if not is_valid_verification_session_id(verification_session):
        raise ValueError("verification_session is invalid")
    parsed = urlsplit(page_url)
    api_url = normalize_api_verify_url(api_verify_url)
    fragment_values = {
        "verification_session": verification_session,
        "api_verify_url": api_url,
    }
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.query,
            urlencode(fragment_values),
        )
    )
