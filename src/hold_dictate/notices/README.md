# Notices

The way hold-dictate tells you something on screen: a small "!" ball that comes from the pill. One QML file per notice; `overlay.qml` creates them and routes the daemon's events to them.

| Notice | File | Event | What it does |
| --- | --- | --- | --- |
| Update | `UpdateNotice.qml` | `U <version\|->`, `D`, `F` | A "!" tab on the pill. A click installs the update, with a chip in the corner of the screen |
| You got Premium | `PremiumNotice.qml` | `P <on\|->` | With the next dictation a ball slides out of the pill and a small character climbs out of it. The character flips to the bottom edge of the screen while the ball closes back into the pill, walks a little and asks for a click, then sinks until only its eyes show and comes out for the mouse. The click opens the Premium settings, whose controls open one by one. With no click for 30 seconds it leaves; the next dictation brings it back |

To add one:

1. Add `notices/YourNotice.qml`. Take the overlay as `required property var shell` for the look (`pillColor`, `barColor`, `sizeScale`, `atTop`, `pillCY`).
2. Choose a letter for its event and handle it in `handle()` in `overlay.qml`; list it in the comment at the top of that file. Old overlays ignore letters they do not know.
3. Send the event from `feedback.py`. The overlay can start after the first line was written, so send it again with each dictation until the notice is done.
4. Keep it light: animate item properties (position, scale, rotation, opacity) rather than repainting a Canvas, and stop every animation when the notice waits. Load a big notice with `LazyLoader`, so it costs nothing until it plays.
5. Stay out of the way: take clicks only on the notice's own items, through the window's `mask`.

Windows and macOS have no overlay, so `app.py` falls back to a system notification there.
