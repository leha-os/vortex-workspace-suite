"""
LEHA Vortex Workspace Suite (v1.2.5)
Enterprise-Grade Windows 10/11 Workspace & Window Management Engine

Architecture:
- Pure Win32 Layered AppBar (120FPS GDI+ DIBSection, zero-flicker, zero-Tkinter-frame artifacts)
- Strict 64-bit Win32 ctypes bindings (HMONITOR, HDC, HGDIOBJ, HWND)
- Independent Per-Monitor Magnetic FancyZones (F1..F6 / Alt+1..6, 2-to-4 zone merge, sticky-edge resize)
- WindowTop Pro (DWM Always-On-Top border with Virtual Desktop cloak detection, live opacity,
  ghost click-through)
- Context-Aware Quick Shell (F9 / F10)
- macOS/iOS Retina Studio UI (Bilingual EN/VI, Dark/Light)

License: MIT License - Copyright (c) 2026 Le Ha
Author: Le Ha
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import gc
import json
import math
import os
import faulthandler
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import urllib.parse
import uuid
import winreg
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# Single-instance guard
_INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "LEHA_VORTEX_WORKSPACE_SUITE_V1_MUTEX")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

# Per-Monitor V2 DPI Awareness
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass

# --- Virtual desktop layer (v1.2.0): the old library for virtual desktops
# was removed entirely (it was a suspect in the 0xc0000409 crashes on
# Windows 11 24H2, where Microsoft changed the internal desktop COM
# interfaces; the crashes continued without it, so the hunt goes on via
# crash dumps). Only stable surfaces are used now: the Explorer registry
# store for reads and renames, the documented IVirtualDesktopManager for
# per-window desktop ids, and (v1.2.3) IVirtualDesktopManagerInternal's
# SwitchDesktop to jump straight to a desktop -- the Win+Ctrl+Arrow key
# simulation remains only as a fallback (it steps through every desktop
# in between, which the user sees as flashing).
# Note (v1.2.1): on Windows 11 24H2 the VirtualDesktopManager coclass is
# not registered for direct activation, so _get_vdm obtains the manager
# through the ImmersiveShell service provider instead.
_VD_REG = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops"


def _vd_guid_list() -> List[str]:
    """Ordered desktop GUIDs (lowercase, no braces) from the registry."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _VD_REG) as key:
            raw = None
            # Windows 11 24H2 stores the list under "VirtualDesktopIDs";
            # older builds used "VirtualDesktops". Try both, in that order.
            # (Reading the wrong name silently fell back to enumerating the
            # Desktops subkeys, which keep orphaned entries for long-deleted
            # desktops: 6 pills for 4 real desktops, confirmed on the user's
            # machine 22/11/2026.)
            for value_name in ("VirtualDesktopIDs", "VirtualDesktops"):
                try:
                    raw, _ = winreg.QueryValueEx(key, value_name)
                    break
                except OSError:
                    continue
        if raw is not None:
            data = bytes(raw)
            return [str(uuid.UUID(bytes_le=data[i:i + 16])) for i in range(0, len(data) - 15, 16)]
    except Exception:
        pass
    try:
        guids: List[str] = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _VD_REG + r"\Desktops") as key:
            idx = 0
            while True:
                try:
                    guids.append(winreg.EnumKey(key, idx).strip("{}").lower())
                    idx += 1
                except OSError:
                    break
        return guids
    except Exception:
        return []


def _vd_current_guid() -> Optional[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _VD_REG) as key:
            raw, _ = winreg.QueryValueEx(key, "CurrentVirtualDesktop")
        return str(uuid.UUID(bytes_le=bytes(raw)[:16]))
    except Exception:
        return None


def _vd_desktop_name(guid: str, num: int) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _VD_REG + r"\Desktops\{" + guid + "}") as key:
            name, _ = winreg.QueryValueEx(key, "Name")
        if name and str(name).strip():
            return str(name).strip()
    except Exception:
        pass
    return f"Desktop {num}"


def _vd_desktops() -> Tuple[int, List[Tuple[int, str, str]]]:
    """(current number, [(number, name, guid), ...]) from the registry."""
    guids = _vd_guid_list()
    if not guids:
        return 1, [(1, "Desktop 1", "")]
    cur = _vd_current_guid()
    curr_num = (guids.index(cur) + 1) if cur in guids else 1
    return curr_num, [(i + 1, _vd_desktop_name(g, i + 1), g) for i, g in enumerate(guids)]


_vdm_instance: Any = None
_vdm_failed = False


def _get_vdm() -> Any:
    """IVirtualDesktopManager COM object (lazy, fail-soft, logged once).

    Two activation routes, in order:
    1. Direct CoCreateInstance of the VirtualDesktopManager coclass --
       works on Windows 10 / early Windows 11.
    2. ImmersiveShell service provider -> QueryService with the service id
       set to the IVirtualDesktopManager IID -- required on Windows 11
       24H2, where the coclass is not registered at all (both registry
       hives checked empty; direct activation returns 0x80040154 "Class
       not registered"). Confirmed working on the user's machine
       23/11/2026: counts came back {1: 2, 2: 3, 3: 1, 4: 1}.
    If both routes fail, the reason is written to vortex_error.log once --
    the per-desktop window counts must never die silently again.
    """
    global _vdm_instance, _vdm_failed
    if _vdm_instance is not None or _vdm_failed:
        return _vdm_instance
    try:
        import comtypes
        from comtypes import COMMETHOD, GUID, IUnknown
        try:
            comtypes.CoInitialize()
        except Exception:
            pass

        class IVirtualDesktopManager(IUnknown):
            _iid_ = GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")
            _methods_ = [
                COMMETHOD([], ctypes.c_long, "IsWindowOnCurrentVirtualDesktop",
                          (["in"], wintypes.HWND, "hwnd"), (["out"], ctypes.POINTER(wintypes.BOOL), "on_current")),
                COMMETHOD([], ctypes.c_long, "GetWindowDesktopId",
                          (["in"], wintypes.HWND, "hwnd"), (["out"], ctypes.POINTER(GUID), "desktop_id")),
                COMMETHOD([], ctypes.c_long, "MoveWindowToDesktop",
                          (["in"], wintypes.HWND, "hwnd"), (["in"], ctypes.POINTER(GUID), "desktop_id")),
            ]

        try:
            _vdm_instance = comtypes.CoCreateInstance(
                GUID("{AA56DE98-9B35-4BF8-9375-3DA2C635E28A}"), IVirtualDesktopManager, comtypes.CLSCTX_ALL)
        except Exception:
            class IServiceProvider(IUnknown):
                _iid_ = GUID("{6D5140C1-7436-11CE-8034-00AA006009FA}")
                _methods_ = [
                    COMMETHOD([], ctypes.c_long, "QueryService",
                              (["in"], ctypes.POINTER(GUID), "guid_service"),
                              (["in"], ctypes.POINTER(GUID), "riid"),
                              (["out"], ctypes.POINTER(ctypes.c_void_p), "ppv_object")),
                ]

            shell = comtypes.CoCreateInstance(
                GUID("{C2F03A33-21F5-47FA-B4BB-156362A2F239}"), IServiceProvider, comtypes.CLSCTX_ALL)
            # comtypes convention: the trailing [out] pointer is returned
            # by the call, not passed to it. Service id = the IID itself.
            ppv = shell.QueryService(GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}"),
                                     GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}"))
            addr = ppv.value if hasattr(ppv, "value") else int(ppv)
            if not addr:
                raise RuntimeError("ImmersiveShell QueryService returned a null pointer")
            unk = ctypes.cast(addr, comtypes.POINTER(IUnknown))
            _vdm_instance = unk.QueryInterface(IVirtualDesktopManager)
    except Exception as exc:
        _vdm_failed = True
        _log_error("vdm-init", exc)
    return _vdm_instance


_vdi_bundle: Any = None
_vdi_failed = False


def _get_vd_internal() -> Any:
    """(manager, IObjectArray, IVirtualDesktop) for the undocumented
    IVirtualDesktopManagerInternal, or None (lazy, fail-soft, logged once).

    Obtained through the same ImmersiveShell QueryService door as
    _get_vdm, with service id {C5E0CDCA-7B6E-41B2-9FC4-D93975CC467B}.
    The interface IID is build-specific -- {53F5CA0B-...} on Windows 11
    24H2 (build 26100+), {A3175F2D-...} on 23H2 (22621-26099) -- both are
    tried, in that order. It exists for ONE reason: SwitchDesktop jumps
    straight to the target desktop, while the Win+Ctrl+Arrow simulation
    steps through every desktop in between (the user sees them flash).
    Verified end-to-end on the user's machine 23/11/2026: GetCount = 4,
    array GUIDs match the registry order, direct jump 3 -> 1 -> 3
    confirmed via GetCurrentDesktop. Main thread only (COM apartment).
    """
    global _vdi_bundle, _vdi_failed
    if _vdi_bundle is not None or _vdi_failed:
        return _vdi_bundle
    try:
        import comtypes
        from comtypes import COMMETHOD, GUID, IUnknown
        try:
            comtypes.CoInitialize()
        except Exception:
            pass

        class IServiceProvider(IUnknown):
            _iid_ = GUID("{6D5140C1-7436-11CE-8034-00AA006009FA}")
            _methods_ = [
                COMMETHOD([], ctypes.c_long, "QueryService",
                          (["in"], ctypes.POINTER(GUID), "guid_service"),
                          (["in"], ctypes.POINTER(GUID), "riid"),
                          (["out"], ctypes.POINTER(ctypes.c_void_p), "ppv_object")),
            ]

        class IVirtualDesktop(IUnknown):
            _iid_ = GUID("{3F07F4BE-B107-441A-AF0F-39D82529072C}")
            _methods_ = [
                COMMETHOD([], ctypes.c_long, "IsViewVisible",
                          (["in"], ctypes.c_void_p, "view"),
                          (["out"], ctypes.POINTER(ctypes.c_int), "visible")),
                COMMETHOD([], ctypes.c_long, "GetId",
                          (["out"], ctypes.POINTER(GUID), "pId")),
                COMMETHOD([], ctypes.c_long, "GetName",
                          (["out"], ctypes.POINTER(ctypes.c_void_p), "pName")),
                COMMETHOD([], ctypes.c_long, "GetWallpaperPath",
                          (["out"], ctypes.POINTER(ctypes.c_void_p), "pPath")),
                COMMETHOD([], ctypes.c_long, "IsRemote",
                          (["out"], ctypes.POINTER(ctypes.c_int), "pRemote")),
            ]

        class IObjectArray(IUnknown):
            _iid_ = GUID("{92CA9DCD-5622-4BBA-A805-5E9F541BD8C9}")
            _methods_ = [
                COMMETHOD([], ctypes.c_long, "GetCount",
                          (["out"], ctypes.POINTER(ctypes.c_uint), "pcObjects")),
                COMMETHOD([], ctypes.c_long, "GetAt",
                          (["in"], ctypes.c_uint, "uiIndex"),
                          (["in"], ctypes.POINTER(GUID), "riid"),
                          (["out"], ctypes.POINTER(ctypes.c_void_p), "ppvObject")),
            ]

        def _make_internal_class(iid_text: str) -> Any:
            class IVirtualDesktopManagerInternal(IUnknown):
                _iid_ = GUID(iid_text)
                _methods_ = [
                    COMMETHOD([], ctypes.c_long, "GetCount",
                              (["out"], ctypes.POINTER(ctypes.c_uint), "pCount")),
                    COMMETHOD([], ctypes.c_long, "MoveViewToDesktop",
                              (["in"], ctypes.c_void_p, "pView"),
                              (["in"], ctypes.c_void_p, "pDesktop")),
                    COMMETHOD([], ctypes.c_long, "CanViewMoveDesktops",
                              (["in"], ctypes.c_void_p, "pView"),
                              (["out"], ctypes.POINTER(ctypes.c_int), "pfCan")),
                    COMMETHOD([], ctypes.c_long, "GetCurrentDesktop",
                              (["out"], ctypes.POINTER(ctypes.c_void_p), "ppDesktop")),
                    COMMETHOD([], ctypes.c_long, "GetDesktops",
                              (["out"], ctypes.POINTER(ctypes.c_void_p), "ppDesktops")),
                    COMMETHOD([], ctypes.c_long, "GetAdjacentDesktop",
                              (["in"], ctypes.c_void_p, "pReference"),
                              (["in"], ctypes.c_int, "uDirection"),
                              (["out"], ctypes.POINTER(ctypes.c_void_p), "ppDesktop")),
                    COMMETHOD([], ctypes.c_long, "SwitchDesktop",
                              (["in"], ctypes.c_void_p, "pDesktop")),
                ]

            return IVirtualDesktopManagerInternal

        shell = comtypes.CoCreateInstance(
            GUID("{C2F03A33-21F5-47FA-B4BB-156362A2F239}"), IServiceProvider, comtypes.CLSCTX_ALL)
        mgr = None
        last_exc: Optional[BaseException] = None
        for iid_text in ("{53F5CA0B-158F-4124-900C-057158060B27}",
                         "{A3175F2D-239C-4BD2-8AA0-EEBA8B0B138E}"):
            try:
                cls = _make_internal_class(iid_text)
                # comtypes convention: the trailing [out] pointer is
                # returned by the call, not passed to it.
                ppv = shell.QueryService(GUID("{C5E0CDCA-7B6E-41B2-9FC4-D93975CC467B}"),
                                         GUID(iid_text))
                addr = ppv.value if hasattr(ppv, "value") else int(ppv)
                if not addr:
                    raise RuntimeError("ImmersiveShell QueryService returned a null pointer")
                unk = ctypes.cast(addr, comtypes.POINTER(IUnknown))
                mgr = unk.QueryInterface(cls)
                break
            except Exception as exc:
                last_exc = exc
        if mgr is None:
            raise RuntimeError(f"no IVirtualDesktopManagerInternal IID worked: {last_exc}")
        _vdi_bundle = (mgr, IObjectArray, IVirtualDesktop)
    except Exception as exc:
        _vdi_failed = True
        _log_error("vd-internal-init", exc)
    return _vdi_bundle


