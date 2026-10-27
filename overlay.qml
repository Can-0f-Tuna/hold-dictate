// Dictation overlay: a small dark pill with a live voice visualiser, shown at the bottom centre
// of the screen while hold-dictate is recording. Runs for the whole session so it appears instantly.
// Driven by lines the daemon appends to $XDG_RUNTIME_DIR/hold-dictate.events:
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

    readonly property int bars: 27
    readonly property int half: (bars - 1) / 2
    property bool active: false
    property bool transcribing: false
    property real shown: 0.0
    property real level: 0.0      // latest loudness from the daemon
    property real envelope: 0.0   // fast-attack, slow-release follower of level
    // Loudness history, newest first; the centre bar shows the newest and older values spread outwards.
    property var history: new Array(half + 1).fill(0)

    function handle(line) {
        const parts = line.trim().split(" ");
        if (parts[0] === "L") {
            level = parseFloat(parts[1]) || 0;
        } else if (parts[0] === "S") {
            transcribing = false;
            level = 0;
            envelope = 0;
            history = new Array(half + 1).fill(0);
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
        anchors.bottom: true
        margins.bottom: 48
        implicitWidth: 240
        implicitHeight: 72
        exclusionMode: ExclusionMode.Ignore
        mask: Region {}
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "hold-dictate"

        Rectangle {
            id: pill
            width: 196
            height: 44
            radius: height / 2
            anchors.centerIn: parent
            color: "#0a0a0b"
            border.width: 1
            border.color: "#2a2a2e"
            opacity: root.shown
            scale: 0.9 + 0.1 * root.shown
            transform: Translate {
                y: (1 - root.shown) * 8
            }

            Row {
                id: row
                anchors.centerIn: parent
                spacing: 3

                Repeater {
                    id: barRepeater
                    model: root.bars

                    Rectangle {
                        property real h: 3
                        property real glow: 0.45
                        width: 3
                        height: h
                        radius: 1.5
                        anchors.verticalCenter: parent.verticalCenter
                        color: "#ffffff"
                        opacity: glow
                    }
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

                    // Shift the history outwards about 60 times a second.
                    const h = root.history;
                    for (let k = h.length - 1; k > 0; k--)
                        h[k] += (h[k - 1] - h[k]) * Math.min(1, 22 * dt);
                    h[0] = root.envelope;

                    const maxH = 30;
                    for (let i = 0; i < root.bars; i++) {
                        const bar = barRepeater.itemAt(i);
                        if (!bar)
                            continue;
                        const d = Math.abs(i - root.half);
                        const taper = 1 - Math.pow(d / (root.half + 1), 2) * 0.55;
                        let target;
                        let glow;
                        if (root.transcribing) {
                            // A soft pulse sweeping left to right while the text is worked out.
                            const pos = ((t * 1.1) % 1.6 - 0.3) * root.bars;
                            const wave = Math.exp(-Math.pow((i - pos) / 2.6, 2));
                            target = 3 + wave * 9;
                            glow = 0.3 + 0.7 * wave;
                        } else {
                            // Slight per-bar wobble so speech looks alive rather than blocky.
                            const wobble = 0.78 + 0.22 * Math.sin(t * 9 + i * 1.7);
                            const v = h[d] * taper * wobble;
                            target = 3 + v * (maxH - 3);
                            glow = 0.45 + 0.55 * Math.min(1, v * 1.6);
                        }
                        bar.h += (target - bar.h) * Math.min(1, 24 * dt);
                        bar.glow += (glow - bar.glow) * Math.min(1, 18 * dt);
                    }
                }
            }
        }
    }
}
