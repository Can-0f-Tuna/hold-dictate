// Dictation overlay: a small dark pill with a live voice visualiser, shown at the bottom centre
// of the screen while hold-dictate is recording, and the home of the notices. Runs for the whole session so it appears instantly.
// Two looks: "dots", the website's halftone bars (each bar is three columns of dots, dense in the
// middle and finer toward the tips), and "lines", the original thin rounded bars.
// Driven by lines the daemon appends to $XDG_RUNTIME_DIR/hold-dictate.events:
//   C <bg> <bars> <bottom|top> <scale> [dots|lines]   look, from the settings page (sent before each S)
//   U <version|->  an update is waiting (sent before each S): a "!" tab on the pill installs it
//   D              the update is installed (sent by the new version): the corner chip ticks and leaves
//   F              the update failed: the corner chip shows "!" and leaves
//   S              recording started: show
//   L <level>      loudness of the last 20 ms, 0..1
//   T              recording finished, transcribing
//   Q              hide (with an update waiting, the "!" tab stays a few seconds so it can be clicked)
//   P <on|->       the "you got Premium" notice: on plays it (sent before each S until it is clicked), - sends it away
// Notices live in notices/, one file each; notices/README.md says how to add one.
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import "notices"

ShellRoot {
    id: root

    // Halftone grid, in logical pixels at size 1.
    readonly property real cell: 2.5
    readonly property real dot: 2.1
    readonly property int pitch: 4                       // a bar is 3 dot columns, then a 1 column gap
    readonly property real fieldW: 192
    readonly property real fieldH: 40
    readonly property int dotBars: Math.floor(fieldW / (cell * pitch)) | 1   // odd, so there is a middle bar
    readonly property int lineBars: 27
    property string style: "dots"
    readonly property bool lines: style === "lines"
    readonly property int bars: lines ? lineBars : dotBars
    readonly property int rows: Math.floor(fieldH / cell)
    readonly property int half: Math.ceil(bars / 2)
    readonly property real pillCY: atTop ? 48 + 40 * sizeScale : 40 * sizeScale   // pill centre, in the window
    property bool active: false
    property bool transcribing: false
    property real shown: 0.0
    readonly property bool updateWaiting: update.updateVersion !== ""
    property bool premiumWanted: false   // the Premium notice is on screen
    property bool premiumSeen: false     // clicked in this session: a late "P on" must not bring it back
    property color pillColor: "#0a0a0b"
    property color barColor: "#6b80ff"
    property bool atTop: false
    property real sizeScale: 1.0
    property real level: 0.0      // latest loudness from the daemon
    property real envelope: 0.0   // fast-attack, slow-release follower of level
    // Loudness history, newest first; the centre bar shows the newest and older values spread outwards.
    property var history: new Array(half + 1).fill(0)
    property var heights: new Array(bars).fill(0.035)
    property var seed: Array.from({ length: Math.max(dotBars, lineBars) }, (_, i) => 0.7 + 0.3 * Math.abs((Math.sin(i * 12.9898) * 43758.5453) % 1))

    function handle(line) {
        const parts = line.trim().split(" ");
        if (parts[0] === "C" && parts.length >= 5) {
            pillColor = parts[1];
            barColor = parts[2];
            atTop = parts[3] === "top";
            sizeScale = Math.min(1.5, Math.max(0.75, parseFloat(parts[4]) || 1));
            style = parts[5] === "lines" ? "lines" : "dots";
        } else if (parts[0] === "L") {
            level = parseFloat(parts[1]) || 0;
        } else if (parts[0] === "U") {
            update.updateVersion = parts[1] && parts[1] !== "-" ? parts[1] : "";
        } else if (parts[0] === "D") {
            update.finish(true);
        } else if (parts[0] === "F") {
            update.finish(false);
        } else if (parts[0] === "P") {
            if (parts[1] === "on" && !premiumSeen) {
                premiumWanted = true;
            } else if (parts[1] !== "on") {
                premiumSeen = false;
                if (premiumNotice.item)
                    premiumNotice.item.leave();
            }
        } else if (parts[0] === "S") {
            update.dictationStarted();
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
            update.dictationEnded();
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

    Region {
        id: noInput
    }
    Region {
        id: tabInput
        item: update.tabItem
    }

    PanelWindow {
        id: win
        visible: root.active || root.shown > 0 || update.visibleNow
        color: "transparent"
        // A strip along the whole edge of the screen, so the update chip can travel to the corner.
        anchors.bottom: !root.atTop
        anchors.top: root.atTop
        anchors.left: true
        anchors.right: true
        implicitHeight: Math.ceil(48 + 80 * root.sizeScale)
        exclusionMode: ExclusionMode.Ignore
        mask: update.tabVisible ? tabInput : noInput   // clicks pass through everything else
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "hold-dictate"

        UpdateNotice {
            id: update
            anchors.fill: parent
            shell: root
        }

        Rectangle {
            id: pill
            // sized rather than scaled, so the dots stay sharp at every size
            width: 224 * root.sizeScale
            height: 56 * root.sizeScale
            radius: height / 2
            anchors.horizontalCenter: parent.horizontalCenter
            y: root.pillCY - height / 2
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
                    if (root.lines) {
                        // 3 px bars, 3 px apart, brighter as they grow
                        const x0 = (root.fieldW - n * 6 + 3) / 2;
                        for (let i = 0; i < n; i++) {
                            const v = root.heights[i], h = 3 + v * 30, x = x0 + i * 6, y = (root.fieldH - h) / 2;
                            ctx.globalAlpha = 0.45 + 0.55 * Math.min(1, v * 1.6);
                            ctx.beginPath();
                            ctx.roundedRect(x, y, 3, h, 1.5, 1.5);
                            ctx.fill();
                        }
                        return;
                    }
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
                        if (root.lines) {
                            // the original look: flat 3 px bars at rest, no breathing
                            const ld = Math.abs(i - mid), lt = 1 - Math.pow(ld / (mid + 1), 2) * 0.55;
                            if (root.transcribing) {
                                const pos = ((t * 1.1) % 1.6 - 0.3) * n;
                                target = 0.3 * Math.exp(-Math.pow((i - pos) / 2.6, 2));
                            } else {
                                target = hist[Math.min(half, Math.round(ld * half / mid))] * lt * (0.78 + 0.22 * Math.sin(t * 9 + i * 1.7));
                            }
                        } else if (root.transcribing) {
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

    // Created only while it plays, so it costs nothing the rest of the time.
    LazyLoader {
        id: premiumNotice
        active: root.premiumWanted
        component: PremiumNotice {
            shell: root
            onClicked: root.premiumSeen = true
            onFinished: root.premiumWanted = false
        }
    }
}
