# -*- coding: utf-8 -*-
"""检查 AI 客户端游戏窗口是否为前台（激活）窗口。
GLFW focus 回调 → Window.onFocus → Minecraft.setWindowActive(bool)。
MouseHandler.grabMouse() 前置 isWindowActive()==true，否则静默 no-op。
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
IsWindowVisible = user32.IsWindowVisible
GetForegroundWindow = user32.GetForegroundWindow
GetWindowText = user32.GetWindowTextW
GetClassName = user32.GetClassNameW

def find_java_windows():
    """枚举所有可见窗口，返回 (hwnd, pid, title, class) 列表（java/LWJGL 窗口类名）"""
    out = []
    def cb(hwnd, lparam):
        if not IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(512)
        GetWindowText(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(512)
        GetClassName(hwnd, cls, 512)
        if pid.value:
            out.append((hwnd, pid.value, title.value, cls.value))
        return True
    EnumWindows(EnumWindowsProc(cb), 0)
    return out

def proc_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return "?"
    buf = ctypes.create_unicode_buffer(1024)
    ok = psapi.GetModuleBaseNameW(h, None, buf, 1024)
    kernel32.CloseHandle(h)
    return buf.value if ok else "?"

fg = GetForegroundWindow()
fg_pid = wintypes.DWORD()
GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
fg_title = ctypes.create_unicode_buffer(512)
GetWindowText(fg, fg_title, 512)

print("前台窗口: hwnd=%s pid=%d proc=%s title=%r" % (fg, fg_pid.value, proc_name(fg_pid.value), fg_title.value))
print()
print("所有 java 进程的可见窗口:")
for hwnd, pid, title, cls in find_java_windows():
    name = proc_name(pid)
    if "java" in name.lower() or "javaw" in name.lower():
        mark = " <== 前台" if hwnd == fg else ""
        print("  hwnd=%s pid=%d proc=%s cls=%s title=%r%s" % (hwnd, pid, name, cls, title[:60], mark))
