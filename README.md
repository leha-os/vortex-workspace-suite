# LEHA Vortex Workspace Suite (v1.5.8)

A Windows 10/11 workspace and window management engine: a virtual
desktop bar, per-desktop snap layouts, and window utilities in a
single-file application.

## Features

- **Desktop Bar** — pure Win32 layered AppBar showing every virtual
  desktop with per-desktop window counts; click a pill to jump
  straight to that desktop.
- **Snap layouts (FancyZones-style)** — per-monitor, per-virtual-desktop
  zone layouts (F1–F6), zone merging, coupled edge resize, a visual
  layout editor, and automatic restore of each window's last zone.
- **WindowTop Pro** — always-on-top with a DWM-accurate border, live
  opacity control, and ghost click-through mode.
- **Quick Shell** — open PowerShell (standard or elevated) at the
  active or hovered Explorer folder.
- **Vortex Studio** — a local web UI (EN/VI, dark/light) for all
  configuration; settings live in `config.json` next to the executable.

## Global Shortcuts

| Shortcut | Action |
| :--- | :--- |
| **`F1` .. `F6`** | Switch active snap layout profile for the current virtual desktop on the monitor under the cursor (each virtual desktop keeps its own layout per monitor) |
| **`F9` / `F10`** | Open PowerShell (Standard / Administrator) at active or hovered Explorer folder |
| **`Alt + T`** | Toggle Always On Top + DWM-accurate Border Highlight |
| **`Alt + Shift + Up / Down`** | Increase / Decrease active window opacity (`20%` – `100%`) |
| **`Alt + G`** | Toggle Ghost Click-Through Mode on active window |
| **`Ctrl + Alt + V`** / **Right-Click Bar** | Open **LEHA Vortex Studio** (Day / Night Theme) |
| **`Ctrl + Alt + H`** | Toggle Desktop Bar visibility |

## Requirements

- Windows 10 or 11 (Windows 11 24H2 recommended)
- To run from source: Python 3.13 with `Pillow` and `comtypes`
- To build: PyInstaller (the build script installs it if missing) and,
  for signing, a code-signing certificate (see below)

## Build, Code-Sign & Release

Run the build script from the source folder:

~~~powershell
.\BUILD_AND_SIGN.ps1
~~~

The script builds a one-file executable with PyInstaller, signs it
with Authenticode (self-signed certificate `CN=Le Ha, O=LEHA Studio,
C=VN`, created on first use and trusted in the CurrentUser stores),
and writes two archives to the Desktop:

- `LEHA_Vortex_Workspace_Suite_v1.5.8_Signed.zip` — portable release
- `LEHA_Vortex_Workspace_Suite_v1.5.8_Source.zip` — clean source backup

## License

Released under the **MIT License** — Copyright (c) 2026 **Le Ha**.
