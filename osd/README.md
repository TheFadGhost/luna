# voxtype OSD — Omarchy monochrome override

Source of truth for the two QML files that restyle voxtype's on-screen voice
indicator. The live copies are at `~/.local/share/voxtype/quickshell/`; these
are here so the customisation survives a `voxtype setup quickshell --force`,
which overwrites that tree from the package defaults.

## What this is

voxtype ships three OSD frontends. The default, `voxtype-osd-gtk4`, draws its
waveform in Cairo from **compiled-in colour constants** — it loads no CSS and
has no styled widget tree, so it cannot be themed. **Do not retry that.** The
fix was to switch frontends, not to restyle the GTK4 one:

    # ~/.config/voxtype/config.toml
    [osd]
    frontend = "quickshell"

That makes the daemon launch `voxtype-osd-quickshell`, which is a thin
launcher around `qs -d -p <dir>` and renders the QML tree in
`~/.local/share/voxtype/quickshell/` (installed once with
`voxtype setup quickshell --skip-bridge`).

## How it picks up the Omarchy theme

`~/.local/share/voxtype/quickshell/` contains two symlinks:

    Commons -> /usr/share/omarchy/shell/Commons
    Ui      -> /usr/share/omarchy/shell/Ui

Their `qmldir` files declare `module qs.Commons` / `module qs.Ui`, and
Quickshell puts the config root on the QML import path — so `import qs.Commons`
resolves inside voxtype's own Quickshell instance even though that is a
different *process* to `omarchy-shell`. Every colour and dimension in
`voxtype-shared/Theme.qml` is then bound to `Color.*` / `Style.*`, and the card
itself is Omarchy's `Ui.BorderSurface` rather than a hand-rolled Rectangle.

## Restoring after a voxtype update

    voxtype setup quickshell --skip-bridge --force
    ln -sfn /usr/share/omarchy/shell/Commons ~/.local/share/voxtype/quickshell/Commons
    ln -sfn /usr/share/omarchy/shell/Ui      ~/.local/share/voxtype/quickshell/Ui
    cp -r ~/Work/luna/osd/OsdSurface.qml ~/Work/luna/osd/voxtype-shared \
          ~/.local/share/voxtype/quickshell/
    systemctl --user restart voxtype

Nothing under `/usr/share/voxtype` or `/usr/lib/voxtype` is ever modified.

See `~/.config/omarchy/CUSTOMISATIONS.md` §8a.4 for the gotchas.
