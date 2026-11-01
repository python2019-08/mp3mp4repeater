# ch01. v1：PyQt6 + libmpv（进程内）AB 复读机

在ubunut24.04上，用 `python-mpv` 在 Python 进程内直接调用 `libmpv`，**不启动外部 mpv 进程，也不走 IPC socket / JSON 协议**。

和 v2 相比：
- v2 需要 `subprocess` 启动 mpv，再用 unix socket 发 JSON 命令，多一层进程和协议；
- v1 只有 libmpv 一个动态库，AB 循环、变速不变调、音视频同步、格式支持全部由它原生完成，UI 侧直接读写属性。

## 创建conda 的python环境
```
# 1. 创建环境
conda create -n av python=3.12
# 2. 激活环境
conda activate av
```

## 依赖

Python 侧（conda `av` 环境已装好）：

```bash
pip install pyqt6 python-mpv
```

系统库：

```bash
sudo apt install libmpv2
```

已确认本机版本：ubunut24.04上 ，PyQt6 6.12、python-mpv 1.0.8、libmpv.so.2、mpv 0.37。

## 运行

```bash
conda activate av
python src/v1/main_repeater01.py
```

## 实现要点

- `ensure_player()`（`src/v1/main_repeater01.py:136`）首次打开文件时创建进程内
  `mpv.MPV(wid=...)`，之后复用；视频直接渲染进 Qt 的 `video_widget`（X11 `wid` 嵌入）。
- AB 循环用原生的 `player.ab_loop_a` / `player.ab_loop_b`。设完 B 后立即
  `seek(A, exact)` 激活循环：mpv 的 ab-loop 只在播放位置“从 B 之前跨过 B”时触发，
  设 B 时当前时间往往已越过 B（事件还有延迟），不 seek 就会表现为“设完 B 继续往后走”。
  重置时把两个属性置为 `"no"`。
- 倍速 `player.speed`，保留 `audio_pitch_correction`（变速不变调）。
- `QTimer` 50ms 读取 `time_pos` / `duration` 刷新 UI；拖动滑块时暂停自动刷新（`_dragging`），
  避免定时器和拖动抢滑块。
- 播放结束用 `keep_open="yes"` 停在最后一帧；再点播放时先 seek 0 再取消暂停
  （否则一取消暂停就又到结尾）。
- 关闭窗口时 `player.terminate()`。

## 踩坑

**LC_NUMERIC 必须是 C。** 进程内 libmpv 在非 C 的 LC_NUMERIC 下会打印
`Non-C locale detected. This is not supported.`，甚至直接段错误（第一次测试就崩过，exit 139）。
v1 在创建 `MPV()` 之前强制设置（`force_c_numeric_locale()`，`__main__` 里再设一次），
不再需要 v2 那套 `ctypes.CDLL("libc.so.6").setlocale` 的 hack。

## 验证结果

- 无头功能测试：设 A=1.49s、B=3.12s，6 秒内回绕 4 次；B 微调 +0.5、1.5 倍速、
  重置 AB 均正常（`ab_loop_a/b` 变回 `no`）。
- 真实 X11 测试：320x240 h264 测试视频成功嵌入 Qt 窗口，`video_params` 为
  yuv420p，`video_format = h264`，`time-pos` 正常推进（2.9s / 8.0s），
  退出无崩溃、无残留进程。

## 已知限制

- `wid` 嵌入：Linux 下只支持 X11（当前环境即 X11），Windows 下是 HWND 嵌入（见下文「在 Windows 上运行」）。以后上 Wayland 需要改成 libmpv 渲染回调：
  `vo="libmpv"` + `render_context` 方式，届时再改。
- 与 v2 一样，本方案不含录音跟读 / 音频流导出；如果需要这些，得另起一条 PyAV + soundtouch 的方案（原先的 PyAV 原型已删除）。

==========================================

# ch02。 在 Windows 上运行

代码本身不需要改：`wid` 在 win32 上就是 HWND，`str(int(self.video_widget.winId()))` 在 PyQt6/Windows 下同样返回可用的窗口句柄。Windows 上要做的是**把 libmpv 的 DLL 准备好**。

### 1. 安装 Python 和依赖

Windows 上用 64 位 Python 3.12（conda 或 python.org 安装包均可）：

```bat
conda create -n av python=3.12
conda activate av
pip install pyqt6 python-mpv
```

### 2. 准备 libmpv DLL

`python-mpv` 只是 ctypes 绑定，**本身不含 libmpv**。Windows 下它会按下面的顺序找 DLL（见 `mpv.py` 源码）：

