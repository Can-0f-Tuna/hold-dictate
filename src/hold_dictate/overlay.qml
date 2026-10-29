// Dictation overlay: a small dark pill with a live voice visualiser, shown at the bottom centre
// of the screen while hold-dictate is recording. Runs for the whole session so it appears instantly.
// The visualiser is the website's halftone bars: each bar is three columns of dots, dense in the
// middle and finer toward the tips. Driven by lines the daemon appends to $XDG_RUNTIME_DIR/hold-dictate.events:
//   C <bg> <bars> <bottom|top> <scale>   look, from the settings page (sent before each S)
//   S              recording started: show
//   L <level>      loudness of the last 20 ms, 0..1
//   T              recording finished, transcribing
//   Q              hide
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root

    // Halftone grid, in logical pixels at size 1.
    readonly property real cell: 2.5
    readonly property real dot: 2.1
    readonly property int pitch: 4                       // a bar is 3 dot columns, then a 1 column gap
    readonly property real fieldW: 192
    readonly property real fieldH: 40
    readonly property int bars: Math.floor(fieldW / (cell * pitch)) | 1   // odd, so there is a middle bar
    readonly property int rows: Math.floor(fieldH / cell)
    readonly property int half: Math.ceil(bars / 2)
    property bool active: false
    property bool transcribing: false
    property real shown: 0.0
    property color pillColor: "#0a0a0b"
    property color barColor: "#6b80ff"
    property bool atTop: false
    property real sizeScale: 1.0
    property real level: 0.0      // latest loudness from the daemon
    property real envelope: 0.0   // fast-attack, slow-release follower of level
    // Loudness history, newest first; the centre bar shows the newest and older values spread outwards.
    property var history: new Array(half + 1).fill(0)
    property var heights: new Array(bars).fill(0.035)
    property var seed: Array.from({ length: bars }, (_, i) => 0.7 + 0.3 * Math.abs((Math.sin(i * 12.9898) * 43758.5453) % 1))

    function handle(line) {
        const parts = line.trim().split(" ");
        if (parts[0] === "C" && parts.length >= 5) {
            pillColor = parts[1];
            barColor = parts[2];
            atTop = parts[3] === "top";
            sizeScale = Math.min(1.5, Math.max(0.75, parseFloat(parts[4]) || 1));
        } else if (parts[0] === "L") {
            level = parseFloat(parts[1]) || 0;
        } else if (parts[0] === "S") {
            transcribing = false;
            level = 0;
            envelope = 0;
            history = new Array(half + 1).fill(0);
            heights = new Array(bars).fill(0.035);
            active = true;
        } else if (parts[0] === "T") {
            transcribing = true;
            level = 0;
        } else if (parts[0] === "Q") {
            active = false;
        }
    }

    Process {
        running: true
        command: ["tail", "-n", "0", "-F", Quickshell.env("XDG_RUNTIME_DIR") + "/hold-dictate.events"]
        stdout: SplitParser {
            onRead: data => root.handle(data)
        }
    }

    Behavior on shown {
        NumberAnimation {
            duration: root.active ? 140 : 160
            easing.type: root.active ? Easing.OutCubic : Easing.InCubic
        }
    }
    onActiveChanged: shown = active ? 1 : 0

    PanelWindow {
        visible: root.active || root.shown > 0
        color: "transparent"
        anchors.bottom: !root.atTop
        anchors.top: root.atTop
        margins.bottom: 48
        margins.top: 48
        implicitWidth: Math.ceil(256 * root.sizeScale)
        implicitHeight: Math.ceil(80 * root.sizeScale)
        exclusionMode: ExclusionMode.Ignore
        mask: Region {}
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "hold-dictate"

        Rectangle {
            id: pill
            // sized rather than scaled, so the dots stay sharp at every size
            width: 224 * root.sizeScale
            height: 56 * root.sizeScale
            radius: height / 2
            anchors.centerIn: parent
            color: root.pillColor
            border.width: 1
            border.color: "#2a2a2e"
            opacity: root.shown
            scale: 0.9 + 0.1 * root.shown
            transform: Translate {
                y: (1 - root.shown) * (root.atTop ? -8 : 8)
            }

            Canvas {
                id: field
                anchors.centerIn: parent
                width: root.fieldW * root.sizeScale
                height: root.fieldH * root.sizeScale
                renderStrategy: Canvas.Cooperative

                onPaint: {
                    const ctx = getContext("2d");
                    const s = root.sizeScale, cell = root.cell, n = root.bars, rows = root.rows;
                    ctx.reset();
                    ctx.scale(s, s);
                    ctx.fillStyle = root.barColor;
                    ctx.beginPath();
                    const mid = (n - 1) / 2, maxH = rows / 2 - 1;
                    const x0 = (root.fieldW - n * root.pitch * cell + cell) / 2;
                    const y0 = (root.fieldH - rows * cell) / 2;
                    for (let i = 0; i < n; i++) {
                        const h = Math.max(1, root.heights[i] * maxH);
                        const cx = x0 + i * root.pitch * cell;
                        for (let r = 0; r < rows; r++) {
                            const y = Math.abs(r + 0.5 - rows / 2);
                            if (y > h + 0.5)
                                continue;
                            const I = 1 - Math.pow(y / (h + 0.5), 1.4) * 0.85;   // dense core, finer dots toward the tips
                            const sz = root.dot * (0.45 + 0.55 * I), off = (cell - sz) / 2;
                            for (let k = 0; k < 3; k++)
                                ctx.rect(cx + k * cell + off, y0 + r * cell + off, sz, sz);
                        }
                    }
                    ctx.fill();
                }
            }

            FrameAnimation {
                running: root.shown > 0
                onTriggered: {
                    const dt = Math.min(frameTime, 0.05);
                    const t = elapsedTime;
                    // Envelope follower: jumps up with the voice, falls back smoothly.
                    const rate = root.level > root.envelope ? 30 : 9;
                    root.envelope += (root.level - root.envelope) * Math.min(1, rate * dt);

                    // Shift the history outwards.
                    const hist = root.history, half = hist.length - 1;
                    for (let k = half; k > 0; k--)
                        hist[k] += (hist[k - 1] - hist[k]) * Math.min(1, 16 * dt);
                    hist[0] = root.envelope;

                    const n = root.bars, mid = (n - 1) / 2, heights = root.heights, seed = root.seed;
                    for (let i = 0; i < n; i++) {
                        const d = Math.abs(i - mid), u = d / mid;
                        const taper = 1 - Math.pow(u, 2) * 0.7;
                        // a faint breathing line while you are silent, so it never looks frozen
                        const syll = 0.5 + 0.5 * Math.sin(t * 1.3 - d * 0.29) * Math.sin(t * 0.47 + d * 0.11 + 1.3);
                        const rest = 0.08 * taper * (0.3 + 0.7 * syll) * seed[i] * (0.8 + 0.2 * Math.sin(t * 5.1 + i * 0.9));
                        let target;
                        if (root.transcribing) {
                            // a soft pulse sweeping left to right while the text is worked out
                            const pos = ((t * 1.1) % 1.6 - 0.3) * n;
                            target = 0.035 + Math.max(rest * 0.45, 0.42 * Math.exp(-Math.pow((i - pos) / (n / 18 + 0.6), 2)));
                        } else {
                            const v = Math.min(1, 1.8 * hist[Math.min(half, Math.round(d * half / mid))]) * taper * seed[i] * (0.78 + 0.22 * Math.sin(t * 9 + i * 1.7));
                            target = 0.035 + Math.max(rest, v * 0.97);
                        }
                        heights[i] += (target - heights[i]) * Math.min(1, 20 * dt);
                    }
                    field.requestPaint();
                }
            }
        }
    }
}
