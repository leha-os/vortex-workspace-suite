# LEHA Vortex Workspace Suite (v1.3.8)

Personal project by Le Ha


---

## Global Shortcuts

| Shortcut | Action |
| :--- | :--- |
| **`F1` .. `F6` (defaults)** | Switch the six FancyZones layout slots; assign a separate global shortcut to each slot in Studio |
| **`Alt+F6` (default)** | Capture the monitor under the cursor to PNG and copy it to the clipboard; the shortcut is configurable in Studio |
| **`Alt+F8` (default)** | Capture a dragged rectangle only (region screenshot) |
| **`Alt+F7` (default)** | Capture the active window only |
| **`Ctrl+Alt+X` (default)** | Extract the archives selected in Explorer to same-name folders; the shortcut is configurable in Studio |
| **`F9` / `F10`** | Open PowerShell (Standard / Administrator) at active or hovered Explorer folder |
| **`Alt + T`** | Toggle Always On Top + DWM-accurate Border Highlight |
| **`Alt + Shift + Up / Down`** | Increase / Decrease active window opacity (`20%` – `100%`) |
| **`Alt + G`** | Toggle Ghost Click-Through Mode on active window |
| **`Ctrl + Alt + V`** / **Right-Click Bar** | Open **LEHA Vortex Studio** (Day / Night Theme) |
| **`Ctrl + Alt + H`** | Toggle Desktop Bar visibility |

Hovering a pill zooms it gently and tints it with the hover color (default amber `#F59E0B`, changeable in Studio → Desktop Bar PRO → Effects & Colors). Hover text defaults to white `#FFFFFF`, changeable in the same card.

---

## Build, Code-Sign & Release

~~~powershell
pip install -r requirements.txt
.\BUILD_AND_SIGN.ps1
~~~

---

## License

Released under the **MIT License** — Copyright (c) 2026 **Le Ha**.
