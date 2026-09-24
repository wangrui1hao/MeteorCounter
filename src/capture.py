"""Read-only Windows window location + visible desktop pixel capture.

No injection, process memory access, hooks, input simulation or network calls.
"""
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass

user32 = C.WinDLL('user32', use_last_error=True)
try:
    user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
except (AttributeError, OSError):
    try:
        C.WinDLL('shcore').SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass

ENUMPROC = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
user32.EnumWindows.argtypes = [ENUMPROC, W.LPARAM]
user32.IsWindowVisible.argtypes = [W.HWND]
user32.IsIconic.argtypes = [W.HWND]
user32.GetWindowTextLengthW.argtypes = [W.HWND]
user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user32.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
user32.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]
user32.WindowFromPoint.argtypes = [W.POINT]
user32.WindowFromPoint.restype = W.HWND
user32.GetAncestor.argtypes = [W.HWND, W.UINT]
user32.GetAncestor.restype = W.HWND
user32.GetForegroundWindow.restype = W.HWND


def monitor_work_area(monitor):
    """Return the selected display's usable rectangle, excluding its taskbar."""
    class MonitorInfo(C.Structure):
        _fields_=[('size',W.DWORD),('monitor',W.RECT),('work',W.RECT),('flags',W.DWORD)]
    user32.MonitorFromPoint.argtypes=[W.POINT,W.DWORD]
    user32.MonitorFromPoint.restype=W.HANDLE
    user32.GetMonitorInfoW.argtypes=[W.HANDLE,C.POINTER(MonitorInfo)]
    handle=user32.MonitorFromPoint(W.POINT(monitor['left']+1,monitor['top']+1),2)
    info=MonitorInfo();info.size=C.sizeof(info)
    if not user32.GetMonitorInfoW(handle,C.byref(info)):raise C.WinError(C.get_last_error())
    return info.work.left,info.work.top,info.work.right,info.work.bottom


@dataclass
class Target:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int
    monitor: int


def find_game(monitors):
    found = []
    @ENUMPROC
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = C.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value.strip() != '洛克王国：世界':
            return True
        rect, point = W.RECT(), W.POINT(0, 0)
        if not user32.GetClientRect(hwnd, C.byref(rect)) or not user32.ClientToScreen(hwnd, C.byref(point)):
            return True
        width, height = rect.right, rect.bottom
        if width < 600 or height < 400:
            return True
        overlaps = []
        for m in monitors[1:]:
            overlap = max(0, min(point.x + width, m['left'] + m['width']) - max(point.x, m['left'])) * max(0,
                min(point.y + height, m['top'] + m['height']) - max(point.y, m['top']))
            overlaps.append(overlap)
        if overlaps and max(overlaps) > 0:
            found.append(Target(int(hwnd), title.value.strip(), point.x, point.y, width, height,
                overlaps.index(max(overlaps)) + 1))
        return True
    user32.EnumWindows(callback, 0)
    if len(found) == 1:
        return found[0], ''
    if len(found) > 1:
        foreground = int(user32.GetForegroundWindow() or 0)
        selected = [w for w in found if w.hwnd == foreground]
        if len(selected) == 1:
            return selected[0], ''
        return None, '发现多个游戏窗口，请将要统计的游戏切到前台'
    return None, '等待游戏窗口（游戏需打开且未最小化）'


def region_visible(target, region, virtual_screen):
    x1, y1, x2, y2 = region
    l, t, r, b = target.left+x1, target.top+y1, target.left+x2, target.top+y2
    v=virtual_screen
    if l<v['left'] or t<v['top'] or r>v['left']+v['width'] or b>v['top']+v['height']:
        return False
    # Ignore input/foreground focus, allowing counting on another monitor.
    # Reject occlusion at a grid of locations in the recognition strip.
    for fx in (.05, .25, .5, .75, .95):
        for fy in (.1, .5, .9):
            hwnd=user32.WindowFromPoint(W.POINT(round(l+(r-l)*fx), round(t+(b-t)*fy)))
            root=user32.GetAncestor(hwnd, 2)
            if int(root or hwnd or 0) != target.hwnd:
                return False
    return True


def capture_region(sct, target, region):
    import numpy as np
    x1, y1, x2, y2 = region
    shot = sct.grab({'left': target.left+x1, 'top': target.top+y1, 'width': x2-x1, 'height': y2-y1})
    return np.asarray(shot)[:, :, :3].copy()
