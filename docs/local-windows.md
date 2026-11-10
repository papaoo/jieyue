# Windows 本地使用

实际目录：`E:\work\机器学习\daily_stock_analysis`。这是本机网页，不部署服务器，也不开放公网；默认地址为 <http://127.0.0.1:18080>。

依赖安装和首次构建完成后，在 PowerShell 执行：

```powershell
cd 'E:\work\机器学习\daily_stock_analysis'
.\scripts\local-web.ps1 Start
.\scripts\local-web.ps1 Status
.\scripts\local-web.ps1 Stop
.\scripts\local-web.ps1 Build
```

如执行策略阻止脚本，可用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local-web.ps1 Start`；只影响该次调用，不修改全局策略。端口占用时使用 `Start -Port 18081`，浏览器也改用新端口。不要停止占用端口的其他项目。

## 首次打开

截图或手动采集展示时，可用 `.\scripts\local-web.ps1 Start -NoAutomation`。它只禁止本次进程启动飞书研究自动推送 worker，不修改已保存的订阅和自动化开关；手动分析仍应关闭通知。不要点击发送/触发推送按钮。恢复日常运行时先 Stop，再不带该参数 Start。

- 根目录 `.env` 设置 `ADMIN_AUTH_ENABLED=true`；第一次打开网页时，由你设置初始登录密码。不要把密码发到聊天里。
- AI 分析需要在本项目 `.env` 或网页设置中另外配置模型和 API key。安装成功不等于已经具备模型额度；手动分析可能产生费用。
- 初始保持 `SCHEDULE_ENABLED=false`。启动只提供网页，不立即分析，并禁用启动参数所管辖的通知；请不要先配置通知机器人或开启定时任务。网页手动分析等入口仍应检查其通知选项。
- 这是研究分析工具，不是获利保证，也不代替你核对行情与投资风险；本地搭建没有连接交易账户或下单。

## 隔离、日志和维护

收盘选股需要预先构建的行业引用包和历史行情库。若按 `docs/screening-backtest.example.json` 将两者准备在 `data/`，须在本机 `.env` 设置 `SCREENING_DATA_DIR=data`；目录内应同时存在 `historical_references/manifest.json` 和 `screening_history.sqlite`。网页默认的 `data/screening/` 缓存目录不等于历史数据集目录，不能用盘中快照代替缺失的收盘数据集。

独立 GitHub 仓库仅存源码，不带本机数据或密钥。每日分析 workflow 的定时运行需显式设置仓库变量 `ENABLE_DAILY_ANALYSIS=true`；未设置时只允许手动触发，避免与本地/服务器产生重复推送。

后端固定使用 `.venv\Scripts\python.exe`，前端构建使用 `npm.cmd run build` 和 `apps\dsa-web\node_modules` 内的依赖。Python 虚拟环境、前端依赖、`local/`、`.env`、`data/` 均在本项目内；不要提交 `.env`、密钥、密码或运行数据。

脚本指定根目录 `.env` 为 `ENV_FILE`，仅临时清理继承的模型、通知及项目配置变量，不修改用户/系统环境。缓存位于 `local/cache/`，日志位于 `local/logs/`，PID 状态位于 `local/web-process.json`，这些本地产物已被 Git 忽略。

`Status` 检查保存的进程身份，不等于网页已经就绪；启动失败时查看最新 `.err.log` 和 `.out.log`，不要公开含密钥的日志。请等待分析任务完成再执行 `Stop`：它会终止验证过启动时间、项目命令行和继承关系的自身进程树，不保证未完成任务能保存；PID 不匹配时拒绝操作。停止保留已有配置、日志和数据。

前端源码修改后执行 `Build` 再刷新网页（必要时 `Ctrl+F5`）；后端或 `.env` 修改后执行 `Stop`、`Start`。脚本不自动安装依赖、不在启动时自动构建，也不修改其他项目。

## 首次搭建验证记录（历史）

本节保留初始搭建时的状态，不代表当前配置或最新测试结果。真实数据展示流程见 [展示与截图说明](showcase.md)。

已验证 Python 包隔离、依赖一致性、网页资源加载、初始密码页、健康接口、未登录 API 返回 401，以及停止后端口释放和重新启动；17 项后端启动/鉴权相关测试通过。初始密码尚未设置，AI/搜索密钥未配置，未调用收费模型、未部署服务器。

前端构建成功；lint 无错误，有一条既有 React Hook 依赖警告。全量前端测试为 1,343 通过、2 失败、2 跳过；定向复跑仍有相同两项失败：已有组件的原生 `title` 不符合 UI 治理规则，以及六月固定日期的 fixture 超出了当前默认 90 天范围。此次未修改业务源码，也未把这两项记为通过。

Python 使用项目 `.venv`，不继承 Anaconda 的第三方包；Node/npm 复用本机运行时，但依赖只安装在本项目 `node_modules`，没有全局安装包。此次网络故障后临时使用[清华 PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)；GitHub wheel 复用已下载副本并验证了仓库指定的 SHA256，未修改原始 `requirements.txt`。Windows 环境不能直接复制到 Linux 服务器，部署时需重新创建服务器环境。

本机已安装依赖快照保存在 `local/requirements-installed-windows.txt`；浏览器验收复用了前端已有 Playwright 和本机 Edge，未另装浏览器。

这是按本次本地搭建需求提供的中文说明，未新增对应英文文档。