def _switch_desktop_direct(target_num: int) -> bool:
    """Jump straight to desktop `target_num` (1-based) via
    IVirtualDesktopManagerInternal.SwitchDesktop. Returns False -- the
    caller then falls back to the Win+Ctrl+Arrow simulation -- if the
    internal route is unavailable, the index is out of range, or the
    desktop sitting at that index is not the one the registry lists
    there (never jump blind). Main thread only."""
    try:
        import comtypes
        from comtypes import GUID, IUnknown

        bundle = _get_vd_internal()
        if bundle is None:
            return False
        mgr, i_object_array, i_virtual_desktop = bundle
        ppv = mgr.GetDesktops()
        addr = ppv.value if hasattr(ppv, "value") else int(ppv)
        if not addr:
            return False
        arr = ctypes.cast(addr, comtypes.POINTER(IUnknown)).QueryInterface(i_object_array)
        if not (1 <= target_num <= int(arr.GetCount())):
            return False
        ppv2 = arr.GetAt(target_num - 1, GUID("{3F07F4BE-B107-441A-AF0F-39D82529072C}"))
        desk_addr = ppv2.value if hasattr(ppv2, "value") else int(ppv2)
        if not desk_addr:
            return False
        desk = ctypes.cast(desk_addr, comtypes.POINTER(IUnknown)).QueryInterface(i_virtual_desktop)
        guids = _vd_guid_list()
        if 1 <= target_num <= len(guids):
            desk_guid = str(desk.GetId()).strip("{}").lower()
            if desk_guid != guids[target_num - 1]:
                return False
        mgr.SwitchDesktop(ctypes.cast(desk, ctypes.c_void_p))
        return True
    except Exception as exc:
        global _vdi_bundle
        _vdi_bundle = None
        _log_error("vd-switch-direct", exc)
        return False


# --- Crash/error diagnostics: pythonw has no console, so failures are written
# to vortex_error.log next to this file instead of vanishing silently.
_ERROR_LOG_PATH = os.path.join(os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__)), "vortex_error.log")


def _log_error(where: str, exc: Optional[BaseException] = None) -> None:
    try:
        with open(_ERROR_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {time.strftime('%d/%m/%Y %H:%M:%S')} [{where}] =====\n")
            if exc is not None:
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except Exception:
        pass


def _sys_excepthook(exc_type: Any, exc: BaseException, tb: Any) -> None:
    _log_error("main-thread", exc)


sys.excepthook = _sys_excepthook


def _thread_excepthook(args: Any) -> None:
    if args.exc_type is not SystemExit:
        _log_error(f"thread:{getattr(args.thread, 'name', '?')}", args.exc_value)


threading.excepthook = _thread_excepthook

try:
    _fh = open(_ERROR_LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(file=_fh, all_threads=True)
except Exception:
    pass


_desk_cache: Dict[str, Any] = {"num": 1, "ms": 0.0}


def current_desktop_number() -> int:
    """Current virtual desktop number (1-based), read from the registry.

    Registry reads do not jitter; the 100ms cache only keeps the 8ms drag
    poll cheap and pins one stable scope for a whole drag session.
    """
    now = time.monotonic()
    if now - float(_desk_cache["ms"]) < 0.025:
        return int(_desk_cache["num"])
    guids = _vd_guid_list()
    if guids:
        cur = _vd_current_guid()
        if cur in guids:
            _desk_cache["num"], _desk_cache["ms"] = guids.index(cur) + 1, now
    return int(_desk_cache["num"])


def snap_scope_key(mon_id: str, desk_num: Optional[int] = None) -> str:
    """Storage key for snap layouts, scoped per (virtual desktop, monitor): 'D{desktop}|{mon_id}'."""
    n = desk_num if desk_num is not None else current_desktop_number()
    return f"D{n}|{mon_id}"

APP_NAME = "LEHA Vortex Workspace Suite"
APP_VERSION = "v1.2.5"

# v1.2.2 root-cause fix (23/11/2026, from crash-dump symbolication with
# the official python.org PDBs): every "random exit" was CPython
# aborting on a fatal error -- PyEval_RestoreThread called with a NULL
# thread state from inside _tkinter's callback machinery (exception
# 0xc0000409 with fastfail code 7 = FATAL_APP_EXIT, not a stack-cookie
# failure as first read). The trigger: the native AppBar WNDPROC (a
# ctypes callback dispatched by Tcl's own message pump) called
# straight into Tk -- tick_now -> border/overlay windows, the
# TrackPopupMenu modal loop, Tk popups. Entering Tk from inside Tcl's
# dispatch overwrites _tkinter's single saved-thread-state slot, so a
# later callback restores NULL and the interpreter aborts with no
# Python traceback. Fix: the four AppBar handlers below now only post
# their work to the main thread's UI queue (the same rule the keyboard
# hook and the Studio server already follow); nothing touches Tk or a
# modal Win32 loop from inside the WNDPROC any more. The v1.2.2-diag
# bisect (WindowTopPro off) still crashed, which cleared WindowTopPro
# and SnapGrid as causes and pointed at the bar core, as did the dumps.
APP_AUTHOR = "by Le Ha"
APP_LICENSE = "MIT License - Copyright (c) 2026 Le Ha"
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# Win32 Constants
ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS, ABE_TOP = 0, 1, 2, 3, 1
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x00000002
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0010, 0x0040
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x00000002, 0x00, 0x01
SW_RESTORE, DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED, GW_OWNER = 9, 9, 14, 4
VK_LBUTTON, VK_SHIFT, VK_F1 = 0x01, 0x10, 0x70
VK_UP, VK_DOWN, VK_G, VK_R = 0x26, 0x28, 0x47, 0x52
WM_LBUTTONDOWN, WM_RBUTTONUP, WM_MOUSEMOVE, WM_MOUSELEAVE, WM_NCHITTEST = 0x0201, 0x0205, 0x0200, 0x02A3, 0x0084
TME_LEAVE = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32


# Win32 Structures
class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


WNDPROC_TYPE = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC_TYPE),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HICON), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


# Strict 64-bit ctypes Signatures (prevents pointer truncation on Windows x64)
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HICON
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.TrackMouseEvent.restype = wintypes.BOOL
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
shell32.SHAppBarMessage.restype = ctypes.c_size_t
# --- 64-bit signature completion (v1.1.5): the calls below previously ran
# with ctypes' default restype (C int), which truncates 64-bit handles and
# pointers. Most critically GetModuleHandleW: its truncated value was stored
# as the hInstance of the AppBar window class, so RegisterClassExW and
# CreateWindowExW intermittently died with an access violation at startup
# (evidence: vortex_error.log, 22/11/2026).
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.Sleep.argtypes = [wintypes.DWORD]
kernel32.Sleep.restype = None
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_ssize_t
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetDpiForSystem.restype = wintypes.UINT
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
user32.RedrawWindow.restype = wintypes.BOOL
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None



FONT_CACHE: Dict[Tuple[int, str, bool], Any] = {}
TEXT_BBOX_CACHE: Dict[Tuple[str, Tuple[int, str, bool]], Tuple[int, int, int, int]] = {}
FONTS = {
    "regular": "C:/Windows/Fonts/segoeui.ttf",
    "bold": "C:/Windows/Fonts/seguisb.ttf",
    "light": "C:/Windows/Fonts/segoeuil.ttf",
    "italic": "C:/Windows/Fonts/segoeuii.ttf",
    "bold_italic": "C:/Windows/Fonts/seguisbi.ttf",
}
LUXURY_MULTI_PALETTE = [
    {"bg": "#0284c7", "fg": "#ffffff", "accent": "#38bdf8"},
    {"bg": "#059669", "fg": "#ffffff", "accent": "#34d399"},
    {"bg": "#7c3aed", "fg": "#ffffff", "accent": "#c084fc"},
    {"bg": "#ea580c", "fg": "#ffffff", "accent": "#fb923c"},
    {"bg": "#db2777", "fg": "#ffffff", "accent": "#f472b6"},
    {"bg": "#ca8a04", "fg": "#ffffff", "accent": "#facc15"},
    {"bg": "#0d9488", "fg": "#ffffff", "accent": "#2dd4bf"},
    {"bg": "#4f46e5", "fg": "#ffffff", "accent": "#818cf8"},
]


def hex_to_rgba(hex_str: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    cleaned = hex_str.lstrip("#")
    if len(cleaned) != 6:
        return (20, 20, 25, alpha)
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16), alpha)


def get_app_dir() -> str:
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


def get_config_path() -> str:
    return os.path.join(get_app_dir(), "config.json")


def ensure_app_icon() -> str:
    ico_path = os.path.join(get_app_dir(), "app_icon.ico")
    try:
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for y in range(12, 244):
            t = (y - 12) / 232.0
            r = int(10 * (1 - t) + 56 * t)
            g = int(132 * (1 - t) + 189 * t)
            b = int(255 * (1 - t) + 248 * t)
            d.line([(12, y), (244, y)], fill=(r, g, b, 255))
        mask = Image.new("L", (256, 256), 0)
        ImageDraw.Draw(mask).rounded_rectangle([12, 12, 244, 244], radius=60, fill=255)
        img.putalpha(mask)
        d2 = ImageDraw.Draw(img)
        d2.rounded_rectangle([54, 106, 202, 150], radius=22, fill=(255, 255, 255, 245))
        img.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    except Exception:
        pass
    return ico_path


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_READ) as key:
            return bool(winreg.QueryValueEx(key, APP_NAME)[0])
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                cmd = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{shutil.which("pythonw.exe") or sys.executable}" "{os.path.abspath(__file__)}"'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def default_snap_layouts() -> Dict[str, Dict[str, Any]]:
    return {
        "1": {"name": "1 Fullscreen Zone", "zones": [[0.0, 0.0, 1.0, 1.0]]},
        "2": {"name": "2 Columns (50/50)", "zones": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]]},
        "3": {"name": "3 Columns (33/33/33)", "zones": [[0.0, 0.0, 0.333, 1.0], [0.333, 0.0, 0.666, 1.0], [0.666, 0.0, 1.0, 1.0]]},
        "4": {"name": "4 Quadrant Grid (2x2)", "zones": [[0.0, 0.0, 0.5, 0.5], [0.5, 0.0, 1.0, 0.5], [0.0, 0.5, 0.5, 1.0], [0.5, 0.5, 1.0, 1.0]]},
        "5": {"name": "5 Focus Grid (Main+4)", "zones": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 0.75, 0.5], [0.75, 0.0, 1.0, 0.5], [0.5, 0.5, 0.75, 1.0], [0.75, 0.5, 1.0, 1.0]]},
        "6": {"name": "6 Matrix Grid (3x2)", "zones": [[0.0, 0.0, 0.333, 0.5], [0.333, 0.0, 0.666, 0.5], [0.666, 0.0, 1.0, 0.5], [0.0, 0.5, 0.333, 1.0], [0.333, 0.5, 0.666, 1.0], [0.666, 0.5, 1.0, 1.0]]},
    }


