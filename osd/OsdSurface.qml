// Voxtype on-screen display surface (Quickshell frontend).
//
// Renders a glassy card with:
//   - State icon + tint (recording / streaming / transcribing)
//   - Scrolling waveform of recent mic peaks (3-second window, 100 Hz)
//   - Peak meter bar with held-peak tick (-60 dB floor, color zones at
//     -12 and -3 dBFS to match the GTK4 and native frontends)
//
// Driven entirely from a parent-provided AudioBridge: the parent is
// expected to subscribe to AudioBridge once and pass it in via the
// `audio` property so the OsdSurface, EnginePicker, and MeetingControls
// share one sidecar process rather than each spawning their own.
//
// Visibility follows `daemonState`: hidden when idle/empty, shown
// otherwise. The component is layered as a WlrLayer.Overlay surface so
// it floats above all other windows without taking input focus.

import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui as Ui
import "voxtype-shared" as VT

PanelWindow {
    id: panel

    /// Current daemon state: idle / recording / streaming / transcribing.
    /// Wired by the parent (typically from VT.StateReader.state).
    property string daemonState: "idle"

    /// The audio bridge instance whose frameReceived signal drives the
    /// waveform. Passed in by the parent so it's shared with sibling
    /// widgets that also want VAD / peak data.
    property var audio: null

    visible: daemonState !== "idle" && daemonState !== ""
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    color: "transparent"

    WlrLayershell.namespace: "voxtype-osd"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    // Subtract the whole panel area from the input region, so pointer
    // events fall through to windows underneath instead of getting
    // eaten by the transparent fullscreen-anchored surface.
    mask: Region {
        intersection: Intersection.Subtract
        x: 0; y: 0
        width: panel.width
        height: panel.height
    }

    // Per-state tint, shared by icon + card border so a Hyprland user
    // can read the daemon's state from screen-edge color alone.
    readonly property color stateColor:
        daemonState === "recording"    ? VT.Theme.recordingColor
      : daemonState === "streaming"    ? VT.Theme.streamingColor
      : daemonState === "transcribing" ? VT.Theme.transcribingColor
      :                                  VT.Theme.idleColor

    // Ring of recent per-frame peaks (0.0..1.0). Capacity = 3 s @ 100 Hz.
    // Stored as a plain array; we shift() when full to keep newest-on-right.
    readonly property int waveformColumns: Math.round(VT.Theme.waveformWindowSecs * 100)
    property var ring: []

    // Peak meter state (kept in dBFS so the held-peak decay math matches
    // src/osd/visual.rs's PeakHold verbatim).
    property real currentPeakDbfs: -120
    property real heldDbfs: -120
    property real lastFrameTsMs: 0

    function _resetMeters() {
        ring = [];
        currentPeakDbfs = -120;
        heldDbfs = -120;
        lastFrameTsMs = 0;
        waveCanvas.requestPaint();
        meterCanvas.requestPaint();
    }

    // Colours are bound to Omarchy tokens; Canvas doesn't repaint on a
    // binding change, so force it when the palette lands.
    Connections {
        target: VT.Theme
        function onWaveformColorChanged() { waveCanvas.requestPaint(); meterCanvas.requestPaint(); }
        function onMeterLowColorChanged() { meterCanvas.requestPaint(); }
    }

    Connections {
        target: panel.audio
        enabled: panel.audio !== null
        function onFrameReceived(peak, rms, vad, tsMs) {
            // Push the new peak onto the ring; drop the oldest when full.
            const r = panel.ring.slice();
            r.push(peak);
            while (r.length > panel.waveformColumns) {
                r.shift();
            }
            panel.ring = r;

            // dBFS from linear peak. Floor at -120 to match visual.rs's
            // PeakHold::held_dbfs sentinel for "effectively silent".
            const dbfs = peak > 0.0 ? 20 * Math.log10(peak) : -120;
            panel.currentPeakDbfs = dbfs;

            // Held-peak: snap up on a louder peak, otherwise decay at
            // peakDecayDbPerSec. dt comes from the frame timestamps so
            // a paused daemon (no frames) doesn't unrealistically decay
            // before we receive the next frame.
            const dtMs = panel.lastFrameTsMs > 0
                ? Math.max(0, tsMs - panel.lastFrameTsMs)
                : 10;
            panel.lastFrameTsMs = tsMs;
            const dt = dtMs / 1000;
            if (dbfs > panel.heldDbfs) {
                panel.heldDbfs = dbfs;
            } else {
                const decayed = panel.heldDbfs - VT.Theme.peakDecayDbPerSec * dt;
                panel.heldDbfs = decayed < -120 ? -120 : decayed;
            }

            waveCanvas.requestPaint();
            meterCanvas.requestPaint();
        }
        function onDisconnected() {
            panel._resetMeters();
        }
    }

    // Clear when the daemon's state moves out of recording so the
    // waveform doesn't show stale audio from the previous recording on
    // the next one.
    onDaemonStateChanged: {
        if (daemonState === "idle" || daemonState === "") {
            _resetMeters();
        } else {
            // Re-read the live Omarchy palette every time the OSD comes up.
            // Cheaper and more reliable than watching the theme directory,
            // which `omarchy theme-set` rm -rf's out from under a watcher.
            VT.Theme.refresh();
        }
    }

    // Omarchy's own card surface, not a hand-rolled lookalike: it carries
    // the shell's border spec handling (native vs. overlay ring) so the
    // OSD keeps matching after a theme or Hyprland rounding change.
    Ui.BorderSurface {
        id: card
        width: VT.Theme.defaultWidthPx
        height: VT.Theme.defaultHeightPx
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: VT.Theme.marginPx
        radius: VT.Theme.cornerRadius
        color: VT.Theme.bgColor
        borderSpec: Border.flat(panel.stateColor, VT.Theme.borderWidth)
        opacity: (panel.daemonState === "recording" && panel.audio && panel.audio.running && !panel.audio.vad)
                 ? VT.Theme.idleDimOpacity : 1.0
        Behavior on opacity { NumberAnimation { duration: 120 } }

        Row {
            anchors.fill: parent
            anchors.leftMargin: VT.Theme.padding
            anchors.rightMargin: VT.Theme.padding
            spacing: VT.Theme.contentGap

            Text {
                width: VT.Theme.iconSlotPx
                anchors.verticalCenter: parent.verticalCenter
                horizontalAlignment: Text.AlignHCenter
                text: panel.daemonState === "recording"    ? "󰍬"
                   : panel.daemonState === "streaming"     ? "󰜟"
                   : panel.daemonState === "transcribing"  ? "󰔟"
                   :                                          "󰍬"
                font.family: VT.Theme.iconFontFamily
                font.pixelSize: VT.Theme.iconFontPx
                color: panel.stateColor
                // Subpixel (RGB LCD) antialiasing puts blue/orange fringes on
                // thin high-contrast glyphs, which on a near-black monochrome
                // card reads as "the mic is blue". QtRendering uses greyscale
                // AA via the distance-field rasteriser, so the glyph stays the
                // single colour it is actually painted with.
                renderType: Text.QtRendering
            }

            Column {
                width: card.width - VT.Theme.iconSlotPx - 2 * VT.Theme.padding
                       - VT.Theme.contentGap - 2 * card.borderLeft
                anchors.verticalCenter: parent.verticalCenter
                spacing: VT.Theme.meterGap

                Canvas {
                    id: waveCanvas
                    width: parent.width
                    height: VT.Theme.waveformHeightPx

                    onPaint: {
                        const ctx = getContext("2d");
                        ctx.clearRect(0, 0, width, height);
                        const r = panel.ring;
                        if (r.length === 0) {
                            return;
                        }

                        const cy = height / 2;
                        const maxHalf = height / 2 - 1;
                        const cols = panel.waveformColumns;
                        const colW = width / cols;
                        // Empty columns on the left when the ring isn't
                        // full yet, so newest data lands flush against
                        // the right edge.
                        const startIdx = cols - r.length;

                        ctx.strokeStyle = VT.Theme.waveformColor;
                        ctx.lineWidth = Math.max(1, colW);
                        ctx.lineCap = "butt";
                        ctx.beginPath();
                        for (let i = 0; i < r.length; i++) {
                            const x = (startIdx + i) * colW + colW / 2;
                            const halfH = Math.min(
                                maxHalf,
                                r[i] * maxHalf * VT.Theme.waveformGain
                            );
                            ctx.moveTo(x, cy - halfH);
                            ctx.lineTo(x, cy + halfH);
                        }
                        ctx.stroke();
                    }
                }

                Canvas {
                    id: meterCanvas
                    width: parent.width
                    height: VT.Theme.meterHeightPx

                    onPaint: {
                        const ctx = getContext("2d");
                        ctx.clearRect(0, 0, width, height);

                        const floor = VT.Theme.meterFloorDbfs;
                        const span = -floor;

                        // Current peak → fill width
                        let fill = 0;
                        if (panel.currentPeakDbfs > floor) {
                            const clipped = Math.min(panel.currentPeakDbfs, 0);
                            fill = Math.max(0, Math.min(1, (clipped - floor) / span));
                        }

                        // Zone color matches src/osd/visual.rs::MeterZone.
                        let zone = VT.Theme.meterLowColor;
                        if (panel.currentPeakDbfs >= -3) {
                            zone = VT.Theme.meterHighColor;
                        } else if (panel.currentPeakDbfs >= -12) {
                            zone = VT.Theme.meterMidColor;
                        }

                        ctx.fillStyle = zone;
                        ctx.fillRect(0, 0, width * fill, height);

                        // Held-peak tick. Skip if floor or below so the
                        // tick doesn't pin to the left edge during silence.
                        if (panel.heldDbfs > floor) {
                            const clippedHeld = Math.min(panel.heldDbfs, 0);
                            const heldFill = Math.max(0, Math.min(1,
                                (clippedHeld - floor) / span));
                            ctx.fillStyle = VT.Theme.waveformPeakColor;
                            ctx.fillRect(
                                Math.max(0, width * heldFill - 1),
                                0, 2, height
                            );
                        }
                    }
                }
            }
        }
    }
}
