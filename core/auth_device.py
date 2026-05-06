import hashlib
import json
import os
import platform
import secrets
from pathlib import Path

from core.paths import writable_path


INSTALL_ID_FILE = "auth_device.json"


def _default_install_id_path():
    return Path(writable_path(INSTALL_ID_FILE))


def _read_install_id(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        value = str(data.get("install_id", "")).strip()
        if len(value) >= 16:
            return value
    except Exception:
        return ""
    return ""


def _write_install_id(path, install_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump({"install_id": install_id}, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def get_or_create_install_id(path=None, token_factory=None):
    target = Path(path) if path is not None else _default_install_id_path()
    existing = _read_install_id(target)
    if existing:
        return existing
    factory = token_factory or (lambda: secrets.token_urlsafe(24))
    install_id = str(factory()).strip()
    if len(install_id) < 16:
        install_id = secrets.token_urlsafe(24)
    _write_install_id(target, install_id)
    return install_id


def _windows_machine_guid():
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _value_type = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip()
    except Exception:
        return ""


def _stable_local_parts(machine_guid=None):
    guid = _windows_machine_guid() if machine_guid is None else str(machine_guid or "")
    return [
        f"platform={platform.system().lower()}",
        f"machine_guid={guid}",
        f"computer={os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or ''}",
    ]


def build_device_hash(install_id=None, machine_guid=None, extra_parts=None):
    local_install_id = install_id or get_or_create_install_id()
    parts = [
        "YHoAutoFish-device-v1",
        f"install_id={local_install_id}",
        *_stable_local_parts(machine_guid=machine_guid),
    ]
    if extra_parts:
        parts.extend(str(part) for part in extra_parts if part is not None)
    normalized = "\n".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha256(normalized).hexdigest()


def is_valid_device_hash(value):
    text = str(value or "")
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text.lower())
