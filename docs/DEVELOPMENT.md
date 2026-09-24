# 开发与发布

## 本地开发

需要 Windows x64、Git、Python 3.12 x64（含 Tcl/Tk），首次安装依赖需要联网。克隆仓库后，在项目根目录依次运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

`setup` 在 `%LOCALAPPDATA%/MeteorCounter/python-env` 安装固定依赖。同一用户的项目副本共用环境；依赖变化后重跑，不复制其他机器的环境。自定义解释器可传给 setup 的 `-PythonExecutable`。

`build` 生成 `publish/陨星计数器.exe`，打包前退出工具。手动运行源码用 `scripts/run.ps1`，会打开窗口并开始监测。协作规则见 [AGENTS.md](../AGENTS.md)。

## 资源与清理

`src/assets` 是运行必需资源，随源码及 EXE 保留，克隆后无需额外收集资源。

`build`、`artifacts`、`publish` 是本地生成物。`scripts/clean.ps1` 清理构建和字节码缓存。不要在构建期间清理，也不要删除 AppData 中的真实数据。

## 发布

- 普通推送、PR：由 [check.yml](../.github/workflows/check.yml) 构建，不上传 EXE。
- 版本标签 `vX.Y.Z`：由 [release.yml](../.github/workflows/release.yml) 构建并发布到当前仓库；附件名为 `MeteorCounter.exe`。

获得发布授权后，同步 `src/diagnostics.py` 的 VERSION 和 `src/windows_version.txt` 中的版本，编写 `docs/releases/vX.Y.Z.md`，仅说明本次调整和修复，不加入验证结果或通用更新说明。提交推送后创建并推送对应标签；手动触发也须选择版本标签。

标签必须匹配源码版本，发布说明不能为空。已发布版本不覆盖；源码修复须使用新标签。自动发布使用 GitHub 内置 Token，不向仓库写个人凭据。
