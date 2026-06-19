import ctypes
import os
import subprocess
import sys


def is_user_admin():
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _message_box(title, message):
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def _current_launch_args():
    if getattr(sys, "frozen", False):
        return sys.executable, sys.argv[1:]
    script = os.path.abspath(sys.argv[0])
    return sys.executable, [script, *sys.argv[1:]]


def relaunch_as_admin():
    if os.name != "nt":
        return False

    executable, args = _current_launch_args()
    parameters = subprocess.list2cmdline(args)
    cwd = os.path.abspath(os.getcwd())

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(None, "runas", executable, parameters, cwd, 1)
    try:
        return int(result) > 32
    except (TypeError, ValueError):
        return False


def ensure_admin_or_relaunch(app_name="YHo AutoFish"):
    if is_user_admin():
        return True
    if relaunch_as_admin():
        return False
    _message_box(
        app_name,
        "本程序需要管理员权限才能模拟按键并控制游戏窗口。\n\n请在 Windows 权限确认窗口中选择“是”，或右键选择“以管理员身份运行”。",
    )
    return False
