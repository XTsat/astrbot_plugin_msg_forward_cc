<div align="center">
<img src="logo.png" alt="跨平台消息转发" width="160" />
<h1>跨平台消息转发</h1>
<p><strong>在不同聊天平台及群聊之间自动转发与同步消息</strong></p>
<p><sub>消息转发 &nbsp;&nbsp; 跨平台 &nbsp;&nbsp; 群聊桥接 &nbsp;&nbsp; 消息过滤 &nbsp;&nbsp; 发送队列 &nbsp;&nbsp; 聊天记录</sub></p>
</div>

## 一、功能介绍

把 A 会话的消息自动同步到 B 会话，让 QQ、微信、Telegram、Discord 等不同平台之间像「同一个群」一样互通。适用于任意 AstrBot 支持的平台。

- **多平台互通**：任意 AstrBot 平台之间双向同步消息、桥接群聊。
- **多源多目标**：一条规则的来源与目标都可填多个会话，一源可同时转发到多个目标。
- **规则管理**：每条规则可设备注、单独启停、单独隐藏来源信息；支持绑定码与一条命令两种建法。
- **来源信息头**：转发时自动标注平台 / 发送者 / 会话 ID，模板可自定义、可按规则隐藏。
- **消息过滤**：全局与规则两级（不过滤 / 黑名单 / 白名单），支持关键词与正则。
- **用户黑白名单**：按发送者 UID 精确过滤，只转发或只屏蔽指定用户。
- **内容类型筛选**：文字 / 指令 / 图片 / 表情 / 语音 / 视频 / 文件 / @提及 / 其他，九类可多选；「指令」默认不转发。
- **转发限流**：全局与规则级冷却，以及按间隔逐条发送的发送队列（可设长度上限、暂停 / 恢复、一键清空）。
- **媒体与聊天记录**：图片、语音、视频、文件照常转发，失败自动重试；合并转发可展开、保留原始时间与发送者头像。
- **@ 转发**：默认把 @ 转为文本 `@昵称`；可选按目标群成员精确 @。
- **WebUI 配置**：全部配置项可在 AstrBot WebUI 可视化修改，也可用 `/mf` 系列命令。

### 安装

在 AstrBot 插件市场或 WebUI 安装页搜索 `astrbot_plugin_msg_forward_cc` 安装即可，无需额外依赖。

### 快速上手

1. 在**来源会话**发送 `/mf add`，得到 6 位绑定码。
2. 在**目标会话**发送 `/mf bind <绑定码>`，规则即创建完成。
3. 也可一条命令直接建规则：`/mf bindraw qq 654321 tg 123456`（QQ 群 → Telegram 群）。
4. 用 `/mf list` 查看规则编号与状态，`/mf help` 查看全部命令。

### 使用前提

- 来源与目标会话都要接入 AstrBot，且对应平台适配器已配置好。
- AstrBot 与目标端不在同一容器（如 AstrBot 与 NapCat 分离部署）时，队列模式下转发媒体需先在 WebUI 配置 `callback_api_base`（基础配置 → 回调地址），否则对方可能取不到文件。
- 聊天记录（合并转发）只能发往 QQ 系平台，其它平台会降级为 `[聊天记录]` 文字。

---

## 二、指令

全部命令以 `mf` 开头，除 `/mf help` 外均为管理员权限。

### 绑定与规则

| 指令 | 说明 |
| --- | --- |
| `mf add` | 生成 6 位绑定码 |
| `mf bind <绑定码>` | 在目标会话接受绑定，创建规则 |
| `mf bindraw [源平台] 源ID [目标平台] 目标ID` | 一条命令直接建规则（平台简写见下） |
| `mf del <编号>` | 删除规则 |
| `mf list` | 列出当前会话相关的规则 |
| `mf listall` | 列出所有规则 |
| `mf hide <编号>` | 切换规则的来源信息显示 / 隐藏 |
| `mf toggle <编号>` | 启用 / 停用规则 |
| `mf remark <编号> [备注]` | 设置规则备注，留空清除 |

