"""Menu-bar / system-tray icon on Windows and macOS: Settings, Copy last dictation, Check for updates, Quit.

Linux has no tray here (Wayland trays need extra system libraries); it gets an app-menu entry instead.
"""
import sys


def available() -> bool:
    if sys.platform not in ("win32", "darwin"):
        return False
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def _icon_image():
    """A dark rounded square with five white voice bars, drawn at runtime so no image file is needed."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(23, 23, 23, 255))
    for i, h in enumerate((14, 26, 38, 26, 14)):
        x = 14 + i * 9
        draw.rounded_rectangle((x, (size - h) // 2, x + 5, (size + h) // 2), radius=2, fill=(255, 255, 255, 255))
    return img


class Tray:
    def __init__(self, on_settings, on_check_updates, on_quit, on_copy_last=None):
        import pystray
        self.on_quit = on_quit
        menu = pystray.Menu(
            pystray.MenuItem("Settings", lambda: on_settings(), default=True),
            pystray.MenuItem("Copy last dictation", lambda: on_copy_last and on_copy_last()),
            pystray.MenuItem("Check for updates", lambda: on_check_updates()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit hold-dictate", self._quit),
        )
        self.icon = pystray.Icon("hold-dictate", _icon_image(), "hold-dictate", menu)

    def _quit(self):
        self.icon.stop()
        self.on_quit()

    def run(self):
        """Blocks; must be called on the main thread (macOS requires it)."""
        self.icon.run()

    def stop(self):
        self.icon.stop()
