import base64
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from core.paths import writable_path


AUTH_STORE_FILE = "auth_state.dat"


@dataclass
class AuthState:
    status: str = "unknown"
    access_token: str = ""
    license_id: str = ""
    device_hash: str = ""
    qq_user_id_hash: str = ""
    expires_at: float = 0.0
    last_checked_at: float = 0.0
    activation_id: str = ""
    user_code: str = ""
    message: str = ""

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        fields = {key: data.get(key) for key in cls.__dataclass_fields__}
        state = cls(**fields)
        state.status = str(state.status or "unknown")
        state.access_token = str(state.access_token or "")
        state.license_id = str(state.license_id or "")
        state.device_hash = str(state.device_hash or "")
        state.qq_user_id_hash = str(state.qq_user_id_hash or "")
        state.activation_id = str(state.activation_id or "")
        state.user_code = str(state.user_code or "")
        state.message = str(state.message or "")
        state.expires_at = _as_float(state.expires_at)
        state.last_checked_at = _as_float(state.last_checked_at)
        return state

    def to_dict(self):
        return asdict(self)

    def is_usable(self, now=None, offline_grace_seconds=0):
        current = time.time() if now is None else float(now)
        if self.status != "authorized" or not self.access_token:
            return False
        if self.expires_at and current >= float(self.expires_at):
            return False
        if self.last_checked_at and float(self.last_checked_at) > current + 60:
            return False
        grace = max(0.0, float(offline_grace_seconds or 0))
        if grace <= 0:
            return bool(self.last_checked_at and current <= float(self.last_checked_at) + 60)
        return bool(self.last_checked_at and current <= float(self.last_checked_at) + grace)


def _as_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _default_store_path():
    return Path(writable_path(AUTH_STORE_FILE))


def _protect_bytes(data):
    if os.name != "nt":
        return False, data
    try:
        import win32crypt

        return True, win32crypt.CryptProtectData(data, "YHoAutoFish auth", None, None, None, 0)
    except Exception:
        return False, data


def _unprotect_bytes(data, protected):
    if not protected:
        return data
    try:
        import win32crypt

        _description, plain = win32crypt.CryptUnprotectData(data, None, None, None, 0)
        return plain
    except Exception:
        return b""


def save_auth_state(state, path=None):
    target = Path(path) if path is not None else _default_store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    protected, payload = _protect_bytes(raw)
    envelope = {
        "version": 1,
        "protected": protected,
        "payload": base64.b64encode(payload).decode("ascii"),
    }
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(envelope, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, target)


def load_auth_state(path=None):
    target = Path(path) if path is not None else _default_store_path()
    if not target.exists():
        return AuthState()
    try:
        with open(target, "r", encoding="utf-8") as file:
            envelope = json.load(file)
        if os.name == "nt" and not bool(envelope.get("protected", False)):
            return AuthState(status="unknown", message="本地授权缓存未受系统保护")
        payload = base64.b64decode(str(envelope.get("payload", "")))
        raw = _unprotect_bytes(payload, bool(envelope.get("protected", False)))
        if not raw:
            return AuthState(status="unknown", message="本地授权缓存无法解密")
        return AuthState.from_dict(json.loads(raw.decode("utf-8")))
    except Exception as exc:
        return AuthState(status="unknown", message=f"本地授权缓存读取失败: {exc}")


def clear_auth_state(path=None):
    target = Path(path) if path is not None else _default_store_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass
