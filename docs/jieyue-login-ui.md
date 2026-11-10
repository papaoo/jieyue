# 阶跃登录页：股票动态视觉与复核说明

2026-10-10。本次只重构现有登录页，集中品牌为「阶跃 / JIEYUE / JY」。用户否定初版抽象液态 Logo 后，主舞台已改为原创股票场景；未重构首页、修改密码或部署服务器。

## 最新状态：恢复石墨配色，修复输入可读性

用户继续反馈后，已撤回全蓝主题，恢复石墨底、浅灰绿按钮、暖红 / 薄荷绿 K 线。当前构图暂时保留，新的金融装置路线正在等待用户选择；下文蓝色版记录仅为历史证据，不是当前认可的定稿。

已定位 `--login-input-fill` 误指向背景变量，经 `-webkit-text-fill-color` 覆盖字符实际着色。修复前深色密码字符对比度为 1:1，浅色 placeholder 约 4.23:1。现指向文字色，提示文字改 secondary、opacity=1；不改共享 Input 或认证接口。

新增真实 Chromium 回归 `e2e/fixtures/login-readability.spec.ts`：深浅主题 × 登录/首次设置共 4 测试先失败再通过；涵盖空提示、密码圆点、明文、确认密码与桌面/手机。没有输入真实密码或发送登录请求，均为 fixture；真实密码管理器自动填充尚未测试。生产构建通过，lint 0 errors / 1 既有 warning。

独立生产页面截图与实际颜色观察：[graphite-recovery](D:/CodexTools/REA/data/websites/2026-10-10-jieyue-login/graphite-recovery/verification.json)。现有服务 `http://127.0.0.1:18080/login` 已读取新构建。新的研究、证据及 A/B 提案见 [V2 设计提案](superpowers/specs/2026-10-10-jieyue-login-v2-design.md)。中文内部设计文档没有同名英文版；面向用户的双语文案未在本次修改。

## 直接体验

打开 <http://127.0.0.1:18080/login>。已登录的浏览器可能被现有认证逻辑跳转，可用未登录的独立窗口检查，不必清除现有会话。

右上角支持主题、语言、动效。开启时即连续运行，不需悬停；关闭时保留静态 K 线。系统开启“减少动态效果”时也会静止，遵循原偏好，不强行覆盖。

已撤回蓝色试验版的历史预览：[GIF](D:/CodexTools/REA/data/websites/2026-10-10-jieyue-login/stock-blue-preview/jieyue-blue-login.gif)。它是本机页面录屏，而非产品嵌入的视频素材。产品本身是清晰、可暂停的 SVG / CSS 动画。旧版录屏保留于 stock-preview 目录，供历史对照；当前恢复后的配色见 graphite-recovery 目录。

## 历史布局、材质与交互（蓝色试验版）

以下设计参数直接来自 `apps/dsa-web/src/pages/LoginPage.css` 和 `src/components/brand/StockMarketVisual.tsx`；当前视觉见下文 B1–B3，E3–E6 为蓝色深化前的历史记录。

