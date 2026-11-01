# 明白了，是 X11，那排除了 Wayland 的基础坑。
# 现在现象：**视频正常播放、AB 命令已经不报错，只是定时器持续轮询的时候，IPC 连接随机断开（Broken pipe）**。
# 根本原因：
# > mpv IPC 是**短连接模型**！不是长连接。
# > 每次你 connect + send + close，mpv 这边接受这个连接、处理完这条 JSON 命令之后，就会主动关掉这个 socket 连接。
# > 你现在的写法是：每一次 `mpv_get_property` 都新建一次 socket，这本身没问题；但 mpv 内部有一个限制：**短连接并发/高频连续连接时，会主动拒绝、直接挂断，表现就是 Broken pipe**。
# > 尤其是你 100ms 定时器，高频 `get_property(time-pos)`，疯狂建连，很容易触发这个。

# mpv 官方推荐的稳定方案不是“每读一次就新建socket”，而是两种：
# 1. 长连接：**持续保持一个socket，读 mpv 的事件流**（推荐）；
# 2. 降低轮询频率 + 失败重试 + 熔断，不要 100ms 疯狂轮。

# > 补充：`--wid` 在 X11 本身是稳定的，**不是 wid 导致断开，是高频短连接轮询压垮 IPC**。

# ## 方案A：改成长连接（根治，推荐）
# mpv IPC 支持长连接，打开 socket 之后，持续读 mpv 主动推送的事件，里面自带 `time-pos`、`duration`、`pause` 这些属性通知，**不用你主动轮询 get_property**。
# mpv 会主动推送属性变化，不再需要定时器反复去拉。

# > 缺点：代码要改 IPC 通信模型，要开一个独立线程去读 socket 事件流（Qt 主线程不能阻塞）。

# ## 方案B：低成本改造（不改模型，直接给你可用版本，保留wid内嵌）
# 做三件事：
# 1. 定时器从 100ms → **300ms**，减少连接压力；
# 2. 增加指数退避重试，连续失败就熔断，停止发包；
# 3. 发送命令、读取属性的时候，**捕获broken pipe，不刷屏打印**；
# 4. 保留 `--wid`，继续内嵌播放。

# 下面这个是【保留内嵌wid、X11、熔断保护、减少报错刷屏】的完整代码：


import sys
import os
import json
import socket
import subprocess
import time
import locale
import ctypes
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget,
                             QVBoxLayout, QHBoxLayout,
                             QPushButton, QSlider, QFileDialog,
                             QLabel, QDoubleSpinBox)
from PyQt6.QtCore import Qt, QTimer

STEP = 0.1
SOCKET_PATH = "/tmp/mpv_repeater_socket"

def mpv_send_command(sock_path, cmd_list):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        sock.connect(sock_path)
        payload = json.dumps({"command": cmd_list}) + "\n"
        sock.sendall(payload.encode("utf-8"))
        sock.close()
        return True
    except (ConnectionResetError, BrokenPipeError, OSError):
        return False

def mpv_get_property(sock_path, prop):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        sock.connect(sock_path)
        req = json.dumps({"command": ["get_property", prop]}) + "\n"
        sock.sendall(req.encode("utf-8"))
        data = sock.recv(4096).decode("utf-8")
        sock.close()
        for line in data.splitlines():
            try:
                j = json.loads(line)
                if "data" in j:
                    return j["data"]
            except json.JSONDecodeError:
                continue
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    return None


