from urllib.parse import urlparse


AUTH_SERVER_IP = "106.52.97.207"
AUTH_PUBLIC_BASE_URL = f"https://{AUTH_SERVER_IP}/api"
TRUSTED_AUTH_HOSTS = {AUTH_SERVER_IP}


class AuthPolicyError(ValueError):
    pass


def normalize_auth_base_url(value):
    return str(value or "").strip().rstrip("/")


def get_auth_base_url(_config=None):
    return AUTH_PUBLIC_BASE_URL


def validate_auth_base_url(value):
    url = normalize_auth_base_url(value)
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AuthPolicyError("授权 API 必须使用 HTTPS")
    host = (parsed.hostname or "").lower()
    if host not in TRUSTED_AUTH_HOSTS:
        allowed = ", ".join(sorted(TRUSTED_AUTH_HOSTS))
        raise AuthPolicyError(f"授权 API 域名未写入客户端信任列表: {host or '<empty>'}; allowed={allowed}")
    if not parsed.path.rstrip("/").endswith("/api"):
        raise AuthPolicyError("授权 API 地址必须以 /api 结尾")
    return url
