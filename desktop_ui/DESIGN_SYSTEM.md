# WineFox Desktop — 设计系统 (Design System)

> 现代 Windows 桌面工具 × 日式精致线描 × 克制半透明材质。
> 保留 WineFox（酒狐）辨识度：**禁止**高饱和堆叠、巨大渐变标题、游戏启动器卡片墙、
> 远程动漫背景/字体/脚本、过度毛玻璃。

## 1. 设计原则

| 原则 | 落地 |
|---|---|
| 桌面工具感 | 紧凑行高、信息密度、明确的层级（顶栏/侧栏/内容区）、1px 低对比边框 |
| 日式线描 | 细线 SVG 纹样只出现在向导欢迎页、空态与品牌标记；太阳圆 + 波浪线 + 远山弧线三个母题 |
| 克制半透明 | `backdrop-filter: blur(18px)` 仅用于顶栏、侧栏、模态、Toast、进度卡；内容区用实色 surface |
| 无墙卡片 | 图片网格用细边框单元格，不用大卡片墙；装饰性渐变禁用，仅有单向 1px 高光 |
| 情感色克制 | 玫瑰粉主色 + 金色点缀，金仅用于装饰/边框/深色强调，不用作浅色文字 |

## 2. 色彩与对比度（WCAG AA 实测）

### Light
| Token | 值 | 用途 | 对比度 |
|---|---|---|---|
| `--canvas` | `#F6F3F1` | 画布 | — |
| `--surface` | `#FFFFFF` | 表面 | — |
| `--glass` | `rgba(255,255,255,.82)` | 半透明层 | — |
| `--primary` | `#A45C73` | 主色/按钮/链接 | **4.82:1** on white（AA） |
| `--primary-strong` | `#8A4A5F` | hover | — |
| `--accent` | `#D3A65A` | 点缀（仅装饰） | 2.24:1（禁止文本） |
| `--text` | `#241F22` | 正文 | **16.23:1** |
| `--muted` | `#756B71` | 次级 | **5.12:1** |
| `--success` | `#3D7A63` | 成功 | **5.05:1** |
| `--danger` | `#B84A50` | 危险 | **5.08:1** |
| 白 on primary | — | 主按钮文字 | **4.82:1** |

### Dark
| Token | 值 | 对比度 |
|---|---|---|
| `--canvas` | `#181416` | — |
| `--surface` | `#242025` | — |
| `--text` | `#F7F2F4` | **14.49:1** |
| `--muted` | `#B9AEB4` | **7.47:1** |
| `--primary` | `#C07A90`（提亮） | **4.91:1** |
| `--accent` | `#D3A65A` | **7.16:1** |

> 深色下主色必须提亮（#A45C73 在深底仅 3.33:1）；主按钮保持 #A45C73 + 白字（4.82:1）。

## 3. 字体 / 间距 / 圆角 / 动效

- 字体：`Segoe UI Variable, Segoe UI, Microsoft YaHei UI, Microsoft YaHei, system-ui`；
  等宽：`Cascadia Mono / Consolas`。**无任何外部字体**。
- 字号：11/12/13/14/16/20/28px；行高 1.35–1.55。
- 间距：4/8/12/16/24/32/48（`--sp-1..7`）。
- 圆角：8（控件）/12（卡片）/16（模态）；胶囊用于徽章与筛选 chip。
- 动效：160ms 反馈 / 200ms 标准 / 220ms 进入；`cubic-bezier(.2,0,0,1)`；
  `@media (prefers-reduced-motion: reduce)` 全部归零 + CSS 兜底（animation/transition 0.01ms）。
- 缩放：rem 基准 14px，布局使用弹性 + 断点（1100/980/720/620px），覆盖 125%–200% DPI 与 1280×720、1920×1080。

## 4. 无障碍

- 完整 `:focus-visible` 焦点环（2px `--ring`），Tab 顺序 = 文档顺序；
- 跳过链接（skip-link）、`aria-current="page"` 导航、`role="tablist/tab"` 模式切换、
  模态 `role="dialog" aria-modal` + Esc 关闭 + 初始焦点、Toast `role="status/alert"` + aria-live；
- 网格单元格可聚焦（tabindex=0）+ Enter/Space 预览；
- 快捷键：`Ctrl+Enter` 生成、`T` 切换主题（输入框内不拦截）。

## 5. 信息架构

```
WineFox Desktop
├─ 向导（首启门禁，8 步）
│   欢迎 → ComfyUI 地址 → 认证/TLS → 连接检测 → 模型与节点 → 模型族/工作流 → 旧数据导入 → 完成
├─ 生成  左：模型族/工作流/LoRA/队列 ｜ 中：画布（拖放） ｜ 右：提示词/负面/种子/尺寸/步数/CFG/采样器
├─ 历史  cursor 分页 + 搜索 + 状态筛选 + 预览 + 收藏 + 回收站（恢复/彻底删除）
├─ 收藏  分类 chip 筛选 + 备注/分类编辑 + 搜索 + 新建
├─ 模型与环境    checkpoint/节点/环境配置；ready/missing/incompatible
├─ LoRA          已安装/发现/匹配测试；本地档案、搜索下载与自动匹配统一入口
├─ 设置  ComfyUI 连接 / TLS / 认证与密钥 / 默认参数 / 界面
└─ 诊断  版本、ComfyUI、节点缺失、配置健康、日志摘要、脱敏导出
```

## 6. 安全约定

- **不渲染绝对路径**：所有名称经 `sanitizeName()`（取路径最后一段）；诊断页 URL 只显示 host。
- **密钥仅显示配置状态**：`GET /api/v1/secrets` 只返回 `{key, configured, updated_at}`；
  值仅通过 `PUT /api/v1/secrets/{key}` 写入；**token/密钥不写入 localStorage**。