### 冷却

| 指令 | 说明 |
| --- | --- |
| `mf cooldown` | 查看冷却配置 |
| `mf cooldown default <秒>` | 设置全局默认冷却（0=关闭） |
| `mf cooldown <编号> <秒\|inherit>` | 设置规则级冷却（0=关闭，`inherit`=继承全局） |

### 过滤

| 指令 | 说明 |
| --- | --- |
| `mf filter` | 查看过滤配置（模式、关键词 / 正则） |

### 用户黑白名单

| 指令 | 说明 |
| --- | --- |
| `mf sender` | 查看用户黑白名单配置 |
| `mf sender default <off\|blacklist\|whitelist>` | 设置全局模式 |
| `mf sender default add <UID...>` | 往全局名单加 UID |
| `mf sender default del <UID...>` | 从全局名单删 UID |
| `mf sender default clear` | 清空全局名单 |
| `mf sender <编号> <模式\|inherit>` | 设置规则级模式（`inherit`=继承全局） |
| `mf sender <编号> add\|del <UID...>` / `clear` | 增删 / 清空规则级名单 |

> **UID 怎么拿**：在来源会话发送 AstrBot 自带的 `/sid` 指令，把输出里的 `UID: 「…」` 原样填入名单；换昵称、改群名片不影响判定。名单只做 UID 精确匹配；同一平台有多个适配器时写 `BotID:UID` 或 `平台名:UID`。

### 内容类型

| 指令 | 说明 |
| --- | --- |
| `mf content` | 查看内容类型配置 |
| `mf content list` | 查看可选内容类型与别名 |
| `mf content default <类型...>` | 设置全局默认（`all`=全选） |
| `mf content <编号> <类型...>` | 设置规则级内容类型（多选） |
| `mf content <编号> inherit` | 重置为继承全局 |

### 发送队列

| 指令 | 说明 |
| --- | --- |
| `mf queue status` | 查看队列状态与配置 |
| `mf queue on` / `mf queue off` | 启用 / 停用发送队列 |
| `mf queue interval <秒>` | 设置全局默认队列间隔（0=关闭） |
| `mf queue maxsize` | 查看队列总上限与各规则上限 |
| `mf queue maxsize <条数>` | 设置队列总上限（0=不限制） |
| `mf queue maxsize default <条数>` | 同上 |
| `mf queue maxsize <编号> <条数>` | 设置规则级队列长度上限（0=不限制） |
| `mf queue maxsize <编号> inherit` | 重置为继承全局上限 |
| `mf queue retention <小时>` | 队列媒体缓存保留时长（0=默认 24 小时） |
| `mf queue set <编号> <秒>` | 设置规则级队列间隔（0=关闭该规则队列） |
| `mf queue set <编号> inherit` | 重置为继承全局默认 |
| `mf queue clear` | 清空积压队列并清理媒体缓存 |
| `mf queue pause` / `mf queue resume` | 暂停 / 恢复队列消费 |

### 其它

| 指令 | 说明 |
| --- | --- |
| `mf help` | 显示帮助 |

> **UMO**：会话的唯一标识，格式 `平台名:消息类型:会话ID`，消息类型为 `GroupMessage`（群聊）或 `FriendMessage`（私聊），例如 `aiocqhttp:GroupMessage:654321`。

### `mf bindraw` 平台简写

| 简写 | 完整平台名 | 对应平台 |
| --- | --- | --- |
| `df` | `default` | 默认 |
| `qq` | `aiocqhttp` | QQ |
| `wx` | `weixin_oc` | 个人微信 |
| `tg` | `telegram` | Telegram |
| `dc` | `discord` | Discord |

省略平台时默认为 `default`；平台简写或群号后加 `s` 表示私聊；超过 3 个字母的平台名直接当完整平台标识使用（如 `aiocqhttp`、`weixin_oc`）。例：`/mf bindraw qq 654321 wx 123456`（QQ 群 → 微信）、`/mf bindraw 654321s 123456`（默认私聊 → 默认群）。