1. `mpv-2.dll`
2. `libmpv-2.dll`
3. `mpv-1.dll`

查找位置：先用 `ctypes.util.find_library` 查系统标准位置 / `%PATH%`，再查 `mpv.py` 所在目录。

**下载**（任选一个）：

- SourceForge：<https://sourceforge.net/projects/mpv-player-windows/files/libmpv/> ，下载 `mpv-dev-x86_64-*.7z`（文件名带 `-v3-` 的版本要求 CPU 支持 AVX2，老 CPU 选不带 `v3` 的）
- GitHub：<https://github.com/shinchiro/mpv-winbuild-cmake/releases> 里的 `mpv-dev-x86_64-*.7z`，或 <https://github.com/zhongfly/mpv-winbuild/releases> 的 `mpv-dev-*.7z`

解压后得到 `libmpv-2.dll`（较新的构建；更早的版本里可能叫 `mpv-1.dll`）。

**放置方式**（任选，按省事排序）：

1. 放到 `mpv.py` 同目录，**不用改代码**：
   - conda：`<conda安装目录>\envs\av\Lib\site-packages\`
   - 官方 Python：`<Python安装目录>\Lib\site-packages\`
2. 放在项目目录（例如 `src\v1\dll\libmpv-2.dll`），并在 `import mpv` **之前**把目录加进 PATH：

   ```python
   import os
   os.environ["PATH"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dll") + os.pathsep + os.environ["PATH"]
   import mpv
   ```

   PATH 里的条目必须是**绝对路径**，否则 `ctypes.find_library` 找不到。
3. 把 DLL 所在目录加入系统环境变量 `PATH`（改完要重开终端）。

注意事项：

- **位数必须匹配**：64 位 Python 只能加载 x86_64 的 DLL；`mpv-dev-i686` 是给 32 位 Python 的。
- python-mpv 1.0.8 要求 libmpv API ≥ 1.108（即 mpv ≥ 0.33），近一两年的 dev 构建都满足。

### 3. 验证 DLL 能加载

```bat
python -c "import mpv; p = mpv.MPV(vo='null', audio='no'); print('libmpv', p.mpv_version); p.terminate()"
```

能打印出版本号（例如 `libmpv 0.37.0`）就说明 OK。如果报 `Cannot find mpv-2.dll ...`，是第 2 步没放对；报 `WinError 193` 一般是位数不匹配。

### 4. 运行

```bat
python src\v1\main_repeater01.py
```

### 5. Windows 下可能遇到的问题

- **黑屏 / 只有声音**：mpv 0.38+ 默认 `vo=gpu-next`，个别驱动或虚拟机里 wid 嵌入不稳。在 `ensure_player()` 里加 `vo="gpu"`；还不行再退回 `vo="software"`：

  ```python
  self.player = mpv.MPV(
      wid=str(int(self.video_widget.winId())),
      vo="gpu",                 # 黑屏时先加这个，再不行换成 "software"
      audio_pitch_correction=True,
      keep_open="yes",
      osc=False,
      input_default_bindings=False,
      input_vo_keyboard=False,
  )
  ```

- **窗口缩放后画面不跟随**：wid 嵌入的通病，拖动窗口大小后点一次「播放/暂停」或重新打开文件即可恢复。
- **locale**：`force_c_numeric_locale()` 在 Windows 上同样有效，不要删；libmpv 一样要求 `LC_NUMERIC=C`。
- **看不到 mpv 日志**：用 `python`（有控制台）运行才能看到 stderr；用 `pythonw` 或打包后没有日志属正常。
- **中文路径/文件名**：Python 3 传 Unicode 路径，mpv 原生支持 UTF-8，正常打开。

### 6. 打包成 exe（可选）

```bat
pip install pyinstaller
pyinstaller --noconfirm --windowed --add-binary "libmpv-2.dll;." src\v1\main_repeater01.py
```

- `--add-binary "libmpv-2.dll;."` 把 DLL 打到程序根目录（与打包后的 `mpv.py` 同级），python-mpv 能找到。
- 分发时拷贝整个 `dist\main_repeater01\` 目录，不能只拷 exe。

### 7. v2 方案能不能直接搬到 Windows？

不建议直接照搬：Windows 下 mpv 的 `--input-ipc-server` 是**命名管道**（`\\.\pipe\名字`），v2 里写死的 `/tmp/mpv_repeater_socket` 和 `AF_UNIX` 连接方式不能原样使用。Windows 上优先用上面的 v1 wid 嵌入方案。
