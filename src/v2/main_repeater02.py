# 结论修正：mpv 的 IPC 是【长连接】模型，不是短连接。
# 日志里的 [ipc_xx] Write error (Broken pipe) 不是 mpv 主动断开，而是我们自己造成的：
# 每次 connect → sendall → close，发完立刻把 socket 关掉，mpv 再把
# {"error":"success"} 的回复写回来时对端已经没了，于是 mpv 打印 EPIPE。
# 每 300ms 轮询两次 = 每 300ms 两条 Broken pipe，和日志完全对得上。
#
# 根治方案（方案A）：
# 1. 全程只保持一条 IPC 长连接，不再每读一次就新建 socket；
# 2. socket 设为非阻塞，定时器里只做 drain（把事件和回复读干净），绝不阻塞 UI；
# 3. 用 observe_property 订阅 time-pos / duration，靠 mpv 主动推送，
#    不再主动 get_property 轮询；
# 4. 连接断了自动重连，mpv 退出则停止。


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
POLL_MS = 100


class MpvIPC:
    """mpv IPC 长连接（非阻塞）。

    - command() 只把 JSON 行放进发送缓冲，不等待回复；
    - poll() 由 QTimer 驱动，把 socket 里的数据读干净；
    - observe_property 推送的属性值缓存在 props 里，UI 直接读缓存。
    """

    PROP_IDS = {"time-pos": 1, "duration": 2}

    def __init__(self):
        self.sock = None
        self.inbuf = b""
        self.outbuf = b""
        self.req_id = 0
        self.pending = {}   # request_id -> 命令文本，仅用于报错
        self.props = {}
        self.last_error = None

    @property
    def alive(self):
        return self.sock is not None

    def connect(self, path, timeout=0.3):
        self.close()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)        # 建连阶段允许短暂阻塞
        s.setblocking(False)   # 之后全部非阻塞
        self.sock = s
        self.inbuf = b""
        self.outbuf = b""
        self.props.clear()
        for name, pid in self.PROP_IDS.items():
            self.command("observe_property", pid, name)
        return True

    def close(self):
        if self.sock is not None:
            try:
                # 先把对端数据读空，避免带着未读数据关闭 socket 触发 RST
                while self.sock.recv(65536):
                    pass
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.inbuf = b""
        self.outbuf = b""
        self.props.clear()

    def command(self, *args):
        if self.sock is None:
            return
        self.req_id += 1
        self.pending[self.req_id] = " ".join(str(a) for a in args)
        payload = json.dumps({"command": list(args),
                              "request_id": self.req_id}) + "\n"
        self.outbuf += payload.encode("utf-8")
        self._flush()

    def _flush(self):
        while self.outbuf and self.sock is not None:
            try:
                n = self.sock.send(self.outbuf)
            except BlockingIOError:
                return
            except OSError:
                self.close()
                return
            self.outbuf = self.outbuf[n:]

    def poll(self):
        """读走当前所有可读数据。返回 False 表示连接已断。"""
        if self.sock is None:
            return False
        self._flush()
        while self.sock is not None:
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                self.close()
                break
            if not chunk:
                self.close()
                break
            self.inbuf += chunk
            while b"\n" in self.inbuf:
                line, self.inbuf = self.inbuf.split(b"\n", 1)
                self._handle_line(line)
        return self.alive

    def _handle_line(self, raw):
        raw = raw.strip()
        if not raw:
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if "event" in msg:
            if msg["event"] == "property-change":
                self.props[msg.get("name")] = msg.get("data")
            elif msg["event"] == "shutdown":
                self.close()
            return
        err = msg.get("error")
        if err not in (None, "success"):
            cmd = self.pending.pop(msg.get("request_id"), "?")
            if err != self.last_error:
                print(f"[mpv ipc] {cmd} -> {err}", file=sys.stderr)
            self.last_error = err
        elif err == "success":
            self.pending.pop(msg.get("request_id"), None)
            self.last_error = None


