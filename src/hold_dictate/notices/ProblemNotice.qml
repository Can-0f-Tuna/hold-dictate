// The problem notice. The pill's bars settle into a flat line and turn red, a mark appears beside the pill, and after a moment
// both close. A crossed-out microphone or network sign says it all, a "!" means the settings have to explain.
// A click opens the settings; the mouse on the mark keeps it open.
import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: notice

    required property var shell              // overlay.qml: the pill's look and position
    readonly property alias markItem: mark   // the only part of the window that takes clicks
    readonly property bool busy: phase === "playing" || phase === "waiting"
    readonly property bool clickable: phase === "waiting"
    readonly property real s: shell.sizeScale
    readonly property real size: 48 * s
    readonly property real restX: width / 2 + 112 * s + 10 * s

    property string phase: ""     // "" | playing | waiting | leaving
    property bool warming: false  // the bars still move, before they stop
    property int warmUp: 0
    property real slide: 1        // 1..0, the mark's last few pixels out of the pill
    property string icon: "alert"

    function play(kind, delay) {
        timeline.stop();
        leave.stop();
        stay.stop();
        icon = kind;
        warmUp = delay;
        warming = delay > 0;
        mark.opacity = 0;
        mark.scale = 0.85;
        slide = 1;
        phase = "playing";
        timeline.restart();
    }

    function dismiss() {
        timeline.stop();
        stay.stop();
        warming = false;
        if (phase !== "") {
            phase = "leaving";
            leave.restart();
        }
    }

    function close() {
        shell.closeAfterProblem();
        dismiss();
    }

    function openDetails() {
        openSettings.running = true;
        close();
    }

    SequentialAnimation {
        id: timeline
        PauseAnimation { duration: notice.warmUp }
        ScriptAction {
            script: {
                notice.warming = false;
                notice.shell.flat = true;
            }
        }
        ParallelAnimation {
            NumberAnimation { target: notice.shell; property: "redBars"; to: 1; duration: 160; easing.type: Easing.OutCubic }
            NumberAnimation { target: mark; property: "opacity"; to: 1; duration: 140; easing.type: Easing.OutCubic }
            NumberAnimation { target: mark; property: "scale"; to: 1; duration: 200; easing.type: Easing.OutCubic }
            NumberAnimation { target: notice; property: "slide"; to: 0; duration: 200; easing.type: Easing.OutCubic }
        }
        PauseAnimation { duration: 80 }   // the bars finish coming down
        ScriptAction {
            script: {
                notice.shell.frozen = true;
                notice.phase = "waiting";
                stay.restart();
            }
        }
    }

    SequentialAnimation {
        id: leave
        NumberAnimation { target: mark; property: "opacity"; to: 0; duration: 160; easing.type: Easing.OutCubic }
        ScriptAction { script: notice.phase = "" }
    }

    Timer {
        id: stay
        interval: 2600
        onTriggered: {
            if (markMouse.containsMouse)
                restart();
            else
                notice.close();
        }
    }

    Process {
        id: openSettings
        command: {
            const python = Quickshell.env("HOLD_DICTATE_PYTHON");
            return (python ? [python, "-m", "hold_dictate"] : ["hold-dictate"]).concat(["settings"]);
        }
    }

    Rectangle {
        id: mark
        x: notice.restX - notice.slide * 8 * notice.s
        y: notice.shell.pillCY - height / 2
        width: notice.size
        height: notice.size
        radius: width / 2
        color: notice.shell.problemColor
        border.width: 1
        border.color: markMouse.containsMouse ? "#ffffff" : "transparent"
        visible: notice.phase !== ""
        opacity: 0

        Text {
            anchors.centerIn: parent
            visible: notice.icon !== "mic" && notice.icon !== "net"
            text: "!"
            color: "#ffffff"
            font.pixelSize: 28 * notice.s
            font.weight: Font.Black
        }

        Canvas {
            id: glyph
            anchors.centerIn: parent
            width: 30 * notice.s
            height: width
            visible: notice.icon === "mic" || notice.icon === "net"
            readonly property string kind: notice.icon
            onKindChanged: requestPaint()
            onPaint: {
                const ctx = getContext("2d");
                ctx.reset();
                ctx.scale(width / 24, height / 24);
                ctx.strokeStyle = "#ffffff";
                ctx.fillStyle = "#ffffff";
                ctx.lineWidth = 2;
                ctx.lineCap = "round";
                ctx.beginPath();
                if (kind === "mic") {
                    ctx.roundedRect(8.5, 2.5, 7, 12, 3.5, 3.5);
                    ctx.fill();
                    ctx.beginPath();
                    ctx.arc(12, 11, 7, 0, Math.PI);
                    ctx.moveTo(12, 18);
                    ctx.lineTo(12, 21.5);
                } else {
                    ctx.arc(12, 19, 15, -Math.PI * 0.77, -Math.PI * 0.23);
                    ctx.moveTo(6.5, 14.2);
                    ctx.arc(12, 19, 8, -Math.PI * 0.77, -Math.PI * 0.23);
                    ctx.moveTo(12.6, 19);
                    ctx.arc(12, 19, 0.6, 0, Math.PI * 2);
                }
                ctx.stroke();
                // the line through it, with a gap in the red so it reads as crossed out
                ctx.strokeStyle = notice.shell.problemColor;
                ctx.lineWidth = 5;
                ctx.beginPath();
                ctx.moveTo(3, 3);
                ctx.lineTo(21, 21);
                ctx.stroke();
                ctx.strokeStyle = "#ffffff";
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(3, 3);
                ctx.lineTo(21, 21);
                ctx.stroke();
            }
        }

        MouseArea {
            id: markMouse
            anchors.fill: parent
            hoverEnabled: true
            enabled: notice.clickable
            cursorShape: Qt.PointingHandCursor
            onClicked: notice.openDetails()
        }
    }
}
