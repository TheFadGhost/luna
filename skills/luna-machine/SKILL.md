---
name: luna-machine
description: Ground truth about THIS machine — hardware ceilings, what desktop stack actually runs, which paths survive updates, and the things that silently fail here. Read before proposing any change to the desktop, installing anything, or reasoning about performance. Triggers - omarchy, hyprland, quickshell, bar widget, voxtype, dictation, luna, piper, TTS, RAM, performance, "why is this slow", installing packages.
---

# This machine

## Hardware ceiling — check against this BEFORE proposing anything
- AMD Ryzen 5 4500U, 6 cores, **7.1 GB RAM**, Vega 6 iGPU. **No NPU, no CUDA, no ROCm.**
- Only **~3–4 GB actually free** once the desktop and a browser are up. 14 GB swap, normally unused.
- 62 GB free on `/home`.
- **Do not propose**: a resident Chromium/Electron app, a local LLM above ~2 GB, anything GPU-accelerated.
  A 3B-class Q4 model is the realistic ceiling and it still competes with the desktop.

## Desktop stack — get this right, it is a common wrong assumption
- Omarchy 4.x: **Hyprland + Quickshell** (`omarchy-shell`). **NOT Waybar. NOT GNOME. NOT KDE.**
- Bar widgets are QML in `~/.config/omarchy/bar/modules/`, or plugins in `~/.config/omarchy/plugins/`.
- Terminal is **foot**. Default browser Chromium. Theme is `monochrome` (pure greyscale).
- Never suggest Waybar/AGS/Astal as "the bar" — they are not what runs here.

## What survives `omarchy update`
- SAFE (user-owned): everything under `~/.config/omarchy/`, `~/.config/hypr/*.lua`,
  `~/.local/share/`, `~/Work/`.
- CLOBBERED: everything under `/usr/share/omarchy/` — it is pacman-owned. Never edit in place.

## Things that silently fail here
- `sudo` REQUIRES A PASSWORD. Unattended installs are impossible; prefer a venv or `~/.local`.
- **IPv6 is broken.** `curl -6` returns nothing, `-4` works. `localhost` resolves to `::1` first and
  `/etc/gai.conf` has no IPv4 precedence rule. This breaks OAuth CLIs that bind IPv4 loopback.
- `/usr/bin/time`, `bc`, `xxd`, `PIL`, `fontTools` are NOT installed. ImageMagick and `od` are.
- `~/.local/bin/claude` is a shim that mutates global mise config on every call — use the mise install path.
- Quickshell `Text` with a Nerd Font glyph gets subpixel colour fringing on dark backgrounds.
- voxtype does not re-read its config without a restart, and `hyprctl dispatch` is wrapped in a Lua
  DSL that rejects `[bracket rule]` syntax.

## Never do these
- **Never `pkill -f`.** It matches your own command line and kills your shell. This has happened twice.
- Never signal a process you did not spawn. Other agent sessions run concurrently on this box.
- Never restart `omarchy-shell` — it segfaults during plugin hot-reload.
- Never write a secret into a file that gets committed. Keys live in `~/.config/*/secrets.env`, chmod 600.

## Where things live
- Luna the assistant: `~/Work/luna` (code), `~/.local/share/luna` (memory, jobs, audit).
- The change log that must be updated with every desktop change: `~/.config/omarchy/CUSTOMISATIONS.md`.
- Public rice repo: `~/Work/omarchy-monochrome`.
