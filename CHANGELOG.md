# 更新日志 · 白泽评审 Baize Review

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的写法，版本号遵循语义化版本。

---

## [1.2.1] — 2026-09-23

### 修复

- **混合尺寸的原图与候选会直接 500（严重）。** `normalize_image_to_target` 的缩放规则
  只在「长边 > 2048 或短边 < 1024」时才动手，而原图与**每一张候选是各自独立对齐**的，
  互不参照。于是很常见的组合 —— 扫描件 1600×1200 配 AI 候选 2400×1800 ——
  会得到 1600×1200 与 2048×1536 两个不同尺寸，`score_candidate` 抛
  `RuntimeError: ... size does not match original`，接口返回 500。

  实测：4 个常见组合里 **2 个会崩**。

  现在以**原图对齐后的尺寸为准**，把不一致的候选补到同尺寸（同比例时是纯等比缩放，不变形）。
  另加一道宽高比闸门（容差 3%，与相柳网格的 `check_aspect_ratio` 一致）：
  比例差太多就不硬拉 —— 硬拉会把候选图拉变形、评出来的分没有意义，
  改为返回 400 + 中文提示「请先把两张图裁成同一比例」。被拒时顺手收掉那半个项目目录。

  实测：5 个组合全部符合预期（4 个正常对齐成同尺寸、1 个按预期明确拒绝）。

### 说明

尺寸对齐的**缩放规则本身未改**（「长边 >2048 或短边 <1024 才缩」那条逻辑一字未动），
只是在它之后补上「保证与原图同尺寸」这一步 —— 而这正是该函数文档字符串里**本来就声明**的契约
（"所有候选图与原图必须像素级同尺寸"）。算法与打分逻辑均未改动；
端到端自检仍是 **50/51**（那 1 条是源目录已归档导致的已知跳过）。

---

## [1.2.0] — 2026-09-23

### 变更

- **确立白泽的颜色身份：石绿 `#1b5e4a`**（SERIES-SPEC §4.1.1）。
  四个工具从此共用同一套骨架 token，各自只换 accent 五个值。此前四个工具都是同一支深朱红，分不出谁是谁。
- accent 的派生色收敛成变量：新增 `--accent-line`（描边）与 `--accent-press`（按下态），
  不再把 `#eccfcf` / `#6b1414` 硬编码在 CSS 里。

---

## [1.1.0] — 2026-09-23

**白泽评审正式加入「大云壁画工具箱」系列。** 这是首次系列化交付：仓库结构、命名、端口、
视觉语言、公共底座、启动行为、安全基线全部对齐 SERIES-SPEC v1.0。

### 新增

- **接入公共底座 `toolkit_core.py`（同步版本 1.0.0）。** 路径解析、CORS、分块传输、
  统一 JSON 与错误结构、启动横幅、健康检查全部改走底座，5 个工具从此改一次全体受益。
- **`GET /api/health` 统一响应。** 返回 `series / tool / nameEn / version / port / core /
  scoringVersion / autoMaskLoaded / runsMaxAgeDays / targetLongEdge / maxCandidates /
  maxUploadBytes`，启动台与 `run.ps1` 靠它探活。页面页头的版本号与端口也取自这里。
- **受限 CORS + `OPTIONS` 预检。** 只放行 `http://127.0.0.1:*`、`http://localhost:*`
  与 `Origin: null`；其它来源一律不发 `Access-Control-Allow-Origin`。
- **统一错误结构**（SERIES-SPEC §7）：`{"error":{"code","message","field","detail"}}`，
  `message` 一律是能直接显示给用户的中文。
- **参数上限校验**：候选图 ≤ 64 张/项目、mask 灵敏度 0.2–2.0、
  修正笔迹 ≤ 20000 条 / 400000 点、笔刷尺寸 2–4096 px，越界返回 400 + 中文提示。
- **上传大小上限 1 GB**，超限返回 413 与中文提示。
- **上传同名文件不覆盖**：重名自动追加 `_1`、`_2`……（`toolkit_core.unique_path`）。
- **`assets/logo.svg`**：圆形朱文印风格的白泽之目（知万物、辨优劣），
  朱色只走线，不做大面积深色填充。
- **`run.bat` / `run.ps1`** 统一启动脚本：定位 Python → 检查依赖 → 启动 →
  轮询 `/api/health`（30 次 × 1 秒）→ 自动开浏览器。

### 变更

- **整体换肤：视觉基准改为「相柳网格」（SERIES-SPEC §4）。** 原设计自带一套深色主题，
  与本系列「冷灰蓝底 + 白色面板 + 深朱红实心按钮」的语言不同源，本次统一：

  | | 改前 | 改后 |
  |---|---|---|
  | 页面底色 | 浅灰 + 可切深色 | `#f3f6f8` 冷灰蓝，**恒定为浅色** |
  | 卡片 | 白底 + 细边线 | 白色面板 + `1px` 边线 + `0 10px 28px` 柔和阴影 |
  | 主按钮 | 任意位置的实心按钮 | `#7f1d1d` 深朱红**实心**，直角，高 50px，字重 800 |
  | 次级按钮 | `--panel-2` 底（部分已到位） | 统一 `--panel-2` **实底** + 描边，高 46px（小号 38px） |
  | 选中标签 | 红底白字 | 保持红底白字，改用语义化 `.nav-tab` |
  | 浅色元素 | 部分透明压白底 | 一律给实底（`--panel-2` / `--field-2`）+ 描边 |
  | 圆角 | 4–999px 混用 | 0（直角），层级靠边线与阴影建立 |

