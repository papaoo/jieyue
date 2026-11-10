# 阶跃 UI 深度调研与审核参考

日期：2026-10-10。状态：研究稿，等待用户审核；不是已批准的 UI 实施规格。

## 1. 本轮结论与范围

中文产品名确定为「阶跃」。英文名称、英文缩写、商标图形仍待设计，不沿用观澜 / Guanlan / GL，也不默认采用 StepFun。

本轮只做案例、实现与许可调研，没有修改产品代码、安装依赖、购买素材或部署。现有页面仍显示上一版工作品牌；下一轮经审核后统一替换。

推荐方向：**液态材质的品牌主视觉 + 精密仪器式界面 + 真正帮助研究的联动交互**。不是把三个网站拼起来，而是提炼同一套「跃迁」形态、材质、运动节奏和选中状态。

此前石墨视觉方案不再作为审美定稿。当前代码可核实的问题：`workbench.css` 的路由入场只有 220ms、4px 的一次性淡入；登录光扫约 400ms 且只在入场播放；主页多处光晕和背景对比很弱。它们不足以形成用户要求的持续可感知动态与鲜明材质。此判断来自代码，非本轮浏览器逐帧测试。

## 2. 核验方法与边界

- 优先使用官网、作者案例说明、官方演示、源码和 LICENSE，不把设计搬运站的静态图片当成交互证据。
- 下文的效果描述来自官网与作者说明；「如何用于阶跃」是本项目的设计判断，不是原站已有的金融功能。
- 已核查候选实现的代表性源码与许可。浏览器预览通道多次超时，本轮未完成可靠的截图、逐帧观察、FPS、移动端或低端设备测试；不能据此声称所有站点已实机体验或性能已验证。
- 个别站点抓取时快时慢。提供可直接访问的官方 URL，但不保证用户所在网络的加载速度。Canvas/WebGL 页面通常需要启用 JavaScript。
- 新颖不等于发布日期越近越好：Atlas Motion 等当前案例与 2Advanced 2024 重制、Observable 较早的研究演示分别标注，不把所有参考都包装成 2026 新作。

## 3. 第一批最值得审核的案例

建议用电脑打开，分别看入场、鼠标移动、悬停、点击与滚动；不要只看截图。先看前五项，判断你喜欢的材质与动态强度。