- 桌面：约 1.42:0.9 非对称双列，容器最大 77rem；金融场景在左，双层访问面板在右。手机小于 768px 转单列，场景高 11.8rem，登录保持独立可读。
- 场景：32 根周期性 K 线重复成两个 800px tile；影线 / 实体 / 微立体边缘 / 成交量副层 / 三层走势。18s 连续横向流动，7s 扫描与聚焦，12s 透视镜头，2.5s 脉冲；追加 4.8s 成交量错峰呼吸、10s 沿走势光点和 6s 深度分布呼吸。不是行情预测。
- 颜色：深海蓝 `#091525`、面板 `#112238`、银白 `#eaf3ff`、冰蓝 `#a7cefa`、按钮 `#c2dcfb`，K 线冰蓝 / 钢蓝。浅色雾蓝 `#edf3fb`；插画蓝色仅用于演示，不改变真实图表涨跌色。
- 文案：品牌加一句“洞见市场，专注研究”；登录改“欢迎回来 / 进入工作台”。移除重复口号、研究维度、英文技术标记、访问编号；保留“市场演示 · 非实时”、投资风险提示和真实 HTTP / HTTPS 提示。中英文同步。
- 字体：Segoe UI Variable / Segoe UI / Microsoft YaHei UI / PingFang SC / 系统无衬线；技术标记用系统等宽。这是 CSS 声明，具体逐字符落到哪个本机字体未逐项测定。无新增远程字体。
- 表单：内外两层圆角和细光边，实色核心；密码标签、显隐按钮、聚焦、禁用、等待及失败状态齐全。新增显隐按钮键盘可达性仅限 `appearance="login"`，保留其他 Input 的原契约。
- 停止条件：动效关闭、系统 reduced-motion、文档隐藏或场景离屏。保留完整静态图；卸载清理 Observer 和监听。无 WebGL / Shader 库 / 外站播放依赖，但 CSS 合成仍可能使用 GPU，不能声称“无 GPU”。

## 采集配置与证据

### 蓝色深化试验版（已撤回）

2026-10-10 用户追加要求后重新采集。原始 [stock-blue-evidence.json](D:/CodexTools/REA/data/websites/2026-10-10-jieyue-login/stock-blue-evidence.json)，拆分文件与 hash 见 [manifest.json](D:/CodexTools/REA/data/websites/2026-10-10-jieyue-login/stock-blue-extracted/manifest.json)。两次 REA 的 completeness 均 complete，无 missing / truncated sections。

| 编号 | 直接证据 | 观察范围 |
| --- | --- | --- |
| B1 | REA `ev_340ce3957d2e`，1440×900 | 深海蓝桌面、简化表单和文案；DOM / AX / 截图 / 网络 / CSS 正文 |
| B2 | REA `ev_d3bb4d8c634c`，390×844 | 手机单列、短幅图形和完整表单；同上采集项 |
| B3 | [live-motion-evidence.json](D:/CodexTools/REA/data/websites/2026-10-10-jieyue-login/stock-blue-preview/live-motion-evidence.json) 与同目录录屏、PNG | 三层新增动画的 transform 均随时间变化；手机真实点击浅色与英文、关闭动效后动画数 0，重新开启后系统 reduce 仍令场景 off；无 pageerror |

两视口加载的 `LoginPage-BEKDh46K.css` 返回 200，响应正文 SHA256 为 `f3d5019f41f5c5205eb5b62b0c0a4c1a6583afd964684656bc0965b171b24c50`，未只记录 CSS URL。B3 实测桌面 scrollWidth/Height=1440/900，手机=390/844；浅色英文手机宽度也为 390，无横向溢出。这些是指定视口的观察，不承诺任意屏幕或字体缩放下一屏容纳。

本次相关 4 文件 / 40 测试通过，生产构建通过，lint 0 errors / 1 既有 StockScreeningPage.tsx:1383 warning；未修改无关页面。无新增依赖、认证接口修改、密码变更或部署。低端真机帧率、实际密码登录未测。下文 E 系列和全量 1436 测试是前一版记录，不冒充本次重跑结果。

### 历史基线与第一版股票场景

REA 使用当前安装的 `capture_browser_scenario` 参数，而不是假设存在的浏览器接口：独立 headless Chrome，1440×900 / 390×844、DPR=1、zh-CN、Asia/Shanghai，显式指定 color_scheme 和 reduced_motion。采集 screenshot、DOM、accessibility、URL、network、page-errors；响应正文开启，请求正文和 header values 关闭。未输入真实密码。

下面 ID 是完整 Evidence ID 的唯一前缀，完整值和原始响应在 bundle 内。根会话证据：[stock-final-verified-evidence.json](D:/CodexTools/REA/data/websites/2026-10-10-jieyue-login/stock-final-verified-evidence.json)；分离的 DOM / PNG / 网络 / CSS 与 URL、hash 对应在 [manifest.json](D:/CodexTools/REA/data/websites/2026-10-10-jieyue-login/stock-final-verified/manifest.json)。Paper 参考由独立采集会话保存，路径另列。

