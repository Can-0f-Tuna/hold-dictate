// The update notice: a "!" tab pulled out of the pill. A click turns it into a chip that peeks up from
// the corner of the screen while the new version installs, ticks when it is done, then slips away.
import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: notice

    required property var shell              // overlay.qml: the pill's look and position
    readonly property alias tabItem: tab     // the only part of the window that takes clicks
    readonly property bool tabVisible: tabShown > 0.5 && !updating
    readonly property bool visibleNow: tabShown > 0 || updating

    property string updateVersion: ""
    property bool nudging: false     // the pill is gone; only the "!" tab is left, waiting for a click
    // After a click the tab becomes a chip that peeks up from the corner of the screen while it installs:
    // "" (no update running) | going | peek | done | failed
    property string phase: ""
    readonly property bool updating: phase !== ""
    property bool doneWaiting: false   // D arrived while the chip was still on its way
    readonly property bool tabWanted: updateVersion !== "" && (shell.active || nudging) && !updating
    property real tabShown: 0.0

    function dictationStarted() {
        nudging = false;
    }

    function dictationEnded() {
        if (updateVersion !== "" && !updating) {
            nudging = true;
            nudgeTimer.restart();
        }
    }

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
                notice.nudging = false;
        }
    }

    function startUpdate() {
        if (updating)
            return;
        nudging = false;
        doneWaiting = false;
        chip.x = tab.x + tab.width - chip.width;
        chip.y = shell.pillCY - chip.height / 2;
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
        onTriggered: notice.finish(false)
    }

    Process {
        id: installUpdate
        command: {
            const python = Quickshell.env("HOLD_DICTATE_PYTHON");
            return python ? [python, "-m", "hold_dictate", "update"] : ["hold-dictate", "update"];
        }
        onExited: code => {
            if (code !== 0)
                notice.finish(false);  // cannot update here; the settings page opened instead
        }
    }

    // The update tab: tucked under the right end of the pill, as if pulled out of it.
    Rectangle {
        id: tab
        readonly property real s: shell.sizeScale
        readonly property real tuck: 20 * s * shell.shown   // shrinks as the pill fades, so the "!" never moves
        readonly property bool open: tabMouse.containsMouse
        x: parent.width / 2 + 112 * s - tuck
        y: shell.pillCY - height / 2
        width: tuck + (open ? label.implicitWidth + 30 * s : 30 * s)
        height: 34 * s
        radius: height / 2
        color: shell.pillColor
        border.width: 1
        border.color: tabMouse.containsMouse ? shell.barColor : "#2a2a2e"
        opacity: notice.tabShown
        visible: notice.tabShown > 0
        transform: Translate {
            x: (1 - notice.tabShown) * -18 * tab.s
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
                color: shell.barColor
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
            onClicked: notice.startUpdate()
        }
    }

    // The update chip: drops to the edge of the screen, slides into the corner and peeks out while
    // the new version installs; ticks when it is done, then slips away.
    Rectangle {
        id: chip
        readonly property real s: shell.sizeScale
        readonly property real edgeY: shell.atTop ? 0 : notice.height              // the screen edge
        readonly property real out: shell.atTop ? -1 : 1                         // away from the screen
        readonly property real peekY: shell.atTop ? -height * 0.4 : edgeY - height * 0.6
        readonly property real upY: shell.atTop ? 18 * s : edgeY - height - 18 * s
        readonly property real awayY: shell.atTop ? -height - 8 : edgeY + 8
        readonly property real cornerX: notice.width - width - 28 * s
        property real tick: 0          // 0..1, how much of the check mark is drawn
        width: 40 * s
        height: 40 * s
        radius: 12 * s
        color: shell.pillColor
        border.width: 1
        border.color: "#2a2a2e"
        visible: notice.updating

        Item {
            // the icon sits in the part that shows while peeking
            width: parent.width
            height: parent.height
            y: notice.phase === "peek" || notice.phase === "going" ? parent.height * (shell.atTop ? 0.14 : -0.14) : 0
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
                visible: notice.phase === "going" || notice.phase === "peek"
                onPaint: {
                    const ctx = getContext("2d");
                    ctx.reset();
                    ctx.lineWidth = 2.2 * chip.s;
                    ctx.lineCap = "round";
                    ctx.strokeStyle = shell.barColor;
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
                visible: notice.phase === "done"
                property real progress: chip.tick
                onProgressChanged: requestPaint()
                onPaint: {
                    const ctx = getContext("2d"), w = width, h = height;
                    ctx.reset();
                    ctx.lineWidth = 2.6 * chip.s;
                    ctx.lineCap = "round";
                    ctx.lineJoin = "round";
                    ctx.strokeStyle = shell.barColor;
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
                visible: notice.phase === "failed"
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
                notice.phase = "peek";
                if (notice.doneWaiting)
                    notice.finish(true);
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
                if (notice.phase === "done")
                    Qt.quit();  // this overlay belongs to the old version; the new one is already running
                notice.phase = "";
            }
        }
    }
}
