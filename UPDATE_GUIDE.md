# 素材万象更新与数据迁移

## 日常使用

在“设置 → 数据与迁移”选择全部库、素材库或 Skill 附属库，导出 `.mu-library.zip`。另一台电脑选择“导入迁移包”，查看新增、重复、冲突与缺失副本数量后合并。导出检查引用附件真实存在；缺失时会停止并指出问题，不生成虚假的完整备份。

首次升级无需先卸载。退出旧软件（托盘 → 退出软件），运行新版安装程序并沿用原目录。1.10.0 起可在设置检查更新，下载、校验后安装；有生成或上传任务时先等任务结束。源代码和便携版也能下载更新，但需要运行安装程序建立安装版后才能自动覆盖。

个人库默认在 `%LOCALAPPDATA%\MaterialUniverse\任务与素材\Skill附属库`，素材索引和文件仍位于原“任务与素材”目录。可以用 `MATERIAL_UNIVERSE_LIBRARY_ROOT` 指向另一个独立个人库；切换之前先导出或复制原库。不要把它指向软件安装目录。

桌面导演读取“内置库＋个人库”，同 ID 优先个人版本。更新只替换内置规则与通用示例。旧版 `director-skill` 资源不被新规则覆盖，首次启动复制到个人库并保留原目录；已有独立 Skill 可用“导入已有 Skill 库”复制。原 Skill 后续改动不会自动双向同步，可再次导入；Agent 可直接维护统一个人库的记录并重建索引。并发维护需使用库锁。

个人学习文件保存在个人库 `learning/<scope>/state.json`；有冲突的导入状态另存到 `learning/imported`，不会覆盖现有开关或修订历史。桌面导演使用案例库，不把这些学习文件自动提升为已验证导演规则。

## Agent 更新入口

安装版无需 Python。使用应用的维护入口（先彻底退出 GUI），通过 `--report` 读取结果；安装版为无控制台程序，调用方应等待进程结束后再读报告。

```powershell
$app = Join-Path $env:LOCALAPPDATA 'Programs\MaterialUniverse\素材万象.exe'
# 下列报告路径请选用任务临时目录。
& $app --maintenance --report 'C:\Temp\mu-check.json' check-update
& $app --maintenance --report 'C:\Temp\mu-download.json' download-update
& $app --maintenance --report 'C:\Temp\mu-install.json' install-update
```

源码运行时使用 `python skills/video-product-swap/scripts/maintenance.py`，参数相同。源码入口不能自动覆盖安装自身。

如果 Agent 所在的 Windows 应用对 AppData 做了目录映射，数据库与日志可能落到不同物理目录。软件会在打开数据库前后检查并停止这种操作。不要删除日志、用映射副本覆盖原库，或更改 `--data-root` 去迁就旧副本。可以从资源管理器直接启动软件，在设置中操作；Agent 也可借助已打开的资源管理器窗口启动维护入口：

```powershell
$windows = (New-Object -ComObject Shell.Application).Windows()
$explorer = @($windows) | Where-Object { $_.FullName -like '*\explorer.exe' } | Select-Object -First 1
if (-not $explorer) { throw '请先打开一个 Windows 资源管理器窗口，再执行维护操作。' }
# 应使用实际安装路径；报告路径应位于 Agent 工作区，且每次使用新文件名。
$arguments = '--maintenance --report "D:\任务目录\mu-result-001.json" export-libraries "D:\任务目录\素材万象.mu-library.zip"'
$explorer.Document.Application.ShellExecute($app, $arguments, (Split-Path $app), 'open', 0)
# 此调用异步返回。等待本次报告出现后，读取 success 和 result/error。
```

维护入口只取得数据库使用锁，不恢复或重新提交历史生成任务。其他命令：

```text
--report RESULT export-libraries DESTINATION --scope all|assets|skills
--report RESULT preview-import ARCHIVE
--report RESULT import-libraries ARCHIVE
--report RESULT import-skill-directory DIRECTORY [--include-learning]
--report RESULT migrate-legacy
```

库变更命令要求 GUI 已退出，不能绕过其进程锁。预览不会写入素材或案例；导入前备份到 `任务与素材/backups/import-*`，异常退出的导入会在下一次启动恢复。恢复和导入不会提交收费任务。库迁移不搬运使用记录、画布、账号授权或外部成片；这些属于完整工作区迁移的后续范围。

更新备份位于 `backups/update-*`，包含 SQLite 快照、个人案例及校验计数。实际素材与成片保持原位置，不在每次更新时重复复制所有视频。数据库格式变化应先新增迁移与恢复测试；不要只换回旧 EXE 去读取不兼容的新数据库。保留备份，必要时在关闭应用后按备份版本恢复数据库及个人库。

## 开发与发布

源码同步与正式发布分开。普通推送执行检查并生成供验收的构建产物；推送 `v1.10.0` 这样的版本标签，检查通过后由 GitHub Actions 自动生成安装程序、`update.json` 和校验文件，发布到 Releases。手动运行工作流可以只构建测试产物，不发布正式版本。无需本地同时生成安装版和便携版。

版本在 `scripts/app_version.py` 与 `packaging/version_info.txt` 保持一致，更新 `RELEASE_NOTES.md`。只有全部离线检查和安装版自检通过才可发布。工作流使用 Windows、Python 3.12、Node 22、FFmpeg 和 Inno Setup。新电脑首次安装可需要 Windows WebView2 Runtime。

公开仓库只收录程序、规则和白名单通用示例。`director_resources.py` 再次过滤打包内容；用户素材、个人案例、学习记录、账号文件、历史发行物不进入源码和发布包。库迁移包不是 GitHub 同步内容。

网络失败时保留旧程序和已下载数据。更新来源固定为项目正式 GitHub Releases；校验值用于检测文件损坏，当前发布包没有 Windows 代码签名，不宣称已经完成签名验证。