| 编号 | 场景与证据 ID 前缀 | 直接观察与边界 |
| --- | --- | --- |
| E1 | 旧登录桌面 `ev_27bd39efaba6` / 手机 `ev_9e8459f17e1e` | 旧观澜品牌、平面左区和普通访问卡片；两视口基线 |
| E2 | Paper 桌面 `ev_edadd6633764` / 手机 `ev_639a3ec1d3ab` | 金属材质、参数面板；手机参数面板被响应式隐藏；完整 DOM / AX 没有搜索 |
| E3 | 股票桌面及 on→off→on `ev_c8ef73cfe619` | 图形 / 表单完整，DOM data-ambient 依次 on / off / on；无 page-error |
| E4 | 最终股票手机深色 `ev_6810e3f2a503`，调整前 `ev_2e8042891e3f` | 390×844 下主视觉与完整登录表单可见；收住透视溢出，页脚自然向下滚动 |
| E5 | 手机浅色英文 `ev_df70f92a3ba5` | 真实点击主题菜单、浅色、语言后得到英文界面；无 page-error |
| E6 | 股票桌面系统 reduce `ev_5a9ddff6079a` | data-ambient=off、静态 K 线与表单保留；无 page-error |
| E7 | `stock-preview/live-motion-evidence.json`、真实录屏及 live-frame-1/2.png | 不冻结 CSS 的实际 transform 变化和两帧变化，证明连续动效；不是 FPS / 性能基准 |

