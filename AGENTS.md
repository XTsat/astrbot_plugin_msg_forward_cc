# AGENTS.md — astrbot_plugin_msg_forward_cc（跨平台消息转发）

> 本文件是任何 AI 编码代理（agent）进入本项目时的**必读**工作说明书。
> 它约定插件的架构、代码规范、文档维护规则。任何对代码、配置、行为的改动，都必须同时遵守本文档的规则。
>
> **来源**：本文件基于 [XTsat/agent-templates](https://github.com/XTsat/agent-templates) 的 `templates/astrbot-plugin/AGENTS.md`，最新版见该仓库 `main` 分支；已回填本项目的插件名、命令前缀、目录结构与特有约定。
> 用户说「更新 AGENTS」时按此流程：① 拉取来源模板最新版 → ② 与本地逐节比对 → ③ 合并（保留本项目已回填的内容）→ ④ 展示 diff 与改动清单，等用户确认后再落盘。**禁止整文件覆盖**。

---

## 1. 项目概览

基于 **AstrBot** 框架的跨平台消息转发插件（AGPL-3.0），用于在不同聊天平台（QQ、微信、Telegram、Discord 等）之间同步消息、桥接群聊。由 [Siaospeed/astrbot_plugin_msg_transfer](https://github.com/Siaospeed/astrbot_plugin_msg_transfer) 修改而来，作者 XTsat。

核心能力：通过「转发规则」把 A 会话的消息自动同步到 B 会话，支持来源信息标注、规则启停与备注、多源多目标规则、消息过滤、转发冷却、发送队列、内容类型筛选、@ 转发与昵称反查、媒体本地化转发、媒体下载代理三态。

最小依赖：AstrBot 4.x 运行时 + 纯 Python 标准库 + AstrBot API。

---

## 2. 目录结构与架构

### 2.1 文件清单

| 文件 | 必选 | 职责 |
|------|------|------|
| `main.py` | ✅ | 插件主体，全部逻辑（3448 行，单文件风格） |
| `_conf_schema.json` | ✅ | WebUI 配置 Schema（238 行） |
| `metadata.yaml` | ✅ | 插件元数据（v0.5.6，支持 19 个平台） |
| `README.md` | ✅ | 中文文档 |
| `CHANGELOG.md` | ✅ | 变更日志 |
| `LICENSE` | ✅ | AGPL-3.0 |
| `logo.png` | ✅ | 插件图标 |
| `AGENTS.md` | ✅ | 本文件（agent 工作说明书） |

> 无 `requirements.txt`：全部依赖已由 AstrBot 运行时自带或按需惰性 import。

### 2.2 架构风格：单文件 + 分区注释

本项目采用**单文件风格**，所有逻辑集中在 `main.py`，用 `# ---- 分区名 ----` 分隔条组织：

```
工具与数据路径（常量、MIME 映射）
→ 内容类型筛选（组件 → 类型键、按类型过滤）
→ 合并转发（聊天记录）展开（拉取内容重建 / 嵌套递归展开 / 时间保留 / 目标平台降级）
→ 媒体工具函数（下载 / 重建 / 本地化 / 序列化）
→ 消息链清洗（@ 清洗、File 清洗、At 转发策略）
→ 存储层 MsgForwardStore（无锁简化）
→ 插件主体 MsgForward（__init__ / 命令 / 转发主逻辑）
```

依赖方向：工具函数不依赖类实例（全模块级函数），类方法只调用模块级工具函数与 `self.config`。

### 2.3 合并转发（聊天记录）展开（模块级）

- `_FORWARD_PLACEHOLDER` / `_NODES_TARGET_PLATFORMS` / `_FORWARD_MAX_DEPTH`：无法展开或目标平台不支持合并转发时的占位文本 `[聊天记录]` / 可直接发送 `Nodes` 的目标平台适配器类型（`aiocqhttp`、`satori`） / 嵌套聊天记录最多再展开的层数（协议端上限 3 层，取 2）
- `_FORWARD_PREVIEW_MAX_LINES` / `_FORWARD_PREVIEW_MAX_CHARS`：合并转发卡片预览（`news` 扩展参数）最多展示的行数（QQ 卡片模板上限 4 行）/ 单行正文最大长度（超出截断并加省略号）
- `_ForwardNodes(Nodes)`：合并转发组件子类，覆写 `to_dict()` 生成目标协议端可直接上传的 payload——AstrBot 内置 `Nodes.to_dict` 不携带节点时间、也不支持节点内嵌套聊天记录，而 aiocqhttp 适配器对 `Nodes` 正是直接调用 `to_dict()`；同时携带 `news` 预览行，避免协议端把嵌套聊天记录显示成 `[消息]`、JSON 卡片显示成 `[JSON消息]`（`news` 为 NapCat / SnowLuma 等 OneBot 实现的扩展参数，不支持的实现会忽略）；`show_card_title` 字段由配置 `forward_card_preview` 决定，控制预览行是否显示卡片真实标题
- `_sole_nested_forward(content)` / `_nodes_without_nested(nodes, show_card_title=True)`：节点内容是否「只有一条合并转发」（协议端只支持纯嵌套 node 数组）/ 把未展开的嵌套记录降级为文本占位
- `_collapse_preview_text(value)` / `_strip_card_prefix(text)` / `_json_card_title(comp)`：预览文本折叠为单行 / 去掉卡片 `prompt` 的来源前缀（`[QQ小程序]`、`【分享】`）/ 从 JSON 卡片取标题（`meta.news.title` → `prompt` 正文 → 其它 `meta.<类型>.title`，**只读标题**，简介 / 来源 / 上传者 / 跳转链接等字段一律不读）
- `_forward_preview_news(nodes, show_card_title=True)` / `_component_preview_text(comp, show_card_title=True)`：按 QQ 展示习惯生成合并转发卡片预览行（昵称 + 内容摘要，嵌套记录为 `[聊天记录]`、卡片为真实标题或 `[卡片消息]`，前 4 行）/ 单个组件的预览文本
- `_component_to_onebot_segment(comp)` / `_node_to_onebot_dict(node, show_card_title=True)`：节点内容组件 → OneBot 消息段（媒体转 base64、混排嵌套降级文本）/ 单个节点 → node 段（带 `time`，嵌套记录输出为嵌套 node 数组，并为其附带内层卡片 `news` 预览）
- `_forward_node_time(raw)`：归一化节点发送时间（秒级时间戳，越界或非法返回 0）
- `_expand_nested_forwards(event, nodes, keep_time, depth, show_card_title=True)`：递归展开节点内嵌套的聊天记录，层数超限或拉取失败降级为文本占位
- `_onebot_bot_of(event)` / `_unwrap_onebot_data(ret)` / `_forward_messages_of(payload)`：取事件所属 OneBot 协议端、归一化 action 返回值、提取节点消息列表
- `_media_url_in_segment(data)`：从 OneBot 媒体段取可下载 http(s) URL（url 优先，file 为 URL 时兜底）
- `_forward_segment_to_components(seg)`：把节点里的一个 OneBot 消息段转换为 AstrBot 组件（At 转文本、带 resId 的嵌套转发保留为 `Forward` 交给递归展开、无 resId 的卡片转文本占位、表情与 JSON 卡片保留）
- `_build_forward_nodes(messages, keep_time=True, show_card_title=True)`：把 `get_forward_msg` 返回的节点消息重建为 `_ForwardNodes`；兼容 `{"type": "node", "data": {...}}` 包裹格式，空内容节点跳过；`keep_time` 决定是否把原始发送时间写入 `Node.time`
- `_fetch_forward_nodes(event, forward_id, keep_time=True, depth=_FORWARD_MAX_DEPTH, show_card_title=True)`：拉取合并转发内容并重建 `_ForwardNodes`（先按 `id` 再按 `message_id` 调用，兼容不同协议端），随后递归展开嵌套记录；失败返回 None 由调用方降级
- `_expand_forward_components(event, chain, keep_time=True, show_card_title=True)`：把链中的 `Forward`（仅带源会话 resId）展开为重建的 `_ForwardNodes`、`Node` 包成 `_ForwardNodes`；拉取失败降级为 `[聊天记录]` 文本，绝不透传无效 forward 段
- `_map_chain_components(chain, mapper)`：对链中组件逐个应用异步变换，`Node` / `Nodes` 内容递归处理（节点内媒体同样需要本地化 / 注册 URL），并保留节点时间、`_ForwardNodes` 子类与其 `show_card_title` 配置（自定义序列化不能被降级成内置 `Nodes`）
- `_chain_has_media(chain)`：链中是否含媒体组件（含节点内媒体），决定发送失败时是否值得本地化重试

### 2.4 媒体处理工具函数（模块级）

- `_comp_type_name(comp)` / `_comp_content_type(comp)`：组件 → 内容类型键 / Content-Type
- `_content_types_for(rule, config)`：解析规则内容类型筛选（规则级覆盖 → 全局默认 → 全选）
- `_filter_chain_by_types(chain, allowed)`：按选中类型过滤组件
- `_should_attach_header(rule, allowed)`：判断是否前置来源头（`hide_header` 或未选中「文字」类型时不前置）
- `_extract_remote_url(comp)`：返回组件引用的远程 http(s) URL；本地文件/base64/data URI 返回 None。**注意 File 组件的 `.file` 是 property（异步上下文访问会触发同步下载），对 File 只检查 `.url` 与 `.file_`**
- `_guess_media_ext(comp, url, content_type)`：按 Content-Type → URL 后缀 → 组件类型确定临时文件后缀
- `_download_url_to_local(comp, url, use_proxy=False, proxy_url=None)`：远程媒体下载到本地临时目录；内部先走正常网络（AF_UNSPEC）、失败再改用强制 IPv4（AF_INET），规避核心 download_file 的 `Cannot connect ... [None]`（aiohttp#9447）问题；代理三态——use_proxy 关→直连，开且 proxy_url 空→走系统代理（trust_env=True），开且非空→走该地址（trust_env=False）；两次都失败抛异常由调用方降级
- `_rebuild_from_local_path(comp, local_path, keep_local=False)`：按本地路径重建组件（File 无 `fromFileSystem`，改用 `File(name=..., file=...)`）
- `_rebuild_media_component(comp, use_proxy=False, proxy_url=None)`：媒体组件重下载重建（先本地路径、失败再远程 URL），解决跨会话转发源端临时路径不可达（ENOENT）问题；失败降级为 `Plain` 占位文本。Video 会清空 cover
- `_prepare_chain_for_forward(chain, ...)`：转发前对 Image/Record/Video/File 逐组件本地化（走 AstrBot 核心 download_file）
- `_prepare_chain_fallback(chain, ...)`：发送失败后的兜底链，仅本地化远程 URL 媒体
- `_prepare_chain_for_queue(chain, ...)`：入队前媒体本地化到插件自有数据目录（重启/重载不丢）
- `_get_media_cache_dir()`：队列媒体缓存目录（`astrbot/data/plugin_data/msg_forward_cc/media/`）
- `_get_file_service_base_url()` / `_file_service_available()` / `_register_file_with_service(local_path, timeout=3600)`：把本地媒体注册到 AstrBot 内置文件服务（读全局 `callback_api_base`），返回 `{base}/api/file/{token}` 可下载 URL；未配置或失败返回 None
- `_local_media_path_of(comp)`：提取组件本地化文件路径（含 file:// URI 解码、媒体缓存目录相对路径查找）
- `_prepare_chain_for_media_urls(chain)`：发送前把媒体实时注册文件服务生成 URL（队列场景 NapCat 跨容器读不到本地路径、源端短效 URL 过期，故每次发送重新注册 token）
- `_is_send_timeout(exc)` / `_send_with_timeout_retry(send, ...)`：协议端调用超时检测与重试——超时（NapCat sendMsg `retcode=1208`/QQ 内核 `1200` 等）属瞬时故障，短延迟 3s 后原链重发至多 2 次，仍失败才进入媒体本地化降级；直发与队列发送两条路径共用
- `_serialize_chain(chain)` / `_deserialize_chain(data)`：队列持久化时的消息链序列化/反序列化
- `_sanitize_chain_for_forward(chain)`：事件级 @ 清洗（空目标丢弃/降级文本，纯数字与 all 保留透传）
- `_sanitize_file_chain_for_forward(chain)`：清洗 File 组件本地路径，仅保留 URL 走下载
- `_sanitize_at_chain_for_target(chain, target, passthrough_platforms)`：At 可透传平台判断（QQ 系按 qq 原样透传）
- `_textify_at_chain(chain)`：默认策略——At 一律转为文本 `@昵称`
- `_parse_platform_set(raw)` / `_at_passthrough_platforms(config)`：平台集合解析（内置 QQ 系白名单 + 配置 `at_passthrough_extra_platforms` 追加）
- `load_json(path)` / `save_json(path, data)`：健壮文件读写，分类记录错误（FileNotFoundError/JSONDecodeError/OSError/TypeError）；原子写入（先写 `.tmp` 再 replace，`ensure_ascii=False, indent=2`）
- `gen_code(n=6)`：`secrets` 生成绑定码（小写字母+数字）

### 2.5 存储层 `MsgForwardStore`

- 维护 `pending.json`，提供 `load_pending / save_pending / add_pending / pop_pending`（pop 不存在的 code 抛 KeyError）
- 无锁简化（见源码注释），数据量小、单进程访问

### 2.6 插件主体 `MsgForward(star.Star)` 生命周期

- `__init__(self, context, config)`：初始化 data_dir（`StarTools.get_data_dir("msg_forward_cc")`）、`pending_file` / `queue_file`、媒体缓存目录、内存冷却表 `_cooldowns`（key = `source_umo|target_umo` → 结束时间戳）、冷却失效告警去重集合 `_cooldown_warned`、发送队列 `_send_queue`（asyncio.Queue）与按规则积压计数 `_queue_rule_counts`、worker/清理任务句柄、暂停标志 `_queue_paused`；随后执行 `_migrate_legacy_umo_lists()`（旧版 list 格式 UMO 字段 → 每行一条 text，修复 WebUI 校验失败，仅在有 list 值时迁移并保存）
- `initialize()`：恢复持久化队列（`_restore_persisted_queue`）→ 启动队列 worker → 启动时清理媒体缓存 → 启动每小时定期清理任务
- `terminate()`：取消 worker 与清理任务

### 2.7 核心数据流

```
事件进入 forward_message（@filter.event_message_type(ALL)）
→ 匹配 source_umo 命中的所有规则（多源：每行一条 UMO）
→ 清洗链（@ 清洗 → File 清洗 → 无 URL File 调 API 取下载 URL）
→ 合并转发展开（Forward → 拉取内容重建 Nodes，递归展开嵌套记录并保留原始时间；失败/跨平台降级为 [聊天记录]）
→ 逐规则：启停检查 → 过滤 → 内容类型筛选 → 冷却/队列分支
→ 逐目标：At 策略（默认文本化 / 反查精确 @）→ 合并转发目标平台降级 → 前置来源头 → 发送（或入队）
→ 失败三级兜底重试（超时重试 → 媒体本地化 → 远程 URL）→ 冷却写入 / 队列 worker 按间隔发送
```

---

## 3. 技术栈与依赖

- **Python 3.10+**（AstrBot 4.x 最低要求），现代联合类型写法（`dict | None`、`list[dict]`）
- **AstrBot API**：`astrbot.api.star`（star, Context, Star, StarTools）、`astrbot.api.event`（filter, AstrMessageEvent）、`astrbot.api`（logger, AstrBotConfig）、`astrbot.core.message.components`（At, Plain, Image, Record, Video, File, Face, Forward, Json, Node, Nodes）、`astrbot.core.message.message_event_result`（MessageEventResult）
- **依赖管理**：不引入新依赖，纯标准库（json/re/secrets/time/pathlib/string/tempfile/ssl/socket/urllib）+ AstrBot API；`aiohttp` / `certifi` 在 `_download_url_to_local` 内惰性 import（AstrBot 运行时已自带，仅用于兜底媒体下载，import 失败时降级为占位文本，不影响其余功能）；`astrbot.core.astrbot_config` / `astrbot.core.file_token_service` 在文件服务函数内惰性 import
- **禁止引入**：不引入与原功能无关的依赖，非必要不新增 `requirements.txt`

---

## 4. 核心代码规范

### 4.1 类型与语法

- Python 3.10+ 现代联合类型写法（`str | None`、`list[dict]`、`tuple[str, str]`）
- 函数签名完整类型注解（参数 + 返回类型）；用 `yield` 返回结果的命令/事件处理方法标注为 `AsyncGenerator[MessageEventResult, None]`
- 现有历史代码部分命令方法未标注返回类型（现状差距）；**新增/修改代码严格执行完整注解，不主动大规模重构旧代码**
- 禁止 `Any` 作为类型注解（调用方签名强制要求时除外），禁止 `# type: ignore`

### 4.2 命名约定

| 元素 | 约定 |
|------|------|
| 模块级常量 | `UPPER_SNAKE`（`_MIME_EXT_MAP`、`_DEFAULT_MEDIA_EXT`、`_QQ_TARGET_PLATFORMS`） |
| 模块级工具函数 | `snake_case`，下划线前缀区分内部实现（`_download_url_to_local`） |
| 私有方法 | `_` 前缀 + `snake_case`（`_should_forward`） |
| 命令方法 | `cmd_` 前缀（`cmd_bindraw`、`cmd_queue_status`） |
| 类名 | `PascalCase`（`MsgForward`、`MsgForwardStore`） |
| 文件名 | `snake_case`（`main.py`、`_conf_schema.json`） |

### 4.3 注释与日志

- 注释只写 non-obvious reason；docstring 用中文说明用途/参数/返回（私有方法用途明显可省略）；禁止残留被注释掉的旧代码与调试输出
- 代码分区用 `# ---- 分区名 ----` 分隔条
- 日志统一 `logger.info/error/warning`，错误信息带 ❌/⚠️ emoji 前缀；分类记录（ValueError = 非法参数/非法 session，OSError = 文件/IO 错误）；单规则失败不影响其他规则；禁止空 `except: pass`（除非注释说明理由）
- 规范要求新增日志带 `[astrbot_plugin_msg_forward_cc]` 前缀（对齐远端模板）；历史日志未带前缀，不强制回改

### 4.4 错误处理与防御性编程

- 面对不规范输入必须优雅 fallback：配置值防御式校验（`max(1, int(...))`、`(TypeError, ValueError)` → 默认值/降级）
- 外部 IO 必须 try/except 并记录日志，不允许静默吞异常
- 降级链路清晰：首选方案 → 兜底方案 → 占位文本（媒体下载失败 → `Plain` 占位）

### 4.5 并发

- 异步方法使用 `async/await`，禁止阻塞事件循环
- 后台任务用 `asyncio.create_task`（队列 worker、定期清理），`terminate()` 中取消
- 队列用 `asyncio.Queue`；冷却表/积压计数为纯内存 dict，无跨线程访问

### 4.6 回复消息与权限

- 回复消息统一 `yield event.plain_result(...)`
- 用户可读输出用中文 + emoji 图标风格
- 管理类命令加 `@filter.permission_type(filter.PermissionType.ADMIN)`；本项目除 `help` 外**全部命令均为 ADMIN 权限**（v0.4.7 起）

### 4.7 配置文件操作

- 配置文件改动后必须调用 `self.config.save_config()`
- 数据文件写操作走 `save_json`（原子写入），不在业务代码里直接 `open().write()`

---

## 5. 配置与数据文件

### 5.1 配置 Schema（_conf_schema.json）

所有配置项中文名取自 `hint` 首句或 `description`，引用格式：中文名（`key`）。配置持久化在 AstrBot 插件配置 `self.config`，`save_config()` 保存。

**全局配置：**

| 配置项 | key | 类型/默认 | 说明 |
|------|------|------|------|
| 默认隐藏来源信息头 | `default_hide_header` | bool, false | 新建规则默认隐藏来源头 |
| 来源信息头模板 | `header_template` | text | 变量 `{sender_name}{sender_id}{platform}{msg_type}{conversation_id}`，留空用默认格式 |
| 消息过滤模式 | `filter_mode` | string, off | off / blacklist / whitelist |
| 全局过滤规则列表（每行一条） | `filter_patterns` | text | `regex:` 前缀=正则，其余=关键词（不区分大小写） |
| 默认转发冷却时间（秒） | `default_cooldown_seconds` | int, 0 | 规则未显式设置时继承 |
| 默认发送队列间隔（秒） | `default_queue_interval_seconds` | int, 0 | 规则未显式设置时继承 |
| 启用发送队列 | `queue_enabled` | bool, false | 总开关，关闭时规则间隔不生效 |
| 队列媒体缓存保留时间（小时） | `queue_media_retention_hours` | int, 24 | 0=默认 24；重启时自动清理全部缓存 |
| 发送队列总长度上限 | `queue_max_size` | int, 0 | **全部规则合计**的队列积压上限，0=不限制；与规则级独立上限同时生效（先判总上限、再判规则级） |
| 发送媒体前先下载到本地 | `download_media_before_send` | bool, false | 跨设备转发提示找不到文件时才开启 |
| 转发聊天记录时保留原始发送时间 | `forward_keep_time` | bool, true | 关闭则聊天记录里每条消息显示为转发时的时间 |
| 聊天记录卡片预览 | `forward_card_preview` | string, title | title=仅标题（默认，卡片预览行显示卡片标题）；off=不显示（统一 `[卡片消息]`）；只读标题，不显示简介 / 来源 / 上传者等其它信息 |
| 默认转发的内容类型 | `default_content_types` | list(checkbox) | 键：plain/image/face/record/video/file/at/other，默认全选 |
| 按昵称反查 @ 对象（默认关闭） | `at_nickname_lookup` | bool, false | 拉取目标群成员列表反查真实成员后精确 @，QQ 系有效 |
| @ 昵称反查的群成员缓存时长（秒） | `at_nickname_lookup_cache_ttl` | int, 300 | 0=每次重新拉取 |
| 额外允许 @ 透传的目标平台 | `at_passthrough_extra_platforms` | list | 默认已含 QQ 系（aiocqhttp/qq_official 等），自建 QQ 适配器在此追加 |
| 平台名称映射 | `platform_names` | list | 每项 `原始平台名=显示名`（如 `aiocqhttp=QQ`），兼容旧版 `platform_name_map` object 格式自动迁移 |

**规则模板 `rule`（`rules` 为 template_list，模板键 `__template_key: "rule"`）：**

| 配置项 | key | 类型/默认 | 说明 |
|------|------|------|------|
| 启用此规则 | `enabled` | bool, true | 停用后规则保留但不再转发（`/mf toggle`） |
| 名称（备注） | `remark` | string, "" | 规则展示名称，留空回退为 `source_umo → target_umo` |
| 源 UMO 列表 | `source_umo` | text（每行一条） | 多源：任一命中即触发转发 |
| 目标 UMO 列表 | `target_umo` | text（每行一条） | 多目标：转发到每个会话 |
| 隐藏来源信息头 | `hide_header` | bool, false | |
| 过滤模式 | `filter_mode` | string, inherit | inherit / off / blacklist / whitelist |
| 过滤规则列表 | `filter_patterns` | list | 非空则覆盖全局，留空继承全局 |
| 转发冷却时间（秒） | `cooldown_seconds` | int, 0 | 0=关闭本规则冷却；不填（删除字段）=继承全局默认 |
| 发送队列间隔（秒） | `queue_interval_seconds` | int, 0 | > 0 且 `queue_enabled` 时进入队列模式；不填=继承全局默认 |
| 发送队列长度（条） | `queue_max_size` | int, 0 | 本规则独立积压上限（一条多目标转发会计入多次）；0 或不填=不限制（仅受全局总上限约束） |
| 发送媒体前先下载到本地 | `download_media_before_send` | string, inherit | inherit / true / false 三态，强制覆盖全局 |
| 转发的内容类型 | `content_types` | list(checkbox) | 非空覆盖全局默认；全部不勾选=继承全局 |
| 按昵称反查 @ 对象 | `at_nickname_lookup` | string, inherit | inherit / true / false 三态 |
| 媒体下载是否走代理 | `use_proxy` | bool, false | |
| 媒体下载代理地址 | `proxy_url` | string, "" | 仅 `use_proxy` 开启时生效 |

### 5.2 数据文件（data_dir = `StarTools.get_data_dir("msg_forward_cc")`）

| 文件/目录 | 用途 | 说明 |
|------|------|------|
| `pending.json` | 绑定码暂存 | 原子写入；绑定码一次性（pop 即删） |
| `queue.json` | 发送队列持久化 | 入队消息同步落盘，重启/重载后 `_restore_persisted_queue` 恢复；条目记录 `rule_key`（稳定规则标识）与序列化链 |
| `media/` | 队列媒体缓存 | 重启/重载不丢；按 `queue_media_retention_hours` 定期清理（每小时），重启时全清 |

### 5.3 配置读取模式

- 统一 `self.config.get(key, default)` 读取，防御式转换（`int(...)` 包 `(TypeError, ValueError)`）
- 规则级字段解析统一走专有方法：`_cooldown_for(rule)`（未设置=继承全局、显式 0=关闭）、`_queue_interval_for(rule)`、`_rule_queue_max_size(rule)`、`_should_download_media(rule)`、`_should_at_lookup(rule)`、`_at_lookup_ttl()`

---

## 6. 指令注册与命令清单

### 6.1 指令注册模式

顶层命令组用 `@filter.command_group("mf")`，嵌套子组用 `@mf.group("queue")`，命令用 `@mf.command("xxx")` 或 `@queue.command("xxx")`：

```python
@filter.command_group("mf")
def mf(self):
    pass

@mf.group("queue")
def queue(self):
    pass

@filter.permission_type(filter.PermissionType.ADMIN)
@queue.command("status")
async def cmd_queue_status(self, event: AstrMessageEvent):
    yield event.plain_result("...")
```

事件监听用 `@filter.event_message_type(filter.EventMessageType.ALL)`。

### 6.2 命令表（全部命令除 help 外均为 ADMIN 权限）

| 命令 | 权限 | 功能 |
|------|------|------|
| `mf help` | 全部 | 按分组树状显示帮助（绑定/规则/冷却/过滤/内容类型/@ 转发/发送队列） |
| `mf add` | 管理员 | 生成 6 位绑定码存入 pending.json，目标会话用 `bind` 接受 |
| `mf bind <code>` | 管理员 | 弹出绑定码创建规则（当前会话为目标），默认 `hide_header` 取自 `default_hide_header` |
| `mf bindraw [源平台] 源ID [目标平台] 目标ID` | 管理员 | 直接建规则（见 6.3） |
| `mf del <编号>` | 管理员 | 删除规则（1-based 索引，与 `/mf list` 显示一致） |
| `mf list` | 管理员 | 列出当前会话（source_umo 匹配）的规则；标记：🟢/⛔ 启停、🔒/🔓 隐藏、❄冷却、⏳队列间隔、📮队列上限、🔔@反查、📦内容类型 |
| `mf listall` | 管理员 | 列出所有规则（同上标记） |
| `mf hide <编号>` | 管理员 | 切换单条规则 hide_header |
| `mf toggle <编号>` | 管理员 | 启用/停用一条规则（enabled 字段） |
| `mf remark <编号> [备注]` | 管理员 | 设置规则备注（留空清除，恢复默认显示） |
| `mf cooldown` | 管理员 | 查看冷却配置 |
| `mf cooldown default <秒>` | 管理员 | 设置全局默认冷却（0=关闭） |
| `mf cooldown <编号> <秒\|inherit>` | 管理员 | 设置规则级冷却（inherit=重置继承全局） |
| `mf filter` | 管理员 | 查看全局+规则级过滤配置、冷却配置、队列配置 |
| `mf content` | 管理员 | 查看内容类型筛选配置 |
| `mf content list` | 管理员 | 查看可选内容类型与别名 |
| `mf content default <类型...>` | 管理员 | 设置全局默认（all=全选） |
| `mf content <编号> <类型...>` | 管理员 | 设置某规则内容类型（多选） |
| `mf content <编号> inherit` | 管理员 | 重置为继承全局默认 |
| `mf at` | 管理员 | 查看 @ 转发与昵称反查配置 |
| `mf at on` / `mf at off` | 管理员 | 全局开启/关闭 @昵称反查 |
| `mf at cache <秒>` | 管理员 | 设置群成员列表缓存时长（0=每次重新拉取） |
| `mf at <编号> on\|off\|inherit` | 管理员 | 规则级昵称反查开关 |
| `mf at test <群号\|UMO>` | 管理员 | 实测目标群 @昵称反查命中情况 |
| `mf queue status` | 管理员 | 查看发送队列状态：总开关/消费状态(暂停/运行)/默认间隔/总上限/媒体保留/当前积压 + 各规则队列（间隔/长度上限/当前积压）表格 |
| `mf queue on` / `off` | 管理员 | 发送队列总开关 |
| `mf queue interval <秒>` | 管理员 | 设置全局默认队列间隔（0=关闭） |
| `mf queue maxsize` | 管理员 | 查看总上限与各规则长度上限 |
| `mf queue maxsize <条数>` | 管理员 | 设置全局总上限（0=不限制） |
| `mf queue maxsize default <条数>` | 管理员 | 同上（与规则编号区分） |
| `mf queue maxsize <编号> <条数>` | 管理员 | 设置某规则队列长度上限（0=不限制） |
| `mf queue maxsize <编号> inherit` | 管理员 | 重置为继承全局上限 |
| `mf queue retention <小时>` | 管理员 | 队列媒体缓存保留时长（0=默认 24） |
| `mf queue set <编号> <秒>` | 管理员 | 设置某规则队列间隔（0=关闭该规则队列） |
| `mf queue set <编号> inherit` | 管理员 | 重置为继承全局默认 |
| `mf queue clear` | 管理员 | 清空积压队列与媒体缓存 |
| `mf queue pause` / `resume` | 管理员 | 暂停/恢复队列消费 |

**`hide` 与 `hidelist` 说明**：`hidelist` / `hidelistall` 命令已在 v0.4.6 移除（来源信息状态由 `/mf list` / `/mf listall` 的 🔒/🔓 标记覆盖），**不要再实现或引用它们**。

### 6.3 bindraw 解析逻辑（`build_umo`）

- 平台简写表：`df=default`、`qq=aiocqhttp`、`wx=weixin_oc`、`tg=telegram`、`dc=discord`
- plat 小写化，以 `s` 结尾 → FriendMessage 并去掉 `s`；`default` 或空 → `default` 平台；`len(plat_key) > 3` 时直接用原字符串作为平台标识（如 `aiocqhttp`、`weixin_oc`）
- ID 末尾 `s` 且平台无 `s` 后缀时也转私聊（如 `/mf bindraw 654321 123456s`）
- 参数省略形式：2 参（源ID 目标ID，平台均 default）、3 参（源ID 目标平台 目标ID 或 源平台 源ID 目标ID）、4 参（完整）

### 6.4 UMO（Unified Message Origin）

格式 `平台名:消息类型:会话ID`，消息类型为 `GroupMessage`（群聊）/ `FriendMessage`（私聊）。规则中 `source_umo` / `target_umo` 为**每行一条的多值文本**（兼容旧 list 格式，见 `_umo_list` / `_migrate_legacy_umo_lists`）。

`_rule_key(rule)`：由规则自身 source_umo/target_umo 内容派生的稳定标识（`src=>dst`），用于队列积压按规则归集统计——规则增删/排序/重启恢复后已入队消息仍能正确归属。

### 6.5 主转发逻辑 `forward_message`（事件监听）

事件级流程（匹配 source 命中的所有规则前）：
1. `event.get_messages()` 取原始链
2. `_sanitize_chain_for_forward`：清洗无效 @（空目标丢弃/降级文本；纯数字与 all 保留）
3. `_sanitize_file_chain_for_forward`：File 组件仅保留 URL
4. `_resolve_file_urls`：无 URL 的 File 尝试从原始 OneBot 消息获取下载 URL（`get_group_file_url`）
5. `_expand_forward_components`：合并转发（`Forward` 只带源会话 resId，直接透传会让目标端显示打不开的卡片）→ 用 `get_forward_msg` 拉取内容重建 `_ForwardNodes`，递归展开节点内嵌套记录、按配置 `forward_keep_time` 保留每条消息的原始发送时间、按配置 `forward_card_preview` 决定卡片预览行显示标题还是 `[卡片消息]`；拉取失败降级为 `[聊天记录]` 文本

逐规则流程：
1. 跳过 `enabled=false` 规则；`_should_forward(event, rule)` 过滤检查（规则级优先，inherit 继承全局）
2. 内容类型筛选 `_content_types_for` + `_filter_chain_by_types`；过滤后为空则跳过（不转发、不占冷却）
3. 队列分支：`queue_interval > 0` 且 `queue_enabled` 时消息不直接发送而是入队 `_enqueue_send`（**先于冷却检查**），由后台 worker 按间隔依次发送；入队前两级上限检查——先全局 `queue_max_size`（总上限），再规则级 `queue_max_size`，任一达到即丢弃并记录 error 日志
4. 冷却检查 `_cooldown_for(rule)`：规则显式值优先（0=关闭本规则冷却），未设置时继承 `default_cooldown_seconds`；冷却期间跳过（冷却表纯内存，key = `source_umo|target_umo`）
5. 主链：默认透传 `sanitized_chain`（媒体交给目标端自行下载）；`download_media_before_send` 开启时先 `_prepare_chain_for_forward` 本地化
6. 逐目标：At 策略（反查开启且选中 @ 类型 → `_sanitize_at_chain_for_target` + `_resolve_at_mentions` 精确 @；默认 → `_textify_at_chain` 转文本）；`_degrade_forward_for_target` 在目标平台不支持 `Nodes` 时把合并转发降级为 `[聊天记录]` 文本；`_should_attach_header` 判定后前置来源头（末尾 `\n\n\u200b` 零宽空格避免连续换行问题）
7. `self.context.send_message(target, event.chain_result(new_chain))` 发送，成功后写入冷却时间戳；单目标失败不影响其他目标
8. 失败三级兜底：① 超时类失败（`_is_send_timeout`）先用 `_send_with_timeout_retry` 原链短延迟（3s）重发至多 2 次；② 仍失败 → AstrBot 核心重新本地化所有媒体重试；③ 仍失败且含媒体 → `_prepare_chain_fallback`（远程 URL 本地化，走规则级代理三态）重试；仍失败记录错误

**队列模式下的冷却（有意设计，非 bug）**：队列分支先于冷却检查 `continue`，冷却判断与写入只在即时发送路径执行、后台 worker 不读 `_cooldowns`，因此规则同时配置冷却与队列间隔时冷却**实际失效**，只有队列间隔在限流；`_cooldown_ignored(rule)` 判定（`queue_enabled` 开启且规则队列间隔 > 0），`_format_rules` / `cmd_filter_list` 显示 `❄失效(队列中)` / `❄Ns（队列中失效）` 标记，`forward_message` 按 `_cooldown_warned`（rule_key 去重）只告警一次。

**队列 worker（`_queue_worker` / `_send_queued_item`）**：FIFO 消费 `_send_queue`，按条目 interval 间隔发送；发送前若 `has_media` 则实时注册文件服务 URL（`_prepare_chain_for_media_urls`），失败走同样的超时重试与两级媒体降级；`_queue_paused` 时停止消费；发送成功/丢弃后 `_remove_persisted_item` 并从 `_queue_rule_counts` 减计数。队列持久化条目记录 `rule_key`、序列化链、header_text、has_media、代理三态。

### 6.6 过滤系统

- 模式：`off`（不过滤）/ `blacklist`（命中不转发）/ `whitelist`（命中才转发）
- 模式优先级：规则级 `filter_mode`（inherit → 全局 `filter_mode`）> 全局
- 规则列表：规则级 `filter_patterns` 非空用它，否则继承全局 `filter_patterns`
- 条目解析 `_parse_filter_item`：`regex:` 前缀 → 正则（`re.search`），否则关键词（小写包含匹配，不区分大小写）
- `_unwrap_patterns`：兼容 text（按行拆分）和 template_list（取 dict 的 `rule` 字段）两种配置格式

### 6.7 内容类型筛选与 @ 转发

- 内容类型键：`plain` 文字 / `image` 图片 / `face` 表情（仅 QQ 内置表情 Face 组件）/ `record` 语音 / `video` 视频 / `file` 文件 / `at` @提及 / `other` 其他；QQ 收藏的表情/表情包以图片形式到达，归入「图片」
- 合并转发（聊天记录，`Nodes`）归入「其他」类型：勾选「其他」才会转发聊天记录；目标平台不支持合并转发（非 `aiocqhttp` / `satori`）时降级为 `[聊天记录]` 文本
- 纯媒体转发（未选中「文字」）时不附带来源信息头（`_should_attach_header`）
- @ 转发默认策略：At 一律转为文本 `@昵称`（不发送真实 At 组件，彻底规避协议端解析/群成员查询超时）
- @昵称反查（可选增强，默认关闭）：开启后按目标群成员列表把 `@昵称` 反查为目标群真实 `qq` 再精确 @；反查顺序「qq 精确匹配优先，昵称兜底」；群成员列表按群缓存（`at_nickname_lookup_cache_ttl` 默认 300s），昵称重名跳过反查，拉取失败/无 QQ 协议端/目标非 QQ 群时静默降级，不影响转发

---

## 7. 文档维护规范（强制，不可省略）

> 本项目的**硬性要求**：任何改动在合并前，必须同步维护 `README.md` 与 `CHANGELOG.md`。未同步文档 = 任务未完成。

### 7.1 CHANGELOG 维护

#### 流程

- 日常改动先记在 `## [Unreleased]` 段（顶部固定）。**发版时**再把内容移动到带版本号的新段。
- 多个不相关改动在同一工作周期内，各自独立记入 `[Unreleased]`，**不要**合并为一条。
- **发版日期**：版本段日期 `(YYYY-MM-DD)` 填**当前日期**（以发版当天的系统时间为准）。

#### 格式

统一使用 **分组式**（与远端模板 scaffold 的 `CHANGELOG.md.template` 一致）：按分类用 `###` 子标题分组，条目用「标题 + 冒号 + 说明」，**仅重点条目/重点内容才加粗**：

```markdown
# Changelog

## [Unreleased]

> 一句话总结这个版本（可选）

### 新增
- 功能 A：说明
- **功能 B**：说明

### 修复
- 问题 C：说明

## v0.5.3 (2026-09-22)

> 一句话总结这个版本（可选）

### 新增
- 功能 D：说明
```

分组子标题分类：`新增` / `修复` / `变更` / `移除` / `性能`（只保留本次有内容的分类，无内容的分类不写空标题）

**加粗规则**：条目默认不加粗；写 changelog 时先识别本次改动中的**重点改动**（核心新功能、重要修复、破坏性变更等），只对重点改动用 `**加粗**` 标题强调，其余条目一律不加粗。

**版本总结**：版本标题（`## [Unreleased]` / `## vX.Y.Z (YYYY-MM-DD)`）与分类子标题之间，可加一行 `> 引用` 形式的一句话总结（概括该版本/该批改动的主旨），可选，不强制。

#### 写作视角（面向用户）

CHANGELOG 的读者是**插件使用者**（服务器管理员、群成员），不是开发者。条目只写「用户能感知到的变化」，不写内部实现：

- **写用户视角的结果**：用户能做什么、会看到什么、行为/默认值/配置有什么不同。
- **禁止内部实现细节**：类名/函数名、方法拆分、数据结构、算法、缓存与并发策略、日志埋点、依赖升级、重构过程，一律不写。
- **禁止出现代码标识符与文件路径**；用户直接接触的**命令名**、**配置项 key** 除外。
- **无用户感知的改动不记入 CHANGELOG**：纯内部重构、格式化、注释与测试调整等，写了用户也读不懂、也用不上。
- 判断标准：使用者读完一条，能决定「要不要升级、升级后有什么不同」。

正反例：

```markdown
### 修复
- ✅ 协议端调用超时自动重试：发送超时后短延迟重发原消息，不再直接重下载媒体、加重协议端负担
- ❌ 新增 `_send_with_timeout_retry` 并在发送路径接入 `_is_send_timeout` 判定
```

> ⚠️ 现状说明：本项目 `CHANGELOG.md` 存量条目为早期扁平式（`- 新增：xxx` 前缀风格），属于历史记录；**新增条目一律按分组式书写**。若整体迁移历史格式，需逐条保留他人记录、另行确认，严禁整文件覆盖。

#### 版本号联动

涉及功能新增/删除、行为变化、配置项变化时，**三处同步递增**（`metadata.yaml` version 字段 vX.Y.Z / CHANGELOG 发版段 / README 版本徽章如有）。纯修复/内部重构可只更新 `[Unreleased]` 条目，不递增版本号。

#### 多会话并行开发

**⚠️ 更新 `CHANGELOG.md` 前必须先读取当前文件内容**，识别并保留其他会话已写入的既有条目（含 `[Unreleased]` 下未提交的功能），只追加自己的条目，**严禁整文件覆盖或删除他人记录**。

### 7.2 README 维护

#### 7.2.1 写作视角（面向用户）

README 的读者是**插件使用者**（服务器管理员、群成员），不是开发者。只写用户能直接用到的东西：

- **该写的**：能做什么（能力清单）、怎么装、怎么配（配置表）、怎么用（指令表与示例）、出问题怎么办（常见问题、前置条件与限制）。
- **不该写的**：实现原理与设计理念、架构分层与模块划分、算法与数据结构、内部调用流程、类名/函数名/文件路径、性能优化手段、依赖内部实现。
- **代码只贴用户要用的最小片段**：安装命令、配置项、指令示例；不贴内部实现代码。
- **技术细节翻译成用户语言**：不写「用 `asyncio.Queue` 做 FIFO 消费」，写「发送队列按设定间隔逐条发送，可暂停 / 恢复」。
- 判断标准：使用者只读 README，就能独立完成安装 → 配置 → 使用 → 排障。

正反例：

```markdown
- ✅ 支持转发语音、视频、文件，并可勾选需要转发的内容类型
- ❌ 媒体组件经 `_rebuild_media_component` 重建为 dataclass 节点后由队列 worker 发送
```

#### 7.2.2 头部表头格式

本项目当前仅维护中文版 `README.md`（`README_en.md` 尚未建立），现有头部为单行标题；未来对齐模板时按以下格式（**元素间不留空行**）：

```html
<div align="center">
<h1>跨平台消息转发</h1>
<p><strong>在不同聊天平台及群聊之间自动转发与同步消息</strong></p>
<p><sub>消息转发 &nbsp;&nbsp; 跨平台 &nbsp;&nbsp; 群聊桥接 &nbsp;&nbsp; 聊天 &nbsp;&nbsp; 消息 &nbsp;&nbsp; 同步 &nbsp;&nbsp; 转发 &nbsp;&nbsp; 过滤 &nbsp;&nbsp; 工具</sub></p>
<p><strong>中文</strong> &nbsp;/&nbsp; <a href="README_en.md">English</a></p>
</div>
```

- **h1**：取 `metadata.yaml` 的 `display_name`（跨平台消息转发）
- **desc**：取 `metadata.yaml` 的 `desc`
- **tags**：取 `metadata.yaml` 的 `tags`（中文标签，用 `&nbsp;&nbsp;` 分隔）

#### 7.2.3 README 四大结构

正文按 4 大板块组织，当前 README 将「指令」与「配置」合并在「二、指令与配置」，「平台兼容性测试」归入「其它」，新增内容按所属板块归位：

| 板块 | 内容 |
|------|------|
| 一、功能/介绍 | 功能介绍、特性列表、用户使用场景 |
| 二、指令 | 命令表格（命令、权限、说明）、使用示例 |
| 三、配置 | 配置项表格（配置项、类型、默认值、说明） |
| 四、其它 | 安装、快速开始、运行环境与依赖、常见问题、平台兼容性、许可证、鸣谢 |

更新后通读一遍 README，确保与新行为一致（默认值、超时说明、数据目录等）。

#### 7.2.4 何时必须更新 README

| 改动类型 | 必须更新的板块 |
|----------|--------------|
| 功能新增/删除 | 一、功能/介绍 |
| 使用方式/命令变化 | 二、指令 |
| 配置项变化 | 三、配置 |
| 依赖/版本要求变化 | 四、其它（安装/依赖部分） |
| 已知问题/行为变化 | 四、其它（常见问题部分） |

**何时不需要更新 README**：纯内部重构（行为完全不变）、样式调整、无用户可见行为的 bug 修复。

#### 7.2.5 双语文档同步

- 本项目当前只有中文版 `README.md`；**若建立英文版** `README_en.md`：中文版是**权威版本（源）**，所有改动先落中文版、再同步到英文版，**禁止反向同步**；章节结构保持一致，禁止逐字机翻、禁止留下半翻译的句子
- 头部语言切换链接互相指向对方文件
- 修改时逐条 diff，确保无内容遗漏或错位

#### 7.2.6 格式与多会话约束

- 配置项在 README/CHANGELOG 中引用时，**中文名在前、英文 key 在后**：统一「中文名（`key`）」形式
- **禁止无意义的换行**：HTML 头部区块内元素间不留空行；正文一段文字保持一行；空行只用于分隔标题、表格、列表、代码块等结构性区块
- **多会话并行修改**：更新 `README.md` / `README_en.md` 前必须先读取当前文件内容，保留他人内容，只追加/修改自己的部分，严禁整文件覆盖

### 7.3 注释规范

- 注释只写 non-obvious reason（解释「为什么」，不解释「做了什么」）；禁止残留被注释掉的旧代码、TODO 未完成方案、调试输出
- 方法级 docstring 用中文说明用途/参数/返回（`_` 开头私有方法用途足够明显可省略）
- 代码分区用 `# ---- 分区名 ----` 分隔条

### 7.4 PR 描述规范

- **PR 描述只写最终行为**：描述最终完成了什么，而不是过程中尝试了什么
- 禁止提及：尝试过的方案、被否定的设计、从未合入的中间状态
- 只写 diff 里看不出来的取舍理由（如「选用方案 A 因为性能比 B 高 20%」）

---

## 8. 提交与发布流程

### 8.1 提交规范

- 提交信息遵循 **Conventional Commits**：`<type>(<scope>): <中文短句式 subject>`（如 `feat: 开放颜色配置项`、`fix: 失败发送通知补重试`、`docs: 补充配置说明`）
- **禁止**将未完成的功能、多轮迭代中间状态、被废弃方案写进提交信息
- **提交前必须先向用户展示改动内容和提交标题**，等用户确认后再执行 commit；展示内容至少包含：待提交文件清单、改动概要（如 `git diff --stat`）、提交标题；涉及推送时另附提交 hash 与目标分支
- 推荐顺序：完成功能代码 → 跑通验证 → 更新 CHANGELOG 与 README → 提交；文档更新与代码改动**在同一提交中完成**（不单独拆「补文档」提交）
- 只有用户明确要求时才 commit / push
- **授权一次性，不延续**（「上一轮说过推送」≠ 本轮可以提交）：
  - 用户的确认**只对当次动作生效**；下一次改动必须重新展示改动内容与提交标题，重新确认
  - 展示后改动内容又变了（用户提了新要求、你又改了文件，哪怕只改一行），**原确认立即失效**，必须重新展示并再次确认
  - 下列情形**都不算授权**：上一轮 / 上一个任务 / 上一个会话说过「提交」「推送」；会话恢复（resume / fork）后继承的历史授权；用户对功能或代码表示满意；用户正在讨论其它需求；用户未回复
  - 授权范围逐项独立：确认 commit ≠ 授权 push；确认推送某分支 ≠ 授权后续继续提交或推送；确认一个提交 ≠ 授权 `--amend` 或追加提交
  - 执行前自检：能指出用户**本轮**对**本次改动**的明确同意原话；指不出来就先问，不要执行

### 8.2 版本号规范

- SemVer：新功能 → minor 递增；bug 修复/小改进 → patch 递增
- `metadata.yaml` version 与 `CHANGELOG.md` 最新版本必须一致
- 发版时打 `git tag vX.Y.Z`，tag 推送同样需用户确认

### 8.3 测试与验证

- 项目无强制测试框架，改动后自检语法与逻辑即可：`python -c "import main"` 无语法错误；若项目已补充单测，改动后需保证测试通过
- 格式检查：`python -m ruff format .`（如果项目历史有 ruff 提交）
- 修改后对照 `/mf help` 的树状帮助确认命令出入（命令表有新增/删除时同步更新本文件 6.2 节）

---

## 9. 最终检查清单（每次任务完成前）

- [ ] 架构：单文件 + 分区注释风格与既有代码一致；新工具函数放在模块级分区
- [ ] 注释：只写 non-obvious reason，无残留中间状态、无被注释掉的旧代码
- [ ] 类型：新增/修改函数完整类型注解，禁止 `Any` 与 `# type: ignore`
- [ ] 日志：`logger.info/error/warning` + ❌/⚠️ emoji，新增日志带 `[astrbot_plugin_msg_forward_cc]` 前缀；异常分类记录；无空 catch
- [ ] CHANGELOG：`[Unreleased]` 已追加分组式条目，条目面向用户（无内部实现细节），保留了他人的既有记录（含扁平式历史条目）
- [ ] README：涉及用户可见变更时已同步更新（4 大板块），且面向用户书写（无实现原理与内部细节）
- [ ] 版本号：功能/配置变化时 `metadata.yaml` 与 `CHANGELOG` 最新版本一致
- [ ] 配置：新增配置项已在 `_conf_schema.json`（含中文 hint/options/labels）和代码中同步，调用 `save_config()`
- [ ] 命令：新增/修改命令已同步本次 `mf help` 树与本文件 6.2 命令表
- [ ] 本地测试：`python -c "import main"` 无语法错误（或 ruff 格式通过）
- [ ] 提交信息：Conventional Commits 格式（`type: 中文短句式`），只描述最终行为；提交前向用户展示改动与标题
- [ ] 提交/推送：已取得用户对本次改动的明确确认（一次性授权，未沿用上一轮）

---

## 附录：已知设计细节与注意点

- 绑定流程：`add` 生成码 → 目标会话 `bind`，绑定码一次性（pop 即删），未绑定的码存在 pending.json
- `bind`/`bindraw` 创建的规则自动带 `__template_key: "rule"`（WebUI 正确渲染模板列表）与 `remark` 编号（`规则 #N`）
- 冷却表是纯内存的，重启后冷却状态丢失（不持久化）
- 转发对同一 source 的规则是循环顺序执行，无并发锁；存储层注释「无锁简化」
- 媒体转发默认不下载（`download_media_before_send=false`），跨设备转发提示找不到文件时才开启；NapCat 等容器场景读不到源端本地路径时依赖 File URL + 文件服务
- 队列模式下媒体必须本地化（`_prepare_chain_for_queue` 无条件执行，不受 `download_media_before_send` 影响），防止源端临时文件延迟后被清理；发送前用文件服务实时注册 URL（NapCat 跨容器 + 短效 token 双重问题）
- 队列持久化恢复（`_restore_persisted_queue`）在 `initialize()` 中执行，恢复后按 `rule_key` 重算规则级积压计数，上限判断保持有效；无法还原的空条目（旧版未序列化合并转发）从 `queue.json` 清除，避免永久残留
- 合并转发（聊天记录）：`Forward` 组件只携带源会话的 resId，直接透传会在目标端生成打不开的卡片（并露出 `#000000`、`34` 等模板字段），因此必须先拉取内容重建 `Nodes` 再发送；只有 `aiocqhttp` / `satori` 目标能发送 `Nodes`，其他平台降级为文本
- 合并转发的时间与嵌套：AstrBot 内置 `Nodes.to_dict` 丢弃节点时间、也表达不了嵌套记录，故用 `_ForwardNodes` 子类覆写 `to_dict`（aiocqhttp 适配器对 `Nodes` 正是调 `to_dict()`）；节点时间来自 `get_forward_msg` 每条消息的 `time`，配置 `forward_keep_time=false` 时不写 `time`（目标端统一显示转发时间）；嵌套记录只有被协议端返回为带 resId 的 `forward` 段时才能递归展开（重建为嵌套 node 数组，协议端上限 3 层，本项目取 2），若源记录里的嵌套卡片只是预览 xml（无 resId，例如旧版坏卡片）则只能降级为 `[聊天记录]` 文本
- 合并转发的卡片预览：协议端自动生成预览时，嵌套聊天记录节点因没有可预览的消息段会显示成 `[消息]`、JSON 卡片显示成 `[JSON消息]`，因此 `_ForwardNodes.to_dict()` 显式携带 OneBot 扩展参数 `news`（前 4 行，文本截断），嵌套节点额外携带内层卡片的 `news`；NapCat 与 SnowLuma 均支持该参数，不支持的实现会忽略，不影响发送。卡片预览行默认显示卡片标题（`meta.news.title` → `prompt` 正文 → 其它 `meta.<类型>.title`，只读标题字段，简介 / 来源 / 上传者 / 跳转链接一律不读），配置 `forward_card_preview=off` 时统一显示 `[卡片消息]`；`_ForwardNodes.show_card_title` 保存该决定，`_map_chain_components` 与队列序列化 / 反序列化（`_serialize_chain` / `_deserialize_chain`）都会保留 / 重新按当前配置注入，避免重建链时丢失
- 合并转发节点的内容形态：节点内容只能是「纯嵌套 node 数组」或「普通消息段数组」，同一节点里混排文本与嵌套记录无法表达（`_component_to_onebot_segment` 会把嵌套记录降级为文本占位）；节点内媒体仍由 `_component_to_onebot_segment` 转 base64 内嵌
- 合并转发节点内媒体：直接发送由 AstrBot `Node.to_dict()` 转 base64 内嵌；队列模式由 `_prepare_chain_for_queue` 递归本地化，发送前 `_prepare_chain_for_media_urls` 递归注册文件服务 URL
- `/mf queue pause` 暂停 worker 消费（`_queue_paused`），持久化条目不被删除；`resume` 恢复
- 媒体缓存清理：启动时全清 + 每小时按保留时长清理；`queue clear` 立即清空（积压 + 媒体 + 持久化）
- 自定义下载器把媒体写入媒体缓存目录（`media/` 或系统临时目录兜底），与 AstrBot 自身临时文件行为一致
- group 嵌套：`@mf.group("queue")` 注册子命令组（AstrBot filter API 支持），queue 命令均为 `@queue.command(...)`