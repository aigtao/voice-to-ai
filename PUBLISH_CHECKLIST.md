# 免费开源发布检查单

版本：`VoiceToAI 0.1.1-preview`

## 公开前

- [ ] 确认只发布 `release\VoiceToAI-0.1.1-preview-windows-x64-offline.zip`、`release\VoiceToAI-0.1.1-preview-source.zip` 和 `release\SHA256SUMS.txt`。
- [ ] 不发布普通用户目录、录音、转写历史、截图、`settings.json` 或任何 API Key。
- [ ] 保留 `LICENSE`、`THIRD_PARTY_NOTICES.md` 和模型目录中的许可证声明。
- [ ] 给每个下载地址标注 Windows x64、预览版和文件大小。
- [ ] 把 `README.md` 和 `QUICKSTART.md` 一起放入源码仓库；发布页链接到 `RELEASE_NOTES.md`。

## 给下载者的最短说明

1. 下载带 `offline` 的完整包。
2. 完整解压，不要只运行压缩包内的 exe。
3. 双击 `VoiceToAI.exe`，保留 `_internal` 和 `models` 文件夹。
4. 在目标 AI 输入框点击一次，按默认快捷键 `Ctrl+Alt+Space` 开始和结束录音。
5. 自动输入失败时，在结果窗口点击“一键复制”，再手动粘贴。

软件本身完全免费；默认使用本地 Whisper。绑定 API 是可选项，API 费用和隐私政策由用户选择的服务商负责。不要把 API Key 发给作者或提交到仓库。

## 发布后反馈模板

请提供以下信息，不要提供 API Key、录音或私人对话：

- Windows 版本和系统位数；
- 使用的包名及 SHA256 是否匹配；
- 麦克风型号；
- 本地模式或 API 模式；
- 目标应用名称；
- 发生时间、界面状态和可复现步骤；
- 是否可以一键复制结果。

## 当前边界

当前包没有数字签名、安装器或自动更新，也没有公开下载地址。发布前由作者补充仓库地址、下载地址和反馈入口；不要在文档中填写占位链接。