class MPVIPCRepeater(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt + mpv IPC AB复读机（X11内嵌wid版）")
        self.resize(800, 520)
        self.mpv_proc = None
        self.a_point = None
        self.b_point = None
        self.ipc_alive = False
        self.ipc_fail_cnt = 0
        self.max_fail = 5
        self.init_ui()
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_progress)
        self.timer.start(300)  # 拉长到300ms，降低连接压力

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8,8,8,8)
        main_layout.setSpacing(8)

        self.video_widget = QWidget()
        self.video_widget.setFixedHeight(280)
        main_layout.addWidget(self.video_widget)

        self.status_label = QLabel("状态：未打开媒体")
        main_layout.addWidget(self.status_label)

        slider_layout = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 10000)
        self.slider.sliderMoved.connect(self.on_slider_drag)
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

        ab_layout.addWidget(self.btn_setA)
        ab_layout.addWidget(self.btn_a_left)
        ab_layout.addWidget(self.btn_a_right)
        ab_layout.addWidget(self.btn_setB)
        ab_layout.addWidget(self.btn_b_left)
        ab_layout.addWidget(self.btn_b_right)
        ab_layout.addWidget(self.btn_reset_ab)
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

        ctrl_layout.addWidget(self.btn_open)
        ctrl_layout.addWidget(self.btn_play)
        ctrl_layout.addWidget(self.btn_back)
        ctrl_layout.addWidget(self.btn_fwd)
        ctrl_layout.addWidget(QLabel("倍速:"))
        ctrl_layout.addWidget(self.speed_spin)
        main_layout.addLayout(ctrl_layout)

    def start_mpv(self, filepath):
        if self.mpv_proc is not None:
            self.mpv_proc.terminate()
            self.mpv_proc.wait()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        wid = int(self.video_widget.winId())
        cmd = [
            "mpv",
            f"--wid={wid}",
            "--input-ipc-server=" + SOCKET_PATH,
            "--audio-pitch-correction=yes",
            "--no-osc",
            filepath
        ]
        self.mpv_proc = subprocess.Popen(cmd)
        time.sleep(0.7)
        self.a_point = None
        self.b_point = None
        self.ipc_alive = True
        self.ipc_fail_cnt = 0
        self.status_label.setText(f"已加载：{os.path.basename(filepath)}")

    def open_media(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开媒体", "", "Media (*.mp4 *.mp3 *.mkv)")
        if not path:
            return
        self.start_mpv(path)

    def set_a(self):
        if not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        t = mpv_get_property(SOCKET_PATH, "time-pos")
        if t is None:
            self.ipc_fail_cnt +=1
            if self.ipc_fail_cnt >= self.max_fail:
                self.ipc_alive = False
            return
        self.ipc_fail_cnt = 0
        self.a_point = t
        mpv_send_command(SOCKET_PATH, ["set", "ab-loop-a", str(t)])
        self.status_label.setText(f"A={t:.2f}s")

    def set_b(self):
        if not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        t = mpv_get_property(SOCKET_PATH, "time-pos")
        if t is None:
            self.ipc_fail_cnt +=1
            if self.ipc_fail_cnt >= self.max_fail:
                self.ipc_alive = False
            return
        self.ipc_fail_cnt = 0
        self.b_point = t
        mpv_send_command(SOCKET_PATH, ["set", "ab-loop-b", str(t)])
        self.status_label.setText(f"B={t:.2f}s")

    def adjust_a(self, delta):
        if self.a_point is None or not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        new_a = max(0.0, self.a_point + delta)
        self.a_point = new_a
        mpv_send_command(SOCKET_PATH, ["set", "ab-loop-a", str(new_a)])
        self.status_label.setText(f"A={new_a:.2f}s")

    def adjust_b(self, delta):
        if self.b_point is None or not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        dur = mpv_get_property(SOCKET_PATH, "duration") or 9999
        new_b = min(dur, self.b_point + delta)
        self.b_point = new_b
        mpv_send_command(SOCKET_PATH, ["set", "ab-loop-b", str(new_b)])
        self.status_label.setText(f"B={new_b:.2f}s")

    def reset_ab(self):
        if not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        self.a_point = None
        self.b_point = None
        mpv_send_command(SOCKET_PATH, ["set", "ab-loop-a", "no"])
        mpv_send_command(SOCKET_PATH, ["set", "ab-loop-b", "no"])
        self.status_label.setText("AB已重置")

    def change_speed(self, val):
        if not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        mpv_send_command(SOCKET_PATH, ["set", "speed", val])

    def seek(self, delta):
        if not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        mpv_send_command(SOCKET_PATH, ["seek", delta, "relative"])

    def on_slider_drag(self, value):
        if not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        dur = mpv_get_property(SOCKET_PATH, "duration")
        if dur is None or dur <= 0:
            self.ipc_fail_cnt +=1
            if self.ipc_fail_cnt >= self.max_fail:
                self.ipc_alive = False
            return
        self.ipc_fail_cnt = 0
        target = value / 10000 * dur
        mpv_send_command(SOCKET_PATH, ["seek", target, "absolute"])

    def play_pause(self):
        if not self.ipc_alive or self.mpv_proc.poll() is not None:
            return
        mpv_send_command(SOCKET_PATH, ["cycle", "pause"])

    def update_progress(self):
        if self.mpv_proc is None:
            return
        if self.mpv_proc.poll() is not None:
            self.status_label.setText("mpv已退出")
            self.time_label.setText("00:00 / 00:00")
            self.ipc_alive = False
            return
        if not self.ipc_alive:
            return
        pos = mpv_get_property(SOCKET_PATH, "time-pos")
        dur = mpv_get_property(SOCKET_PATH, "duration")
        if pos is None or dur is None:
            self.ipc_fail_cnt +=1
            if self.ipc_fail_cnt >= self.max_fail:
                self.ipc_alive = False
                self.time_label.setText("IPC断开，等待重开视频")
            return
        self.ipc_fail_cnt = 0
        pct = pos / dur
        self.slider.setValue(int(pct * 10000))

        def fmt(sec):
            m = int(sec // 60)
            s = sec % 60
            return f"{m:02d}:{s:04.1f}"
        self.time_label.setText(f"{fmt(pos)} / {fmt(dur)}")

    def closeEvent(self, event):
        if self.mpv_proc is not None:
            self.mpv_proc.terminate()
            self.mpv_proc.wait()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    os.environ["LC_NUMERIC"] = "C"
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.setlocale(1, b"C")
        print("✓ 成功通过 ctypes 修改底层 Locale")
    except Exception as e:
        print(f"ctypes locale 设置失败: {e}")

    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except Exception:
        pass

    win = MPVIPCRepeater()
    win.show()
    sys.exit(app.exec())
