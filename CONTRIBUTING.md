# Contributing

感谢参与 Voice to AI。当前项目仍处于 Windows 开发预览阶段。

## 本地检查

```powershell
python -m py_compile app.py
```

提交前请至少手动检查：快捷键开始/结束录音、取消录音、无语音输入、中文长句、英文识别、数字/路径原样保留，以及转写完成后是否输入到录音开始前的 AI 输入框。

## 提交问题

请提供 Windows 版本、Python 版本、模型版本、复现步骤和实际输出。请先删除 API Key、录音、转写历史、个人路径和其他隐私信息。

## Pull Request

保持修改范围清晰，并说明行为变化、验证方式和已知限制。不要提交 `%LOCALAPPDATA%\\VoiceToAI-MVP\\settings.json`、录音或历史记录。