---

## 三、配置

全部配置项在 AstrBot WebUI 的插件配置页修改；运行时状态可用 `/mf filter`、`/mf sender`、`/mf content`、`/mf queue status` 查看。唤醒词用 AstrBot 全局配置（`wake_prefix`，默认 `/`），插件不单独设置。

<details>
<summary>基础设置（4 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 转发规则列表（`rules`） | template_list | `[]` | 转发规则，WebUI 中增删改；字段见「规则字段」 |
| 默认隐藏来源信息头（`default_hide_header`） | bool | `false` | 新建规则默认隐藏来源信息头 |
| 来源信息头模板（`header_template`） | text | 见下方 | 来源信息头模板，变量见下方 |
| 平台名称映射（`platform_names`） | list | `["aiocqhttp=QQ"]` | 平台显示名映射，格式见下方 |

</details>

<details>
<summary>过滤与指令（4 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 消息过滤模式（`filter_mode`） | string | `off` | `off` 不过滤 / `blacklist` 命中不转发 / `whitelist` 命中才转发 |
| 全局过滤规则列表（`filter_patterns`） | template_list | `[]` | 每行一条：`regex:表达式`=正则，其余=关键词（不区分大小写） |
| 用户黑白名单模式（`sender_filter_mode`） | string | `off` | 按发送者 UID 过滤；名单为空时不生效 |
| 全局用户名单（`sender_filter_list`） | text | 空 | 每行一个 UID（`/sid` 获取），可写 `BotID:UID` / `平台名:UID` |

</details>

> 过滤分全局与规则两级，规则级留空继承全局。以唤醒词开头的消息归入内容类型「指令」，是否转发由「指令」勾选决定（默认不勾），与过滤模式无关。

<details>
<summary>冷却与队列（5 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 默认转发冷却时间（`default_cooldown_seconds`） | int | `0` | 全局默认冷却秒数，0=关闭；规则级可覆盖 |
| 默认发送队列间隔（`default_queue_interval_seconds`） | int | `0` | 全局默认队列间隔，0=关闭 |
| 启用发送队列（`queue_enabled`） | bool | `false` | 发送队列总开关 |
| 发送队列总长度上限（`queue_max_size`） | int | `0` | 全部规则合计的积压上限，0=不限制 |
| 队列媒体缓存保留时间（`queue_media_retention_hours`） | int | `24` | 队列媒体缓存保留小时数，0=默认 24；重启时清空 |

</details>

> 队列模式下冷却不生效（只有队列间隔在限流），需要冷却限流请关闭该规则的队列间隔。

<details>
<summary>内容与媒体（5 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 默认转发的内容类型（`default_content_types`） | list | 除「指令」外全选 | 新建规则默认内容类型；「指令」默认不勾 |
| 发送媒体前先下载到本地（`download_media_before_send`） | bool | `false` | 跨设备提示找不到文件时开启 |
| 转发聊天记录时保留原始发送时间（`forward_keep_time`） | bool | `true` | 关闭则统一显示转发时间 |
| 聊天记录卡片预览（`forward_card_preview`） | string | `source` | `source` 来源 + 标题 / `title` 仅标题 / `off` 统一 `[卡片消息]` |
| 转发聊天记录时获取发送者头像（`forward_node_avatar`） | bool | `true` | 让聊天记录显示发送者本人头像；昵称重名、已改名或不在机器人所在群时补不回来 |

</details>

### 高级 @

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 按昵称反查 @ 对象（`at_nickname_lookup`） | bool | `false` | 开启后把 `@昵称` 精确 @ 到目标群成员，仅 QQ 系有效 |
| 群成员列表缓存时长（`at_nickname_lookup_cache_ttl`） | int | `300` | 成员列表缓存秒数，0=每次重新获取 |
| 额外允许 @ 透传的目标平台（`at_passthrough_extra_platforms`） | list | `[]` | 自建 QQ 适配器在此追加平台名（内置已含 QQ 系平台） |

