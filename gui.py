#!/usr/bin/env python3
"""DVR live view window: all cameras in a grid; click one to open it full size.

The grid is the same ffmpeg mosaic as `hikview live`, read here as raw RGB
frames so the window knows which tile was clicked. A clicked camera opens in
its own ffplay window on the main stream.

Keys: F full screen, Esc leave full screen, R rescan cameras, Q quit.
"""

import subprocess
import sys
import threading
import time

from PySide6.QtCore import QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

import hikview

# Tile size: larger when there are only a few cameras.
def cell_size(n):
    return (640, 360) if n <= 4 else (480, 270)


RETRY_SECONDS = 5


class GridReader(QThread):
    """Runs the ffmpeg mosaic and keeps the latest frame."""

    status = Signal(str)
    layout = Signal(list, int, int)  # channels, cols, rows

    def __init__(self, cfg, rescan=False):
        super().__init__()
        self.cfg = cfg
        self.rescan = rescan
        self.lock = threading.Lock()
        self.frame = None
        self.proc = None
        self.stopping = False

    def run(self):
        urls = hikview.grid_streams(self.cfg, self.rescan, log=self.status.emit)
        if not urls:
            self.status.emit("No camera answered. Check the password and that RTSP is enabled.")
            return
        chans = list(urls)
        cols, rows = hikview.grid_shape(len(chans))
        self.layout.emit(chans, cols, rows)
        cw, chh = cell_size(len(chans))
        w, h = cols * cw, rows * chh
        cmd = hikview.grid_command(urls, cw, chh) + [
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        size = w * h * 3
        while not self.stopping:
            self.status.emit("Connecting to {} cameras...".format(len(chans)))
            self.proc = hikview.popen(cmd, stdout=subprocess.PIPE)
            first = True
            while not self.stopping:
                data = self.proc.stdout.read(size)
                if len(data) < size:
                    break
                img = QImage(data, w, h, w * 3, QImage.Format_RGB888).copy()
                with self.lock:
                    self.frame = img
                if first:
                    self.status.emit("")
                    first = False
            self.proc.kill()
            self.proc.wait()
            if not self.stopping:
                self.status.emit("Lost the video feed; reconnecting in {} s...".format(RETRY_SECONDS))
                time.sleep(RETRY_SECONDS)

    def latest(self):
        with self.lock:
            return self.frame

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.wait(3000)


class GridWindow(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("DVR live")
        self.resize(1440, 810)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.chans, self.cols, self.rows = [], 1, 1
        self.message = "Starting..."
        self.hover = None
        self.frame = None
        self.viewers = {}  # channel -> ffplay process
        self.reader = None
        self.start_reader(rescan=False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000 // 15)

    def start_reader(self, rescan):
        if self.reader:
            self.reader.stop()
        self.frame = None
        self.reader = GridReader(self.cfg, rescan)
        self.reader.status.connect(self.set_message)
        self.reader.layout.connect(self.set_layout)
        self.reader.start()

    def set_message(self, text):
        self.message = text
        self.update()

    def set_layout(self, chans, cols, rows):
        self.chans, self.cols, self.rows = chans, cols, rows
        self.setWindowTitle("DVR live - click a camera to open it")

    def tick(self):
        img = self.reader.latest() if self.reader else None
        if img is not None and img is not self.frame:
            self.frame = img
            self.update()

    def picture_rect(self):
        """Where the mosaic is drawn: as large as fits, keeping its shape."""
        if self.frame is None:
            return QRect()
        fw, fh = self.frame.width(), self.frame.height()
        scale = min(self.width() / fw, self.height() / fh)
        w, h = int(fw * scale), int(fh * scale)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def channel_at(self, pos):
        r = self.picture_rect()
        if r.isNull() or not r.contains(pos):
            return None
        col = (pos.x() - r.x()) * self.cols // r.width()
        row = (pos.y() - r.y()) * self.rows // r.height()
        i = row * self.cols + col
        return self.chans[i] if i < len(self.chans) else None

    def tile_rect(self, ch):
        r = self.picture_rect()
        i = self.chans.index(ch)
        tw, th = r.width() / self.cols, r.height() / self.rows
        return QRect(int(r.x() + (i % self.cols) * tw), int(r.y() + (i // self.cols) * th),
                     int(tw), int(th))

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if self.frame is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(self.picture_rect(), self.frame)
            if self.hover in self.chans:
                p.setPen(QPen(QColor(255, 200, 0), 3))
                p.drawRect(self.tile_rect(self.hover).adjusted(1, 1, -2, -2))
        if self.message:
            p.setPen(Qt.white)
            p.setFont(QFont("Sans", 14))
            p.drawText(self.rect(), Qt.AlignCenter, self.message)

    def mouseMoveEvent(self, event):
        ch = self.channel_at(event.position().toPoint())
        if ch != self.hover:
            self.hover = ch
            self.setToolTip("Camera {}".format(ch) if ch else "")
            self.update()

    def leaveEvent(self, event):
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        ch = self.channel_at(event.position().toPoint())
        if ch:
            self.open_camera(ch)

    def open_camera(self, ch):
        """Open one camera's main stream in its own window (once)."""
        proc = self.viewers.get(ch)
        if proc and proc.poll() is None:
            return
        url = hikview.live_url(self.cfg, ch, 1)
        self.viewers[ch] = hikview.popen([
            "ffplay", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            *hikview.LOW_DELAY, *hikview.FAST_OPEN,
            "-window_title", "Camera {} live".format(ch),
            "-vf", "setdar=" + self.cfg["aspect"], url])

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif key == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        elif key == Qt.Key_R:
            self.start_reader(rescan=True)
        elif key == Qt.Key_Q:
            self.close()

    def closeEvent(self, event):
        self.timer.stop()
        if self.reader:
            self.reader.stop()
        for proc in self.viewers.values():
            if proc.poll() is None:
                proc.terminate()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DVR live")
    app.setDesktopFileName("hikview-live")
    win = GridWindow(hikview.load_config())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
