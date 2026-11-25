# VtoH 实时问答

VtoH 将播放声音或麦克风音频转成实时字幕，在听到完整的问题后以「结论＋要点」回答。电脑、Android 客户端和配对网页共用同一个 FastAPI 会话。原始音频经 WebSocket 转送给识别服务，应用默认不写音频文件。

## 开通接口

两端在「设置」保存服务地址和访问密钥后会自动记住。Windows 使用当前用户的 DPAPI 加密，Android 使用系统 Keystore 加密。

- Windows 可点击「悬浮字幕」打开置顶横条，拖动标题调整位置。全局快捷键 `Ctrl+Alt+F9` 开始或停止监听，`Ctrl+Alt+F10` 显示或收起悬浮窗。小窗内可切换标准、轻透和高对比。
- Android 从右上角的「悬浮字幕」菜单开启并选择样式。首次开启时允许「显示在其他应用上层」。可拖动悬浮条标题调整位置。
- 两端悬浮窗都按顺序显示客户端已收到的会话字幕和可用译文，支持「原文＋译文」或「仅译文」。滚动到底部会跟随新字幕，向上翻阅时保留阅读位置。
- 悬浮窗可以调整字体。Windows 在悬浮窗顶部选择字体和字号，Android 在悬浮字幕菜单中选择字号。选择会记住。

开通阿里云百炼的中国内地（北京）工作空间，创建一个 DashScope API Key。默认的 Paraformer 实时语音识别和 Qwen Plus 共用这个密钥。

- [阿里云百炼控制台](https://bailian.console.aliyun.com/)
- [获取 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)
- [Paraformer 实时语音识别 WebSocket API](https://help.aliyun.com/zh/model-studio/paraformer-real-time-speech-recognition-api)
- [实时语音识别选型与音频指南](https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide)
- [Paraformer Python SDK](https://help.aliyun.com/zh/model-studio/realtime-websocket-python-sdk-paraformer)
- [Qwen OpenAI 兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)

服务端通过官方 WebSocket 协议接入 `paraformer-realtime-v2`。采集统一使用 16 kHz、16-bit、单声道 PCM，100 毫秒约 3,200 字节。收到 `task-started` 后开始推送音频，并持续接收 `result-generated`。同一句字幕按开始时间更新，`sentence_end` 为真时发布定稿并进入问题判断。停止时发送 `finish-task` 并等待尾部字幕。官方 DashScope Python SDK 用 `send_audio_frame()`、`events()` 和 `stop()` 表达相同流程，官方新版示例要求 `dashscope>=2.0.1`。

## 在同一 Wi-Fi 下联调

两端都通过右上角「设置」填写连接信息。开始监听前可在主界面选择「翻译为」；翻译默认关闭。开启后原文会实时转写，每句定稿后追加译文，Windows、Android 和配对网页共用译文。翻译使用百炼 `qwen-mt-flash`，默认复用 `VTOH_CHAT_KEY`，也可通过 `VTOH_TRANSLATION_KEY` 指定百炼密钥。若问答使用其他供应商的密钥，为翻译单独指定一个百炼密钥。开启翻译会产生模型调用费用。[百炼机器翻译](https://help.aliyun.com/zh/model-studio/qwen-mt)

在 Windows PowerShell 中启动 Python 3.11 或更新版本的服务。将同一个百炼密钥赋给两个服务端变量。启动脚本也识别官方示例使用的 `DASHSCOPE_API_KEY`，以及环境中已有的 `AI_BAILIAN_API_KEY`。需要指定 ASR 业务空间时可通过 `VTOH_ASR_WORKSPACE` 传入工作空间 ID。未设置 `VTOH_ACCESS_KEY` 时，脚本会生成一个临时客户端访问密钥并显示在终端。

```powershell
python -m pip install -r requirements.txt
$env:VTOH_ASR_KEY = '替换为你的百炼 Key'
$env:VTOH_CHAT_KEY = $env:VTOH_ASR_KEY
.\scripts\start-service.ps1
```

1. 打开 Windows 客户端的「设置」，将服务地址填为手机可访问的 Windows 局域网地址，例如 `http://192.168.1.10:8765/vtoh/`。访问密钥填写启动脚本显示的客户端访问密钥。
2. 播放声音后点击「刷新音源」。选择正在发声的进程或默认播放设备，然后开始监听。选中已定稿的字幕可手动触发回答。
3. 打开「手机配对」，用 Android 客户端或系统相机扫码。系统相机打开浏览器查看页。
4. Android 独立模式下填写同一服务地址和访问密钥，选择「麦克风」或「应用播放声音」，在系统界面授权并开始监听。若手机访问不到 Windows 服务，检查两端是否在同一个可互访的 Wi-Fi，并允许专用网络入站 TCP 8765。

Windows 的指定进程采集要求系统内部版本至少为 20348。默认播放采集跟随开始监听时的默认输出端点。Android 需要 10 或更新版本，且目标应用允许系统采集播放声音。应用播放内录适用于允许采集的媒体和游戏音频，电话和会议场景需逐个应用验证。

## 部署及构建

将服务部署到独立主机能让手机在电脑关机时继续使用。仓库包含 `Dockerfile` 和 `compose.yaml`。在主机配置 `VTOH_ASR_KEY`、`VTOH_CHAT_KEY` 和 `VTOH_ACCESS_KEY` 后运行 `docker compose up -d --build`。客户端默认使用 `https://www.ploverjoe.xyz/vtoh/`。

将以下规则加到现有的 `www.ploverjoe.xyz` Caddy 站点块中。Caddy 会保留 `/vtoh/` 前缀并转发 WebSocket。

```caddyfile
reverse_proxy /vtoh/* 127.0.0.1:8765
```

在服务器上用 `curl -f http://127.0.0.1:8765/vtoh/health` 检查应用，并用 `curl -f https://www.ploverjoe.xyz/vtoh/health` 检查公网转发。避免记录配对 URL 参数。服务目前使用单进程内存会话。服务重启后需要重新配对，未录制的会话在两小时无新事件后过期。

- Windows 安装包使用 Edge WebView2 和 C++ WASAPI。先运行 `python -m pip install -r requirements-windows.txt`，再用 x64 MinGW-w64 运行 `.\scripts\build-windows.ps1 -Portable`，随后使用 NSIS 编译 `scripts/windows-installer.nsi`。运行目录下保留 `vtoh_capture.exe`。运行客户端的电脑需要 [Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)。
- Windows Flutter 构建可通过 `.\scripts\build-windows.ps1` 生成，需要 Flutter、带 C++ 桌面工作负载的 Visual Studio 和 x64 MinGW-w64。
- Android 构建进入 `client`，安装 Flutter 和 Android SDK 后运行 `flutter build apk --debug`。验证用 APK 使用调试签名，发布需要配置自己的签名密钥。
- 运行 `python -m pytest -q` 检查服务端事件、配对鉴权和去重。运行 `flutter analyze` 检查客户端。
"# VtoH" 
