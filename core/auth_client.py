import json
import shutil
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.paths import app_base_dir, resource_path


AUTH_CA_FILENAMES = ("yho_auth_ca.pem", "yho_root_ca.pem")
AUTH_CHECK_INTERVAL_SECONDS = 60
AUTH_OFFLINE_GRACE_SECONDS = 5 * 60


class AuthClientError(Exception):
    pass


@dataclass
class GateDecision:
    allowed: bool
    status: str
    message: str


def _copy_ca_to_stable_path(source_path):
    source = Path(source_path)
    target = Path(app_base_dir()) / "certs" / source.name
    try:
        if source.resolve() == target.resolve():
            return str(source)
    except OSError:
        pass
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or source.stat().st_mtime > target.stat().st_mtime:
            shutil.copy2(source, target)
        if target.is_file():
            return str(target)
    except OSError:
        return str(source)
    return str(source)


def find_auth_ca_bundle(base_dir=None):
    if base_dir is not None:
        for filename in AUTH_CA_FILENAMES:
            path = Path(base_dir) / "certs" / filename
            if path.is_file():
                return str(path)
        return None

    stable_dir = Path(app_base_dir()) / "certs"
    for filename in AUTH_CA_FILENAMES:
        path = stable_dir / filename
        if path.is_file():
            return str(path)

    for filename in AUTH_CA_FILENAMES:
        path = Path(resource_path("certs", filename))
        if path.is_file():
            return _copy_ca_to_stable_path(path)
    return None


class AuthClient:
    def __init__(self, base_url, timeout=8, transport=None, ca_bundle_path=None):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self.ca_bundle_path = ca_bundle_path if ca_bundle_path is not None else find_auth_ca_bundle()
        self._ssl_context = None
        if not self.base_url:
            raise AuthClientError("授权服务器地址未配置")

    def _url(self, path):
        return f"{self.base_url}/{str(path).lstrip('/')}"

    def _get_ssl_context(self):
        if not self.ca_bundle_path:
            return None
        if not Path(self.ca_bundle_path).is_file():
            refreshed = find_auth_ca_bundle()
            self.ca_bundle_path = refreshed
            self._ssl_context = None
        if not self.ca_bundle_path or not Path(self.ca_bundle_path).is_file():
            raise AuthClientError("授权证书文件缺失，请重新解压完整程序包后再启动")
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context(cafile=self.ca_bundle_path)
        return self._ssl_context

    def _request(self, method, path, payload=None, token=None):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "YHoAutoFish-auth-client/1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = payload or {}
        url = self._url(path)
        if self.transport is not None:
            response = self.transport(method, url, headers, body, self.timeout)
            if isinstance(response, tuple):
                status_code, data = response
            else:
                status_code, data = 200, response
            if int(status_code) < 200 or int(status_code) >= 300:
                raise AuthClientError(str(data))
            return data

        data = None if method.upper() == "GET" else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        last_error = None
        for attempt in range(3):
            try:
                context = self._get_ssl_context()
                handlers = [urllib.request.ProxyHandler({})]
                if context is not None:
                    handlers.append(urllib.request.HTTPSHandler(context=context))
                opener = urllib.request.build_opener(*handlers)
                with opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if not raw:
                        return {}
                    return json.loads(raw.decode("utf-8"))
            except AuthClientError:
                raise
            except ssl.SSLError as exc:
                last_error = exc
                message = str(exc).lower()
                if attempt < 2 and ("eof" in message or "timed out" in message):
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise AuthClientError(f"HTTPS 证书校验失败: {exc}") from exc
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")
                raise AuthClientError(detail or str(exc)) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                reason = str(getattr(exc, "reason", exc)).lower()
                if attempt < 2 and ("eof" in reason or "timed out" in reason or "connection reset" in reason):
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise AuthClientError(str(exc)) from exc
            except TimeoutError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise AuthClientError("授权服务器请求超时") from exc
        raise AuthClientError(str(last_error) if last_error else "授权服务器请求失败")

    def start_activation(self, device_hash, install_id, app_version):
        return self._request(
            "POST",
            "/activation/start",
            {
                "device_hash": device_hash,
                "install_id": install_id,
                "app_version": app_version,
            },
        )

    def poll_activation(self, activation_id, device_hash):
        return self._request(
            "POST",
            "/activation/poll",
            {
                "activation_id": activation_id,
                "device_hash": device_hash,
            },
        )

    def check_entitlement(self, access_token, device_hash):
        return self._request(
            "POST",
            "/entitlement/check",
            {"device_hash": device_hash},
            token=access_token,
        )

    def get_entitlement_status(self, access_token, device_hash):
        return self._request(
            "POST",
            "/entitlement/status",
            {"device_hash": device_hash},
            token=access_token,
        )

    def list_public_groups(self):
        return self._request("GET", "/public/groups")


def auth_config_required(config):
    return True


def auth_offline_grace_seconds(config):
    return AUTH_OFFLINE_GRACE_SECONDS


def auth_check_interval_seconds(config):
    return AUTH_CHECK_INTERVAL_SECONDS


def decide_cached_authorization(config, state, now=None):
    if not auth_config_required(config):
        return GateDecision(True, "disabled", "来源验证未启用")
    current = time.time() if now is None else float(now)
    if state is not None and state.is_usable(current, auth_offline_grace_seconds(config)):
        return GateDecision(True, "authorized", "授权缓存有效")
    return GateDecision(False, "needs_activation", "需要完成来源验证")