def load_config_data() -> Dict[str, Any]:
    default_cfg: Dict[str, Any] = {
        "studio_theme": "dark",
        "studio_lang": "vi",
        "modules": {"desktop_bar": True, "quick_shell": True, "always_on_top": True, "snap_grid": True},
        "shell_use_wt": True,
        "aot_border_color": "#ffffff",
        "aot_border_width": 3,
        "ghost_opacity": 0.45,
        "snap_active_slot": "3",
        "snap_monitor_active_slots": {},
        "snap_drag_overlay": True,
        "snap_coupled_resize": True,
        "snap_require_shift": False,
        "snap_zone_spacing": 8,
        "snap_span_margin": 95,
        "snap_overlay_opacity": 0.38,
        "snap_restore_last_zone": True,
        "snap_app_history": {},
        "snap_monitors": {"default": default_snap_layouts()},
        "bar_transparent": True,
        "color_mode": "multi",
        "pill_style": "filled",
        "bg_color": "#0d0d10",
        "border_color": "#222226",
        "mono_idle_bg": "#18181f",
        "mono_idle_text": "#b4b4be",
        "mono_active_bg": "#0284c7",
        "mono_active_text": "#ffffff",
        "mono_accent": "#38bdf8",
        "multi_pills": {str(i + 1): LUXURY_MULTI_PALETTE[i % len(LUXURY_MULTI_PALETTE)].copy() for i in range(12)},
        "idle_has_bg": True,
        "active_underline": False,
        "active_border": True,
        "active_pop_padding": 2,
        "hover_zoom": True, "smoothness": 0.45,
        "font_size": 13,
        "font_weight": "bold",
        "font_italic": False,
        "text_case": "normal",
        "layout_mode": "center",
        "pill_size": "wide",
        "pill_gap": 22,
        "pill_radius": 9,
        "height": 28,
        "opacity": 0.94,
        "auto_hide_fullscreen": False,
    }
    p = get_config_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    default_cfg.update(loaded)
        except Exception:
            pass
    default_cfg["autostart"] = is_autostart_enabled()
    return default_cfg


def save_config_silent(cfg: Dict[str, Any]) -> None:
    try:
        with open(get_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def get_process_exe_name(hwnd: int) -> Optional[str]:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h_proc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h_proc)
    return None


MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL

def get_all_physical_monitors() -> List[Dict[str, Any]]:
    raw_mons: List[Dict[str, Any]] = []

    def _cb(hmon: int, hdc: int, lprect: Any, lparam: int) -> bool:
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            raw_mons.append({
                "rcMonitor": (mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom),
                "rcWork": mi.rcWork,
                "isPrimary": bool(mi.dwFlags & 1),
                "w": mi.rcMonitor.right - mi.rcMonitor.left,
                "h": mi.rcMonitor.bottom - mi.rcMonitor.top,
            })
        return True

    cb_ref = MONITORENUMPROC(_cb)
    user32.EnumDisplayMonitors(None, None, cb_ref, 0)
    raw_mons.sort(key=lambda m: (m["rcMonitor"][0], m["rcMonitor"][1]))
    for idx, m in enumerate(raw_mons):
        m["id"] = f"DISPLAY_{idx + 1}"
        role = "Primary" if m["isPrimary"] else "External"
        m["label"] = f"Display {idx + 1}: {m['w']}x{m['h']} ({role})"
    return raw_mons


def enumerate_monitors() -> List[Dict[str, str]]:
    mons = get_all_physical_monitors()
    return [{"id": m["id"], "label": m["label"]} for m in mons] or [{"id": "DISPLAY_1", "label": "Display 1"}]


def get_explorer_target_folder() -> str:
    com_inited = False
    try:
        import pythoncom
        pythoncom.CoInitialize()
        com_inited = True
    except Exception:
        pass
    try:
        import win32com.client
        shell = win32com.client.Dispatch("Shell.Application")
        windows = list(shell.Windows())

        # 1. Prefer the Explorer window/tab directly under the cursor (even before it is clicked/focused)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hwnd_pt = user32.WindowFromPoint(pt)
        root_pt = user32.GetAncestor(hwnd_pt, 2) or hwnd_pt

        # 2. The active (foreground) window
        fg_hwnd = user32.GetForegroundWindow()
        root_fg = user32.GetAncestor(fg_hwnd, 2) or fg_hwnd

        for target_h in (root_pt, hwnd_pt, root_fg, fg_hwnd):
            if not target_h:
                continue
            for w in windows:
                try:
                    if int(w.HWND) == int(target_h):
                        url = str(w.LocationURL or "")
                        if url.lower().startswith("file:///"):
                            path = urllib.parse.unquote(url[8:]).replace("/", "\\")
                            if os.path.isdir(path):
                                return path
                except Exception:
                    continue

        # 3. If inside Explorer but on a different tab handle, take the topmost Explorer window
        cls_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(root_pt, cls_buf, 64)
        fg_cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(root_fg, fg_cls, 64)
        if "CabinetWClass" in (cls_buf.value, fg_cls.value):
            for w in windows:
                try:
                    url = str(w.LocationURL or "")
                    if url.lower().startswith("file:///"):
                        path = urllib.parse.unquote(url[8:]).replace("/", "\\")
                        if os.path.isdir(path):
                            return path
                except Exception:
                    continue
    except Exception as exc:
        _log_error("quick-shell-explorer", exc)
    finally:
        if com_inited:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
    return os.path.expanduser("~")


def launch_powershell(admin: bool = False, use_wt: bool = True) -> None:
    folder = os.path.normpath(get_explorer_target_folder())
    if use_wt and shutil.which("wt.exe"):
        if admin:
            shell32.ShellExecuteW(None, "runas", "wt.exe", f'-d "{folder}"', folder, 1)
        else:
            subprocess.Popen(["wt.exe", "-d", folder], cwd=folder, creationflags=0x00000008)
    else:
        ps_cmd = f'Set-Location -LiteralPath \'{folder}\''
        shell32.ShellExecuteW(None, "runas" if admin else "open", "powershell.exe", f'-NoExit -Command "{ps_cmd}"', folder, 1)


