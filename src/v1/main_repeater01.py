import sys
import av
import soundtouch
import numpy as np
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QSlider, QFileDialog, QLabel, QSpinBox)
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QImage, QPixmap

STEP = 0.1  # 微调步长 0.1秒，对应一心复读机的微调

class ABRepeater(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python AB复读机（复刻一心复读机AB模式）")
        self.resize(900, 600)

        # 播放器状态
        self.container = None
        self.audio_stream = None
        self.video_stream = None
        self.current_time = 0.0
        self.duration = 0.0
        self.a_point = None
        self.b_point = None
        self.speed = 1.0
        self.is_playing = False

        self.soundtouch = soundtouch.SoundTouch()
        self.soundtouch.setTempo(self.speed)

        self.init_ui()
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.setInterval(30)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 视频画面
        self.video_label = QLabel("打开MP3/MP4文件")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.video_label)

        # 时间轴
        slider_layout = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 10000)
        self.slider.sliderMoved.connect(self.on_slider_move)
        self.time_label = QLabel("00:00 / 00:00")
        slider_layout.addWidget(self.slider)
        slider_layout.addWidget(self.time_label)
        layout.addLayout(slider_layout)

        # AB控制按钮
        ab_layout = QHBoxLayout()
        self.btn_setA = QPushButton("设置A")
        self.btn_setB = QPushButton("设置B")
        self.btn_a_left = QPushButton("A←微调")
        self.btn_a_right = QPushButton("A→微调")
        self.btn_b_left = QPushButton("B←微调")
        self.btn_b_right = QPushButton("B→微调")
        self.btn_reset_ab = QPushButton("重置AB")

        self.btn_setA.clicked.connect(self.set_a)
        self.btn_setB.clicked.connect(self.set_b)
        self.btn_a_left.clicked.connect(lambda: self.adjust_a(-STEP))
        self.btn_a_right.clicked.connect(lambda: self.adjust_a(STEP))
        self.btn_b_left.clicked.connect(lambda: self.adjust_b(-STEP))
        self.btn_b_right.clicked.connect(lambda: self.adjust_b(STEP))
        self.btn_reset_ab.clicked.connect(self.reset_ab)

        ab_layout.addWidget(self.btn_setA)
        ab_layout.addWidget(self.btn_a_left)
        ab_layout.addWidget(self.btn_a_right)
        ab_layout.addWidget(self.btn_setB)
        ab_layout.addWidget(self.btn_b_left)
        ab_layout.addWidget(self.btn_b_right)
        ab_layout.addWidget(self.btn_reset_ab)
        layout.addLayout(ab_layout)

        # 播放控制
        play_layout = QHBoxLayout()
        self.btn_open = QPushButton("打开文件")
        self.btn_play = QPushButton("播放/暂停")
        self.btn_back = QPushButton("快退-0.5s")
        self.btn_forward = QPushButton("快进+0.5s")
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(5, 20)
        self.speed_spin.setValue(10)
        self.speed_spin.setPrefix("倍速 x")
        self.speed_spin.valueChanged.connect(self.change_speed)

        self.btn_open.clicked.connect(self.open_file)
        self.btn_play.clicked.connect(self.play_pause)
        self.btn_back.clicked.connect(lambda: self.seek(-0.5))
        self.btn_forward.clicked.connect(lambda: self.seek(0.5))

        play_layout.addWidget(self.btn_open)
        play_layout.addWidget(self.btn_play)
        play_layout.addWidget(self.btn_back)
        play_layout.addWidget(self.btn_forward)
        play_layout.addWidget(self.speed_spin)
        layout.addLayout(play_layout)

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择MP3/MP4", "", "Media (*.mp3 *.mp4)")
        if not path:
            return
        if self.container:
            self.container.close()
        self.container = av.open(path)
        self.audio_stream = self.container.streams.audio[0] if self.container.streams.audio else None
        self.video_stream = self.container.streams.video[0] if self.container.streams.video else None
        self.duration = self.container.duration / av.time_base
        self.current_time = 0.0
        self.a_point = None
        self.b_point = None
        self.update_time_display()
        self.video_label.setText("加载完成，点击播放")

    def set_a(self):
        self.a_point = self.current_time
        print(f"A点：{self.a_point:.2f}s")
    def set_b(self):
        self.b_point = self.current_time
        print(f"B点：{self.b_point:.2f}s")
    def adjust_a(self, delta):
        if self.a_point is not None:
            self.a_point = max(0.0, self.a_point + delta)
    def adjust_b(self, delta):
        if self.b_point is not None:
            self.b_point = min(self.duration, self.b_point + delta)
    def reset_ab(self):
        self.a_point = None
        self.b_point = None
    def change_speed(self, val):
        self.speed = val / 10.0
        self.soundtouch.setTempo(self.speed)

    def seek(self, delta):
        new_t = self.current_time + delta
        self.seek_to(new_t)
    def seek_to(self, t):
        t = np.clip(t, 0, self.duration)
        self.current_time = t
        self.container.seek(int(t * av.time_base), stream=self.audio_stream)
        self.update_time_display()

    def on_slider_move(self, val):
        t = val / 10000 * self.duration
        self.seek_to(t)

    def play_pause(self):
        self.is_playing = not self.is_playing
        if self.is_playing:
            self.timer.start()
        else:
            self.timer.stop()

    def update_frame(self):
        if not self.container:
            return
        # AB循环逻辑
        if self.a_point is not None and self.b_point is not None and self.b_point > self.a_point:
            if self.current_time >= self.b_point:
                self.seek_to(self.a_point)
                return
        # 时间推进
        self.current_time += 0.03 * self.speed
        if self.current_time >= self.duration:
            self.is_playing = False
            self.timer.stop()
            return
        # 视频帧渲染
        if self.video_stream:
            for frame in self.container.decode(video=0):
                img = frame.to_image()
                qimg = QImage(img.tobytes(), img.width, img.height, QImage.Format.Format_RGB888)
                pix = QPixmap.fromImage(qimg).scaled(self.video_label.size(), Qt.AspectRatioMode.KeepAspectRatio)
                self.video_label.setPixmap(pix)
                break
        self.update_time_display()

    def update_time_display(self):
        if self.duration <= 0:
            return
        pct = self.current_time / self.duration
        self.slider.setValue(int(pct * 10000))
        def fmt(t):
            m = int(t//60)
            s = t % 60
            return f"{m:02d}:{s:04.1f}"
        self.time_label.setText(f"{fmt(self.current_time)} / {fmt(self.duration)}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ABRepeater()
    win.show()
    sys.exit(app.exec())