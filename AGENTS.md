# AGENTS.md — AstrBot 跨平台消息转发插件

> 本文件为 AI 代理/协作者提供本项目完整上下文。修改代码前请先阅读本节全部内容。

## 项目概述

基于 **AstrBot** 框架的跨平台消息转发插件（AGPL-3.0），用于在不同聊天平台（QQ、微信、Telegram、Discord 等）之间同步消息、桥接群聊。由 [Siaospeed/astrbot_plugin_msg_transfer](https://github.com/Siaospeed/astrbot_plugin_msg_transfer) 修改而来，作者 XTsat。核心功能：通过「转发规则」把 A 会话的消息自动同步到 B 会话，支持来源信息标注、消息过滤、转发冷却、媒体本地化转发。

## 文件清单

| 文件 | 职责 |
|------|------|
| `main.py` | 插件主体，全部逻辑（988 行） |
| `_conf_schema.json` | WebUI 配置 Schema（131 行） |
| `metadata.yaml` | 插件元数据（v0.4.1，支持 aiocqhttp/wechatpadpro/telegram/discord） |
| `README.md` | 中文文档 |
| `LICENSE` | AGPL-3.0 |
| `logo.png` | 插件图标 |

## 技术要点

- 依赖 AstrBot API：`astrbot.api.star`、`astrbot.api.event`（filter, AstrMessageEvent）、`astrbot.api`（Context, Star, logger, AstrBotConfig）、`astrbot.core.message.components`（Plain, Image, Record, Video, File）
- 命令通过 `@filter.command_group("mf")` 注册，均为 `yield event.plain_result(...)` 风格（异步生成器）
- 规则持久化在 AstrBot 插件配置 `self.config["rules"]`（`config.save_config()` 保存）；绑定码暂存在 `astrbot/data/plugin_data/msg_forward_cc/pending.json`（原子写入：先写 .tmp 再 replace）
- **UMO（Unified Message Origin）** 格式：`平台名:消息类型:会话ID`，消息类型为 `GroupMessage`/`FriendMessage`

## 核心架构（main.py）

### 1. 工具函数

- `_rebuild_media_component(comp)`：媒体组件重下载到本进程临时目录，用 `fromFileSystem` 重建，解决跨会话转发时源端临时路径不可达（ENOENT）问题；失败时降级为 `Plain` 占位文本。Video 会清空 cover（源平台临时路径跨进程不可达）；File 保留原文件名（兼容旧版签名，TypeError 时回退）
- `_prepare_chain_for_forward(chain)`：转发前对 Image/Record/Video/File 逐组件本地化，返回新链（走 AstrBot 核心 download_file，正常网络）
- `_extract_remote_url(comp)`：返回组件引用的远程 http(s) URL；本地文件/base64/data URI 返回 None。注意 File 组件的 `.file` 是 property（异步上下文访问会触发同步下载），对 File 只检查 `.url` 与 `.file_`
- `_download_url_to_local(comp, url, use_proxy=False, proxy_url=None)`：把远程媒体下载到本地临时目录；内部先走正常网络（AF_UNSPEC）、失败再改用强制 IPv4（AF_INET），规避核心 download_file 的 `Cannot connect ... [None]`（aiohttp#9447）问题；代理三态——use_proxy 关→直连，开且 proxy_url 空→走系统代理（trust_env=True），开且非空→走该地址（trust_env=False）；两次都失败抛异常由调用方降级
- `_guess_media_ext(comp, url, content_type)`：按 Content-Type → URL 后缀 → 组件类型确定临时文件后缀
- `_rebuild_from_local_path(comp, path)`：按本地路径重建组件（File 无 `fromFileSystem`，改用 `File(name=..., file=...)`）
- `_prepare_chain_fallback(chain, use_proxy=False, proxy_url=None)`：转发失败后的兜底链，仅本地化远程 URL 媒体（用 `_download_url_to_local`，代理三态同上），失败降级占位文本
- `load_json` / `save_json`：健壮的文件读写，带详细错误日志（FileNotFoundError/JSONDecodeError/OSError 分类处理）
- `gen_code(n=6)`：用 `secrets` 生成绑定码（小写字母+数字）

### 2. 存储层 `MsgForwardStore`

- 维护 `pending.json`，提供 `load_pending / save_pending / add_pending / pop_pending`（pop 不存在的 code 抛 KeyError）

### 3. 插件主体 `MsgForward(star.Star)`

- `__init__`：初始化 data_dir（`StarTools.get_data_dir("msg_forward_cc")`）、store、内存冷却表 `_cooldowns`（key = `source_umo|target_umo` → 冷却结束时间戳）
- `_format_origin_header(event, umo)`：生成来源信息头。解析 UMO 三字段；平台友好名从 `platform_name_map` 配置（合并内置默认映射，覆盖全部 AstrBot 官方适配器与常见社区适配器，见 `_conf_schema.json`）；消息类型映射 GroupMessage→群组、FriendMessage→私聊；支持 `header_template` 模板变量 `{sender_name}{sender_id}{platform}{msg_type}{conversation_id}`，留空用默认格式

### 4. 命令（`/mf` 前缀）

| 命令 | 权限 | 功能 |
|------|------|------|
| `help` | 全部 | 显示帮助 |
| `add` | ADMIN | 生成 6 位绑定码存入 pending.json，目标会话用 `bind` 接受 |
| `bind <code>` | ADMIN | 弹出绑定码创建规则（当前会话为目标），默认 `hide_header` 取自 `default_hide_header` |
| `bindraw [源平台] 源ID [目标平台] 目标ID` | ADMIN | 直接建规则；平台简写 df/qq/wx/tg/dc（3 字母以上按完整平台名处理），末尾 `s` 表示私聊（平台后或 ID 后加 s 均兼容）；支持 2/3/4 参数省略形式 |
| `del <编号>` | ADMIN | 删除规则（1-based 索引） |
| `list` | 全部 | 列出当前会话（source_umo 匹配）的规则，显示 🔒/🔓 隐藏状态和 ❄冷却 |
| `listall` | ADMIN | 列出所有规则 |
| `hide <编号>` | ADMIN | 切换单条规则的 hide_header |
| `hidelist` / `hidelistall` | 全部/ADMIN | 查看来源信息状态（允许显示/禁止显示分组） |
| `filter` | 全部 | 查看全局+规则级过滤配置与冷却配置 |

**bindraw 解析逻辑**（`build_umo`）：plat 小写化，以 `s` 结尾 → FriendMessage 并去掉 `s`；`default` 或空 → `default` 平台；`len(plat_key) > 3` 时直接用原字符串作为平台标识；ID 末尾 `s` 且平台无 `s` 后缀时也转私聊。

### 5. 主转发逻辑 `forward_message`

- 监听 `@filter.event_message_type(filter.EventMessageType.ALL)` 全部消息
- 匹配 source_umo 相同的所有规则，逐规则：
  1. `_should_forward(event, rule)` 过滤检查（规则级优先，inherit 继承全局）
  2. 冷却检查（规则 `cooldown_seconds` 优先，否则 `default_cooldown_seconds`；冷却期内跳过）
  3. 主链：默认透传 `sanitized_chain`（正常网络，媒体交给目标端自行下载）；`download_media_before_send` 开启时先 `_prepare_chain_for_forward` 本地化
  4. 构造消息链：`hide_header` 为 true 直接透传；否则前置来源头（末尾加 `\n\n\u200b` 零宽空格避免连续换行问题）
  5. `self.context.send_message(target, event.chain_result(new_chain))` 发送，成功后写入冷却时间戳
  6. 失败自动降级：发送失败且消息含远程 URL 媒体时，用 `_prepare_chain_fallback` 本地化后重试一次；仍失败记录错误
- 异常分类记录日志（ValueError = 非法 session 字符串），单规则失败不影响其他规则

### 6. 过滤系统

- 模式：`off`（不过滤）/ `blacklist`（命中不转发）/ `whitelist`（命中才转发）
- 模式优先级：规则级 `filter_mode`（inherit → 全局 `filter_mode`）> 全局
- 规则列表：规则级 `filter_patterns` 非空用它，否则继承全局 `filter_patterns`
- 条目解析 `_parse_filter_item`：`regex:` 前缀 → 正则（`re.search`），否则关键词（小写包含匹配，不区分大小写）
- `_unwrap_patterns`：兼容 text（按行拆分）和 template_list（取 dict 的 `rule` 字段）两种配置格式

### 7. 配置 Schema 字段（_conf_schema.json）

- `default_hide_header`（bool，false）：新建规则默认隐藏来源头
- `rules`（template_list，模板 `rule`：source_umo/target_umo/hide_header/filter_mode[inherit|off|blacklist|whitelist]/filter_patterns/cooldown_seconds）
- `platform_name_map`（object）：平台显示名映射
- `header_template`（text）：来源头模板
- `filter_mode`（string，off）、`filter_patterns`（text，每行一条）
- `default_cooldown_seconds`（int，0）
- `download_media_before_send`（bool，false）：发送前媒体先下载到本地
- `rules[].use_proxy`（bool，false）+ `rules[].proxy_url`（string，空）：规则级媒体下载代理三态——use_proxy 关→直连；开且 proxy_url 空→走 AstrBot 自带代理（系统环境变量）；开且非空→走该地址（如 `http://127.0.0.1:7890`）

## 已知设计细节与注意点

- 绑定流程：`add` 生成码 → 目标会话 `bind`，绑定码一次性（pop 即删），未绑定的码存在 pending.json
- `bind`/`bindraw` 创建的规则 `__template_key: "rule"` 用于 WebUI 正确渲染模板列表
- 冷却表是纯内存的，重启后冷却状态丢失（不持久化）
- 转发对同一 source 的规则是循环顺序执行，无并发锁；存储层注释「无锁简化」
- 媒体转发默认不下载（`download_media_before_send=false`），跨设备转发提示找不到文件时才开启
- 媒体失败自动降级：转发默认透传（正常网络），发送失败且含远程 URL 媒体时用 `_prepare_chain_fallback` 本地化后重试一次（下载器内部先正常网络、失败再 IPv4）；下载仍失败降级为占位文本
- 自定义下载器把媒体写入系统临时目录 `msg_forward_cc_media` 子目录，暂不做清理（与 AstrBot 自身临时文件行为一致）

## 开发约束

- **任何功能更改（新增 / 修改 / 修复）必须同步更新 `CHANGELOG.md` 与 `README.md`**：CHANGELOG 风格为 `# Changelog` + `## vX.Y.Z (YYYY-MM-DD)` + 扁平列表，每条以 `- 新增：` / `- 修复：` / `- 变更：` 前缀标注类型；README 同步更新对应的功能说明、命令表、配置说明等文档
- **⚠️ 多会话并行开发：更新 `CHANGELOG.md` 前必须先读取当前文件内容，识别并保留其他会话已写入的既有条目（含 [Unreleased] 下未提交的功能），只追加自己的条目，严禁整文件覆盖或删除他人记录**；git commit 前再次核对 CHANGELOG.md 是否完整保留了所有已存在功能的变更记录
- Python 代码遵循 AstrBot 插件规范：类继承 `star.Star`，`__init__(self, context, config)`，注册命令组用 `@filter.command_group`，事件处理用 `@filter.event_message_type`
- 所有用户可读输出用中文 + emoji 图标风格，日志用 `logger.info/error/warning`（错误信息带 ❌ 前缀）
- 配置文件改动后必须调用 `self.config.save_config()`
- 回复消息统一用 `yield event.plain_result(...)`
- 权限控制：管理类命令加 `@filter.permission_type(filter.PermissionType.ADMIN)`
- 不引入新依赖，纯标准库（json/re/secrets/time/pathlib/string/tempfile/ssl/socket/urllib）+ AstrBot API；`aiohttp` / `certifi` 在 `_download_url_to_local` 内惰性 import（AstrBot 运行时已自带，仅用于兜底媒体下载，import 失败时降级为占位文本，不影响其余功能）