class WindowTopProManager:
    def __init__(self, tk_root: tk.Tk, get_cfg_fn: Callable[[], Dict[str, Any]]) -> None:
        self.root = tk_root
        self.get_cfg = get_cfg_fn
        self.pinned_hwnds: Dict[int, tk.Toplevel] = {}
        self._border_cache: Dict[int, Tuple[Any, ...]] = {}
        self.window_alphas: Dict[int, int] = {}
        self.ghost_hwnds: set[int] = set()

    @staticmethod
    def get_real_window_rect(hwnd: int) -> wintypes.RECT:
        rect = wintypes.RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect

    def _get_target_hwnd(self) -> int:
        fg = user32.GetForegroundWindow()
        if not fg:
            return 0
        root_hwnd = user32.GetAncestor(fg, 2) or fg
        own_root = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        if root_hwnd in (self.root.winfo_id(), own_root):
            return 0
        return root_hwnd if user32.IsWindow(root_hwnd) else 0

    def adjust_opacity(self, delta: int) -> None:
        hwnd = self._get_target_hwnd()
        if not hwnd:
            return
        cur = self.window_alphas.get(hwnd, 255)
        nxt = max(25, min(255, cur + delta))
        self.window_alphas[hwnd] = nxt
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if nxt < 255:
            if not (style & WS_EX_LAYERED):
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            user32.SetLayeredWindowAttributes(hwnd, 0, nxt, LWA_ALPHA)
            user32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0080)
        else:
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            if hwnd not in self.ghost_hwnds:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_LAYERED)
            user32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0080)

    def toggle_ghost_mode(self) -> None:
        hwnd = self._get_target_hwnd()
        if self.ghost_hwnds:
            for h in list(self.ghost_hwnds):
                self.ghost_hwnds.discard(h)
                if user32.IsWindow(h):
                    st = user32.GetWindowLongW(h, GWL_EXSTYLE)
                    user32.SetWindowLongW(h, GWL_EXSTYLE, st & ~WS_EX_TRANSPARENT)
                    alpha = self.window_alphas.get(h, 255)
                    user32.SetLayeredWindowAttributes(h, 0, alpha, LWA_ALPHA)
                    if alpha == 255:
                        user32.SetWindowLongW(h, GWL_EXSTYLE, st & ~(WS_EX_TRANSPARENT | WS_EX_LAYERED))
            return

        if not hwnd:
            return
        ghost_alpha = int(max(0.05, min(0.95, float(self.get_cfg().get("ghost_opacity", 0.45)))) * 255)
        self.ghost_hwnds.add(hwnd)
        st = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, st | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        user32.SetLayeredWindowAttributes(hwnd, 0, ghost_alpha, LWA_ALPHA)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def sync_live_properties(self) -> None:
        cfg = self.get_cfg()
        ghost_alpha = int(max(0.05, min(0.95, float(cfg.get("ghost_opacity", 0.45)))) * 255)
        for h in list(self.ghost_hwnds):
            if user32.IsWindow(h):
                user32.SetLayeredWindowAttributes(h, 0, ghost_alpha, LWA_ALPHA)
            else:
                self.ghost_hwnds.discard(h)

    def restore_all_windows(self) -> None:
        for h in list(self.ghost_hwnds):
            self.ghost_hwnds.discard(h)
            if user32.IsWindow(h):
                st = user32.GetWindowLongW(h, GWL_EXSTYLE)
                user32.SetWindowLongW(h, GWL_EXSTYLE, st & ~(WS_EX_TRANSPARENT | WS_EX_LAYERED))
                user32.SetLayeredWindowAttributes(h, 0, 255, LWA_ALPHA)

    def toggle_pin(self) -> None:
        hwnd = self._get_target_hwnd()
        if not hwnd:
            return
        if hwnd in self.pinned_hwnds:
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self._border_cache.pop(hwnd, None)
            self.pinned_hwnds.pop(hwnd).destroy()
        else:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            ov = tk.Toplevel(self.root)
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            ov.attributes("-transparentcolor", "#010101")
            ov.configure(bg="#010101")
            ov.update_idletasks()
            hwnd_ov = user32.GetParent(ov.winfo_id()) or ov.winfo_id()
            user32.SetWindowLongW(hwnd_ov, GWL_EXSTYLE, user32.GetWindowLongW(hwnd_ov, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            c = tk.Canvas(ov, bg="#010101", highlightthickness=0)
            c.pack(fill="both", expand=True)
            ov._canvas = c  # type: ignore[attr-defined]
            self.pinned_hwnds[hwnd] = ov

    def sync_borders(self, is_dragging_other: bool = False) -> None:
        # v1.2.0: only touch Tk when something actually changed. The crash
        # dump of 22/11/2026 (fastfail stack-cookie, ucrtbase) showed the
        # process dying inside the Tcl/Tk event dispatch on the main thread
        # with no ctypes/PIL frames active, while this method used to call
        # withdraw/deiconify and fully redraw the border canvas on EVERY
        # tick (~28x/second per pinned window) even when nothing moved.
        # Idle ticks now cost one rect read; the Win32-only re-raise below
        # (no Tcl involved) still runs twice a second so the border stays
        # above its window.
        cfg = self.get_cfg()
        bw, color = int(cfg.get("aot_border_width", 3)), cfg.get("aot_border_color", "#ffffff")
        now_ms = int(kernel32.GetTickCount64())
        stale: List[int] = []
        for hwnd, overlay in list(self.pinned_hwnds.items()):
            if not user32.IsWindow(hwnd):
                stale.append(hwnd)
                continue
            is_cloaked = wintypes.DWORD(0)
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(is_cloaked), ctypes.sizeof(is_cloaked))
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or is_cloaked.value != 0 or is_dragging_other:
                if self._border_cache.get(hwnd, (None,))[0] != "hidden":
                    self._border_cache[hwnd] = ("hidden", 0)
                    overlay.withdraw()
                continue
            rect = self.get_real_window_rect(hwnd)
            w, h = max(10, rect.right - rect.left), max(10, rect.bottom - rect.top)
            state = ("shown", rect.left, rect.top, w, h, bw, color)
            prev = self._border_cache.get(hwnd)
            if prev is not None and prev[0] == "shown" and prev[1:7] == state[1:]:
                if now_ms - prev[7] >= 500:
                    self._border_cache[hwnd] = state + (now_ms,)
                    h_ov = user32.GetParent(overlay.winfo_id()) or overlay.winfo_id()
                    user32.SetWindowPos(h_ov, HWND_TOPMOST, rect.left, rect.top, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
                continue
            self._border_cache[hwnd] = state + (now_ms,)
            overlay.deiconify()
            h_ov = user32.GetParent(overlay.winfo_id()) or overlay.winfo_id()
            user32.SetWindowPos(h_ov, HWND_TOPMOST, rect.left, rect.top, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            canvas: tk.Canvas = overlay._canvas  # type: ignore[attr-defined]
            canvas.delete("all")
            canvas.create_rectangle(bw // 2, bw // 2, w - (bw // 2), h - (bw // 2), outline=color, width=bw)
        for hwnd in stale:
            self._border_cache.pop(hwnd, None)
            self.pinned_hwnds.pop(hwnd).destroy()


class SnapGridManager:
    def __init__(self, tk_root: tk.Tk, get_cfg_fn: Callable[[], Dict[str, Any]]) -> None:
        self.root, self.get_cfg = tk_root, get_cfg_fn
        self.overlay_win: Optional[tk.Toplevel] = None
        self.overlay_canvas: Optional[tk.Canvas] = None
        self.overlay_mon_id: Optional[str] = None
        self.selected_indices: Tuple[int, ...] = ()
        self.dragging_hwnd: Optional[int] = None
        self.resizing_hwnd: Optional[int] = None
        self.is_resize_session: bool = False
        self.resize_start_rect: Optional[wintypes.RECT] = None
        self.resize_start_norm: Optional[List[float]] = None
        self.seen_hwnds: set[int] = set()
        self.active_hwnd_bounds: Dict[int, List[float]] = {}
        self._last_restore_scan = 0
        self._flash_until_ms = 0
        self._overlay_zones_sig: Optional[Tuple[Tuple[float, ...], ...]] = None
        self._drag_desk: Optional[int] = None

    @staticmethod
    def get_monitor_context_from_point(x: int, y: int) -> Tuple[str, wintypes.RECT]:
        mons = get_all_physical_monitors()
        for m in mons:
            l, t, r, b = m["rcMonitor"]
            if l <= x < r and t <= y < b:
                return m["id"], m["rcWork"]
        if mons:
            return mons[0]["id"], mons[0]["rcWork"]
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(user32.MonitorFromPoint(POINT(x, y), 2), ctypes.byref(mi))
        return "DISPLAY_1", mi.rcWork

    @staticmethod
    def get_monitor_context_for_window(hwnd: int) -> Tuple[str, wintypes.RECT]:
        wr = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        return SnapGridManager.get_monitor_context_from_point((wr.left + wr.right) // 2, (wr.top + wr.bottom) // 2)

    @staticmethod
    def _get_work_area_for_mon_id(target_mon_id: str, fallback_hwnd: int) -> Tuple[str, wintypes.RECT]:
        for m in get_all_physical_monitors():
            if m["id"] == target_mon_id:
                return target_mon_id, m["rcWork"]
        return SnapGridManager.get_monitor_context_for_window(fallback_hwnd)

    def get_active_slot_for_monitor(self, mon_id: str, desk_num: Optional[int] = None) -> str:
        cfg = self.get_cfg()
        slots_map = cfg.setdefault("snap_monitor_active_slots", {})
        scope = snap_scope_key(mon_id, desk_num)
        if scope not in slots_map or not slots_map[scope]:
            legacy = slots_map.get(mon_id)
            slots_map[scope] = str(legacy) if legacy else "3"
        return str(slots_map[scope])

    def get_zones_for_monitor(self, mon_id: str, slot_key: Optional[str] = None, desk_num: Optional[int] = None) -> List[List[float]]:
        cfg = self.get_cfg()
        slot = slot_key or self.get_active_slot_for_monitor(mon_id, desk_num)
        monitors_map = cfg.setdefault("snap_monitors", {})
        scope = snap_scope_key(mon_id, desk_num)
        if scope not in monitors_map:
            legacy = monitors_map.get(mon_id)
            base = legacy if isinstance(legacy, dict) and legacy else (monitors_map.get("default") or default_snap_layouts())
            monitors_map[scope] = json.loads(json.dumps(base))
        layouts = monitors_map[scope]
        layout = layouts.get(slot) or layouts.get("3") or default_snap_layouts()["3"]
        return layout.get("zones", [])

    @staticmethod
    def _point_to_rect_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
        return math.hypot(max(x1 - px, 0.0, px - x2), max(y1 - py, 0.0, py - y2))

    def compute_magnetic_selection(self, pt_x: int, pt_y: int, work: wintypes.RECT, zones: List[List[float]]) -> Tuple[int, ...]:
        if not zones:
            return ()
        mw, mh = max(1, work.right - work.left), max(1, work.bottom - work.top)
        threshold_px = float(self.get_cfg().get("snap_span_margin", 95))
        near_with_dist: List[Tuple[float, int]] = []
        for idx, z in enumerate(zones):
            zx1, zy1 = work.left + z[0] * mw, work.top + z[1] * mh
            zx2, zy2 = work.left + z[2] * mw, work.top + z[3] * mh
            d = self._point_to_rect_distance(float(pt_x), float(pt_y), zx1, zy1, zx2, zy2)
            if d <= threshold_px:
                near_with_dist.append((d, idx))
        if 2 <= len(near_with_dist) <= 4:
            near_with_dist.sort(key=lambda item: item[0])
            return tuple(sorted(idx for _, idx in near_with_dist))
        best_idx, best_dist = 0, float("inf")
        for idx, z in enumerate(zones):
            cx = work.left + ((z[0] + z[2]) * 0.5) * mw
            cy = work.top + ((z[1] + z[3]) * 0.5) * mh
            dist = math.hypot(pt_x - cx, pt_y - cy)
            if dist < best_dist:
                best_dist, best_idx = dist, idx
        return (best_idx,)

    @staticmethod
    def merge_zone_rects(zones: List[List[float]], indices: Tuple[int, ...]) -> List[float]:
        selected = [zones[i] for i in indices if 0 <= i < len(zones)]
        return [min(z[0] for z in selected), min(z[1] for z in selected), max(z[2] for z in selected), max(z[3] for z in selected)]

    def apply_bounds_to_window(
        self, hwnd: int, mon_id: str, work: wintypes.RECT, norm_rect: List[float], slot_key: str, indices: Tuple[int, ...], record_history: bool = True
    ) -> None:
        mw, mh = work.right - work.left, work.bottom - work.top
        spacing = int(self.get_cfg().get("snap_zone_spacing", 8))
        half_gap = spacing // 2

        if user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        wr, dr = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(dr), ctypes.sizeof(dr)) == 0:
            pad_l, pad_t = max(0, dr.left - wr.left), max(0, dr.top - wr.top)
            pad_r, pad_b = max(0, wr.right - dr.right), max(0, wr.bottom - dr.bottom)
        else:
            pad_l = pad_t = pad_r = pad_b = 0

        x = int(work.left + norm_rect[0] * mw) + half_gap - pad_l
        y = int(work.top + norm_rect[1] * mh) + half_gap - pad_t
        w = max(80, int((norm_rect[2] - norm_rect[0]) * mw) - spacing + pad_l + pad_r)
        h = max(60, int((norm_rect[3] - norm_rect[1]) * mh) - spacing + pad_t + pad_b)
        user32.SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOACTIVATE)
        self.active_hwnd_bounds[hwnd] = norm_rect

        if record_history:
            exe = get_process_exe_name(hwnd)
            if exe and exe not in ("python.exe", "pythonw.exe", "applicationframehost.exe"):
                cfg = self.get_cfg()
                hist = cfg.setdefault("snap_app_history", {})
                desk_now = current_desktop_number()
                rec = {"mon_id": mon_id, "slot": slot_key, "indices": list(indices), "norm_rect": norm_rect, "desk": desk_now}
                hist[exe] = rec
                hist[f"{exe}|D{desk_now}"] = rec
                save_config_silent(cfg)

    def switch_monitor_layout(self, slot_key: str) -> None:
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        mon_id, work = self.get_monitor_context_from_point(pt.x, pt.y)

        desk_now = current_desktop_number()
        cfg = self.get_cfg()
        slots_map = cfg.setdefault("snap_monitor_active_slots", {})
        slots_map[snap_scope_key(mon_id, desk_now)] = str(slot_key)
        save_config_silent(cfg)

        zones = self.get_zones_for_monitor(mon_id, str(slot_key), desk_now)
        if not zones:
            return
        self._render_drag_zones(work, zones, (), banner=f"Desktop {desk_now}  •  {mon_id}  •  Layout F{slot_key} ({len(zones)} Zones)")
        self._flash_until_ms = int(kernel32.GetTickCount64()) + 280
        self.root.after(285, self._check_flash_expire)

    def _check_flash_expire(self) -> None:
        if self._flash_until_ms > 0 and int(kernel32.GetTickCount64()) >= self._flash_until_ms:
            self._flash_until_ms = 0
            if not self.dragging_hwnd:
                self._hide_overlay()

    def _ensure_overlay(self, work: wintypes.RECT) -> tk.Canvas:
        w, h = work.right - work.left, work.bottom - work.top
        alpha_val = float(self.get_cfg().get("snap_overlay_opacity", 0.38))
        if self.overlay_win is None or not self.overlay_win.winfo_exists():
            ov = tk.Toplevel(self.root)
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            ov.configure(bg="#090a0f")
            ov.update_idletasks()
            h_ov = user32.GetParent(ov.winfo_id()) or ov.winfo_id()
            user32.SetWindowLongW(h_ov, GWL_EXSTYLE, user32.GetWindowLongW(h_ov, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            c = tk.Canvas(ov, bg="#090a0f", highlightthickness=0)
            c.pack(fill="both", expand=True)
            self.overlay_win, self.overlay_canvas = ov, c
        self.overlay_win.attributes("-alpha", max(0.05, min(0.95, alpha_val)))
        self.overlay_win.deiconify()
        h_ov = user32.GetParent(self.overlay_win.winfo_id()) or self.overlay_win.winfo_id()
        user32.SetWindowPos(h_ov, HWND_TOPMOST, work.left, work.top, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        return self.overlay_canvas  # type: ignore[return-value]

    def _hide_overlay(self) -> None:
        if self.overlay_win and self.overlay_win.winfo_exists():
            self.overlay_win.withdraw()
        self.overlay_mon_id = None
        self.selected_indices = ()
        self._overlay_zones_sig = None

    def _render_drag_zones(self, work: wintypes.RECT, zones: List[List[float]], selected: Tuple[int, ...], banner: str = "") -> None:
        canvas = self._ensure_overlay(work)
        w, h = work.right - work.left, work.bottom - work.top
        canvas.delete("all")
        spacing = int(self.get_cfg().get("snap_zone_spacing", 8))
        half_gap = spacing // 2

        for idx, z in enumerate(zones):
            x1, y1 = int(z[0] * w) + half_gap, int(z[1] * h) + half_gap
            x2, y2 = int(z[2] * w) - half_gap, int(z[3] * h) - half_gap
            is_sel = idx in selected
            canvas.create_rectangle(x1, y1, x2, y2, fill="#1e293b" if is_sel else "#11131a", outline="#60a5fa" if is_sel else "#334155", width=2)
            canvas.create_text((x1 + x2) // 2, (y1 + y2) // 2, text=str(idx + 1), fill="#e2e8f0" if is_sel else "#64748b", font=("Segoe UI", 22, "bold"))

        if len(selected) >= 2:
            merged = self.merge_zone_rects(zones, selected)
            mx1, my1 = int(merged[0] * w) + half_gap, int(merged[1] * h) + half_gap
            mx2, my2 = int(merged[2] * w) - half_gap, int(merged[3] * h) - half_gap
            canvas.create_rectangle(mx1, my1, mx2, my2, fill="#1d4ed8", stipple="gray25", outline="#93c5fd", width=3)
            canvas.create_text((mx1 + mx2) // 2, (my1 + my2) // 2, text=f"MERGE [{' + '.join(str(i + 1) for i in selected)}]", fill="#ffffff", font=("Segoe UI", 20, "bold"))
        elif banner:
            canvas.create_text(w // 2, 34, text=banner, fill="#38bdf8", font=("Segoe UI", 14, "bold"))

    def _sync_coupled_neighbors(self, active_hwnd: int) -> None:
        if not self.resize_start_norm:
            return
        mon_id, work = self.get_monitor_context_for_window(active_hwnd)
        mw, mh = max(1, work.right - work.left), max(1, work.bottom - work.top)
        spacing = int(self.get_cfg().get("snap_zone_spacing", 8))
        half_gap = spacing // 2

        wr, dr = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(active_hwnd, ctypes.byref(wr))
        if dwmapi.DwmGetWindowAttribute(active_hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(dr), ctypes.sizeof(dr)) != 0:
            dr = wr

        new_norm = [
            round(max(0.0, min(1.0, (dr.left - half_gap - work.left) / mw)), 4),
            round(max(0.0, min(1.0, (dr.top - half_gap - work.top) / mh)), 4),
            round(max(0.0, min(1.0, (dr.right + half_gap - work.left) / mw)), 4),
            round(max(0.0, min(1.0, (dr.bottom + half_gap - work.top) / mh)), 4),
        ]
        old_norm = self.active_hwnd_bounds.get(active_hwnd, self.resize_start_norm)
        self.active_hwnd_bounds[active_hwnd] = new_norm

        eps = 0.025
        for h_other, b_other in list(self.active_hwnd_bounds.items()):
            if h_other == active_hwnd or not user32.IsWindow(h_other) or not user32.IsWindowVisible(h_other):
                continue
            o_mon, _ = self.get_monitor_context_for_window(h_other)
            if o_mon != mon_id:
                continue
            updated = list(b_other)
            changed = False
            if abs(b_other[0] - old_norm[2]) < eps and 0.05 < new_norm[2] < 0.95:
                updated[0], changed = new_norm[2], True
            if abs(b_other[2] - old_norm[0]) < eps and 0.05 < new_norm[0] < 0.95:
                updated[2], changed = new_norm[0], True
            if abs(b_other[1] - old_norm[3]) < eps and 0.05 < new_norm[3] < 0.95:
                updated[1], changed = new_norm[3], True
            if abs(b_other[3] - old_norm[1]) < eps and 0.05 < new_norm[1] < 0.95:
                updated[3], changed = new_norm[1], True

            if changed and (updated[2] - updated[0]) > 0.08 and (updated[3] - updated[1]) > 0.08:
                self.apply_bounds_to_window(h_other, mon_id, work, updated, self.get_active_slot_for_monitor(mon_id), (0,), record_history=False)

    def poll_drag_and_restore(self) -> None:
        cfg = self.get_cfg()
        now_ms = int(kernel32.GetTickCount64())
        if not cfg.get("modules", {}).get("snap_grid", True):
            self._hide_overlay()
            return

        gti = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        fg_hwnd = user32.GetForegroundWindow()
        tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        in_movesize = False
        if tid and user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
            if (gti.flags & 0x0002) and gti.hwndMoveSize and (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000):
                in_movesize = True
                if self.dragging_hwnd != gti.hwndMoveSize:
                    self.dragging_hwnd = gti.hwndMoveSize
                    self._drag_desk = current_desktop_number()
                    pt_hit = POINT()
                    user32.GetCursorPos(ctypes.byref(pt_hit))
                    lparam_pt = (pt_hit.y << 16) | (pt_hit.x & 0xFFFF)
                    ht = user32.SendMessageW(gti.hwndMoveSize, WM_NCHITTEST, 0, lparam_pt)
                    self.is_resize_session = (10 <= ht <= 17)
                    cur_r = wintypes.RECT()
                    user32.GetWindowRect(gti.hwndMoveSize, ctypes.byref(cur_r))
                    self.resize_start_rect = cur_r
                    if self.is_resize_session and gti.hwndMoveSize in self.active_hwnd_bounds:
                        self.resizing_hwnd = gti.hwndMoveSize
                        self.resize_start_norm = list(self.active_hwnd_bounds[gti.hwndMoveSize])

        if in_movesize and self.is_resize_session:
            self._flash_until_ms = 0
            self._hide_overlay()
            if self.resizing_hwnd and cfg.get("snap_coupled_resize", True):
                self._sync_coupled_neighbors(self.resizing_hwnd)
        elif in_movesize and self.dragging_hwnd and not self.is_resize_session and cfg.get("snap_drag_overlay", True):
            self._flash_until_ms = 0
            need_shift = bool(cfg.get("snap_require_shift", False))
            shift_held = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
            if not need_shift or shift_held:
                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                mon_id, work = self.get_monitor_context_from_point(pt.x, pt.y)
                zones = self.get_zones_for_monitor(mon_id, desk_num=self._drag_desk)
                sel = self.compute_magnetic_selection(pt.x, pt.y, work, zones)
                zones_sig = tuple(tuple(z) for z in zones)
                if mon_id != self.overlay_mon_id or sel != self.selected_indices or zones_sig != self._overlay_zones_sig:
                    self.overlay_mon_id, self.selected_indices, self._overlay_zones_sig = mon_id, sel, zones_sig
                    self._render_drag_zones(work, zones, sel)
            else:
                self._hide_overlay()
        else:
            self.is_resize_session = False
            self.resize_start_rect = None
            self.resize_start_norm = None
            if self.resizing_hwnd is not None:
                self.resizing_hwnd = None
                self.dragging_hwnd = None
                self._drag_desk = None
            elif self.dragging_hwnd is not None:
                target_hwnd, target_mon, target_sel = self.dragging_hwnd, self.overlay_mon_id, self.selected_indices
                self.dragging_hwnd = None
                self._hide_overlay()
                if target_mon and target_sel and user32.IsWindow(target_hwnd):
                    pt = POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    _, work = self.get_monitor_context_from_point(pt.x, pt.y)
                    slot_key = self.get_active_slot_for_monitor(target_mon, self._drag_desk)
                    zones = self.get_zones_for_monitor(target_mon, slot_key, self._drag_desk)
                    self._drag_desk = None
                    self.apply_bounds_to_window(target_hwnd, target_mon, work, self.merge_zone_rects(zones, target_sel), slot_key, target_sel, True)
            elif self._flash_until_ms > 0 and now_ms >= self._flash_until_ms:
                self._flash_until_ms = 0
                self._hide_overlay()

        if cfg.get("snap_restore_last_zone", True) and (now_ms - self._last_restore_scan >= 15):
            self._last_restore_scan = now_ms
            self._scan_and_restore_new_windows()

    def _find_parent_zone_for_child(self, hwnd: int, history: Dict[str, Dict[str, Any]]) -> Optional[Tuple[int, List[float]]]:
        owner_hwnd = user32.GetWindow(hwnd, GW_OWNER) or user32.GetParent(hwnd)
        if owner_hwnd and user32.IsWindow(owner_hwnd):
            root_owner = user32.GetAncestor(owner_hwnd, 2) or owner_hwnd
            if root_owner in self.active_hwnd_bounds:
                return root_owner, self.active_hwnd_bounds[root_owner]
            owner_exe = get_process_exe_name(root_owner)
            if owner_exe and owner_exe in history and history[owner_exe].get("norm_rect"):
                return root_owner, history[owner_exe]["norm_rect"]

        child_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(child_pid))
        if child_pid.value:
            for parent_h, parent_rect in list(self.active_hwnd_bounds.items()):
                if parent_h != hwnd and user32.IsWindow(parent_h) and user32.IsWindowVisible(parent_h):
                    p_pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(parent_h, ctypes.byref(p_pid))
                    if p_pid.value == child_pid.value:
                        return parent_h, parent_rect
        return None

    def _restore_window_delayed(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return
        cls_buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls_buf, 128)
        if cls_buf.value in ("#32770", "OperationStatusWindow", "Shell_TrayWnd", "NotifyIconOverflowWindow"):
            return
        cfg = self.get_cfg()
        if not cfg.get("snap_restore_last_zone", True):
            return
        history: Dict[str, Dict[str, Any]] = cfg.get("snap_app_history", {})

        parent_info = self._find_parent_zone_for_child(hwnd, history)
        if parent_info is not None:
            parent_h, parent_norm = parent_info
            _, work = self.get_monitor_context_for_window(parent_h)
            mw, mh = work.right - work.left, work.bottom - work.top
            zx, zy = int(work.left + parent_norm[0] * mw), int(work.top + parent_norm[1] * mh)
            zw, zh = int((parent_norm[2] - parent_norm[0]) * mw), int((parent_norm[3] - parent_norm[1]) * mh)
            cr = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(cr))
            cw, ch = max(120, cr.right - cr.left), max(90, cr.bottom - cr.top)
            fit_w, fit_h = min(cw, max(240, zw - 16)), min(ch, max(180, zh - 16))
            user32.SetWindowPos(hwnd, 0, zx + max(0, (zw - fit_w) // 2), zy + max(0, (zh - fit_h) // 2), fit_w, fit_h, SWP_NOACTIVATE)
            return

        exe = get_process_exe_name(hwnd)
        rec = (history.get(f"{exe}|D{current_desktop_number()}") or history.get(exe)) if exe else None
        if rec:
            norm_rect = rec.get("norm_rect")
            if norm_rect:
                target_mon, work = self._get_work_area_for_mon_id(rec.get("mon_id", ""), hwnd)
                self.apply_bounds_to_window(hwnd, target_mon, work, norm_rect, str(rec.get("slot", "3")), tuple(rec.get("indices", [0])), False)

    def restore_single_hwnd_instant(self, hwnd: int) -> None:
        if not hwnd or hwnd in self.seen_hwnds:
            return
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if (ex_style & WS_EX_TOOLWINDOW) or user32.GetWindowTextLengthW(hwnd) <= 0:
            return
        own_root = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        if hwnd in (self.root.winfo_id(), own_root):
            return
        self.seen_hwnds.add(hwnd)
        self._restore_window_delayed(hwnd)

    def _scan_and_restore_new_windows(self) -> None:
        visible_now: set[int] = set()
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _enum_cb(hwnd: int, _: int) -> bool:
            if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                if not (ex_style & WS_EX_TOOLWINDOW) and user32.GetWindowTextLengthW(hwnd) > 0:
                    visible_now.add(hwnd)
            return True

        user32.EnumWindows(enum_proc(_enum_cb), 0)
        if not self.seen_hwnds:
            self.seen_hwnds = visible_now
            return

        brand_new_hwnds = [h for h in visible_now if h not in self.seen_hwnds]
        self.seen_hwnds = visible_now

        own_root = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        for hwnd in brand_new_hwnds:
            if hwnd not in (self.root.winfo_id(), own_root):
                self._restore_window_delayed(hwnd)


class PureWin32AppBar:
    """Zero-flicker native Win32 Layered Window for the Top Virtual Desktop Bar."""

    def __init__(self, on_left_click: Callable[[int, int], None], on_right_click: Callable[[int, int], None],
                 on_hover: Callable[[int], None], on_leave: Callable[[], None]) -> None:
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.on_hover = on_hover
        self.on_leave = on_leave
        self.hwnd: int = 0
        self.appbar_registered = False
        self.last_reserved_rect: Optional[Tuple[int, int, int, int]] = None
        self._dib_cache: Optional[Tuple[int, int, int, int, int, ctypes.c_void_p]] = None
        self._tracking_leave = False
        self._wndproc_ref = WNDPROC_TYPE(self._wndproc)
        self._create_window()

    def _create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "LEHA_Vortex_Layered_AppBar_Class"
        wc = WNDCLASSEXW(
            cbSize=ctypes.sizeof(WNDCLASSEXW),
            style=0,
            lpfnWndProc=self._wndproc_ref,
            cbClsExtra=0,
            cbWndExtra=0,
            hInstance=hinst,
            hIcon=None,
            hCursor=user32.LoadCursorW(None, ctypes.c_void_p(32512)),
            hbrBackground=None,
            lpszMenuName=None,
            lpszClassName=cls_name,
            hIconSm=None,
        )
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            _reg_err = kernel32.GetLastError()
            if _reg_err != 1410:  # 1410 = class already registered (harmless)
                _log_error(f"appbar-register-class winerror={_reg_err}")
        ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex_style, cls_name, "LEHA Desktop Bar", WS_POPUP, 0, 0, 100, 28, None, None, hinst, None)
        if not self.hwnd:
            _log_error(f"appbar-create-window winerror={kernel32.GetLastError()}")

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_LBUTTONDOWN:
            x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
            self.on_left_click(x, y)
            return 0
        if msg == WM_RBUTTONUP:
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            self.on_right_click(pt.x, pt.y)
            return 0
        if msg == WM_MOUSEMOVE:
            x = ctypes.c_short(lparam & 0xFFFF).value
            if not self._tracking_leave:
                self._tracking_leave = True
                tme = TRACKMOUSEEVENT(cbSize=ctypes.sizeof(TRACKMOUSEEVENT), dwFlags=TME_LEAVE, hwndTrack=hwnd, dwHoverTime=0)
                user32.TrackMouseEvent(ctypes.byref(tme))
            self.on_hover(x)
            return 0
        if msg == WM_MOUSELEAVE:
            self._tracking_leave = False
            self.on_leave()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _ensure_dib(self, w: int, h: int) -> Tuple[int, int, int, int, int, ctypes.c_void_p]:
        if self._dib_cache and self._dib_cache[0] == w and self._dib_cache[1] == h:
            return self._dib_cache
        if self._dib_cache:
            _, _, mem_dc, h_bitmap, old_bmp, _ = self._dib_cache
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(h_bitmap)
            gdi32.DeleteDC(mem_dc)

        screen_dc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        user32.ReleaseDC(0, screen_dc)

        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32)
        bits_ptr = ctypes.c_void_p()
        h_bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0)
        old_bmp = gdi32.SelectObject(mem_dc, h_bitmap)
        self._dib_cache = (w, h, mem_dc, h_bitmap, old_bmp, bits_ptr)
        return self._dib_cache

    def reserve_space(self, left: int, top: int, width: int, height: int) -> Tuple[int, int]:
        rect_tuple = (left, top, width, height)
        if rect_tuple == self.last_reserved_rect and self.appbar_registered:
            return left, top
        abd = APPBARDATA(cbSize=ctypes.sizeof(APPBARDATA), hWnd=self.hwnd, uEdge=ABE_TOP)
        if not self.appbar_registered:
            shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(abd))
            self.appbar_registered = True
        abd.rc.left, abd.rc.top, abd.rc.right, abd.rc.bottom = left, top, left + width, top + height
        shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))
        abd.rc.bottom = abd.rc.top + height
        shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))
        self.last_reserved_rect = rect_tuple
        return abd.rc.left, abd.rc.top

    def release_space(self) -> None:
        if self.appbar_registered:
            shell32.SHAppBarMessage(ABM_REMOVE, ctypes.byref(APPBARDATA(cbSize=ctypes.sizeof(APPBARDATA), hWnd=self.hwnd)))
            self.appbar_registered = False
            self.last_reserved_rect = None

    def hide(self) -> None:
        self.release_space()
        user32.ShowWindow(self.hwnd, 0)

    def render_frame(self, pil_rgba_img: Image.Image, left: int, top: int, opacity: float) -> None:
        w, h = pil_rgba_img.size
        final_left, final_top = self.reserve_space(left, top, w, h)

        r, g, b, a = pil_rgba_img.split()
        if opacity < 0.99:
            a = a.point(lambda p: int(p * opacity))
        black = Image.new("L", (w, h), 0)
        r, g, b = Image.composite(r, black, a), Image.composite(g, black, a), Image.composite(b, black, a)
        bgra_premul = Image.merge("RGBA", (b, g, r, a)).tobytes()

        _, _, mem_dc, _, _, bits_ptr = self._ensure_dib(w, h)
        ctypes.memmove(bits_ptr, bgra_premul, len(bgra_premul))

        if not user32.IsWindowVisible(self.hwnd):
            user32.ShowWindow(self.hwnd, 4)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, final_left, final_top, w, h, SWP_NOACTIVATE)

        screen_dc = user32.GetDC(0)
        user32.UpdateLayeredWindow(
            self.hwnd, screen_dc, ctypes.byref(POINT(final_left, final_top)), ctypes.byref(SIZE(w, h)),
            mem_dc, ctypes.byref(POINT(0, 0)), 0, ctypes.byref(BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)), ULW_ALPHA
        )
        user32.ReleaseDC(0, screen_dc)


class StudioRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/":
            base_dir = getattr(sys, "_MEIPASS", get_app_dir())
            html_path = os.path.join(base_dir, "studio_ui.html")
            if not os.path.exists(html_path):
                html_path = os.path.join(get_app_dir(), "studio_ui.html")
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/init":
            desktops = [{"num": num, "name": name} for num, name, _g in _vd_desktops()[1]]
            curr_num = current_desktop_number()
            curr_name = next((d["name"] for d in desktops if d["num"] == curr_num), f"Desktop {curr_num}")
            payload = json.dumps({"config": load_config_data(), "desktops": desktops, "monitors": enumerate_monitors(), "current_desktop": {"num": curr_num, "name": curr_name}}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload)

    def do_POST(self) -> None:
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        if self.path == "/api/save_config":
            if "autostart" in data:
                set_autostart(bool(data["autostart"]))
            disk_cfg = load_config_data()
            if isinstance(disk_cfg.get("snap_monitor_active_slots"), dict):
                merged_slots = dict(disk_cfg["snap_monitor_active_slots"])
                if isinstance(data.get("snap_monitor_active_slots"), dict):
                    for k_m, v_s in data["snap_monitor_active_slots"].items():
                        if v_s:
                            merged_slots[k_m] = str(v_s)
                data["snap_monitor_active_slots"] = merged_slots
            if isinstance(disk_cfg.get("snap_monitors"), dict):
                merged_mons = dict(data.get("snap_monitors") or {})
                for k_scope, v_layouts in disk_cfg["snap_monitors"].items():
                    if k_scope not in merged_mons:
                        merged_mons[k_scope] = v_layouts
                data["snap_monitors"] = merged_mons
            if isinstance(disk_cfg.get("snap_app_history"), dict):
                merged_hist = dict(data.get("snap_app_history") or {})
                merged_hist.update(disk_cfg["snap_app_history"])
                data["snap_app_history"] = merged_hist
            save_config_silent(data)
            if hasattr(self.server, "on_config_saved"):
                self.server.on_config_saved()
        elif self.path == "/api/rename_desktops":
            vd_guids = _vd_guid_list()
            for item in data:
                if item.get("name") and 1 <= int(item.get("num", 0)) <= len(vd_guids):
                    try:
                        key_path = _VD_REG + r"\Desktops\{" + vd_guids[int(item["num"]) - 1] + "}"
                        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as reg_key:
                            winreg.SetValueEx(reg_key, "Name", 0, winreg.REG_SZ, str(item["name"]))
                    except Exception:
                        pass
            if hasattr(self.server, "on_config_saved"):
                self.server.on_config_saved()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


