"""PyQt6 + libmpv（进程内）AB 复读机

和 v2 的区别：
- 不启动独立的 mpv 进程，也不走 IPC socket / JSON 协议；
- 通过 python-mpv 直接在 Python 进程内调用 libmpv；
- AB 循环、变速不变调、音视频同步、格式支持全部由 libmpv 原生完成。

依赖：
    pip install pyqt6 python-mpv
    系统库：libmpv（Ubuntu: sudo apt install libmpv2）
"""

import locale
import os
import sys

import mpv
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget,
                             QVBoxLayout, QHBoxLayout,
                             QPushButton, QSlider, QFileDialog,
                             QLabel, QDoubleSpinBox)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut

STEP = 0.1        # A/B 微调步长（秒）
TICK_MS = 50      # UI 刷新间隔；进程内读属性开销极小
SEEK_PRECISION = "exact"
SHORTCUT_LABELS = {"Space": "空格", "Left": "←", "Right": "→"}  # 按钮下方提示文案


def force_c_numeric_locale():
    """libmpv 要求 LC_NUMERIC=C，否则选项里的数字会被按本地格式解析。

    注意：必须在创建 MPV 实例之前设置；PyQt 有可能在启动过程中改回系统 locale，
    所以 ensure_player() 里还会再设一次。
    """
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass


class MPVRepeater(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt + libmpv 进程内 AB复读机")
        self.resize(900, 600)

        self.player = None
        self.a_point = None
        self.b_point = None
        self._dragging = False
        self.shortcuts = []

        self.init_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(TICK_MS)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # libmpv 会把视频渲染进这个 QWidget 的 X11 窗口
        self.video_widget = QWidget()
        self.video_widget.setMinimumHeight(320)
        main_layout.addWidget(self.video_widget, 1)

        self.status_label = QLabel("状态：未打开媒体")
        main_layout.addWidget(self.status_label)

        slider_layout = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 10000)
        self.slider.sliderMoved.connect(self.on_slider_drag)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.time_label = QLabel("00:00 / 00:00")
        slider_layout.addWidget(self.slider)
        slider_layout.addWidget(self.time_label)
        main_layout.addLayout(slider_layout)

        ab_layout = QHBoxLayout()
        self.btn_setA = QPushButton("设置A")
        self.btn_a_left = QPushButton("A←微调")
        self.btn_a_right = QPushButton("A→微调")
        self.btn_setB = QPushButton("设置B")
        self.btn_b_left = QPushButton("B←微调")
        self.btn_b_right = QPushButton("B→微调")
        self.btn_reset_ab = QPushButton("重置AB")

        self.btn_setA.clicked.connect(self.set_a)
        self.btn_a_left.clicked.connect(lambda: self.adjust_a(-STEP))
        self.btn_a_right.clicked.connect(lambda: self.adjust_a(STEP))
        self.btn_setB.clicked.connect(self.set_b)
        self.btn_b_left.clicked.connect(lambda: self.adjust_b(-STEP))
        self.btn_b_right.clicked.connect(lambda: self.adjust_b(STEP))
        self.btn_reset_ab.clicked.connect(self.reset_ab)

        # 快捷键：A/B 用同一字母设置，微调用 Z X / C V（左键位←，右键位→）
        for key, button, slot in (
            ("A", self.btn_setA, self.set_a),
            ("Z", self.btn_a_left, lambda: self.adjust_a(-STEP)),
            ("X", self.btn_a_right, lambda: self.adjust_a(STEP)),
            ("B", self.btn_setB, self.set_b),
            ("C", self.btn_b_left, lambda: self.adjust_b(-STEP)),
            ("V", self.btn_b_right, lambda: self.adjust_b(STEP)),
            ("R", self.btn_reset_ab, self.reset_ab),
        ):
            ab_layout.addWidget(self.bind_shortcut(key, button, slot))

        main_layout.addLayout(ab_layout)

        ctrl_layout = QHBoxLayout()
        self.btn_open = QPushButton("打开文件")
        self.btn_play = QPushButton("播放/暂停")
        self.btn_back = QPushButton("快退 -0.5s")
        self.btn_fwd = QPushButton("快进 +0.5s")

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.5, 2.0)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.valueChanged.connect(self.change_speed)

        self.btn_open.clicked.connect(self.open_media)
        self.btn_play.clicked.connect(self.play_pause)
        self.btn_back.clicked.connect(lambda: self.seek(-0.5))
        self.btn_fwd.clicked.connect(lambda: self.seek(0.5))

        # 播放控制：空格播放/暂停，←/→ 快退/快进
        ctrl_layout.addWidget(self.btn_open)
        for key, button, slot in (
            ("Space", self.btn_play, self.play_pause),
            ("Left", self.btn_back, lambda: self.seek(-0.5)),
            ("Right", self.btn_fwd, lambda: self.seek(0.5)),
        ):
            ctrl_layout.addWidget(self.bind_shortcut(key, button, slot))

        ctrl_layout.addWidget(QLabel("倍速:"))
        ctrl_layout.addWidget(self.speed_spin)
        main_layout.addLayout(ctrl_layout)

    def bind_shortcut(self, key, button, slot):
        """绑定窗口级快捷键，返回「按钮 + 下方按键提示」的组合控件。"""
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(slot)
        self.shortcuts.append(shortcut)  # 持有引用，避免被垃圾回收

        hint = QLabel(SHORTCUT_LABELS.get(key, key))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #666; font-size: 14px;")

        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(button)
        layout.addWidget(hint)
        return wrapper

    # ---------- libmpv 生命周期 ----------

    def ensure_player(self):
        """首次打开文件时创建进程内 libmpv 实例，之后复用。"""
        if self.player is None:
            force_c_numeric_locale()
            self.player = mpv.MPV(
                wid=str(int(self.video_widget.winId())),
                audio_pitch_correction=True,
                keep_open="yes",          # 播完停在最后一帧，便于重新开始
                osc=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
            )
        return self.player

    def open_media(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开媒体", "", "Media (*.mp4 *.mp3 *.mkv *.m4a *.wav)")
        if path:
            self.load_media(path)

    def load_media(self, path):
        player = self.ensure_player()
        self.a_point = None
        self.b_point = None
        player.ab_loop_a = "no"
        player.ab_loop_b = "no"
        player.speed = self.speed_spin.value()
        player.pause = False
        player.play(path)
        self.status_label.setText(f"已加载：{os.path.basename(path)}")

    def _prop(self, name):
        """安全读取属性（player 被销毁或未加载时返回 None）。"""
        if self.player is None:
            return None
        try:
            return getattr(self.player, name)
        except Exception:
            return None

    # ---------- 定时刷新 ----------

    def on_tick(self):
        if self.player is None:
            return
        pos = self._prop("time_pos")
        dur = self._prop("duration")
        if pos is None or dur is None or dur <= 0:
            return

        if not self._dragging:
            self.slider.setValue(int(min(max(pos / dur, 0.0), 1.0) * 10000))

        def fmt(sec):
            m = int(sec // 60)
            s = sec % 60
            return f"{m:02d}:{s:04.1f}"
        self.time_label.setText(f"{fmt(pos)} / {fmt(dur)}")

    def _on_slider_pressed(self):
        self._dragging = True

    def _on_slider_released(self):
        self._dragging = False

    # ---------- AB 打点 ----------

    def set_a(self):
        t = self._prop("time_pos")
        if t is None:
            self.status_label.setText("读不到播放进度，请先打开媒体")
            return
        self.a_point = float(t)
        self.player.ab_loop_a = float(t)
        self.status_label.setText(f"A={t:.2f}s")

    def set_b(self):
        t = self._prop("time_pos")
        if t is None:
            self.status_label.setText("读不到播放进度，请先打开媒体")
            return
        self.b_point = float(t)
        self.player.ab_loop_b = float(t)
        if self.a_point is not None and self.a_point < t:
            # 设完 B 立即从 A 开始循环，行为可预期
            self.player.seek(self.a_point, reference="absolute",
                             precision=SEEK_PRECISION)
            self.status_label.setText(
                f"A={self.a_point:.2f}s ↔ B={t:.2f}s，循环中")
        elif self.a_point is None:
            self.status_label.setText(f"B={t:.2f}s（请先设置A点）")
        else:
            self.status_label.setText(f"B={t:.2f}s（A点必须在B之前）")

    def adjust_a(self, delta):
        if self.a_point is None or self.player is None:
            return
        new_a = max(0.0, self.a_point + delta)
        self.a_point = new_a
        self.player.ab_loop_a = new_a
        self.status_label.setText(f"A={new_a:.2f}s")

    def adjust_b(self, delta):
        if self.b_point is None or self.player is None:
            return
        dur = self._prop("duration") or 9999
        new_b = min(dur, self.b_point + delta)
        self.b_point = new_b
        self.player.ab_loop_b = new_b
        self.status_label.setText(f"B={new_b:.2f}s")

    def reset_ab(self):
        if self.player is None:
            return
        self.a_point = None
        self.b_point = None
        self.player.ab_loop_a = "no"
        self.player.ab_loop_b = "no"
        self.status_label.setText("AB已重置")

    # ---------- 播放控制 ----------

    def change_speed(self, val):
        if self.player is not None:
            self.player.speed = float(val)

    def seek(self, delta):
        if self.player is not None:
            self.player.seek(delta, reference="relative",
                             precision=SEEK_PRECISION)

    def on_slider_drag(self, value):
        dur = self._prop("duration")
        if dur is None or dur <= 0 or self.player is None:
            return
        self.player.seek(value / 10000 * dur, reference="absolute",
                         precision=SEEK_PRECISION)

    def play_pause(self):
        if self.player is None:
            return
        if self._prop("eof_reached"):
            # keep-open=yes 时播完会停在结尾，直接继续播放等于立刻又到结尾
            self.player.seek(0, reference="absolute", precision=SEEK_PRECISION)
            self.player.pause = False
            return
        self.player.pause = not bool(self._prop("pause"))

    def closeEvent(self, event):
        if self.player is not None:
            try:
                self.player.terminate()
            except Exception:
                pass
            self.player = None
        event.accept()


if __name__ == "__main__":
    force_c_numeric_locale()
    app = QApplication(sys.argv)
    win = MPVRepeater()
    win.show()
    sys.exit(app.exec())
