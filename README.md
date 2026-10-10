# Voice to AI

免费的 Windows 语音输入工具，面向 ChatGPT、Codex、Kimi 等 AI 对话：按快捷键开始/结束录音，转写后输入当前对话框，无法自动输入时可一键复制。

## 免费分享定位

- 软件功能完全免费，不设置订阅、付费解锁或试用次数限制。
- 默认使用本地 Whisper，无需绑定 API；首次使用可能需要下载模型。
- 用户可自选音频 API 并填写自己的 Key。第三方 API 的费用、额度和限制由服务商决定，软件免费不代表所有 API 都免费。
- 项目用于免费分享、交流反馈和积累关注。关注作者属于自愿行为，不作为下载或使用条件。
- 提供 Windows x64 便携预览版和 MIT 源码；新用户推荐含 Whisper 模型的离线完整包。已公开发布：https://github.com/aigtao/voice-to-ai/releases/tag/0.1.1-preview

没有 API 时使用本地 Whisper；绑定支持音频输入的 API 后，由所选模型直接转写录音。标点、专有名词识别和语义整理能力取决于具体模型，普通 ASR 接口不保证能整理表达逻辑。

默认不需要云端 API。绑定 API 后，录音音频会发送到你指定的音频转写接口；音频隐私、费用和保存政策由服务商决定。中文结果会统一转换为简体中文。

当前代码仍是开发预览。已打包不代表已完成全部跨设备兼容、签名或隐私审计。

### 许可证

本项目已选择 MIT，见 `LICENSE`。允许使用、修改、商用和再分发，请保留版权和许可证声明。第三方依赖和模型保留各自许可证，见 `THIRD_PARTY_NOTICES.md`。

## 启动

普通用户：完整解压 `VoiceToAI-0.1.1-preview-windows-x64-offline.zip`，双击 `VoiceToAI.exe`，保留 `_internal` 和 `models` 文件夹。不需要 Python、API 或首次模型下载。具体操作见 `QUICKSTART.md`。

以下为源码开发启动方式：

推荐直接双击 `启动语音工具.vbs`。它会在后台启动语音工具，不显示 PowerShell 窗口。也可以双击 `run.bat`，效果相同。

```powershell
python .\app.py
```

