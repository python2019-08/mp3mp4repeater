# ch01.先说结论：**PyQt + python-mpv 方案，对你这个需求，确实更好**
对比前面 `pyav+soundtouch` 自研播放器：
✅ 优势（非常关键，就是一心复读机想要的效果）
1. **音视频同步、时间精度、AB循环、变速不变调全部由 mpv 底层搞定**，不用自己啃音频重采样、音画同步、soundtouch时序、缓冲这些地狱级坑；mpv 本身原生就支持精准 `ab-loop-a` / `ab-loop-b`，毫秒级打点，和一心复读机的AB逻辑完全对齐。
2. 原生支持 mp3、mp4、mkv、字幕srt、倍速、音频轨道切换，几乎所有媒体格式直接通吃。
3. 性能稳定，大视频、长音频不容易卡顿、不同步。
4. 代码量少很多，重心放在UI交互（A/B微调、按钮、时间轴），而不是媒体内核。

❌ 缺点
1. 依赖独立 mpv 程序（不是纯pip包，需要电脑安装 mpv），打包分发给别人麻烦一点；自己电脑用完全没问题。
2. 底层音频流拿出来做**录音跟读**会麻烦一点（跟读对比这个功能，mpv不方便直接截流）；但如果你现阶段只做【AB复读、播放、微调、倍速】，这个缺点无关紧要。

> 一心复读机核心就是：**精准AB循环 + 毫秒微调 + 倍速 + 时间轴**，mpv 天生就有这些参数，非常适配。

---

## 一、环境准备
### 1. 安装 mpv
- Windows：去 mpv.io 下载，或者用 scoop / choco 安装；**推荐把 mpv 加入系统PATH**
- Mac：`brew install mpv`
- Linux：`apt install mpv`
```sh
    sudo apt install mpv libmpv2
    sudo apt install libxcb-cursor0
```
### 2. 安装python包
```bash
pip install python-mpv pyqt6
```
> 注意：`python-mpv` 是 mpv 的绑定库，不是mpv本体！

## 二、完整可运行代码（PyQt6 + mpv，复刻一心复读机AB模式）

src/v2/main_repeater02.py

功能清单（对标第一张截图的AB模式）
- ✅ 打开 mp3 / mp4
- ✅ 播放 / 暂停 / 快退 / 快进
- ✅ 设置A点、设置B点
- ✅ A左微调 / A右微调、B左微调 / B右微调（0.1s步进，和一心复读机微调一致）
- ✅ 重置AB（取消循环）
- ✅ 倍速调节（0.5 ~ 2.0，变速不变调，mpv自带 `audio-pitch-correction=yes`）
- ✅ 拖动时间轴跳转
- ✅ 视频画面嵌入Qt窗口内
- ✅ AB区间自动循环播放

 

## 三、怎么继续扩展，一步步贴近一心复读机
mpv 本身能力很强，后续加功能都很顺：
1. **加载srt字幕**：`self.player.sub_files = "xxx.srt"`，mpv原生渲染字幕，支持字幕断句
2. **闪播**：本质就是一个很短的AB区间，你可以单独写一个按钮，选中一小段自动设置AB
3. **收藏片段**：把A、B时间、文件名保存到json，打开时直接加载ab-loop
4. **上一句/下一句（字幕断句模式）**：读取srt的时间列表，切换的时候直接seek到对应字幕时间，设置AB为该字幕起止时间
5. 长按微调、批量移动AB区间：直接修改 `ab_loop_a` / `ab_loop_b` 两个参数即可

> mpv 全部都有命令/属性可以控制，不需要自己处理媒体解码。

## 四、两个方案对比总结，方便你选
| 方案 | PyQt+mpv | PyQt+pyav+soundtouch |
|---|---|---|
| AB循环精度 | ⭐⭐⭐⭐⭐ 原生支持ab-loop | ⭐⭐⭐ 需要自己写循环判断，容易跳错 |
| 变速不变调 | ⭐⭐⭐⭐⭐ mpv内置audio-pitch-correction | ⭐⭐⭐ 依赖soundtouch，同步麻烦 |
| 音视频同步 | ⭐⭐⭐⭐⭐ mpv成熟稳定 | ⭐⭐ 大坑，需要自己做时间对齐 |
| 支持格式 | mp3/mp4/mkv/srt全部原生支持 | 需要自己处理不同流 |
| 上手开发 | 简单，重心写UI交互 | 复杂，大量底层音视频代码 |
| 录音跟读、音频截流 | 麻烦，mpv音频不容易导出 | 方便，音频流在python侧，可以直接录 |
| 打包成exe | 要附带mpv程序 | 打包体积大，但不需要外置mpv |

👉 **如果你优先做【AB复读、精听、播放mp4/mp3、字幕】，强烈推荐 PyQt+mpv，就是这个方案**
👉 **如果你后面核心需求是【录音跟读、AI打分、拿到原始音频流做处理】，再换回 pyav+soundtouch 自研方案**

## 五、补充踩坑提醒
1. Windows上 `python-mpv` 嵌入窗口，`wid` 绑定，部分显卡渲染后端会黑屏；遇到黑屏可以初始化mpv时加参数 `vo="gpu"` 或者 `vo="software"`
```python
self.player = mpv.MPV(
    wid=str(int(self.winId())),
    audio_pitch_correction="yes",
    vo="gpu"
)
```
2. `ab_loop_a` / `ab_loop_b` 是mpv原生属性，**设置后自动循环**，不需要你在python里写循环跳转逻辑，这是最香的一点。

如果你想要，我可以继续给你加上：**SRT字幕加载显示 + 字幕断句模式（第二张截图的功能）**。