- 无外部资源：无 CDN、无远程字体/图片/脚本；mock 图片为内联 SVG data URI。
- Mock 仅在 `?mock=1` / `?dev=1` 时动态 import `desktop_ui/dev/mock.js`，生产默认不启用。

## 7. API 契约（Agent 07 后端实现）

Base：`/api/v1`，JSON。错误统一 `{ "error": string }` + 非 2xx 状态码；离线时 fetch 拒绝。

| 方法 | 路径 | 用途 | 关键响应 |
|---|---|---|---|
| GET | `/health` | 健康/首启 | `{status, app:{name,version}, plugin:{version}, comfyui:{connected,version}, first_run:bool}` |
| GET | `/settings` | 读设置（无密钥） | `{wizard:{completed}, comfyui:{server_address,ssl_verify,connect_timeout,read_timeout}, tls:{enabled,verify,ca_configured}, auth:{mode}, defaults:{family,workflow,steps,cfg,sampler,width,height}, ui:{reduced_motion}}` |
| PUT | `/settings` | 部分合并保存 | 同上 |
| GET | `/secrets` | 密钥配置状态 | `{items:[{key,configured,updated_at?}]}` |
| PUT | `/secrets/{key}` | 写入密钥 `{value}` | `{ok,configured}` |
| DELETE | `/secrets/{key}` | 清除密钥 | `{ok}` |
| GET | `/comfyui` | 连接详情 | `{connected,version,devices,nodes_total,custom_nodes:[{name,installed,version}],missing:[]}` |
| POST | `/probe` | 连接检测（向导第 4 步） | `{result:'ok'|'warn'|'fail', summary, steps:[{id,label,status:'pending'|'running'|'ok'|'warn'|'fail',detail}]}` |
| GET | `/catalog` | 生成页资源 | `{families:[{id,label,description}], checkpoints:[{id,name,family,size,compat}], loras:[{id,label,family,tags,strength,compat}], workflows:[{id,label,mode:'txt2img'|'img2img'|'i2v'|'any',family}], nodes:[{name,installed,version}]}` |
| GET | `/models` | 模型页 | `{checkpoints:[], loras:[], nodes:[], missing_nodes:[], download:{enabled:false, reason:'v1 不支持在线下载'}}` |
| POST | `/uploads` | 上传参考图 (multipart `file`) | `{url, name, width, height}` |
| POST | `/jobs` | 提交任务 | `{id, status:'queued', queue_position, seed(解析后)}` (202) |
| GET | `/jobs/{id}` | 任务状态 | `{id,status:'queued'|'running'|'completed'|'failed'|'cancelled',progress:0..1,image:{url,width,height}?,seed,error?,queue_position?}` |
| POST | `/jobs/{id}/cancel` | 取消 | `{ok}` |
| GET | `/history?cursor&limit&q&status&trash=1` | cursor 分页 | `{items:[{id,prompt,negative_prompt,seed,width,height,steps,cfg,family,workflow,status,favorite,trashed,image:{url},created_at}], total, next_cursor, prev_cursor}` |
| POST | `/history/{id}/favorite` · `/unfavorite` · `/trash` · `/restore` · `/purge` | 操作 | `{ok}` |
| GET | `/categories` | 收藏分类计数 | `{items:[{id,label,count}]}` |
| GET | `/favorites?q&category` | 收藏列表 | `{items:[{id,note,category,prompt,image:{url},created_at}], total}` |
| POST | `/favorites` | 新建 `{note,category,prompt}` | 201 收藏对象 |
| PUT/DELETE | `/favorites/{id}` | 改/删 | 收藏对象 / `{ok}` |
| GET | `/diagnostics` | 诊断页 | `{app, plugin, comfyui:{connected,version,url}, nodes:{total,missing:[]}, config_health:[{id,label,status:'ok'|'warn'|'fail',detail}], log_tail:{lines:[], truncated}}` |
| POST | `/diagnostics/export` | 脱敏日志导出 | `{format:'json', content:'...'}`（内容不得含绝对路径与密钥） |

**轮询规则（前端实现）**：`running` → 1s；`queued` → 2s；请求失败 → 指数退避 1,2,4,8s → 封顶 10s。

**字段约定**：`image.url` 可为 `/media/...`、`blob:` 或 `data:`；`compat ∈ ready|missing|incompatible`；
所有名称类字段由后端给 basename（前端另有兜底清洗）。

## 8. 目录结构

```
desktop_ui/
├─ index.html          应用外壳（向导 + 顶栏 + 侧栏 + 视图容器）
├─ css/  tokens.css（令牌）· base.css（重置/焦点）· components.css（组件）· pages.css（页面布局）
├─ js/   app.js（引导/路由/门禁）· api.js（客户端/轮询退避）· store.js（内存态）
│       utils.js（工具/图标/toast）· views/{wizard,generate,history,favorites,models,settings,diagnostics}.js
├─ dev/  mock.js（仅 ?mock=1|dev=1 加载；场景 success/queued/fail/offline/history）
└─ DESIGN_SYSTEM.md
```

## 9. 开发 Mock 场景

`index.html?mock=1&scenario=success|queued|fail|offline|history`
- success：任务 1–3s 完成，含 26 条历史与收藏种子数据
- queued：任务先排队 5s（2s 轮询可见）再运行再完成
- fail：连接降级、节点缺失、任务失败（错误可见于历史）
- offline：所有请求拒绝（验证诊断页离线可用、无崩溃）
- history：等价 success（历史页数据种子）
