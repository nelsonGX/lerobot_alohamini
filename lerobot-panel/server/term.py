"""A minimal terminal screen buffer for showing console programs in the browser.

Calibration redraws a live min/max table with cursor-up escapes, so plain line logging
would repeat it thousands of times. This keeps a scrollback of lines and applies the
handful of escapes our scripts use (cursor movement, erase line); colours are dropped.
"""

from __future__ import annotations

import codecs
import re
import time

TOKEN_RE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b[()][A-Za-z0-9]|\x1b[^\[()]|[\r\n\b\t]|[^\x1b\r\n\b\t]+")
PARTIAL_ESC_RE = re.compile(r"\x1b(\[[0-9;?]*|[()])?$")


class TermBuffer:
    def __init__(self, max_lines: int = 3000) -> None:
        self.max_lines = max_lines
        self.lines: list[str] = [""]
        self.row = 0
        self.col = 0
        self.dropped = 0  # lines trimmed from the top; absolute row = dropped + row
        # Starts at a timestamp so a restarted program never reuses the previous one's version.
        self.version = int(time.time() * 1000)
        self._pending = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def cursor_line(self) -> int:
        return self.dropped + self.row

    def feed(self, data: bytes) -> None:
        text = self._pending + self._decoder.decode(data)
        self._pending = ""
        if m := PARTIAL_ESC_RE.search(text):
            self._pending, text = text[m.start() :], text[: m.start()]
        for m in TOKEN_RE.finditer(text):
            tok = m.group(0)
            if m.group(2):
                self._csi(m.group(1), m.group(2))
            elif tok == "\n":
                self._move_row(self.row + 1)
                self.col = 0
            elif tok == "\r":
                self.col = 0
            elif tok == "\b":
                self.col = max(0, self.col - 1)
            elif tok == "\t":
                self._write(" " * (8 - self.col % 8))
            elif tok[0] != "\x1b":
                self._write("".join(c for c in tok if c >= " " or c == "\x7f").replace("\x7f", ""))
        self.version += 1

    def append_note(self, text: str) -> None:
        """A panel message on its own line (e.g. "[panel] stopping...")."""
        if self.lines[self.row]:
            self._move_row(self.row + 1)
        self.col = 0
        self._write(text)
        self._move_row(self.row + 1)
        self.col = 0
        self.version += 1

    def tail(self, n: int) -> tuple[int, list[str]]:
        """Last n lines (without a trailing empty cursor line) and the absolute index of the first."""
        lines = self.lines
        end = len(lines)
        while end > 0 and not lines[end - 1].strip() and end - 1 >= self.row:
            end -= 1
        start = max(0, end - n)
        return self.dropped + start, [line.rstrip() for line in lines[start:end]]

    def _write(self, s: str) -> None:
        if not s:
            return
        line = self.lines[self.row]
        if len(line) < self.col:
            line += " " * (self.col - len(line))
        self.lines[self.row] = line[: self.col] + s + line[self.col + len(s) :]
        self.col += len(s)

    def _move_row(self, row: int) -> None:
        row = max(0, row)
        while row >= len(self.lines):
            self.lines.append("")
        self.row = row
        extra = len(self.lines) - self.max_lines
        if extra > 0:
            del self.lines[:extra]
            self.dropped += extra
            self.row = max(0, self.row - extra)

    def _csi(self, params: str, cmd: str) -> None:
        nums = [int(p) for p in params.lstrip("?").split(";") if p.isdigit()]
        n = nums[0] if nums else 1
        if cmd == "A":
            self._move_row(self.row - n)
        elif cmd in ("B", "E"):
            self._move_row(self.row + n)
            if cmd == "E":
                self.col = 0
        elif cmd == "F":
            self._move_row(self.row - n)
            self.col = 0
        elif cmd == "C":
            self.col += n
        elif cmd == "D":
            self.col = max(0, self.col - n)
        elif cmd == "G":
            self.col = max(0, n - 1)
        elif cmd == "K":
            mode = nums[0] if nums else 0
            line = self.lines[self.row]
            if mode == 0:
                self.lines[self.row] = line[: self.col]
            elif mode == 1:
                self.lines[self.row] = " " * min(self.col, len(line)) + line[self.col :]
            else:
                self.lines[self.row] = ""
        # Colours (m), screen clears (J), cursor positioning (H) etc. are ignored: this is a log view.
