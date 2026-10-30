// The "you got Premium" notice. With the next dictation a ball slides out of the end of the pill, the
// pill's own height and colour, shows a "!" and lets a small character climb out onto it. The character
// flips down to the bottom edge of the screen, the ball closes back into the pill, and it walks along it and asks for a click in a short bubble.
// Then it sinks into the edge until only its eyes show, and comes back out when the mouse touches it. The click opens the Premium settings. With no click for 30 seconds
// it leaves, and the next dictation brings it back.
// Built from plain items moved by one timeline, so nothing is repainted and it stays smooth. While it
// waits for the click nothing moves but a short blink and a look around now and then.
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

Scope {
    id: notice

    required property var shell     // overlay.qml: the pill's look and position
    signal clicked()
    signal finished()

    readonly property real s: shell.sizeScale
    readonly property real w: 46 * s
    readonly property real h: 50 * s
    readonly property real ballSize: 56 * s                // as tall as the pill: it is the pill's end, pulled out
    readonly property real ballCY: shell.atTop ? 48 + 40 * s : win.height - 48 - 40 * s
    readonly property real side: shell.updateWaiting ? -1 : 1   // the update tab takes the right
    readonly property real ballInX: win.width / 2 + side * 84 * s     // exactly over the round end of the pill
    readonly property real ballOutX: win.width / 2 + side * 146 * s
    readonly property real floorY: win.height - 49 * s     // the feet on the bottom edge of the screen
    readonly property real peekY: win.height - 33 * s      // only the eyes and the tuft show
    readonly property real awayY: win.height + 20 * s

    // "" before it starts | playing | waiting (for the click) | leaving
    property string phase: ""
    property bool peeking: false  // sunk into the edge of the screen
    property real walk: 0       // radians; the feet, the bob and the sway follow it
    property real lean: 0       // degrees, forward while walking
    property real flip: 0       // 0..1, along the jump
    property real squash: 1     // height of the body: below 1 crouching, above 1 stretching
    property real blink: 1      // height of the eyes
    property real lookX: 0      // -1..1, where the eyes look
    // Worked out when it starts: the animations must not follow the character as it moves
    property real ballX: 0
    property real startX: 0
    property real startY: 0
    property real landX: 0
    property real stopX: 0
    property real arc: 0        // how far the jump rises over the straight line to the landing
    readonly property bool hovered: mouse.containsMouse && phase === "waiting"

    onFlipChanged: {
        // one curve for the whole jump, so it never jerks at the top
        guy.x = startX + (landX - startX) * flip;
        guy.y = startY + (floorY - startY) * flip - arc * 4 * flip * (1 - flip);
    }

    onHoveredChanged: {
        if (hovered) {
            hideDelay.stop();
            giveUp.stop();
            rise();
        } else if (phase === "waiting") {
            hideDelay.restart();
            giveUp.restart();
        }
    }

    function sink() {
        if (phase !== "waiting" || peeking || hovered)
            return;
        peeking = true;
        riseAnim.stop();
        sinkAnim.restart();
    }

    function rise() {
        if (!peeking)
            return;
        peeking = false;
        sinkAnim.stop();
        riseAnim.restart();
    }

    function stopWaiting() {
        idle.stop();
        hideDelay.stop();
        giveUp.stop();
        sinkAnim.stop();
        riseAnim.stop();
    }

    function open() {
        ballX = ballInX;
        startX = ballOutX - w / 2;
        startY = ballCY - ballSize / 2 - 47 * s;   // standing on the ball
        landX = Math.min(startX + win.width * 0.16, win.width - w - 200 * s);
        stopX = Math.min(landX + 130 * s, win.width - w - 120 * s);
        arc = Math.min(120 * s, Math.max(20 * s, startY));
        guy.x = startX;
        guy.y = ballCY - h / 2;                    // hidden behind the ball
        phase = "playing";
        intro.restart();
    }

    function leave() {
        if (phase === "leaving" || phase === "")
            return;
        intro.stop();
        stopWaiting();
        phase = "leaving";
        outro.restart();
    }

    Timer {
        id: hideDelay  // it stands a moment with its bubble, then sinks
        interval: 1000
        onTriggered: notice.sink()
    }

    Timer {
        id: giveUp  // no click: gone for now, and back with the next dictation
        interval: 30000
        onTriggered: {
            notice.stopWaiting();
            notice.phase = "leaving";
            goneAnim.restart();
        }
    }

    Timer {
        // a new layer surface has no size until the compositor answers
        interval: 16
        repeat: true
        running: notice.phase === "" && win.width > 0 && win.height > 0
        onTriggered: notice.open()
    }

    Process {
        id: openSettings
        command: {
            const python = Quickshell.env("HOLD_DICTATE_PYTHON");
            return (python ? [python, "-m", "hold_dictate"] : ["hold-dictate"]).concat(["settings", "--premium"]);
        }
    }

    Region {
        id: noInput
    }
    Region {
        id: hitInput
        item: hit
    }

    PanelWindow {
        id: win
        visible: true
        color: "transparent"
        anchors.top: true
        anchors.bottom: true
        anchors.left: true
        anchors.right: true
        exclusionMode: ExclusionMode.Ignore   // the ball has to line up with the pill
        mask: notice.phase === "waiting" ? hitInput : noInput   // clicks pass through everything else
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "hold-dictate-notice"

        // The ball.
        Rectangle {
            id: ball
            x: notice.ballX - width / 2
            y: notice.ballCY - height / 2
            width: notice.ballSize
            height: notice.ballSize
            radius: width / 2
            z: 1                     // the character climbs out from behind it
            color: notice.shell.pillColor
            border.width: 1
            border.color: "#2a2a2e"
            opacity: 0
            Text {
                id: mark
                anchors.centerIn: parent
                text: "!"
                opacity: 0
                color: notice.shell.barColor
                font.pixelSize: 26 * notice.s
                font.weight: Font.Black
            }
        }

        Item {
            id: guy
            width: notice.w
            height: notice.h
            scale: 0
            transformOrigin: Item.Center
            // a soft curve with no corners, twice a stride
            readonly property real bob: Math.pow(Math.sin(notice.walk), 2) * -2.5 * notice.s
            readonly property color bodyColor: notice.shell.pillColor

            // the feet
            Repeater {
                model: 2
                Rectangle {
                    required property int index
                    readonly property real stride: Math.sin(notice.walk + index * Math.PI)
                    x: ((index ? 26 : 11) + 3.5 * Math.cos(notice.walk + index * Math.PI) * Math.min(1, notice.lean / 5)) * notice.s
                    y: (42 - 3.5 * Math.max(0, stride)) * notice.s
                    width: 9 * notice.s
                    height: 7 * notice.s
                    radius: height / 2
                    color: Qt.darker(guy.bodyColor, 1.35)
                }
            }

            Item {
                // Everything but the feet: squashes from the bottom, and bobs while walking
                width: parent.width
                height: parent.height
                transform: [
                    Scale {
                        origin.x: notice.w / 2
                        origin.y: 45 * notice.s
                        yScale: notice.squash
                        xScale: 2 - notice.squash
                    },
                    Translate {
                        y: guy.bob
                    },
                    Rotation {
                        origin.x: notice.w / 2
                        origin.y: notice.h
                        angle: notice.lean + 3 * Math.sin(notice.walk) * Math.min(1, notice.lean / 5)
                    }
                ]

                // the tuft
                Repeater {
                    model: 3
                    Rectangle {
                        required property int index
                        x: 21.5 * notice.s
                        y: 1 * notice.s
                        width: 3 * notice.s
                        height: 11 * notice.s
                        radius: width / 2
                        color: guy.bodyColor
                        rotation: (index - 1) * 28 + 7 * Math.sin(notice.walk)
                        transformOrigin: Item.Bottom
                    }
                }

                Rectangle {
                    x: 3 * notice.s
                    y: 9 * notice.s
                    width: 40 * notice.s
                    height: 37 * notice.s
                    radius: 18.5 * notice.s
                    color: guy.bodyColor
                    border.width: 1
                    border.color: "#3a3a40"

                    Repeater {
                        model: 2
                        Item {
                            id: side
                            required property int index
                            x: (index ? 24 : 7) * notice.s
                            y: 12 * notice.s
                            width: 9.5 * notice.s
                            height: 11 * notice.s

                            Rectangle {
                                // the cheek
                                x: (side.index ? 2.5 : -1.5) * notice.s
                                y: 11 * notice.s
                                width: 8.5 * notice.s
                                height: 4.5 * notice.s
                                radius: height / 2
                                color: "#ff9fb8"
                                opacity: 0.75
                            }

                            Rectangle {
                                // the eye
                                x: notice.lookX * 1.5 * notice.s
                                width: parent.width
                                height: parent.height
                                radius: width / 2
                                color: "#ffffff"
                                transform: Scale {
                                    origin.y: 5.5 * notice.s
                                    yScale: notice.blink
                                }
                                Rectangle {
                                    x: 2.5 * notice.s
                                    y: 2.5 * notice.s
                                    width: 5 * notice.s
                                    height: width
                                    radius: width / 2
                                    color: "#16161c"
                                }
                            }
                        }
                    }
                }
            }
        }

        Rectangle {
            id: bubble
            x: Math.min(guy.x + guy.width / 2 - width / 2, win.width - width - 12)
            y: guy.y - height - 12 * notice.s
            width: line.implicitWidth + 28 * notice.s
            height: line.implicitHeight + 16 * notice.s
            radius: height / 2
            color: notice.shell.pillColor
            border.width: 1
            border.color: notice.hovered ? notice.shell.barColor : "#2a2a2e"
            transformOrigin: Item.Bottom
            scale: 0

            Rectangle {
                // the bubble's tail, pointing at the character, behind the bubble
                z: -1
                x: guy.x + guy.width / 2 - bubble.x - width / 2
                y: parent.height - height / 2 - 1
                width: 11 * notice.s
                height: 11 * notice.s
                rotation: 45
                color: notice.shell.pillColor
            }

            Text {
                id: line
                anchors.centerIn: parent
                text: "You got Premium! Click me"
                color: "#ffffff"
                font.pixelSize: 13.5 * notice.s
                font.weight: Font.Medium
            }
        }

        Item {
            // What takes the click: the character and its bubble
            id: hit
            // Out of the edge it reaches back to the edge, so a mouse left there keeps it out.
            x: notice.peeking ? guy.x : Math.min(bubble.x, guy.x)
            y: notice.peeking ? guy.y : Math.min(bubble.y, guy.y)
            width: (notice.peeking ? guy.x + guy.width : Math.max(bubble.x + bubble.width, guy.x + guy.width)) - x
            height: win.height - y
            MouseArea {
                id: mouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    notice.clicked();
                    openSettings.running = true;
                    notice.leave();
                }
            }
        }

        SequentialAnimation {
            id: intro

            // the pill comes up, then its end slides out as a ball and shows the "!"
            PauseAnimation {
                duration: 260
            }
            ScriptAction {
                script: ball.opacity = 1
            }
            ParallelAnimation {
                NumberAnimation {
                    target: notice
                    property: "ballX"
                    to: notice.ballOutX
                    duration: 480
                    easing.type: Easing.OutCubic
                }
                SequentialAnimation {
                    PauseAnimation {
                        duration: 200
                    }
                    NumberAnimation {
                        target: mark
                        property: "opacity"
                        to: 1
                        duration: 220
                    }
                }
            }
            PauseAnimation {
                duration: 500
            }
            SequentialAnimation {
                loops: 3
                NumberAnimation {
                    target: ball
                    property: "rotation"
                    to: 14
                    duration: 60
                }
                NumberAnimation {
                    target: ball
                    property: "rotation"
                    to: -14
                    duration: 100
                }
                NumberAnimation {
                    target: ball
                    property: "rotation"
                    to: 0
                    duration: 60
                }
            }
            // the "!" goes and the character climbs out onto the ball
            NumberAnimation {
                target: mark
                property: "opacity"
                to: 0
                duration: 140
            }
            ParallelAnimation {
                NumberAnimation {
                    target: guy
                    property: "y"
                    to: notice.startY
                    duration: 520
                    easing.type: Easing.OutBack
                    easing.overshoot: 1.5
                }
                NumberAnimation {
                    target: guy
                    property: "scale"
                    from: 0.6
                    to: 1
                    duration: 360
                    easing.type: Easing.OutCubic
                }
            }

            // it looks around
            NumberAnimation {
                target: notice
                property: "lookX"
                to: -1
                duration: 110
            }
            PauseAnimation {
                duration: 170
            }
            NumberAnimation {
                target: notice
                property: "lookX"
                to: 1
                duration: 150
            }
            PauseAnimation {
                duration: 170
            }

            // the crouch before the flip
            NumberAnimation {
                target: notice
                property: "squash"
                to: 0.68
                duration: 300
                easing.type: Easing.InOutSine
            }

            // the flip to the right
            ParallelAnimation {
                NumberAnimation {
                    target: notice
                    property: "flip"
                    from: 0
                    to: 1
                    duration: 1350
                }
                NumberAnimation {
                    target: guy
                    property: "rotation"
                    from: 0
                    to: 360
                    duration: 1350
                    easing.type: Easing.InOutSine
                }
                // pushed by the jump, the ball closes back into the pill
                SequentialAnimation {
                    PauseAnimation {
                        duration: 120
                    }
                    NumberAnimation {
                        target: notice
                        property: "ballX"
                        to: notice.ballInX
                        duration: 560
                        easing.type: Easing.InOutCubic
                    }
                    NumberAnimation {
                        target: ball
                        property: "opacity"
                        to: 0
                        duration: 100
                    }
                }
                SequentialAnimation {
                    NumberAnimation {
                        target: notice
                        property: "squash"
                        to: 1.12
                        duration: 220
                        easing.type: Easing.OutSine
                    }
                    NumberAnimation {
                        target: notice
                        property: "squash"
                        to: 1
                        duration: 900
                        easing.type: Easing.InOutSine
                    }
                }
            }

            // the landing
            NumberAnimation {
                target: notice
                property: "squash"
                to: 0.74
                duration: 120
                easing.type: Easing.OutSine
            }
            NumberAnimation {
                target: notice
                property: "squash"
                to: 1
                duration: 520
                easing.type: Easing.OutBack
                easing.overshoot: 2.2
            }
            PauseAnimation {
                duration: 160
            }

            // a few more steps, slow to start and slow to stop
            ParallelAnimation {
                NumberAnimation {
                    target: guy
                    property: "x"
                    to: notice.stopX
                    duration: 2200
                    easing.type: Easing.InOutSine
                }
                NumberAnimation {
                    target: notice
                    property: "walk"
                    from: 0
                    to: Math.PI * 8
                    duration: 2200
                    easing.type: Easing.InOutSine
                }
                SequentialAnimation {
                    NumberAnimation {
                        target: notice
                        property: "lean"
                        to: 5
                        duration: 420
                        easing.type: Easing.InOutSine
                    }
                    PauseAnimation {
                        duration: 1360
                    }
                    NumberAnimation {
                        target: notice
                        property: "lean"
                        to: 0
                        duration: 420
                        easing.type: Easing.InOutSine
                    }
                }
            }

            // the news
            ParallelAnimation {
                NumberAnimation {
                    target: notice
                    property: "lookX"
                    to: 0
                    duration: 160
                }
                NumberAnimation {
                    target: bubble
                    property: "scale"
                    from: 0
                    to: 1
                    duration: 300
                    easing.type: Easing.OutBack
                }
            }
            ScriptAction {
                script: {
                    notice.phase = "waiting";
                    idle.restart();
                    giveUp.restart();
                    if (!notice.hovered)
                        hideDelay.restart();
                }
            }
        }

        // While it waits: a blink and a look around every few seconds.
        Timer {
            id: idle
            interval: 4000
            repeat: true
            onTriggered: waiting.restart()
        }

        SequentialAnimation {
            id: waiting
            NumberAnimation {
                target: notice
                property: "blink"
                to: 0.1
                duration: 70
            }
            NumberAnimation {
                target: notice
                property: "blink"
                to: 1
                duration: 90
            }
            NumberAnimation {
                target: notice
                property: "lookX"
                to: -1
                duration: 160
                easing.type: Easing.InOutSine
            }
            PauseAnimation {
                duration: 320
            }
            NumberAnimation {
                target: notice
                property: "lookX"
                to: 1
                duration: 240
                easing.type: Easing.InOutSine
            }
            PauseAnimation {
                duration: 320
            }
            NumberAnimation {
                target: notice
                property: "lookX"
                to: 0
                duration: 160
                easing.type: Easing.InOutSine
            }
        }

        // Down into the edge of the screen, leaving the eyes out.
        ParallelAnimation {
            id: sinkAnim
            NumberAnimation {
                target: bubble
                property: "scale"
                to: 0
                duration: 220
                easing.type: Easing.InSine
            }
            NumberAnimation {
                target: guy
                property: "y"
                to: notice.peekY
                duration: 560
                easing.type: Easing.InOutSine
            }
        }

        // Back out when the mouse touches it.
        ParallelAnimation {
            id: riseAnim
            NumberAnimation {
                target: guy
                property: "y"
                to: notice.floorY
                duration: 340
                easing.type: Easing.OutCubic
            }
            SequentialAnimation {
                PauseAnimation {
                    duration: 180
                }
                NumberAnimation {
                    target: bubble
                    property: "scale"
                    to: 1
                    duration: 280
                    easing.type: Easing.OutBack
                }
            }
        }

        // No click: it slips away for now.
        SequentialAnimation {
            id: goneAnim
            ParallelAnimation {
                NumberAnimation {
                    target: bubble
                    property: "scale"
                    to: 0
                    duration: 200
                }
                NumberAnimation {
                    target: guy
                    property: "y"
                    to: notice.awayY
                    duration: 600
                    easing.type: Easing.InSine
                }
            }
            ScriptAction {
                script: notice.finished()
            }
        }

        // After the click: a happy hop, and it is gone.
        SequentialAnimation {
            id: outro
            ParallelAnimation {
                NumberAnimation {
                    target: bubble
                    property: "scale"
                    to: 0
                    duration: 150
                }
                NumberAnimation {
                    target: guy
                    property: "y"
                    to: notice.floorY - 34 * notice.s
                    duration: 190
                    easing.type: Easing.OutQuad
                }
            }
            NumberAnimation {
                target: guy
                property: "scale"
                to: 0
                duration: 220
                easing.type: Easing.InBack
            }
            ScriptAction {
                script: notice.finished()
            }
        }
    }
}