- **深色主题（`body.dark`）移除。** 与系列「页面底色绝不能是纯白、也不做主题切换」的
  统一视觉基线冲突；同时删掉页头的「深色/浅色」切换按钮与 `localStorage` 记忆。
  这是本次唯一被移除的用户可见功能。
- **前端三分。** 旧版是 614 行的单文件 `web/index.html`（内联 CSS + JS），
  拆成 `web/index.html`（只放语义结构）+ `web/styles.css`（token / 布局 / 组件）+
  `web/app.js`（状态 / API 客户端 / 工作流）。
- **页头改为系列固定顺序**：Logo → 「大云壁画工具箱」→「白泽评审 Baize Review」→ 版本号；
  右侧是服务状态点 + 端口。版本号与端口在运行时从 `/api/health` 取，不再写死在页面里。
- **版本号从 `0.2.0` 提到 `1.1.0`。** 因为这是一次包含整体视觉改版的向后兼容交付，
  按 SERIES-SPEC §3 走次版本位；同时对齐样板仓库当前的版本号。
  评分算法自己的版本 `SCORING_VERSION` 仍是 `v2.1-lightweight-calibration`，**未动**。
- `web/` 静态资源、`runs/` 产物、`assets/` 品牌资源全部改由 `core.safe_join` 解析；
  二进制一律走 `core.stream_file` 分块发送。

### 修复

- **路径越界（S1）。** 旧版在 `do_GET`、`/api/preview`、`/assets/`、`export_winners`
  四处各写了一套包含判断，风格不一（有的先 `resolve()` 再 `relative_to`，有的直接拼绝对路径）。
  现在**全部收敛到 `core.safe_join(root, rel)` 单一入口**：URL 解码 → 反斜杠归一 →
  `resolve()` → 真路径包含校验，越界抛 `PathEscapeError`（403）。
  `GET /..\server.py`、`/../server.py`、`/..%5cserver.py`、`/%2e%2e%2fserver.py`、
  `/%252e%252e%252fserver.py` 五种写法均实测被拦（403/404），响应体不含任何源码片段。
- **`OPTIONS` 完全没有实现（S2）。** 旧版没有 `do_OPTIONS`，预检直接 501；现在返回 204 + CORS 头。
- **JSON 请求体无上限（S3）。** 旧版 `read_json` 直接按 `Content-Length` 全量读；
  现在走 `core.read_json`，默认 1 MB 上限，超限 413。
- **上传无大小上限（S4）。** 旧版 `/api/upload` 只按 `Content-Length` 落盘，没有任何上限，
  一个错误请求就能把磁盘写满；现在有 1 GB 上限与中文提示。
- **数值参数无上限（S5）。** 灵敏度 `float(...)` 直接进算法，`NaN`、`1e308` 都能过；
  现在统一夹取并返回 400。
- **错误一律 500。** 旧版任何异常都返回 `{"error": str(exc)}` + 500，
  用户看到的是 Python 栈信息；现在按语义分成 400 / 403 / 404 / 413 / 500，
  `detail` 里只保留技术信息供排查。
- **`create_project_from_uploads` 抛英文 `RuntimeError`。**
  `"Original upload is missing"` / `"Please upload at least one candidate"` 这类英文提示
  会直接出现在中文界面上；现在换成中文 `ValidationError`。
- **未生成 mask 就保存人工修正会 500。** `/api/save-override` 直接去开
  `masks/evidence_mask.png`，项目还没生成 mask 时抛 `FileNotFoundError`，
  用户看到的是"工具内部出错了"。现在返回 400 + 「这个项目还没有 mask，请先点「生成 mask」再保存人工修正。」
- **状态栏文案笔误。** `上传候选图 ${i}/{$("candidateInput").files.length}` 中第二个占位符
  少了一个 `$`，页面上会原样显示 `{$("candidateInput").files.length}`；已修正。

### 旧版实测对照（不是为了甩锅，是为了把差距说准）

改造前把旧版 `server.py` 复制到工作区沙箱里跑起来（端口 8797，只读源目录、不写
`D:\vibecodingtool`），逐条探了一遍，结论如下：

| 项 | 旧版实测 | 新版实测 |
|---|---|---|
| `GET /..\server.py` 等 5 种越界写法 | 403 / 404，**未泄漏源码** | 403 / 404，未泄漏源码 |
| `GET /api/preview?path=..\server.py` | 403，未泄漏源码 | 403（`resolve_tool_path`） |
| `OPTIONS /api/score` | **501 Not Implemented** | **204** + 预检头 |
| 外部 Origin 请求 | 完全没有任何 `Access-Control-Allow-*` 头 | 依然不发（同时本机来源可放行） |
| JSON 请求体申报 1.2 MB | **无响应（一直等 body）**，无上限 | **413** + 中文提示 |
| 上传申报 2 GB | **无响应（一直等 body）**，会一直往磁盘写 | **413** + 中文提示 |

