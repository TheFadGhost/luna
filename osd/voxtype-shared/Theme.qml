pragma Singleton

// Voxtype Quickshell theme singleton — Omarchy monochrome build.
//
// LOCAL OVERRIDE of /usr/share/voxtype/quickshell/voxtype-shared/Theme.qml.
// Do not edit the packaged copy; `voxtype setup quickshell --force` will
// overwrite THIS file, so re-apply from ~/Work/luna after a voxtype update.
//
// Every colour and dimension below is bound to an Omarchy design token
// (qs.Commons Color / Style / Util) rather than hardcoded. The Omarchy
// Commons and Ui modules are reachable because ../Commons and ../Ui are
// symlinks to /usr/share/omarchy/shell/*, and their qmldir files declare
// `module qs.Commons` / `module qs.Ui` — so `import qs.Commons` resolves
// inside voxtype's OWN Quickshell instance, which is a different process
// to omarchy-shell.
//
// GOTCHA: Color's FileViews for colors.toml / shell.toml are
// `watchChanges: false`. omarchy-shell refreshes them by pushing the
// payload over shell IPC after `omarchy theme-set`; that IPC never
// reaches this process. So they never load at all here unless something
// calls reload(). `refresh()` below does that, and OsdSurface calls it
// every time the daemon leaves idle — which also means a theme switch is
// picked up on the next recording without watching the theme directory
// (`omarchy theme-set` does rm -rf + mv on it, so a file watch would go
// stale anyway).

import QtQuick
import qs.Commons

QtObject {
    id: theme

    /// Re-read the live Omarchy palette + shell tokens. Cheap; safe to
    /// call on every OSD show.
    function refresh() {
        Color.colorsFile.reload();
        Color.shellFile.reload();
        Style.refresh();
    }

    // ------------------------------------------------------------ colours
    //
    // Monochrome themes have no hue to spend, so daemon state is encoded as
    // a brightness ramp: recording is the brightest thing on screen, and
    // each subsequent state steps down. On a coloured Omarchy theme the
    // same bindings pick up that theme's accent/urgent hues instead.

    /// Card background. The notifications surface role, so the OSD sits in
    /// the same visual family as Omarchy's notification stack.
    property color bgColor: Color.notifications.background

    /// Theme accent.
    property color accentColor: Color.accent

    /// Idle — dimmest.
    property color idleColor: Util.alpha(Color.muted, 0.6)

    /// Recording — brightest: the mic is live.
    property color recordingColor: Color.foreground

    /// Streaming (live partial tokens) — mid.
    property color streamingColor: Color.accent

    /// Transcribing (model working, mic closed) — dim.
    property color transcribingColor: Color.muted

    /// Foreground text.
    property color textColor: Color.notifications.text

    /// Waveform body. Held back from full brightness so the peak tick and
    /// the state-tinted border still read against it.
    property color waveformColor: Util.alpha(Color.foreground, 0.75)

    /// Waveform held-peak tick — full foreground.
    property color waveformPeakColor: Color.foreground

    /// Peak meter "safe" zone (-inf..-12 dBFS).
    property color meterLowColor: Util.alpha(Color.foreground, 0.35)

    /// Peak meter "warning" zone (-12..-3 dBFS).
    property color meterMidColor: Util.alpha(Color.foreground, 0.65)

    /// Peak meter "danger" zone (-3..0 dBFS). Color.urgent, so a coloured
    /// theme turns this red while monochrome keeps it grey.
    property color meterHighColor: Color.urgent

    // ----------------------------------------------------------- geometry
    //
    // Raw pixel numbers go through Style.space() so `[spacing] scale` and
    // the font base size move them with the rest of the desktop.

    /// Mirrors Hyprland's decoration:rounding, via Style.
    property int cornerRadius: Style.cornerRadius

    /// Inner padding for the OSD card.
    property int padding: Style.spacing.popupPadding

    /// Border width — the same hairline every other Omarchy surface uses.
    property int borderWidth: Style.normalBorderWidth

    /// Gap between the icon column and the meters.
    property int contentGap: Style.spacing.controlGap

    /// Gap between the waveform and the peak meter.
    property int meterGap: Style.spacing.sm

    /// Distance from the bottom screen edge. Derived from the bar's own
    /// cross-axis size so the OSD clears a bottom bar if the user moves it.
    property int marginPx: Style.bar.sizeHorizontal + Style.gapsOut * 2

    /// OSD surface width.
    property int defaultWidthPx: Style.space(400)

    /// Icon column width and glyph size.
    property int iconSlotPx: Style.space(28)
    property int iconFontPx: Style.font.display
    property string fontFamily: Style.font.family

    // Icon glyphs are Nerd Font private-use codepoints. Omarchy's UI font does
    // not contain them, so Qt silently falls back to Noto Color Emoji - a colour
    // bitmap font that IGNORES the `color` property, which is why the mic
    // rendered blue in a monochrome theme. Pin the icon slot to a font that
    // actually has the glyphs so the tint applies.
    property string iconFontFamily: "JetBrainsMono Nerd Font"

    /// Waveform canvas height.
    property int waveformHeightPx: Style.space(36)

    /// Peak meter height.
    property int meterHeightPx: Style.space(6)

    /// Total card height: padding + waveform + gap + meter + padding.
    property int defaultHeightPx: padding * 2 + waveformHeightPx + meterGap + meterHeightPx

    /// Dim factor applied while recording but below the VAD threshold.
    property real idleDimOpacity: 1.0 - Style.selectedFillAlpha

    // ------------------------------------------------------------- signal
    // Untouched from the packaged Theme.qml: these mirror src/osd/visual.rs
    // and OsdConfig::default, and are not theme concerns.

    property real waveformWindowSecs: 3.0
    property real peakDecayDbPerSec: 6.0
    property real waveformGain: 10.0
    property real meterFloorDbfs: -60.0
    property real defaultOpacity: 0.95
}