class LehaVortexSuite:
    def __init__(self, auto_open_studio: bool = False) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda exc, val, tb: _log_error("ui-callback", val)  # type: ignore[method-assign]
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.root.title(f"{APP_NAME} ({APP_AUTHOR})")
        ico = ensure_app_icon()
        if os.path.exists(ico):
            try:
                self.root.iconbitmap(ico)
            except Exception:
                pass

        self.running = True
        self.manual_hidden = False
        self.fullscreen_hidden = False
        self.last_state: Optional[Tuple[Any, ...]] = None
        self.force_redraw = True
        self.pill_hitboxes: List[Tuple[int, int, int, int, int]] = []
        self.optimistic_active_num: Optional[int] = None
        self.optimistic_until_ms = 0
        self.studio_port = 0
        self.hover_x: Optional[int] = None
        self.hover_scales: Dict[int, float] = {}
        self.is_animating = False
        self._last_sys_check = 0
        self._cached_curr = 1
        self._cached_desktops: Tuple[Tuple[int, str], ...] = ((1, "Desktop 1"),)

        self.config = load_config_data()
        self._config_str = json.dumps(self.config, sort_keys=True)

        self.wtop = WindowTopProManager(self.root, lambda: self.config)
        self.snap = SnapGridManager(self.root, lambda: self.config)
        self.appbar = PureWin32AppBar(
            on_left_click=self._handle_bar_click,
            on_right_click=self._handle_bar_right_click,
            on_hover=self._handle_bar_hover,
            on_leave=self._handle_bar_leave,
        )

        self.start_local_studio_server()
        self.update_loop()
        if auto_open_studio:
            self.root.after(200, self.open_settings_popup)

    # --- AppBar WNDPROC entry points ------------------------------------
    # These four run INSIDE the native window procedure (a ctypes
    # callback dispatched by Tcl's own message pump). They must never
    # call Tk, tick_now or a modal Win32 loop directly -- see the
    # v1.2.2 note at APP_VERSION. They only enqueue; the main thread
    # does the work via _drain_ui_queue.
    def _handle_bar_hover(self, x: int) -> None:
        self._latest_hover_x = x
        if not getattr(self, "_hover_queued", False):
            self._hover_queued = True
            self._post_ui(self._flush_bar_hover)

    def _flush_bar_hover(self) -> None:
        self._hover_queued = False
        x = getattr(self, "_latest_hover_x", None)
        if x is not None:
            self._bar_hover_main(x)

    def _handle_bar_leave(self) -> None:
        self._latest_hover_x = None
        self._post_ui(self._bar_leave_main)

    def _handle_bar_click(self, x: int, y: int) -> None:
        self._post_ui(lambda: self._bar_click_main(x, y))

    def _handle_bar_right_click(self, x_root: int, y_root: int) -> None:
        self._post_ui(lambda: self.show_native_win32_menu(x_root, y_root))

    # --- AppBar actions (main thread, via the UI queue) -----------------
    def _bar_hover_main(self, x: int) -> None:
        if self.config.get("hover_zoom", False):
            was_idle = self.hover_x is None and not self.is_animating
            self.hover_x = x
            if was_idle:
                self.tick_now()

    def _bar_leave_main(self) -> None:
        if self.hover_x is not None:
            self.hover_x = None
            self.tick_now()

    def _bar_click_main(self, x: int, y: int) -> None:
        for x1, y1, x2, y2, target_num in self.pill_hitboxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                curr_num = current_desktop_number()
                if target_num != curr_num:
                    self.optimistic_active_num, self.optimistic_until_ms, self.force_redraw = target_num, int(kernel32.GetTickCount64()) + 1500, True
                    self.tick_now()
                    if not _switch_desktop_direct(target_num):
                        threading.Thread(target=self._switch_desktop_by_keys, args=(curr_num, target_num), daemon=True).start()
                break

    def _switch_desktop_by_keys(self, curr_num: int, target_num: int) -> None:
        steps = target_num - curr_num
        vk_arrow = 0x27 if steps > 0 else 0x25  # VK_RIGHT / VK_LEFT
        for _ in range(abs(steps)):
            user32.keybd_event(0x5B, 0, 0, 0)  # LWin down
            user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
            user32.keybd_event(vk_arrow, 0, 0, 0)
            user32.keybd_event(vk_arrow, 0, 0x0002, 0)
            user32.keybd_event(0x11, 0, 0x0002, 0)
            user32.keybd_event(0x5B, 0, 0x0002, 0)
            time.sleep(0.18)

    def show_native_win32_menu(self, x_root: int, y_root: int) -> None:
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, 0x0000, 1001, "Open LEHA Vortex Studio (Ctrl+Alt+V)")
            user32.AppendMenuW(hmenu, 0x0000, 1002, "Hide Bar (Ctrl+Alt+H)")
            user32.AppendMenuW(hmenu, 0x0800, 0, None)
            user32.AppendMenuW(hmenu, 0x0000, 1003, "Exit LEHA Vortex Suite")
            user32.SetForegroundWindow(self.appbar.hwnd)
            _, _, _, dpi_scale = self.get_primary_monitor_info()
            bar_bottom = int(self.config.get("height", 28) * dpi_scale)
            menu_y = max(y_root + 4, bar_bottom + 2)
            cmd = user32.TrackPopupMenu(hmenu, 0x0100 | 0x0002, x_root, menu_y, 0, self.appbar.hwnd, None)
            if cmd == 1001:
                self.open_settings_popup()
            elif cmd == 1002:
                self.toggle_visibility()
            elif cmd == 1003:
                self.quit_app()
        finally:
            user32.DestroyMenu(hmenu)

    def is_foreground_fullscreen(self) -> bool:
        if not self.config.get("auto_hide_fullscreen", False):
            return False
        fg_hwnd = user32.GetForegroundWindow()
        if not fg_hwnd or fg_hwnd == self.appbar.hwnd:
            return False
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(fg_hwnd, class_buf, 256)
        if class_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(fg_hwnd, ctypes.byref(rect)):
            return False
        return rect.left <= 0 and rect.top <= 0 and rect.right >= user32.GetSystemMetrics(0) and rect.bottom >= user32.GetSystemMetrics(1)

    def start_local_studio_server(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), StudioRequestHandler)
        server.on_config_saved = self.reload_config_immediately  # type: ignore[attr-defined]
        self.studio_port = server.server_address[1]
        def _serve_with_com() -> None:
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception:
                pass
            server.serve_forever()
        threading.Thread(target=_serve_with_com, daemon=True).start()

    def reload_config_immediately(self) -> None:
        def _apply() -> None:
            self.config = load_config_data()
            self._config_str = json.dumps(self.config, sort_keys=True)
            TEXT_BBOX_CACHE.clear()
            self.wtop.sync_live_properties()
            if not self.config.get("modules", {}).get("desktop_bar", True):
                self.appbar.hide()
            else:
                self.force_redraw = True
        self._post_ui(_apply)

    @staticmethod
    def get_primary_monitor_info() -> Tuple[int, int, int, float]:
        width = user32.GetSystemMetrics(0)
        try:
            scale = user32.GetDpiForSystem() / 96.0
        except Exception:
            scale = 1.0
        return 0, 0, width, scale

    def toggle_visibility(self) -> None:
        def _toggle() -> None:
            self.manual_hidden = not self.manual_hidden
            if self.manual_hidden:
                self.appbar.hide()
            else:
                self.force_redraw = True
        self.root.after(80, _toggle)

    def listen_hotkeys(self) -> None:
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        def _is_down(vk: int) -> bool:
            return bool(user32.GetKeyState(vk) & 0x8000)

        def _ll_cb(nCode: int, wParam: int, lParam: int) -> int:
            try:
                if nCode >= 0 and wParam in (0x0100, 0x0104):
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk = kb.vkCode
                    ctrl, alt, shift = _is_down(0x11), _is_down(0x12), _is_down(0x10)
                    win = _is_down(0x5B) or _is_down(0x5C)
                    mods = self.config.get("modules", {})

                    if ctrl and alt and not shift and not win:
                        if vk == 0x48:
                            self._post_ui(self.toggle_visibility)
                            return 1
                        if vk == 0x56:
                            self._post_ui(self.open_settings_popup)
                            return 1

                    if alt and shift and not ctrl and not win and mods.get("always_on_top", True):
                        if vk == VK_UP:
                            self._post_ui(lambda: self.wtop.adjust_opacity(38))
                            return 1
                        if vk == VK_DOWN:
                            self._post_ui(lambda: self.wtop.adjust_opacity(-38))
                            return 1

                    if alt and not ctrl and not shift and not win:
                        if 0x31 <= vk <= 0x36 and mods.get("snap_grid", True):
                            slot_str = str(vk - 0x30)
                            self._post_ui(lambda s=slot_str: self.snap.switch_monitor_layout(s))
                            return 1
                        if mods.get("always_on_top", True):
                            if vk == 0x54:
                                self._post_ui(self.wtop.toggle_pin)
                                return 1
                            if vk == VK_G:
                                self._post_ui(self.wtop.toggle_ghost_mode)
                                return 1

                    if not ctrl and not alt and not shift and not win:
                        if VK_F1 <= vk <= (VK_F1 + 5) and mods.get("snap_grid", True):
                            slot_str = str(vk - VK_F1 + 1)
                            self._post_ui(lambda s=slot_str: self.snap.switch_monitor_layout(s))
                            return 1
                        if vk == 0x78 and mods.get("quick_shell", True):
                            threading.Thread(target=launch_powershell, args=(False, self.config.get("shell_use_wt", True)), daemon=True).start()
                            return 1
                        if vk == 0x79 and mods.get("quick_shell", True):
                            threading.Thread(target=launch_powershell, args=(True, self.config.get("shell_use_wt", True)), daemon=True).start()
                            return 1

            except Exception as exc:
                _log_error("kb-hook", exc)
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        hook_cb = HOOKPROC(_ll_cb)
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        h_hook = user32.SetWindowsHookExW(13, hook_cb, None, 0)

        msg = wintypes.MSG()
        try:
            while self.running:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                kernel32.Sleep(8)
        finally:
            if h_hook:
                user32.UnhookWindowsHookEx(h_hook)

    def get_desktop_app_counts(self) -> Dict[int, int]:
        now_ms = int(kernel32.GetTickCount64())
        if hasattr(self, "_app_count_cache") and (now_ms - getattr(self, "_last_app_count_ms", 0) < 600):
            return self._app_count_cache  # type: ignore[return-value]
        counts: Dict[int, int] = {}
        global _vdm_instance
        vdm = _get_vdm()
        vd_guids = _vd_guid_list()
        if vdm is not None and vd_guids:
            guid_to_num = {g: i + 1 for i, g in enumerate(vd_guids)}
            visible: List[int] = []
            enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _enum_cb(hwnd: int, _lparam: int) -> bool:
                if user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                    if not (user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW) and user32.GetWindowTextLengthW(hwnd) > 0:
                        if not user32.GetWindow(hwnd, GW_OWNER):
                            visible.append(hwnd)
                return True

            try:
                user32.EnumWindows(enum_proc(_enum_cb), 0)
                for hwnd in visible:
                    if not user32.IsWindow(hwnd):
                        continue
                    try:
                        desk_guid = vdm.GetWindowDesktopId(hwnd)
                        num = guid_to_num.get(str(desk_guid).strip("{}").lower())
                        if num:
                            counts[num] = counts.get(num, 0) + 1
                    except Exception as exc:
                        if "0x800706ba" in str(exc).lower() or "0x800706be" in str(exc).lower():
                            _vdm_instance = None
                            break
            except Exception:
                _vdm_instance = None
        self._app_count_cache, self._last_app_count_ms = counts, now_ms
        return counts

    def get_all_desktops_data(self) -> Tuple[int, Tuple[Tuple[int, str], ...]]:
        curr_num, desks = _vd_desktops()
        app_counts = self.get_desktop_app_counts()
        items: List[Tuple[int, str]] = []
        for num, name, _guid in desks:
            cnt = app_counts.get(num, 0)
            items.append((num, f"{name} ({cnt})" if cnt > 0 else name))
        return curr_num, tuple(items)

    def open_settings_popup(self) -> None:
        url = f"http://127.0.0.1:{self.studio_port}/"
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            shutil.which("msedge"),
        ]
        edge_exe = next((p for p in edge_paths if p and os.path.exists(p)), None)
        if edge_exe:
            subprocess.Popen(
                [edge_exe, f"--app={url}", "--window-size=920,680", "--no-first-run", "--no-default-browser-check", "--disable-extensions"],
                creationflags=0x00000008,
                close_fds=True,
            )
        else:
            os.startfile(url)

    def resolve_font(self, size: int, is_active: bool) -> Tuple[Any, Tuple[int, str, bool]]:
        cfg = self.config
        w, it = cfg.get("font_weight", "bold"), cfg.get("font_italic", False)
        if is_active and w == "light":
            w = "bold"
        key = (size, w, it)
        if key in FONT_CACHE:
            return FONT_CACHE[key], key
        path = FONTS["bold_italic"] if (it and w == "bold") else (FONTS["italic"] if it else FONTS.get(w, FONTS["regular"]))
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            f = ImageFont.load_default()
        FONT_CACHE[key] = f
        return f, key

    @staticmethod
    def get_cached_bbox(draw: ImageDraw.ImageDraw, text: str, font: Any, font_key: Tuple[int, str, bool]) -> Tuple[int, int, int, int]:
        k = (text, font_key)
        res = TEXT_BBOX_CACHE.get(k)
        if res is None:
            res = draw.textbbox((0, 0), text, font=font)
            TEXT_BBOX_CACHE[k] = res
        return res

    def apply_text_case(self, text: str) -> str:
        c = self.config.get("text_case", "normal")
        return text.upper() if c == "upper" else (text.lower() if c == "lower" else text)

    def get_pill_visuals(
        self, d_num: int, is_active: bool
    ) -> Tuple[Optional[Tuple[int, int, int, int]], Tuple[int, int, int, int], Tuple[int, int, int, int], Optional[Tuple[int, int, int, int]]]:
        cfg = self.config
        style, idle_bg_on = cfg.get("pill_style", "filled"), cfg.get("idle_has_bg", True)
        if is_active:
            if cfg["color_mode"] == "mono":
                bg, fg, accent = hex_to_rgba(cfg["mono_active_bg"], 245), hex_to_rgba(cfg["mono_active_text"], 255), hex_to_rgba(cfg["mono_accent"], 255)
            else:
                p = cfg["multi_pills"].get(str(d_num), LUXURY_MULTI_PALETTE[(d_num - 1) % len(LUXURY_MULTI_PALETTE)])
                bg, fg, accent = hex_to_rgba(p["bg"], 245), hex_to_rgba(p["fg"], 255), hex_to_rgba(p.get("accent", p["fg"]), 255)
            outline = accent if cfg.get("active_border", True) else None
            if style == "minimal":
                return None, accent, accent, None
            elif style == "outline":
                return (bg[0], bg[1], bg[2], 45), accent, accent, accent
            return bg, fg, accent, outline
        else:
            bg = hex_to_rgba(cfg["mono_idle_bg"], 215) if idle_bg_on else None
            fg = hex_to_rgba(cfg["mono_idle_text"], 255)
            outline = hex_to_rgba(cfg["border_color"], 180) if idle_bg_on else None
            return (None, fg, fg, outline if style == "outline" else None) if style in ("minimal", "outline") else (bg, fg, fg, outline)

    def render_workspaces_bar(
        self, curr_num: int, desktops_list: Tuple[Tuple[int, str], ...], mon_left: int, mon_top: int, mon_width: int, scale: float
    ) -> None:
        cfg = self.config
        is_trans = cfg.get("bar_transparent", True)
        bar_height = int(cfg["height"] * scale)
        base_font_size = max(8, min(bar_height - 4, int(int(cfg.get("font_size", 13)) * scale)))

        canvas_bg = (0, 0, 0, 2) if is_trans else hex_to_rgba(cfg["bg_color"], 245)
        img = Image.new("RGBA", (mon_width, bar_height), canvas_bg)
        draw = ImageDraw.Draw(img)
        if not is_trans:
            draw.line([(0, bar_height - 1), (mon_width, bar_height - 1)], fill=hex_to_rgba(cfg["border_color"], 255), width=1)

        pad_x = int(14 * scale)
        cy = bar_height // 2
        avail_width = mon_width - (pad_x * 2)
        base_pad_x = int({"compact": 10, "normal": 16, "wide": 30}.get(cfg["pill_size"], 16) * scale)
        active_extra_pad = int(cfg.get("active_pop_padding", 2) * scale)
        pill_gap = int(cfg.get("pill_gap", 22) * scale)
        pill_radius = int(cfg.get("pill_radius", 9) * scale)

        pills_meta: List[Dict[str, Any]] = []
        total_base_w = 0
        for d_num, d_name in desktops_list:
            is_active = d_num == curr_num
            f_base, fk_base = self.resolve_font(base_font_size, is_active)
            label_str = self.apply_text_case(d_name)
            bbox_l = self.get_cached_bbox(draw, label_str, f_base, fk_base)
            lw = bbox_l[2] - bbox_l[0]
            this_pad = base_pad_x + (active_extra_pad if is_active else 0)
            pw = this_pad * 2 + lw
            pills_meta.append({"num": d_num, "label": label_str, "active": is_active, "base_pw": pw, "base_pad": this_pad})
            total_base_w += pw
        if len(pills_meta) > 1:
            total_base_w += pill_gap * (len(pills_meta) - 1)

        layout_mode = cfg.get("layout_mode", "center")
        est_x = max(pad_x, (mon_width // 2) - (total_base_w // 2)) if layout_mode == "center" else pad_x
        still_animating = False

        for pm in pills_meta:
            d_num = pm["num"]
            est_cx = est_x + pm["base_pw"] / 2.0
            est_x += pm["base_pw"] + pill_gap
            target_zoom = 1.0
            if cfg.get("hover_zoom", False) and self.hover_x is not None:
                dist = abs(self.hover_x - est_cx)
                radius = float(cfg.get("hover_radius", 90)) * scale
                if dist < radius:
                    target_zoom = 1.0 + (float(cfg.get("max_zoom", 1.45)) - 1.0) * (0.5 * (1.0 + math.cos(math.pi * dist / radius)))
            cur_zoom = self.hover_scales.get(d_num, 1.0)
            new_zoom = cur_zoom + (target_zoom - cur_zoom) * float(cfg.get("smoothness", 0.35))
            if abs(target_zoom - new_zoom) < 0.002:
                new_zoom = target_zoom
            else:
                still_animating = True
            self.hover_scales[d_num] = new_zoom
            pm["zoom"] = new_zoom

            z_font_size = max(8, int(round(base_font_size * (1.0 + (new_zoom - 1.0) * 0.65))))
            f_use, fk_use = self.resolve_font(z_font_size, pm["active"])
            bbox_z = self.get_cached_bbox(draw, pm["label"], f_use, fk_use)
            lw_z, lh_z = bbox_z[2] - bbox_z[0], bbox_z[3] - bbox_z[1]
            pad_z = int(round(pm["base_pad"] * new_zoom))
            pm.update({"font": f_use, "lw": lw_z, "lh": lh_z, "bbox": bbox_z, "pad": pad_z, "pw": pad_z * 2 + lw_z})

        self.is_animating = still_animating
        total_pills_width = sum(p["pw"] for p in pills_meta) + (pill_gap * max(0, len(pills_meta) - 1))
        equal_pw = max(int(40 * scale), (avail_width - pill_gap * (len(pills_meta) - 1)) // len(pills_meta)) if (layout_mode == "stretch" and pills_meta) else 0
        cur_x = pad_x if layout_mode != "center" else max(pad_x, (mon_width // 2) - (total_pills_width // 2))

        new_hitboxes: List[Tuple[int, int, int, int, int]] = []
        for pm in pills_meta:
            pw = equal_pw if layout_mode == "stretch" else pm["pw"]
            if cur_x + pw > mon_width - pad_x and layout_mode != "stretch":
                break
            pill_top, pill_bottom = 0, bar_height - 1
            x1, x2 = cur_x, cur_x + pw
            fill_bg, fill_fg, fill_accent, fill_outline = self.get_pill_visuals(pm["num"], pm["active"])
            if fill_bg is not None or fill_outline is not None:
                draw.rounded_rectangle([x1, pill_top, x2, pill_bottom], radius=pill_radius, fill=fill_bg, outline=fill_outline, width=max(1, int(1.6 * scale)) if pm["active"] else 1)
            if pm["active"] and cfg.get("active_underline", False):
                ind_w = min(pw - int(12 * scale), int(26 * scale))
                if ind_w > 6:
                    ix1 = x1 + (pw - ind_w) // 2
                    iy = pill_bottom - max(1, int(1.5 * scale))
                    draw.line([(ix1, iy), (ix1 + ind_w, iy)], fill=fill_accent, width=max(2, int(2.2 * scale)))
            text_x = (x1 + max(int(6 * scale), (pw - pm["lw"]) // 2)) if layout_mode == "stretch" else (x1 + pm["pad"])
            draw.text((text_x, cy - (pm["lh"] // 2) - pm["bbox"][1]), pm["label"], font=pm["font"], fill=fill_fg)
            new_hitboxes.append((x1, 0, x2, bar_height, pm["num"]))
            cur_x = x2 + pill_gap

        self.pill_hitboxes = new_hitboxes
        self.appbar.render_frame(img, mon_left, mon_top, float(cfg.get("opacity", 0.94)))

    def tick_now(self) -> None:
        if self.config.get("modules", {}).get("always_on_top", True):
            self.wtop.sync_borders(bool(self.snap.dragging_hwnd))
        try:
            self.snap.poll_drag_and_restore()
        except Exception as exc:
            _log_error("drag-poll", exc)
        if self.manual_hidden or not self.config.get("modules", {}).get("desktop_bar", True):
            return

        now_ms = int(kernel32.GetTickCount64())
        is_hovering = self.hover_x is not None or self.is_animating
        if not is_hovering or (now_ms - self._last_sys_check >= 45):
            self._last_sys_check = now_ms
            is_fs = self.is_foreground_fullscreen()
            if is_fs and not self.fullscreen_hidden:
                self.fullscreen_hidden = True
                self.appbar.hide()
                return
            elif not is_fs and self.fullscreen_hidden:
                self.fullscreen_hidden = False
                self.force_redraw = True
            if self.fullscreen_hidden:
                return
            curr_num, desktops_list = self.get_all_desktops_data()
            self._cached_curr, self._cached_desktops = curr_num, desktops_list
        else:
            curr_num, desktops_list = self._cached_curr, self._cached_desktops
        # A pill click sets an optimistic active desktop: it must light
        # up in THIS render and STAY lit until the desktop data confirms
        # the switch (or a generous timeout passes) -- even when the
        # data above came from the 200 ms cache. The mouse is hovering
        # the bar at click time, so the cached branch is the one a click
        # almost always takes. History: before v1.2.4 the override only
        # applied on a data refresh, so the highlight trailed the jump
        # by up to ~200 ms; v1.2.4 applied it per render but only for 4
        # ticks, which at the 8 ms hover redraw rate burned in ~30 ms --
        # long before the cached data caught up -- so the pill blinked
        # back to the old desktop once (the flash the user reported).
        # v1.2.5 therefore holds the override by time + confirmation,
        # not by tick count.
        if self.optimistic_active_num is not None:
            if curr_num == self.optimistic_active_num or now_ms > self.optimistic_until_ms:
                self.optimistic_active_num = None
            else:
                curr_num = self.optimistic_active_num

        mon_left, mon_top, mon_width, scale = self.get_primary_monitor_info()
        current_state = (curr_num, desktops_list, mon_left, mon_top, mon_width, scale, self.hover_x, self._config_str)
        if current_state != self.last_state or self.force_redraw or self.is_animating:
            self.last_state, self.force_redraw = current_state, False
            self.render_workspaces_bar(curr_num, desktops_list, mon_left, mon_top, mon_width, scale)

    def _post_ui(self, fn: Callable[[], None]) -> None:
        """Thread-safe hand-off: worker threads (keyboard hook, Studio server)
        must never call into Tk/Tcl directly -- under Tk 9 that can abort the
        whole process without leaving a Python traceback (vortex_error.log,
        22/11/2026). They enqueue here; the main thread executes."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception as exc:
                _log_error("ui-queue", exc)

    def update_loop(self) -> None:
        if not self.running:
            return
        self._drain_ui_queue()
        try:
            self.tick_now()
        except Exception as exc:
            _log_error("update-loop", exc)
        self.root.after(6, self.update_loop)

    def quit_app(self) -> None:
        self.running = False
        try:
            self.wtop.restore_all_windows()
        except Exception:
            pass
        self.appbar.hide()
        self.root.after(80, self.root.destroy)

    def show_bar_context_menu(self, x_root: int, y_root: int) -> None:
        if hasattr(self, "_ctx_win") and self._ctx_win and self._ctx_win.winfo_exists():
            self._ctx_win.destroy()
        popup = tk.Toplevel(self.root)
        self._ctx_win = popup
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="#2a2a35", padx=1, pady=1)
        box = tk.Frame(popup, bg="#18181b")
        box.pack(fill="both", expand=True)

        def _close(_: Any = None) -> None:
            if popup.winfo_exists():
                popup.destroy()

        def _add_item(label: str, cmd: Callable[[], None], danger: bool = False) -> None:
            btn = tk.Label(box, text=label, bg="#18181b", fg="#f87171" if danger else "#f4f4f5", font=("Segoe UI", 9), anchor="w", padx=12, pady=6, cursor="hand2")
            btn.pack(fill="x")
            btn.bind("<Enter>", lambda _: btn.configure(bg="#27272f"))
            btn.bind("<Leave>", lambda _: btn.configure(bg="#18181b"))
            btn.bind("<Button-1>", lambda _: (_close(), cmd()))

        _add_item("Open LEHA Vortex Studio (Ctrl+Alt+V)", self.open_settings_popup)
        _add_item("Hide Bar (Ctrl+Alt+H)", self.toggle_visibility)
        tk.Frame(box, bg="#2a2a35", height=1).pack(fill="x", pady=2)
        _add_item("Exit LEHA Vortex Suite", self.quit_app, danger=True)

        popup.update_idletasks()
        sw = user32.GetSystemMetrics(0)
        pw, ph = popup.winfo_reqwidth(), popup.winfo_reqheight()
        hwnd_p = user32.GetParent(popup.winfo_id()) or popup.winfo_id()
        user32.SetWindowPos(hwnd_p, HWND_TOPMOST, min(x_root, sw - pw - 8), y_root + 4, pw, ph, SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd_p)
        popup.focus_force()
        popup.bind("<FocusOut>", _close)
        popup.bind("<Escape>", _close)
        popup.after(3000, _close)

    def run(self) -> None:
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        gc.disable()
        def _periodic_gc() -> None:
            if self.running:
                gc.collect()
                self.root.after(5000, _periodic_gc)
        self.root.after(5000, _periodic_gc)
        threading.Thread(target=self.listen_hotkeys, daemon=True).start()
        self.root.mainloop()


if __name__ == "__main__":
    LehaVortexSuite(auto_open_studio="--open-studio" in sys.argv).run()