| 编号 | 直达案例 | 重点观看 / 尝试 | 如何用于阶跃 | 源码与使用边界 |
| --- | --- | --- | --- | --- |
| A | [Paper · Liquid Metal](https://shaders.paper.design/liquid-metal)；[效果总览](https://shaders.paper.design/) | 切换形状、颜色与速度；看流动反射、轮廓和表面高光。另比较玻璃、纹理、Mesh Gradient | 原创阶跃符号做金属表面；登录和首页上方拥有明显但有限面积的动态；研究正文保持清晰 | 核心 [Shaders 源码](https://github.com/paper-design/shaders) 可用，Apache-2.0。编辑器服务与素材权限另计 |
| B | [Atlas Motion](https://atlasmotion.com/)；[Lusion 作者说明](https://lusion.co/projects/atlas_motion/) | 看首页到不同章节的视觉节奏，机械结构分层、精准运动与选择性 WebGL 的叙事方式 | 宏观驱动 → 资产 → 证据的逐层展开；精密金属、结构剖面和空间层级，而不是重复卡片 | 成品站是审美参考，未取得整站源码或模型复用许可；原模型不能直接搬用 |
| C | [2Advanced · V3 Reboot](https://v3.2advanced.com/v3expansionsreboot/)；[Kesey Signal](https://keseysignal.2advanced.com/) | V3 看动效驱动的导航和面板切换；Kesey 看分区终端、状态与入口反馈。可能加载较慢 | 研究驾驶舱的选中轨道、侧栏、证据抽屉和执行状态；用真实任务状态驱动动画 | V3 官网明确为 2024 版 Rive + React 重制。Kesey 为另一当前案例；[作者资料](https://www.ericjordan.com/portfolios/2advanced-v3-rive/) 涉及订阅源码，不能当作免费开源 |
| D | [Spline Viewer](https://viewer.spline.design/) | 依次尝试 Scroll、Orbit & Zoom、Follow、Look At；观察相机与鼠标输入如何改变场景 | 为原创阶跃体做悬浮、折射、缓慢转向；切换资产时只在主视觉区重组，不让整个行情区摇摆 | 可嵌入不等于场景与模型可免费商用；编辑、导出与素材许可须分别确认 |
| E | [Observable · Mosaic 多视图联动](https://observablehq.github.io/framework/lib/mosaic)；[利率区间演示](https://observablehq.observablehq.cloud/framework-example-mortgage-rates/) | 在时间直方图或地图中框选，比较其他视图同步变化；利率例子拖选时间窗 | 同一研究窗口联动 A 股 / BTC / 黄金 / 美股；选事件同步高亮区间、文档与假设，而不是只是把图画得漂亮 | 交互模式可借鉴；[Framework](https://github.com/observablehq/framework) 是 ISC。Mosaic 及其依赖要独立审查，不能用 Framework 的许可覆盖一切 |
| F | [Lusion](https://lusion.co/)；[ION 实验](https://exp-ion.lusion.co/) | 官网看品牌与三维体验组织；ION 是概念产品展示，重点是实时 WebGL 材质与光照表达 | 借鉴材质、镜头和信息出现的节奏；适合品牌入口、短展示或讲课封面 | 官网内容会更新，不把旧版宇航员/流体交互当作当前已确认特性；作品不是免费素材库 |
| G | [Dropbox Motion](https://brand.dropbox.com/motion) | 重点看即时反馈、具有重量感的运动与统一的动作语言 | 全产品共享一次「跃迁」节奏：按压、展开、选中、保存、返回都像同一种系统 | 参考原则；不复制品牌、图标和插画，也不把页面示例数值当作本产品实测结果 |

### 我的筛选判断

最值得先看 **A + C + E**：分别回答「材质够不够高级」「动态是否显著」「交互是否有研究价值」。B + D 帮我们把空间与三维质感提高；F 是实验上限；G 是统一细节的参考。

不建议完整照搬大型作品集的滚动故事：日常投研的主任务是快速比较、查证和记录。可以把强视觉放在首屏品牌模块与关键转场，工作区域用同一材质语言但减少持续运动；不能把高级感简化成全屏大字、海量霓虹或不停旋转的数据图。

## 4. 国内参考：创意表达与研究交互分开看

### H. 码语派：具体的 WebGL 实验，比泛泛的大屏模板更有参考价值

- [官方案例首页](https://www.mayupai.com/)。在 Latest Works 找「3D追光渐变色效果」「3D异次元传送门效果」「three.js炫酷单车产品交互动画」和「3D智慧城市辉光特效」。官网目录能确认这些作品，抓取有间歇性超时；本轮未取得稳定的各作品直达链接与逐帧体验。
- 借鉴追光、轮廓、粒子到实体的过渡方法，而不是给投研系统塞一个虚拟车展或要求用户在三维空间里行走。
- 未确认作品源码与模型的商用许可，因此列为视觉参考，不列为已批准引入的依赖。

### I. VisActor VStory：数据叙事，不只是图表入场

- [官方案例入口](https://www.visactor.io/vstory/example)、[源码](https://github.com/VisActor/VStory)、[MIT 许可](https://github.com/VisActor/VStory/blob/develop/LICENSE)。仓库许可主体为字节跳动及关联方；官方说明包含注释、动画、流程控制与叙事模板。
- 更适合做「一次降息如何影响不同资产」的解释页、研究报告播放、讲课演示。动画在讲解过程中逐步引入证据、变化与结论。
- 不默认整体替换当前图表层。先评估一页数据叙事或导出报告的增量价值；官方案例页在抓取中不稳定，具体示例效果待实机审核。

### J. AntV G6：宏观事件与资产证据的关系探索

- [官方示例](https://g6.antv.antgroup.com/examples)、[源码](https://github.com/antvis/G6)、[MIT 许可](https://github.com/antvis/G6/blob/v5/LICENSE)。官方确认支持布局、交互、动画、分组与不同渲染方式；示例站本轮抓取超时，仓库与文档信息可核验。
- 借鉴节点展开、分组聚合、选中高亮、定位与详情联动，构成「事件—指标—资产—来源」的证据地图。
- 边的语义必须明确：引用、关联、人工假设与已验证因果不是一回事。动画不能把相关性包装成确定因果。

### K. FIM 飞沐：中文品牌与信息编排的补充参考

- [官方案例集](https://www.fim.com.cn/works/)、[云锋基金项目说明](https://www.fim.com.cn/works/yf-capital/)。后者明确是 2022 年小程序项目，不是最新交互趋势证据。
- 用于审核中文排版、金融品牌内容层级与图文组织，不把作品截图误说成可直接运行的 Web 动效或开源实现。
- 其成品属于客户与工作室作品，不能直接复制图像、文案或品牌标识。

国内创意工作室与国外作品集适合找表现形式；VStory / G6 / Observable 更适合研究实际交互。它们不处于同一个评分维度，也不应只按首页截图好看与否排名。

## 5. 有源码的实现候选：不是越装越多越好

现有前端为 React 19、TypeScript、Tailwind 4，已有 Motion 与 Recharts。优先做小范围、可撤回的增量集成，不同时建立多套主题与动画状态。

下列许可是本轮对所链接仓库的核验结果，不代替实施时对固定版本、具体文件及素材的复核。中 / 高复杂度是源码与集成边界判断，不是已测 FPS。

| 候选 | 直达效果 / 官方源码与许可 | 用途 | 工程判断 |
| --- | --- | --- | --- |
| Paper Shaders | [Liquid Metal](https://shaders.paper.design/liquid-metal)、[Mesh Gradient](https://shaders.paper.design/mesh-gradient)、[Apache-2.0 LICENSE](https://github.com/paper-design/shaders/blob/main/LICENSE) | 品牌符号、高级材质和局部动态背景；第一优先 | 中。React 组件可适配当前栈；WebGL2 无法使用时给静态版本。保留许可与适用 NOTICE，不复制无授权 Logo |
| Magic UI 免费部分 | [主题揭幕](https://magicui.design/docs/components/animated-theme-toggler)、[Shimmer Button](https://magicui.design/docs/components/shimmer-button)、[MIT LICENSE](https://github.com/magicuidesign/magicui/blob/main/LICENSE.md) | 主题转场、按钮反馈、选中强调 | 低到中。按需摘取，不整站套模板；接现有主题状态和动效偏好。[Pro 许可](https://pro.magicui.design/license) 独立，不能混为 MIT |
| React Bits | [Silk](https://reactbits.dev/backgrounds/silk)、[源码与 LICENSE](https://github.com/DavidHDev/react-bits/blob/main/LICENSE.md) | 丝绸材质、磁吸与悬停实验 | 中到高。**MIT + Commons Clause，不是纯 MIT，也不按无附加限制的开源处理**；条款对出售/再许可/再分发组件产品有限制，终端产品使用边界须按实际商业模式复核 |
| React Three Fiber + Drei | [RenderTexture 演示](https://pmndrs.github.io/examples/drei-rendertexture/)、[Fiber MIT](https://github.com/pmndrs/react-three-fiber/blob/master/LICENSE)、[Drei MIT](https://github.com/pmndrs/drei/blob/master/LICENSE) | 原创三维阶跃体、模型材质、展示模式 | 高。Fiber 9 与 React 19 配套；按需渲染、懒加载、限制 DPR。模型、HDR、字体与贴图并不自动继承代码许可 |
| tsParticles | [Playground](https://particles.js.org/playground/)、[MIT LICENSE](https://github.com/tsparticles/tsparticles/blob/main/LICENSE) | 低密度粒子、局部轮廓形成或任务完成反馈 | 中。轻量包优先，离屏与后台停止；版本必须匹配。不是所有页面加粒子，不用无意义粒子冒充资金流 |
| Lightweight Charts | [金融图表示例](https://tradingview.github.io/lightweight-charts/tutorials/demos/whitespace)、[React 示例](https://tradingview.github.io/lightweight-charts/tutorials/react/simple)、[Apache-2.0 LICENSE](https://github.com/tradingview/lightweight-charts/blob/master/LICENSE) | K 线、十字光标、时间缩放与成交量；研究交互优先 | 中。Canvas 生命周期需正确卸载/缩放；[官方署名要求](https://tradingview.github.io/lightweight-charts/docs/5.0) 应保留 TradingView 署名及链接。库不附行情，也不提供策略盈利 |
| visx | [Gallery](https://visx.airbnb.tech/gallery)、[MIT LICENSE](https://github.com/airbnb/visx/blob/master/LICENSE)、[v4.0.0 发布](https://github.com/airbnb/visx/releases/tag/v4.0.0) | Brush、分布、相关性、置信区间与注释 | 中到高。v4 稳定版支持 React 18/19，不能沿用旧版兼容性结论；保留已有 Recharts，按新研究需求局部采用 |
| Aceternity UI | [Stateful Button](https://ui.aceternity.com/components/stateful-button)、[官方 registry](https://ui.aceternity.com/registry/stateful-button.json)、[Pro 许可](https://ui.aceternity.com/licence) | 加载—完成状态、标签与时间线的设计参考 | 免费组件源码可访问，但未确认一份覆盖所有免费组件的官方 MIT 许可。**逐项确认后才能复制**；Pro 条款也不能代表所有免费组件。当前只作为参考 |

### 代表性源码检查带来的差异

- Paper 的 [shader-mount.ts](https://github.com/paper-design/shaders/blob/main/packages/shaders/src/shader-mount.ts) 包含离屏 / 页面隐藏暂停、像素预算、销毁与速度为零停止 RAF 的处理。它是优先候选，但不代表集成后一定低耗电。
- React Bits 的 [Silk.tsx](https://github.com/DavidHDev/react-bits/blob/main/src/ts-tailwind/Backgrounds/Silk/Silk.tsx) 使用持续帧循环；把 `speed` 调成零不等于停止渲染。关闭动效要卸载或调整帧循环。[Magnet.tsx](https://github.com/DavidHDev/react-bits/blob/main/src/ts-tailwind/Animations/Magnet/Magnet.tsx) 的全局鼠标监听不适合每个按钮都复制一份。
- Magic 的 [主题切换组件](https://github.com/magicuidesign/magicui/blob/main/apps/www/registry/magicui/animated-theme-toggler.tsx) 需接入我们已有的主题与动效偏好，不能引入第二套控制状态。
- Aceternity 的 stateful-button 等待 `onClick` 后演示成功动画；我们的业务若返回失败对象而不是抛出异常，不能仍显示成功，必须使用明确的业务状态机。
- [visx xychart package](https://github.com/airbnb/visx/blob/master/packages/visx-xychart/package.json) 的 peer 范围包括 React 19。是否适合本项目，还要结合维护成本与真实数据规模，不能只凭兼容就重写所有图表。

## 6. 三条可审核的「阶跃」设计路线

以下是原创构思，尚未制作效果图或运行原型。

| 路线 | 视觉与交互设想 | 参考组合 | 取舍 |
| --- | --- | --- | --- |
| 1. 液态跃迁 | 黑曜石底色、银色液态阶跃体、窄幅偏色反射；鼠标靠近时表面响应，切换研究资产时形态跨越一个台阶后重组 | Paper + Atlas Motion + Spline | 品牌感最强。需要限制三维区域与材质预算，不能让阅读层全部半透明 |
| 2. 精密驾驶舱 | 有结构的分区、细刻度与清晰状态轨道；面板以展开 / 对齐 / 归位体现秩序，来源证据可从资产侧展开 | 2Advanced + Atlas Motion + G6 | 信息感与动态最鲜明。删去假的系统代码、无意义扫描和默认音效，避免游戏 HUD 的疲劳感 |
| 3. 数据剧场 | 以同一时间窗口控制多资产图形，宏观事件展开后逐层叙述事实、假设与风险；日常模式与演示模式分离 | Observable + VStory + visx | 对投研与讲课最有价值。依赖数据和证据结构，不能通过随意图形变换伪装新分析 |

**我的推荐是 1 做品牌表达、2 做工作区骨架、3 做研究操作方式。** 不需要三套独立 UI，而是一个主题：从观察到证据、再到判断的一次「跃迁」。若用户更喜欢纯驾驶舱或纯材质方向，可调整权重。

### 可具体感知的首页设想

- 首屏上方有真正可感知、限定面积的原创阶跃体：金属轮廓由两个错位平面构成，跨越后短暂聚合；不是普通圆球加名字。持续的低速材质变化与近距离鼠标响应让静止停留时仍能感到空间。
- 资产切换不只是换按钮颜色：活动轨道移动、摘要布局有连续关系、图表与证据沿同一选中状态更新。标为概念的视觉动画不代表行情实时更新。
- 分析输入是一个有边缘厚度、聚焦反馈和运行状态的控件；真实运行进入队列、取数、分析、完成 / 失败的状态，不播放虚构的进度百分比。
- 历史记录 → 报告详情保持位置与身份连续，可固定两份研究作比较；不是每次整页重新淡入。
- 顶部概览保留清晰数字与来源时间，正文用实色高对比底面。装饰高光和语义颜色分离，不能把上升 / 下降红绿用作随意装饰。

## 7. 动效应有三层，而不是只加一个动画包

1. **环境层**：首屏限定区域的动态材质、原创品牌形态、可见的空间光照。静置 5–10 秒仍可识别动态，不只在页面加载 200ms 内出现一次。
2. **操控层**：鼠标靠近、悬停、按压、聚焦、选中、展开、关闭均有统一响应；键盘操作同样完整。磁吸不能改变实际命中区域或拖慢高频按钮。
3. **研究层**：框选区间、多图十字光标、事件与报告定位、证据展开、固定比较、真实任务反馈。交互减少理解成本，是质感的一部分。

动效关闭、系统 reduced-motion、离屏与后台状态要成为真实执行路径，不只是把 CSS 速度设成零而 Canvas 继续工作。减少运动时保留材质、静态层次与完整研究能力，不退回无设计的页面。

## 8. 后续原型与验收顺序（待批准）

### 审核 1：选择参考方向

用户先从 A–K 中选择最喜欢的 2–3 个，或明确哪些材质 / 动态不喜欢。此时不需要先决定所有组件与颜色，也不需要买素材。

### 审核 2：两版可交互首页样稿

在现有项目框架内做可撤回的两版原型，使用明确标为演示的内容：

- 液态跃迁版：突出材质与原创符号，仍有可用的分析入口和历史记录。
- 精密驾驶舱版：突出结构、面板交互与状态连续性，品牌材质作为局部重点。

评审必须使用实际运行的原型和短视频 / 动态演示，而不是只交两张静态图。先验证视觉偏好与动作手感，再铺开研究页、配置页和移动布局。

### 审核 3：工程与真实数据联动

- 保留现有认证、分析、持仓、回测、接口与数据来源契约；不为换皮重新搭建整个项目。
- 优先 Paper + 现有 Motion，按需要加入金融图表或研究组件；避免 Paper、R3F、粒子同时铺满全站。
- 数据缺失、陈旧、来源失败必须直接表达；动画不能美化成实时行情或高可信结论。图表库不解决数据授权与数据接入。
- 测试首屏加载、点击到反馈、滚动与图表交互、CPU / GPU / 内存、后台耗电及离屏暂停。目标值在固定设备与真实数据规模下制定，本轮没有实测性能数字。
- 覆盖深浅主题、移动端无悬停、低 GPU / 无 WebGL、键盘、减少运动与高对比阅读。截图与动态片段都纳入视觉验收。
- 检查页面导航、登录、输入、分析任务成功 / 失败、报告详情、来源链接与历史记录，不让新增材质遮挡操作或截获焦点。

## 9. 素材与品牌注意事项

- 参考构图、材质、运动与操作逻辑；不直接复制他人的 Logo、三维模型、图片、声音、整站文案与付费源码。
- 代码许可、演示素材许可、托管服务价格、字体许可是不同层次。正式采用时记录固定版本、原始链接、许可及修改说明。
- 「阶跃」是用户选定的产品名；已有 [阶跃星辰 / StepFun](https://www.stepfun.com/) 品牌，因此后续英文名、标志和搜索识别应独立设计。本轮没有做商标可注册性查询，不能保证名称独占或注册成功。

## 10. 保留的探索入口

- [Awwwards · Interactive 网站](https://www.awwwards.com/websites/web-interactive/)：用于发现新案例，再回到原站 / 作者核查；不是免费素材授权证明。
- [Paper Shaders](https://shaders.paper.design/)：适合直接比较材质参数，优先于只有截图的灵感站。
- [VisActor](https://www.visactor.io/)、[AntV G6](https://g6.antv.antgroup.com/) 与 [visx Gallery](https://visx.airbnb.tech/gallery)：研究可视化与真实交互的后续候选。

本稿作为下一轮审核的依据。用户确认前，不执行新 UI 实施、不自动购买或安装候选，也不把研究结论当成完成了产品升级。
