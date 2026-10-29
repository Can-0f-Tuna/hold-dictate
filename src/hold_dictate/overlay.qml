// Dictation overlay: a small dark pill with a live voice visualiser, shown at the bottom centre
// of the screen while hold-dictate is recording, plus the update tab and its corner chip. Runs for the whole session so it appears instantly.
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
    property string updateVersion: ""
    property bool nudging: false     // the pill is gone; only the "!" tab is left, waiting for a click
    // After a click the tab becomes a chip that peeks up from the corner of the screen while it installs:
    // "" (no update running) | going | peek | done | failed
    property string phase: ""
    readonly property bool updating: phase !== ""
    property bool doneWaiting: false   // D arrived while the chip was still on its way
    readonly property bool tabWanted: updateVersion !== "" && (active || nudging) && !updating
    property real tabShown: 0.0
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
            updateVersion = parts[1] && parts[1] !== "-" ? parts[1] : "";
        } else if (parts[0] === "D") {
            root.finish(true);
        } else if (parts[0] === "F") {
            root.finish(false);
        } else if (parts[0] === "S") {
            nudging = false;
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
            if (updateVersion !== "" && !updating) {
                nudging = true;
                nudgeTimer.restart();
            }
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

    Behavior on tabShown {
        NumberAnimation {
            duration: 220
            easing.type: Easing.OutCubic
        }
    }
    onTabWantedChanged: tabShown = tabWanted ? 1 : 0

    Timer {
        id: nudgeTimer
        interval: 7000
        onTriggered: {
            if (tabMouse.containsMouse)
                restart();
            else
                root.nudging = false;
        }
    }

    function startUpdate() {
        if (updating)
            return;
        nudging = false;
        doneWaiting = false;
        chip.x = tab.x + tab.width - chip.width;
        chip.y = pillCY - chip.height / 2;
        chip.tick = 0;
        phase = "going";
        installUpdate.running = true;
        giveUp.restart();
        goAnim.restart();
    }

    function finish(ok) {
        if (phase === "going" && ok) {
            doneWaiting = true;
            return;
        }
        if (phase !== "going" && phase !== "peek")
            return;  // not ours: this overlay did not start an update
        giveUp.stop();
        phase = ok ? "done" : "failed";
        doneAnim.restart();
    }

    Timer {
        id: giveUp  // no word from the daemon: let the chip go rather than peek for ever
        interval: 300000
        onTriggered: root.finish(false)
    }

    Process {
        id: installUpdate
        command: {
            const python = Quickshell.env("HOLD_DICTATE_PYTHON");
            return python ? [python, "-m", "hold_dictate", "update"] : ["hold-dictate", "update"];
        }
        onExited: code => {
            if (code !== 0)
                root.finish(false);  // cannot update here; the settings page opened instead
        }
    }

    Region {
        id: noInput
    }
    Region {
        id: tabInput
        item: tab
    }

    PanelWindow {
        id: win
        visible: root.active || root.shown > 0 || root.tabShown > 0 || root.updating
        color: "transparent"
        // A strip along the whole edge of the screen, so the update chip can travel to the corner.
        anchors.bottom: !root.atTop
        anchors.top: root.atTop
        anchors.left: true
        anchors.right: true
        implicitHeight: Math.ceil(48 + 80 * root.sizeScale)
        exclusionMode: ExclusionMode.Ignore
        mask: root.tabShown > 0.5 && !root.updating ? tabInput : noInput   // clicks pass through everything else
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "hold-dictate"

        // The update tab: tucked under the right end of the pill, as if pulled out of it.
        Rectangle {
            id: tab
            readonly property real s: root.sizeScale
            readonly property real tuck: 20 * s * root.shown   // shrinks as the pill fades, so the "!" never moves
            readonly property bool open: tabMouse.containsMouse
            x: parent.width / 2 + 112 * s - tuck
            y: root.pillCY - height / 2
            width: tuck + (open ? label.implicitWidth + 30 * s : 30 * s)
            height: 34 * s
            radius: height / 2
            color: root.pillColor
            border.width: 1
            border.color: tabMouse.containsMouse ? root.barColor : "#2a2a2e"
            opacity: root.tabShown
            visible: root.tabShown > 0
            transform: Translate {
                x: (1 - root.tabShown) * -18 * tab.s
            }
            Behavior on width {
                NumberAnimation {
                    duration: 180
                    easing.type: Easing.OutCubic
                }
            }

            Row {
                x: tab.tuck + 11 * tab.s
                anchors.verticalCenter: parent.verticalCenter
                spacing: 7 * tab.s
                Text {
                    text: "!"
                    color: root.barColor
                    font.pixelSize: 17 * tab.s
                    font.weight: Font.Black
                    anchors.verticalCenter: parent.verticalCenter
                }
                Text {
                    id: label
                    text: "Update"
                    color: "#e8e8ea"
                    font.pixelSize: 13 * tab.s
                    font.weight: Font.Medium
                    opacity: tab.open ? 1 : 0
                    anchors.verticalCenter: parent.verticalCenter
                    Behavior on opacity {
                        NumberAnimation {
                            duration: 140
                        }
                    }
                }
            }

            MouseArea {
                id: tabMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.startUpdate()
            }
        }

        // The update chip: drops to the edge of the screen, slides into the corner and peeks out while
        // the new version installs; ticks when it is done, then slips away.
        Rectangle {
            id: chip
            readonly property real s: root.sizeScale
            readonly property real edgeY: root.atTop ? 0 : win.height              // the screen edge
            readonly property real out: root.atTop ? -1 : 1                         // away from the screen
            readonly property real peekY: root.atTop ? -height * 0.4 : edgeY - height * 0.6
            readonly property real upY: root.atTop ? 18 * s : edgeY - height - 18 * s
            readonly property real awayY: root.atTop ? -height - 8 : edgeY + 8
            readonly property real cornerX: win.width - width - 28 * s
            property real tick: 0          // 0..1, how much of the check mark is drawn
            width: 40 * s
            height: 40 * s
            radius: 12 * s
            color: root.pillColor
            border.width: 1
            border.color: "#2a2a2e"
            visible: root.updating

            Item {
                // the icon sits in the part that shows while peeking
                width: parent.width
                height: parent.height
                y: root.phase === "peek" || root.phase === "going" ? parent.height * (root.atTop ? 0.14 : -0.14) : 0
                Behavior on y {
                    NumberAnimation {
                        duration: 200
                    }
                }

                Canvas {
                    id: spinner
                    anchors.centerIn: parent
                    width: 18 * chip.s
                    height: width
                    visible: root.phase === "going" || root.phase === "peek"
                    onPaint: {
                        const ctx = getContext("2d");
                        ctx.reset();
                        ctx.lineWidth = 2.2 * chip.s;
                        ctx.lineCap = "round";
                        ctx.strokeStyle = root.barColor;
                        ctx.beginPath();
                        ctx.arc(width / 2, height / 2, width / 2 - ctx.lineWidth, 0, Math.PI * 1.4);
                        ctx.stroke();
                    }
                    RotationAnimator on rotation {
                        from: 0
                        to: 360
                        duration: 900
                        loops: Animation.Infinite
                        running: spinner.visible
                    }
                }

                Canvas {
                    id: check
                    anchors.centerIn: parent
                    width: 20 * chip.s
                    height: width
                    visible: root.phase === "done"
                    property real progress: chip.tick
                    onProgressChanged: requestPaint()
                    onPaint: {
                        const ctx = getContext("2d"), w = width, h = height;
                        ctx.reset();
                        ctx.lineWidth = 2.6 * chip.s;
                        ctx.lineCap = "round";
                        ctx.lineJoin = "round";
                        ctx.strokeStyle = root.barColor;
                        // two strokes, short then long, drawn in order as progress runs 0..1
                        const a = [w * 0.12, h * 0.55], b = [w * 0.4, h * 0.82], c = [w * 0.9, h * 0.2];
                        const first = Math.min(1, progress / 0.35), second = Math.max(0, (progress - 0.35) / 0.65);
                        ctx.beginPath();
                        ctx.moveTo(a[0], a[1]);
                        ctx.lineTo(a[0] + (b[0] - a[0]) * first, a[1] + (b[1] - a[1]) * first);
                        if (second > 0)
                            ctx.lineTo(b[0] + (c[0] - b[0]) * second, b[1] + (c[1] - b[1]) * second);
                        ctx.stroke();
                    }
                }

                Text {
                    anchors.centerIn: parent
                    visible: root.phase === "failed"
                    text: "!"
                    color: "#ff6b6b"
                    font.pixelSize: 19 * chip.s
                    font.weight: Font.Black
                }
            }
        }

        SequentialAnimation {
            id: goAnim
            NumberAnimation {
                target: chip
                property: "y"
                to: chip.peekY
                duration: 340
                easing.type: Easing.InOutCubic
            }
            NumberAnimation {
                target: chip
                property: "x"
                to: chip.cornerX
                duration: 560
                easing.type: Easing.InOutCubic
            }
            ScriptAction {
                script: {
                    root.phase = "peek";
                    if (root.doneWaiting)
                        root.finish(true);
                }
            }
        }

        SequentialAnimation {
            id: doneAnim
            NumberAnimation {
                target: chip
                property: "y"
                to: chip.upY
                duration: 300
                easing.type: Easing.OutBack
            }
            NumberAnimation {
                target: chip
                property: "tick"
                from: 0
                to: 1
                duration: 420
                easing.type: Easing.OutCubic
            }
            PauseAnimation {
                duration: 1100
            }
            NumberAnimation {
                target: chip
                property: "y"
                to: chip.awayY
                duration: 320
                easing.type: Easing.InCubic
            }
            ScriptAction {
                script: {
                    if (root.phase === "done")
                        Qt.quit();  // this overlay belongs to the old version; the new one is already running
                    root.phase = "";
                }
            }
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
}
