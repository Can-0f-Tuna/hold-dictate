"""The macOS pieces, against a stand-in for AppKit, so they run on every system."""
import sys
import types

import pytest

from hold_dictate import macos


class Item:
    def __init__(self, kinds=None):
        self.kinds = dict(kinds or {})

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def types(self):
        return list(self.kinds)

    def dataForType_(self, kind):
        return self.kinds.get(kind)

    def setData_forType_(self, data, kind):
        self.kinds[kind] = data


class Board:
    def __init__(self, items):
        self.items, self.count = items, 1

    def pasteboardItems(self):
        return self.items

    def changeCount(self):
        return self.count

    def clearContents(self):
        self.items, self.count = [], self.count + 1

    def setString_forType_(self, text, kind):
        self.items = [Item({kind: text})]

    def writeObjects_(self, items):
        self.items = list(items)


@pytest.fixture
def board(monkeypatch):
    board = Board([Item({"public.png": b"\x89PNG", "public.rtf": b"{\\rtf1}"}), Item({"public.utf8-plain-text": b"two"})])
    appkit = types.SimpleNamespace(NSPasteboard=types.SimpleNamespace(generalPasteboard=lambda: board),
                                   NSPasteboardItem=Item, NSPasteboardTypeString="public.utf8-plain-text")
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    return board


def test_the_clipboard_comes_back_whole_images_and_rich_text_too(board):
    clip = macos.Clipboard()
    clip.put("dictated")
    assert [i.kinds for i in board.items] == [{"public.utf8-plain-text": "dictated"}]
    clip.restore()
    assert [i.kinds for i in board.items] == [{"public.png": b"\x89PNG", "public.rtf": b"{\\rtf1}"}, {"public.utf8-plain-text": b"two"}]


def test_something_copied_meanwhile_is_not_overwritten(board):
    clip = macos.Clipboard()
    clip.put("dictated")
    board.clearContents()
    board.setString_forType_("copied by hand", "public.utf8-plain-text")
    clip.restore()
    assert [i.kinds for i in board.items] == [{"public.utf8-plain-text": "copied by hand"}]
