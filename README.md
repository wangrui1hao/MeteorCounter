# 陨星计数器

《洛克王国：世界》的 Windows 工具，统计陨星、流星雨、咕噜球消耗与成本。本机识别，不上传截图或统计。

## 下载与使用

从 [最新 Release](https://github.com/wangrui1hao/MeteorCounter/releases/latest) 下载 `MeteorCounter.exe`，支持 Windows x64，无需安装 Python。首次打开可能等待几秒；更新时退出程序、替换 EXE，记录保留。

保持游戏识别区域可见。首次打开咕噜球背包建立库存，使用后再次开包更新消耗。

- **开始新一轮**：重置本轮统计；换游戏账号时使用，正常重启会续接上轮。
- **单价**：双击编辑，回车或点击别处保存，Esc 取消；输入 0 清除，未设置按 0 计算成本。
- **置顶、诊断截图**：按需开启，每次启动默认关闭。
- **清理缓存**：按确认框范围清理，保留当前轮次、球图标和单价。

数据保存在 `%LOCALAPPDATA%/MeteorCounter`，同一 Windows 用户共用球图标和单价。

[数据与隐私](docs/PRIVACY.md) · [开发与发布](docs/DEVELOPMENT.md) · [架构](docs/ARCHITECTURE.md) · [Agent 约定](AGENTS.md)