class MPVIPCRepeater(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt + mpv IPC AB复读机（X11内嵌wid版）")
        self.resize(800, 520)
        self.mpv_proc = None
        self.ipc = MpvIPC()
        self.a_point = None
        self.b_point = None
        self.init_ui()
        self.timer = QTimer()
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(POLL_MS)

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

    # ---------- mpv 进程 / IPC 生命周期 ----------

    def _mpv_running(self):
        return self.mpv_proc is not None and self.mpv_proc.poll() is None

    def _send(self, *args):
        if self._mpv_running() and self.ipc.alive:
            self.ipc.command(*args)

    def _try_connect_once(self):
        if not self._mpv_running() or not os.path.exists(SOCKET_PATH):
            return False
        try:
            return self.ipc.connect(SOCKET_PATH)
        except OSError:
            return False

    def _connect_with_wait(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._try_connect_once():
                return True
            if not self._mpv_running():
                return False
            time.sleep(0.05)
        return False

    def stop_mpv(self):
        self.ipc.close()
        if self.mpv_proc is not None:
            if self.mpv_proc.poll() is None:
                self.mpv_proc.terminate()
                try:
                    self.mpv_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.mpv_proc.kill()
                    self.mpv_proc.wait()
            self.mpv_proc = None
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass

    def start_mpv(self, filepath):
        self.stop_mpv()

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
        self.a_point = None
        self.b_point = None
        self.status_label.setText(f"已加载：{os.path.basename(filepath)}")
        if not self._connect_with_wait():
            self.status_label.setText("IPC 连接失败（mpv 是否在运行？）")

    def open_media(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开媒体", "", "Media (*.mp4 *.mp3 *.mkv)")
        if not path:
            return
        self.start_mpv(path)

    # ---------- 定时器：drain + 刷新 UI ----------

    def on_tick(self):
        if self.mpv_proc is None:
            return
        if not self._mpv_running():
            self.ipc.close()
            self.status_label.setText("mpv已退出")
            self.time_label.setText("00:00 / 00:00")
            return
        if not self.ipc.alive:
            if self._try_connect_once():
                self.status_label.setText("IPC已重新连接")
            if not self.ipc.alive:
                return
        if not self.ipc.poll():
            self.status_label.setText("IPC连接断开，重连中…")
            return
        self.update_progress()

    def _time_pos(self):
        t = self.ipc.props.get("time-pos")
        return float(t) if isinstance(t, (int, float)) else None

    def _duration(self):
        d = self.ipc.props.get("duration")
        return float(d) if isinstance(d, (int, float)) and d > 0 else None

    def update_progress(self):
        pos = self._time_pos()
        dur = self._duration()
        if pos is None or dur is None:
            return
        self.slider.setValue(int(min(max(pos / dur, 0.0), 1.0) * 10000))

        def fmt(sec):
            m = int(sec // 60)
            s = sec % 60
            return f"{m:02d}:{s:04.1f}"
        self.time_label.setText(f"{fmt(pos)} / {fmt(dur)}")

    # ---------- AB 打点 ----------

    def set_a(self):
        if not self._mpv_running():
            return
        t = self._time_pos()
        if t is None:
            self.status_label.setText("暂时读不到播放进度，请稍后再试")
            return
        self.a_point = t
        # mpv 0.37 的 set 命令只接受字符串值，传数字会报 invalid parameter
        self._send("set", "ab-loop-a", str(t))
        self.status_label.setText(f"A={t:.2f}s")

    def set_b(self):
        if not self._mpv_running():
            return
        t = self._time_pos()
        if t is None:
            self.status_label.setText("暂时读不到播放进度，请稍后再试")
            return
        self.b_point = t
        self._send("set", "ab-loop-b", str(t))
        # mpv 的 ab-loop 只在播放位置“从 B 之前跨过 B”时触发。设置 B 时 B 恰好等于
        # 当前时间（事件还有延迟，实际播放位置往往已越过 B），这个跨越永远等不到，
        # 表现就是设完 B 继续往后走。设完 B 立刻 seek 回 A 激活循环。
        if self.a_point is not None and self.a_point < t:
            self._send("seek", self.a_point, "absolute")
            self.status_label.setText(
                f"A={self.a_point:.2f}s ↔ B={t:.2f}s，循环中")
        elif self.a_point is None:
            self.status_label.setText(f"B={t:.2f}s（请先设置A点）")
        else:
            self.status_label.setText(f"B={t:.2f}s（A点必须在B之前）")

    def adjust_a(self, delta):
        if self.a_point is None or not self._mpv_running():
            return
        new_a = max(0.0, self.a_point + delta)
        self.a_point = new_a
        self._send("set", "ab-loop-a", str(new_a))
        self.status_label.setText(f"A={new_a:.2f}s")

    def adjust_b(self, delta):
        if self.b_point is None or not self._mpv_running():
            return
        dur = self._duration() or 9999
        new_b = min(dur, self.b_point + delta)
        self.b_point = new_b
        self._send("set", "ab-loop-b", str(new_b))
        self.status_label.setText(f"B={new_b:.2f}s")

    def reset_ab(self):
        if not self._mpv_running():
            return
        self.a_point = None
        self.b_point = None
        self._send("set", "ab-loop-a", "no")
        self._send("set", "ab-loop-b", "no")
        self.status_label.setText("AB已重置")

    # ---------- 播放控制 ----------

    def change_speed(self, val):
        self._send("set", "speed", str(val))

    def seek(self, delta):
        self._send("seek", delta, "relative")

    def on_slider_drag(self, value):
        dur = self._duration()
        if dur is None:
            return
        self._send("seek", value / 10000 * dur, "absolute")

    def play_pause(self):
        self._send("cycle", "pause")

    def closeEvent(self, event):
        self.stop_mpv()
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