也就是说：**S1 旧版其实没有越界漏洞**（它用的是真路径 `relative_to` 判断，这一点值得澄清），
真正的差距在 **S2 / S3 / S4 / S5** —— 旧版没有 `OPTIONS`、没有请求体上限、
没有上传上限、没有参数上限。本次改造把 S1 的"四处各写一套"收敛成单一入口属于**加固**，
不是修漏洞；顺手把 S2–S5 从"没有"补到"有并且实测通过"。

这也是为什么上面没写"修复了路径越界漏洞"——实测不支持这个说法。

补充一条（据代码阅读，未单独跑探针）：**S6 分块传输旧版本就已满足**——
旧版 `serve_file` 本来就是 `read(1024*1024)` 分块发送，并没有 `read_bytes()` 整读。
本次只是把实现统一换成 `core.stream_file`，不是修漏洞。

### 保留（算法一行未改）

本次改造**只动 HTTP 外壳、安全层、错误处理与前端**。以下打分与 mask 算法逐行保留，
数值、公式、权重、阈值、排序规则全部未做任何数学改动：

- `auto_mask.py` —— **整个文件逐字节复制**（md5 `ef2dea3b0535cd5fa908784d8a62c005`），
  包括 `percentile_stretch` / `otsu_threshold` / `sobel_magnitude` / `max_filter` /
  `min_filter` / `open_mask` / `close_mask` / `coarsen_mask` / `block_density` /
  `build_masks` / `make_preview` / `confidence_label`。
- `server.py` 算法层 —— `load_rgb_array` / `load_mask` / `save_mask` / `luminance` /
  `gradient` / `rgb_to_hsv` / `hue_distance` / `block_std` / `masked_mean` / `dilate_mask` /
  `robust_threshold` / `clamp_score` / `score_candidate` / `generate_masks` /
  `_rebuild_final_masks` / `apply_overrides` / `reset_overrides`。
- 两张榜的公式与排序、`SCORING_VERSION`、`TARGET_LONG_EDGE=2048`、
  `RUNS_MAX_AGE_DAYS=7`、报告章节结构、校准归档文件名格式，全部保持原样。

### 已知取舍

- **`/api/save-override` 的 JSON 上限放宽到 8 MB。** 笔迹是逐点 JSON，正常画一张 2048 长边的
  mask 就可能超过 1 MB；按底座默认 1 MB 会把正常使用挡在门外。其余 JSON 接口仍走 1 MB。
- **单文件上传上限取 1 GB（偏宽）。** 壁画扫描件常见 100–300 MB，宁可宽一点也不要
  误伤正常流程；这个值可以按实际素材下调。
- **`/api/pick-file` 返回的本机绝对路径继续被接受**（`create-project` 仍要求文件已在
  `uploads/` 内）。这是旧版既有行为：文件选择框给出的是仓库外的绝对路径，
  强行收窄会直接打断"选原图"这条路。
- **前端「选择候选」按钮的既有缺陷未改。** 它用 `fetch("file:///…")` 去读本机文件，
  浏览器会拦截 `file://` 请求，所以这个按钮实际上一直不生效（旧版同样如此）。
  本次为「保持业务逻辑等价」未改它，改为记录在案。日常请用拖拽或多选框。
- **深色主题被移除**（见「变更」），这是本次唯一的功能性删减。

### 验收

- 语法检查：`py -3 -m py_compile server.py toolkit_core.py`、`node --check web/app.js` 通过；
  `run.ps1` 另用 PowerShell 语法解析器验过，0 错误。
- 自检脚本（合成图跑完整链路）：**53 / 53 通过，0 失败**。覆盖健康检查、S1–S7 七条安全基线、
  参数越界、以及"上传 → 建项目 → 生成 mask → 人工修正 → 评分 → 导出"主流程。
- 算法未改：`auto_mask.py` 与只读源文件 md5 逐字节一致（`ef2dea3b0535cd5fa908784d8a62c005`）；
  9 个算法函数与旧版逐行相同；打分公式关键常量全部原样命中。
- 页面：1600×1000 / 1280×800 / 390×844 三视口无头截图复核，三档横向溢出均为 **0 px**；
  主按钮计算样式 50px / `rgb(127,29,29)` / 圆角 0，次级按钮 46px / `rgb(247,249,251)`，一眼可分；
  页面底色 `rgb(243,246,248)`，非纯白。
- 渲染级校验：`--dump-dom` 确认 JS 把版本号 `v1.1.0`、健康状态「正常」、端口 `:8796`
  正确写进了页面。
- 编码：`fix-ps1-encoding.ps1 -Path .\run.ps1,.\run.bat -Check` 全 OK
  （`run.ps1` UTF-8 带 BOM 且无弯引号，`run.bat` 纯 ASCII）。