<details>
<summary>规则字段（17 项）</summary>

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 是否启用；关闭后规则保留但不再转发 |
| `remark` | string | 规则备注，留空显示 `source_umo → target_umo` |
| `source_umo` | text | 来源会话，每行一条，可多个（任一命中即触发） |
| `target_umo` | text | 目标会话，每行一条，可多个 |
| `hide_header` | bool | 隐藏来源信息头 |
| `filter_mode` | string | `inherit` 继承全局 / `off` / `blacklist` / `whitelist` |
| `filter_patterns` | list | `regex:表达式` 或关键词；留空继承全局 |
| `sender_filter_mode` | string | `inherit` / `off` / `blacklist` / `whitelist` |
| `sender_filter_list` | text | 每行一个 UID 或 `BotID:UID`；留空继承全局 |
| `cooldown_seconds` | int | 冷却秒数，0=关闭；不填=继承全局 |
| `queue_interval_seconds` | int | 队列间隔秒数，0=立即发送；不填=继承全局 |
| `queue_max_size` | int | 规则级积压上限，0 或不填=不限制 |
| `download_media_before_send` | string | `inherit` / `true` / `false` |
| `content_types` | list | 转发的内容类型（含「指令」），空=继承全局 |
| `at_nickname_lookup` | string | `inherit` / `true` / `false` |
| `use_proxy` | bool | 媒体下载是否走代理 |
| `proxy_url` | string | 代理地址，仅 `use_proxy` 开启时生效 |

</details>

<details>
<summary>平台名称映射（参考）</summary>

每项格式 `原始平台名=显示名`，默认已包含全部常见平台映射（`default=默认`、`aiocqhttp=QQ`、`qq_official=QQ官方机器人`、`weixin_oc=个人微信`、`telegram=Telegram`、`discord=Discord`、`lark=飞书`、`dingtalk=钉钉`、`kook=KOOK`、`slack=Slack`、`vocechat=VoceChat`、`line=LINE`、`satori=Satori`、`matrix=Matrix`、`misskey=Misskey`、`mattermost=Mattermost`、`wecom=企业微信`、`wecom_ai_bot=企微AI机器人`、`weixin_official_account=微信公众号`、`qq_official_webhook=QQ官方机器人(Webhook)`）。删空列表则全部平台显示原名。

</details>

<details>
<summary>来源信息头模板（参考）</summary>

| 变量 | 含义 |
| --- | --- |
| `{sender_name}` | 发送者昵称 |
| `{sender_id}` | 发送者 ID |
| `{platform}` | 平台名称 |
| `{msg_type}` | 消息类型（群聊 / 私聊） |
| `{conversation_id}` | 会话 ID |

默认模板：

```
[转发] {sender_name} ({sender_id})
来自 {platform} 的 {msg_type}（ID: {conversation_id}）消息
```

</details>

---

## 四、其它

### 平台兼容性

支持 AstrBot 官方全部平台适配器；各平台之间「文字 / 图片 / 语音 / 动图 / 视频 / 合并消息 / 文件」的实测结果见 **[平台兼容性测试](docs/platform-compatibility.md)**。

### 贡献

欢迎提交 Bug 报告、功能建议、PR 与测试记录（请注明平台、会话与规则）。

### 许可证

本插件以 **AGPL-3.0** 开源（见 [LICENSE](LICENSE)）：插件在 AstrBot（AGPL）基础上开发，并派生自 `astrbot_plugin_msg_transfer`，因此沿用同一许可证。

### 上游来源

- 框架：[AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）
- 前身项目：[Siaospeed/astrbot_plugin_msg_transfer](https://github.com/Siaospeed/astrbot_plugin_msg_transfer)
- 本仓库：[XTsat/astrbot_plugin_msg_forward_cc](https://github.com/XTsat/astrbot_plugin_msg_forward_cc)（作者：XTsat）