开发安装建议使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python .\app.py
```

源码启动脚本优先使用项目内的 `.venv`，其次查找当前用户的 Python 3.12 安装或系统 Python。便携版直接双击 exe。

本地模式优先加载 exe/app.py 旁的 `models/small`，兼容个人数据目录的模型及 Hugging Face 缓存；全部本地文件齐全时不请求模型网站。模型缺失才尝试从 Hugging Face 下载，并显示下载状态。下载超时可使用离线完整包。模型运行在本机 CPU。

依赖中包含 `opencc-python-reimplemented`，用于把中文转写和 AI 整理结果统一转换为简体中文。

## 使用

1. 启动后选择麦克风，默认使用系统默认输入设备。
2. 在任意 AI 输入框中点击需要输入的位置。
3. 按一下快捷键开始说话，再按一下结束，不需要持续按住三个键。
4. 未绑定 API 时，程序把**分段后的本地转写**粘贴到录音前的窗口，不会自动发送；如果 Windows 拒绝切换，会明确提示已复制并保留手动粘贴。
5. 没有检测到有效语音时，不会粘贴任何内容，也不会重复使用上一次内容。
6. 绑定 API 后，录音音频会直接交给音频模型完成识别、断句、标点和整理，再把最终文字输入当前 AI 对话框。
7. 点击“设置快捷键”可以保存自定义快捷键。

Windows 下已绑定组合键的普通按键会被工具拦截，避免同时触发微信、Kimi 等目标应用的快捷键；其他按键照常传入当前窗口。转写过程中再次按快捷键可取消本次输入。

如果已绑定音频转写 API，自动输入失败时的复制窗口会显示 API 模型生成的整理结果；如果 API 请求失败，会回退为可输入或复制的本地转写。未绑定 API 时，复制窗口立即复制本地转写。

自定义键位会按捕获到的实际按键类型保存在本机，适配修饰键、字母/数字、功能键和可识别的系统特殊键。窗口使用 Voice to AI 专属麦克风图标和更新后的浅色界面。

默认显示屏幕底部的圆角小胶囊，点击麦克风或使用快捷键开始录音；录音时左侧按钮结束并转写，右侧 × 取消并丢弃本次内容。波纹跟随实际麦克风音量，短提示显示录音时长和转写状态。转写中可取消本次输入，取消的结果不会粘贴。

空闲时点击胶囊右侧箭头或文字区展开主界面；圆角双栏分别显示原始转写和 AI 整理结果，原始转写支持一键复制和输入当前窗口。主窗口点“最小化”会进入任务栏；点右上角关闭会隐藏到系统托盘，托盘右键菜单可以重新打开、显示浮条或彻底退出。程序使用单实例锁，重复启动时会提示使用已有窗口。

## 隐私边界

- 默认使用本地 `faster-whisper`，音频不会自动上传到外部服务。
- 音频和文本只保存在当前用户的本地应用数据目录，窗口中的“清理本地记录”会删除它们。
- 绑定 API 后，会发送原始录音音频；API 地址、模型和 Key 保存在 `%LOCALAPPDATA%\\VoiceToAI-MVP\\settings.json`，不要把该文件提交到公开仓库。
- 本地分段和标点是保守规则，不等于人工校对；涉及金额、路径、代码和重要事实时必须复核。

## 已知限制

- 便携版面向 Windows 10/11 x64；构建使用 Python 3.12。
- 本地分段使用规则，API 输出能力取决于所选模型；复杂表达和专有名词需要用户复核。
- 模型推理速度取决于 CPU；第一次加载可能需要等待。
- Whisper 会自动判断中文或英文；短音频、夹杂中英文或环境噪声可能导致判断错误。
- 无安装器、自动更新、账号、云同步和团队功能。

## 绑定 AI 音频转写 API

### 当前低成本测试方案（2026-10-10 核对）

目前优先推荐注册[硅基流动](https://siliconflow.cn/)。其官方[价格页](https://www.siliconflow.cn/pricing)将 `FunAudioLLM/SenseVoiceSmall` 标为免费；官方[语音转文本接口文档](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post)明确支持 `POST /v1/audio/transcriptions`、`file` 音频上传和 Bearer API Key，和本程序的接口格式一致。

先用下面的免费模型验证完整链路：

```json
{
  "endpoint": "https://api.siliconflow.cn/v1/audio/transcriptions",
  "model": "FunAudioLLM/SenseVoiceSmall",
  "api_key": "填你自己的 Key"
}
```

价格页同时列出免费的 `TeleAI/TeleGSR-V1.0`（星辰生成式 ASR，带语义优化），但当前语音转文本接口文档列出的模型值主要是 `FunAudioLLM/SenseVoiceSmall` 和 `TeleAI/TeleSpeechASR`。因此先用 `FunAudioLLM/SenseVoiceSmall` 验证链路；只有注册后控制台明确允许 `TeleAI/TeleGSR-V1.0` 时，才切换到它。免费状态、限流和可用模型可能变化，以注册后控制台和官方价格页为准。

展开主界面后点击“设置 API”。填写：

- API 地址：支持 multipart 音频上传的转写地址，例如 `POST /v1/audio/transcriptions`；
- 模型名称：支持音频输入的模型 ID，例如 `gpt-4o-mini-transcribe`；
- API Key：只保存在本机设置文件，不要提交到 Git。

留空 API Key 时，程序只使用本地 Whisper。配置 API 后，录音文件会直接上传给音频模型，由模型完成语音转写、断句、标点和整理；程序把模型返回的最终文字输入当前 AI 对话框。API 失败时会回退到本地转写，不会丢失内容。

默认示例地址和模型只是示例值。用户需要自行确认服务商是否兼容该接口、模型是否可用、收费方式、数据保留和隐私政策。

## 构建

```powershell
.\build_release.ps1
# 另外下载和校验本地模型，制作离线完整包：
.\build_release.ps1 -Offline
# 官方模型站点不可访问时，可显式指定公共镜像：
.\build_release.ps1 -Offline -ModelEndpoint https://hf-mirror.com
```

模型固定版本、权重 SHA256 和 Git 对象校验在 `tools/prepare_model.py`，模型文件不纳入源码包。运行库在隔离构建环境安装，输出位于 `release`。模型目录已准备时，构建同时生成离线完整包和不含模型的较小包。

## 发布检查

1. 删除或确认没有提交 API Key、录音、转写历史和本机路径。
2. 在一台没有模型缓存的 Windows 机器上测试首次安装和下载提示。
3. 测试中文、英文、数字、代码、路径和超长口述，人工复核本地分段结果。
4. 保留 MIT、模型与第三方许可证，补充贡献方式、反馈和下载地址。

完整发布步骤和反馈信息模板见 `PUBLISH_CHECKLIST.md`。
