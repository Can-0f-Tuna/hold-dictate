"""The pill's window on macOS: a borderless non-activating panel, in AppKit through PyObjC (pynput and pystray
already bring it). Tk won't do: a Tk app comes to the front and takes the keyboard when it opens a window."""
import os
import threading

import objc
import Quartz
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSCompositingOperationSourceOver,
    NSEvent,
    NSGraphicsContext,
    NSImage,
    NSImageSymbolConfiguration,
    NSMouseInRect,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindowAnimationBehaviorNone,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSObject, NSTimer
from PyObjCTools import AppHelper

FRAME = 1 / 30
EDGE = 16  # from the edge of the screen's usable area
SYMBOLS = {"mic": "mic.slash.fill", "net": "wifi.slash", "alert": "exclamationmark"}


def _symbol(name: str, size: float):
    try:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(SYMBOLS.get(name, SYMBOLS["alert"]), None)
        config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, 8.0)  # heavy
        config = config.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithPaletteColors_([NSColor.whiteColor()]))
        return image.imageWithSymbolConfiguration_(config)
    except AttributeError:  # before macOS 12: a plain red dot still says it
        return None


class PillPanel(NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class PillView(NSView):
    pill = None

    def isFlipped(self):
        return True

    def drawRect_(self, _rect):
        pill = self.pill
        ctx = NSGraphicsContext.currentContext().CGContext()
        s, height = pill.scale, self.bounds().size.height
        width = self.bounds().size.width
        left = (width - pill.WIDTH * s) / 2
        Quartz.CGContextClearRect(ctx, self.bounds())

        Quartz.CGContextSetRGBFillColor(ctx, *pill.bg, 1)
        Quartz.CGContextSetRGBStrokeColor(ctx, *pill.BORDER, 1)
        Quartz.CGContextSetLineWidth(ctx, 1)
        pill_h = pill.HEIGHT * s
        box = Quartz.CGRectMake(left + 0.5, 0.5, pill.WIDTH * s - 1, pill_h - 1)
        path = Quartz.CGPathCreateWithRoundedRect(box, (pill_h - 1) / 2, (pill_h - 1) / 2, None)
        Quartz.CGContextAddPath(ctx, path)
        Quartz.CGContextDrawPath(ctx, Quartz.kCGPathFillStroke)

        Quartz.CGContextSaveGState(ctx)
        Quartz.CGContextTranslateCTM(ctx, left + (pill.WIDTH - pill.FIELD_W) / 2 * s, (pill.HEIGHT - pill.FIELD_H) / 2 * s)
        Quartz.CGContextScaleCTM(ctx, s, s)
        shapes = pill.shapes()
        color = pill.bar_color()
        if pill.lines:
            for x, y, w, h, alpha in shapes:
                Quartz.CGContextSetRGBFillColor(ctx, *color, alpha)
                Quartz.CGContextAddPath(ctx, Quartz.CGPathCreateWithRoundedRect(Quartz.CGRectMake(x, y, w, h), 1.5, 1.5, None))
                Quartz.CGContextFillPath(ctx)
        else:
            Quartz.CGContextSetRGBFillColor(ctx, *color, 1)
            Quartz.CGContextAddRects(ctx, [Quartz.CGRectMake(x, y, w, h) for x, y, w, h, _ in shapes], len(shapes))
            Quartz.CGContextFillPath(ctx)
        Quartz.CGContextRestoreGState(ctx)

        if pill.mark > 0.01:
            size = pill.MARK * s
            x, y = left + (pill.WIDTH + pill.MARK_GAP) * s, (height - size) / 2
            Quartz.CGContextSetRGBFillColor(ctx, *pill.PROBLEM, pill.mark)
            Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(x, y, size, size))
            glyph = _symbol(pill.icon, 22 * s)
            if glyph:
                w, h = glyph.size().width, glyph.size().height
                glyph.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                    NSMakeRect(x + (size - w) / 2, y + (size - h) / 2, w, h), NSMakeRect(0, 0, 0, 0),
                    NSCompositingOperationSourceOver, pill.mark, True, None)


class Controller(NSObject):
    @objc.python_method
    def setup(self, pill):
        self.pill = pill
        self.timer = None
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = PillPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 10, 10), style, NSBackingStoreBuffered, False)
        panel = self.panel
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setAnimationBehavior_(NSWindowAnimationBehaviorNone)  # the fade is ours
        panel.setLevel_(NSStatusWindowLevel)
        panel.setIgnoresMouseEvents_(True)  # clicks go to whatever is underneath
        panel.setHidesOnDeactivate_(False)  # this app is never active
        panel.setCollectionBehavior_(  # every Space, over full-screen apps, not in Mission Control or Cmd+`
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle)
        panel.setReleasedWhenClosed_(False)
        view = PillView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        view.pill = pill
        panel.setContentView_(view)
        return self

    def place(self):
        """On the screen the mouse is on, checked each time: displays come and go."""
        pill = self.pill
        where = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens() if NSMouseInRect(where, s.frame(), False)), NSScreen.mainScreen())
        if screen is None:
            return
        area = screen.visibleFrame()
        s = pill.scale
        width = (pill.WIDTH + 2 * (pill.MARK + pill.MARK_GAP)) * s
        height = pill.HEIGHT * s
        y = area.origin.y + area.size.height - height - EDGE if pill.top else area.origin.y + EDGE
        self.panel.setFrame_display_(NSMakeRect(area.origin.x + (area.size.width - width) / 2, y, width, height), False)

    def sync(self):
        self.pill.step(0)
        if self.pill.wants_frames() and self.timer is None:
            self.place()
            self.panel.setAlphaValue_(max(0.01, self.pill.shown))
            self.panel.orderFrontRegardless()  # shows it without making anything key or active
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                FRAME, self, b"frame:", None, True)
            self.timer.setTolerance_(FRAME / 4)

    def frame_(self, _timer):
        self.pill.step(FRAME)
        if self.pill.wants_frames():
            self.panel.setAlphaValue_(max(0.01, self.pill.shown))
            self.panel.contentView().setNeedsDisplay_(True)
        else:  # no timer at all between recordings, so the helper sleeps
            self.timer.invalidate()
            self.timer = None
            self.panel.orderOut_(None)


def run(pill):
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon, no menu bar
    controller = Controller.alloc().init().setup(pill)
    wake = lambda: AppHelper.callAfter(controller.sync)
    threading.Thread(target=pill.read_stdin, args=(wake, lambda: os._exit(0)), daemon=True).start()
    NSApp.run()