E2 原件目录：[desktop](D:/CodexTools/REA/data/websites/20261010-paper-liquid-reference/desktop/evidence-bundle.json)、[mobile](D:/CodexTools/REA/data/websites/20261010-paper-liquid-reference/mobile/evidence-bundle.json)。来源：[Paper Liquid Metal](https://shaders.paper.design/liquid-metal)。借鉴材质研究方法，不复制 Logo、字体或整页。

E3–E6 completeness 均 complete，missing / truncated sections 为空；CSS 返回 200，正文已提取而不是只记 URL。E3 桌面采集时的两份 CSS（随后手机增加舞台裁切，因此构建 hash 会更新）：

- `index-kfC57uEr.css`：`ev_c8ef73cfe619-1440-request-8.css`，SHA256 `d2a6053a1ea6ada3094252be68f95b11d7cae6911d947825183fa6f4bad12ff7`。
- `LoginPage-1yMLgwEF.css`：`ev_c8ef73cfe619-1440-request-15.css`，SHA256 `cd4e0ca9810ca2588f370afa11d459d8c816232302cd00f97ce982de169c3eb3`。

部分 capture 的结果超过 MCP 10MiB 接收预算，工具报告操作已完成且证据保留；通过 export_evidence_bundle 读取完整记录，未把传输限制当成页面失败，也未截断后冒充完整数据。

### 为什么不能用 REA 两张截图证明这版动画

直接源码证据：`D:/CodexTools/REA/node_modules/rea-agents/dist/browser/PlaywrightScenarioArtifacts.js:33` 固定 `animations:"disabled"`。这会冻结 CSS 循环进行截图。初版股票两帧因此一致，并不证明浏览器没有动效。

补充独立 Chrome 实录采用普通播放及 `animations:"allow"` 截图；E7 保存 transform、两帧与录屏。先发现桌面总高 942px 后调整舞台高度与边距，最终桌面实际 scrollWidth=viewportWidth=1440、scrollHeight=viewportHeight=900。手机首次测出横向 401px，补舞台局部裁切后复测 scrollWidth=viewportWidth=390，文档总高 887px 自然滚动。最终 E4 再由 REA 相同视口采集确认，无 page-error。真机键盘和帧率未测。

## 输入 → 请求 → 数据处理 → 呈现

搜索：E2 没有搜索控件；登录页也不需要搜索。本轮不存在“搜索前后结果”可以采集，明确记为 N/A，不将地址跳转或参数输入冒充搜索。

现有登录链可区分证据来源：

1. **源码与测试直接证据**：密码 / 首次确认输入 → LoginPage 校验 / 提交禁用 → useAuth.login(password, confirm?)。
2. **源码直接证据，非实测 POST**：authApi 调用 `POST /api/v1/auth/login`，成功后刷新状态；本轮没有输入真实密码，也未用错误密码触发接口限流。
3. **浏览器直接证据**：初始化 `GET /api/v1/auth/status` 返回 200，随后登录 UI 出现（E3–E6）。请求顺序本身不是完整因果或服务器内部实现证据。
4. **源码与 fixture 测试直接证据**：成功采用受限站内 redirect，保留 query/hash；失败展示 SettingsAlert；意外拒绝恢复可提交状态。真实服务器成功密码 / Cookie 持久化未在本轮验证。

股票场景链是本地合成周期坐标 → SVG 图形 → CSS 动画。无市场数据请求、股票代码、价格、收益或交易建议；旧版标注“动态演示”及 NOT LIVE DATA（E3–E6），当前精简为“市场演示 · 非实时”（B1–B2）。

## 金融参考与素材边界

[TradingView 官方案例](https://en.tradingview.com/widget-docs/widgets/charts/advanced-chart/demos/)用于理解价格、指标、成交量的金融层次，没有嵌入它的完整终端或复制标识。当前原创 SVG 不是对第三方图表 API 的二次封装。

[Pexels 动态股票屏幕](https://www.pexels.com/video/dynamic-stock-market-display-with-trends-38905686/)页面标为免费、4K、27秒、50fps；[许可](https://www.pexels.com/license/)允许网站 / App 使用与修改，并限制背书与商标等用途。仅作为后续候选，没有下载、播放质量验收或嵌入。[Mixkit 3D 股票概念](https://mixkit.co/free-stock-video/stock-market-concept-3d-30873/)免费版本限个人用途，本轮不纳入产品。

当前产品新增运行依赖为零；未保留被拒绝的 Paper 材质实现及其本轮新增依赖。原创绘制不等于已完成产品名称商标检索，名称权利审查仍未知。

## 第一版验证记录、通用限制和复现

- 相关 10 文件 / 75 测试通过，覆盖登录 / 初始化 / 错误 / 重复提交 / redirect / 主题 / 动效生命周期 / 双语文案。测试中的接口为明确 fixture，不冒充服务器成功登录。
- 最终生产构建通过。lint 0 errors，保留一个既有 StockScreeningPage.tsx:1383 Hook 依赖 warning；未扩大修改范围。
- 与构建同时跑完整测试出现 6 个既有重页面失败；3 文件独立保持默认超时重新运行，201/201 通过。随后无并行构建、保持默认超时，以 maxWorkers=2 复跑：114 文件全部通过，1436 passed / 2 skipped（167.59s）。未改测试超时或修补其他页面来掩盖失败，也不把先前失败武断归因于单一原因。
- 未测：低端真机 FPS、真实移动键盘、物理触摸、断网环境、真实密码登录、正式服务器发布、商标检索；视觉好坏仍需用户审核。

现有本机脚本使用 `scripts/local-web.ps1 -Action Status/Build/Start/Stop`，没有 Restart 参数。前端测试在 `apps/dsa-web` 内运行 `npm test`；构建 `npm run build`；检查 `npm run lint`。环境仍是项目原有隔离环境，未修改全局设置。

若要撤销只撤销本轮登录 / 品牌相关补丁，或使用任务变更面板 Undo；不要对当前有大量既有改动的仓库执行 git reset / checkout 整体回滚。清理的仅是本轮创建且未采用的旧液态组件 / 测试 / 许可证副本，可从本任务历史恢复；未删除用户原有内容。
