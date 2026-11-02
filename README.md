<div align="center">
<h1>跨平台消息转发</h1>
<p><strong>在不同聊天平台及群聊之间自动转发与同步消息</strong></p>
<p><sub>消息转发 &nbsp;&nbsp; 跨平台 &nbsp;&nbsp; 群聊桥接 &nbsp;&nbsp; 聊天 &nbsp;&nbsp; 消息 &nbsp;&nbsp; 同步 &nbsp;&nbsp; 转发 &nbsp;&nbsp; 过滤 &nbsp;&nbsp; 工具</sub></p>
</div>

## 一、功能介绍

把 A 会话的消息自动同步到 B 会话，让 QQ、微信、Telegram、Discord 等不同平台之间像「同一个群」一样互通。适用于任意 AstrBot 支持的平台。

### 能做什么

- **多平台互通**：任意 AstrBot 平台之间双向同步消息、桥接群聊。
- **多源多目标规则**：一条规则的来源与目标都可填多个会话（每行一条），一源可同时转发到多个目标。
- **规则管理**：每条规则可设备注、单独启停、单独隐藏来源信息；`/mf add` + `/mf bind` 绑定码两步建规则，或 `/mf bindraw` 一条命令建规则。
- **来源信息标注**：转发时自动加来源信息头（平台 / 发送者 / 会话 ID），模板可自定义，也可按规则隐藏。
- **消息过滤**：全局与规则两级（不过滤 / 黑名单 / 白名单），支持关键词与正则。
- **用户黑白名单**：按发送者 UID 精确过滤，只转发指定用户的消息或只屏蔽指定用户；UID 用 AstrBot `/sid` 指令获取，全局与规则两级各自可设。
- **指令默认不转发**：以 AstrBot 唤醒词开头的消息（如 `/help`）归入内容类型「指令」，该类型默认不勾选，勾上才转发。
- **转发冷却**：全局默认与规则级冷却时间，避免刷屏。
- **发送队列**：按规则设置间隔逐条转发，各规则独立调度；可设队列长度上限、暂停 / 恢复、一键清空。
- **内容类型筛选**：文字 / 指令 / 图片 / 表情 / 语音 / 视频 / 文件 / @提及 / 其他，九类可多选，未勾选的不转发。
- **媒体转发**：图片、语音、视频、文件跨平台转发，发送失败会自动重试并本地化兜底。
- **聊天记录转发**：合并转发可正常展开、保留每条消息原始发送时间、可显示发送者头像，卡片预览样式可选；目标平台不支持时降级为文字。
- **@ 转发**：默认把 @ 转为文本 `@昵称`（最稳妥）；可选开启「高级 @」，按目标群成员反查后精确 @。
- **WebUI 配置**：全部配置项可在 AstrBot WebUI 可视化修改，也可用 `/mf` 系列命令。

### 安装

在 AstrBot 插件市场或 WebUI 安装页搜索 `astrbot_plugin_msg_forward_cc` 安装即可，依赖由 AstrBot 自动安装；最低 AstrBot 4.0.0。

### 快速上手

1. 在**来源会话**发送 `/mf add`，得到 6 位绑定码。
2. 在**目标会话**发送 `/mf bind <绑定码>`，规则即创建完成。
3. 也可以一条命令直接建规则：`/mf bindraw qq 654321 tg 123456`（QQ 群 → Telegram 群）。
4. 用 `/mf list` 查看规则编号与状态，`/mf help` 查看全部命令。

### 使用前提

- 来源与目标会话都要接入 AstrBot，且对应平台适配器已配置好。
- AstrBot 与目标端不在同一容器（如 AstrBot 与 NapCat 分离部署）时，队列模式下的媒体转发需要先在 AstrBot WebUI 配置 `callback_api_base`（基础配置 → 回调地址），否则对方可能取不到文件。
- 聊天记录（合并转发）只能发往 QQ 系平台，转发到 Telegram / Discord / 微信等平台时会降级为 `[聊天记录]` 文字。
- 各平台支持情况见 [平台兼容性测试](docs/platform-compatibility.md)。

---

## 二、指令

全部命令以 `mf` 开头，除 `/mf help` 外均为管理员权限。

### 绑定与规则

| 指令 | 说明 |
| -------------- | ------------------ |
| `mf add` | 创建一则消息转发绑定的请求（生成 6 位绑定码） |
| `mf bind <绑定码>` | 在目标会话接受绑定请求，创建规则 |
| `mf bindraw [源平台] 源ID [目标平台] 目标ID` | 一条命令直接建规则（平台简写见下） |
| `mf del <编号>` | 删除一条转发规则 |
| `mf list` | 列出与当前会话相关的规则（可用于获取当前群号） |
| `mf listall` | 列出所有规则 |
| `mf hide <编号>` | 切换规则的来源信息显示 / 隐藏 |
| `mf toggle <编号>` | 启用 / 停用一条规则 |
| `mf remark <编号> [备注]` | 设置规则备注名称，留空清除 |

### 冷却

| 指令 | 说明 |
| -------------- | ------------------ |
| `mf cooldown` | 查看冷却配置（全局默认 + 规则级） |
| `mf cooldown default <秒>` | 设置全局默认冷却（0=关闭） |
| `mf cooldown <编号> <秒\|inherit>` | 设置某条规则冷却（0=关闭该规则冷却） |

### 过滤

| 指令 | 说明 |
| -------------- | ------------------ |
| `mf filter` | 查看过滤配置（模式、关键词 / 正则条目、指令处理方式） |

### 用户黑白名单

| 指令 | 说明 |
| -------------- | ------------------ |
| `mf sender` | 查看用户黑白名单配置（全局 + 规则级） |
| `mf sender default <off\|blacklist\|whitelist>` | 设置全局模式：不过滤 / 黑名单（名单里的 UID 不转发）/ 白名单（只转名单里的 UID） |
| `mf sender default add <UID...>` | 往全局名单里加 UID（多个用空格分隔；UID 用 `/sid` 获取） |
| `mf sender default del <UID...>` | 从全局名单里删 UID |
| `mf sender default clear` | 清空全局名单 |
| `mf sender <编号> <模式\|inherit>` | 设置某条规则的模式（`inherit`=继承全局） |
| `mf sender <编号> add\|del <UID...>` / `clear` | 增删 / 清空某条规则的名单 |

> **UID 怎么拿**：在来源会话给机器人发送 AstrBot 自带的 `/sid` 指令，把输出里的 `UID: 「…」` 那一行原样填进名单。它就是这个人在该平台上的唯一 ID（QQ 号、openid、guild id 等由各平台适配器决定），换昵称、改群名片都不影响判定。名单只做 UID 精确匹配，不做昵称或数字形态的模糊匹配；同一平台开了多个适配器、需要区分实例时写 `BotID:UID` 或 `平台名:UID`（Bot ID 同样在 `/sid` 输出里）。

### 内容类型

| 指令 | 说明 |
| -------------- | ------------------ |
| `mf content` | 查看内容类型筛选配置（全局默认 + 规则级覆盖） |
| `mf content list` | 查看可选内容类型与别名 |
| `mf content default <类型...>` | 设置全局默认内容类型（`all`=全选） |
| `mf content <编号> <类型...>` | 设置某条规则转发的内容类型（多选） |
| `mf content <编号> inherit` | 重置为继承全局默认 |

### 发送队列

| 指令 | 说明 |
| -------------- | ------------------ |
| `mf queue status` | 查看发送队列状态与配置（含各规则间隔 / 长度上限 / 当前积压） |
| `mf queue on` / `mf queue off` | 启用 / 停用发送队列总开关 |
| `mf queue interval <秒>` | 设置全局默认队列间隔（0=关闭） |
| `mf queue maxsize` | 查看队列总上限与各规则长度上限 |
| `mf queue maxsize <条数>` | 设置队列总上限（0=不限制） |
| `mf queue maxsize default <条数>` | 同上（`default` 前缀与规则编号区分） |
| `mf queue maxsize <编号> <条数>` | 设置某条规则的队列长度上限（0=不限制） |
| `mf queue maxsize <编号> inherit` | 重置为继承全局上限 |
| `mf queue retention <小时>` | 队列媒体缓存保留时长（0=默认 24 小时） |
| `mf queue set <编号> <秒>` | 设置某条规则的队列间隔（0=关闭该规则队列） |
| `mf queue set <编号> inherit` | 重置为继承全局默认 |
| `mf queue clear` | 清空当前积压的发送队列并清理媒体缓存 |
| `mf queue pause` / `mf queue resume` | 暂停 / 恢复发送队列消费 |

### 其它

| 指令 | 说明 |
| -------------- | ------------------ |
| `mf help` | 显示帮助信息 |

> **UMO**：会话的唯一标识，格式 `平台名:消息类型:会话ID`，消息类型为 `GroupMessage`（群聊）或 `FriendMessage`（私聊），例如 `aiocqhttp:GroupMessage:654321`。

### `mf bindraw` 平台简写

| 简写 | 完整平台名 | 对应平台 |
| ------ | ----------- | --------- |
| `df` | `default` | 默认 |
| `qq` | `aiocqhttp` | QQ |
| `wx` | `weixin_oc` | 个人微信 |
| `tg` | `telegram` | Telegram |
| `dc` | `discord` | Discord |

- 省略平台时默认为 `default`；平台简写或群号后加 `s` 表示私聊；超过 3 个字母的平台名直接当完整平台标识使用（如 `aiocqhttp`、`weixin_oc`）。
- 例：`/mf bindraw qq 654321 wx 123456`（QQ 群 → 微信）、`/mf bindraw 654321s 123456`（默认私聊 → 默认群）。

---

## 三、配置

全部配置项在 AstrBot WebUI 的插件配置页修改（含内容类型勾选、@ 昵称反查等）；运行时状态可用 `/mf filter`、`/mf sender`、`/mf content`、`/mf queue status` 查看。唤醒词用 AstrBot 全局配置，插件不单独设置。

### 基础设置

| 配置项 | 类型 | 默认值 | 说明 |
| ------- | ------ | -------- | ------ |
| 转发规则列表（`rules`） | template_list | `[]` | 转发规则列表，WebUI 中增删改，字段见下方「规则字段」 |
| 默认隐藏来源信息头（`default_hide_header`） | bool | `false` | 新建规则时默认是否隐藏来源信息头 |
| 来源信息头模板（`header_template`） | text | 见下方 | 来源信息头模板，支持变量替换 |
| 平台名称映射（`platform_names`） | list | `["aiocqhttp=QQ"]` | 每项 `原始平台名=显示名`，未命中的平台使用内置名称 |

### 过滤与指令

| 配置项 | 类型 | 默认值 | 说明 |
| ------- | ------ | -------- | ------ |
| 消息过滤模式（`filter_mode`） | string | `off` | `off` 不过滤 / `blacklist` 命中不转发 / `whitelist` 命中才转发 |
| 全局过滤规则列表（`filter_patterns`） | template_list | `[]` | 每条一条规则：`regex:表达式`=正则，其余=关键词（不区分大小写） |
| 用户黑白名单模式（`sender_filter_mode`） | string | `off` | `off` 不过滤 / `blacklist` 名单里的 UID 不转发 / `whitelist` 只转发名单里的 UID；名单为空时不生效 |
| 全局用户名单（`sender_filter_list`） | text | 空 | 每行一个 UID（精确匹配，`/sid` 输出里的 UID 行）；需区分适配器实例时写 `BotID:UID`（详见下方说明） |

> 过滤分**全局**与**逐规则**两层：规则级 `filter_mode` / `filter_patterns` 留空即继承全局。唤醒词用 AstrBot 全局配置（`wake_prefix`，默认 `/`），插件不单独设置；以唤醒词开头的消息归入内容类型「指令」，是否转发由「指令」勾选决定（默认不勾），与过滤模式无关。

> 用户黑白名单按**消息发送者 UID** 过滤（UID 用 AstrBot 自带的 `/sid` 指令获取），与上面按消息内容的过滤相互独立、同时生效：UID 放行的消息才继续判断关键词 / 正则。规则级 `sender_filter_mode` 为「继承全局」时跟随全局模式，规则级名单留空时跟随全局名单。名单为空时不生效（避免误开白名单后一条都转发不出去）；条目也可以直接用 `/mf sender` 增删。

### 冷却与队列

| 配置项 | 类型 | 默认值 | 说明 |
| ------- | ------ | -------- | ------ |
| 默认转发冷却时间（`default_cooldown_seconds`） | int | `0` | 转发一次后在该秒数内不再重复转发，规则级可覆盖 |
| 默认发送队列间隔（`default_queue_interval_seconds`） | int | `0` | 规则未单独设置时继承；> 0 时消息进队列，每隔该秒数转发一条 |
| 启用发送队列（`queue_enabled`） | bool | `false` | 队列总开关，关闭时规则级队列间隔不生效（立即转发） |
| 发送队列总长度上限（`queue_max_size`） | int | `0` | 全部规则合计的积压上限，0=不限制；达上限后新消息丢弃，已在队列中的不受影响 |
| 队列媒体缓存保留时间（`queue_media_retention_hours`） | int | `24` | 队列模式下缓存的媒体保留小时数，0=默认 24；重启时自动清理 |

> 队列模式下冷却不生效（只有队列间隔在限流），`/mf list` 中以 `❄失效(队列中)` 标记；需要冷却限流请关闭该规则的队列间隔。

### 内容与媒体

| 配置项 | 类型 | 默认值 | 说明 |
| ------- | ------ | -------- | ------ |
| 默认转发的内容类型（`default_content_types`） | list | 除「指令」外全选 | 新建规则默认勾选的内容类型（文字 / 图片 / 表情 / 语音 / 视频 / 文件 / @提及 / 其他）；「指令」默认不勾选=指令消息默认不转发，需要时勾上 |
| 发送媒体前先下载到本地（`download_media_before_send`） | bool | `false` | 跨设备转发提示找不到文件时开启；正常情况不必开 |
| 转发聊天记录时保留原始发送时间（`forward_keep_time`） | bool | `true` | 关闭则聊天记录里每条消息统一显示转发时间 |
| 聊天记录卡片预览（`forward_card_preview`） | string | `source` | `source`=来源 + 标题（与 QQ 原生预览一致）/ `title`=仅标题 / `off`=统一 `[卡片消息]`；只显示标题，不显示简介、上传者等 |
| 转发聊天记录时获取发送者头像（`forward_node_avatar`） | bool | `true` | 按昵称反查真实 QQ 号，让聊天记录里显示发送者本人头像；昵称重名、已改名、发送者不在机器人所在群时补不回来；关闭后沿用协议端返回的信息 |

### 高级 @（昵称反查）

| 配置项 | 类型 | 默认值 | 说明 |
| ------- | ------ | -------- | ------ |
| 按昵称反查 @ 对象（`at_nickname_lookup`） | bool | `false` | 关闭时 @ 一律转为文本 `@昵称`（最稳妥）；开启后按目标群成员列表把 `@昵称` 换成真实成员再精确 @，仅 QQ 系目标有效，规则级可覆盖 |
| 群成员列表缓存时长（`at_nickname_lookup_cache_ttl`） | int | `300` | 同一目标群在该秒数内复用成员列表；0=每次重新拉取 |
| 额外允许 @ 透传的目标平台（`at_passthrough_extra_platforms`） | list | `[]` | 自建 QQ 适配器在此追加平台名（内置已含 QQ 系平台） |

### 规则字段（`rules`）

<details>
<summary>规则字段（18 项）</summary>

| 字段 | 类型 | 说明 |
| ----- | ------ | ------ |
| `enabled` | bool | 是否启用此规则，关闭后规则保留但不再转发 |
| `remark` | string | 自定义备注（规则展示名称），留空显示 `source_umo → target_umo` |
| `source_umo` | text | 来源会话标识，每行一条，可填多个（任一命中即触发） |
| `target_umo` | text | 目标会话标识，每行一条，可填多个（转发到每一个） |
| `hide_header` | bool | 是否隐藏来源信息头 |
| `filter_mode` | string | 过滤模式：`inherit` 继承全局 / `off` / `blacklist` / `whitelist` |
| `filter_patterns` | list | 过滤条目：`regex:表达式` 或关键词；留空继承全局 |
| `sender_filter_mode` | string | 用户黑白名单模式：`inherit` 继承全局 / `off` / `blacklist` / `whitelist` |
| `sender_filter_list` | text | 本规则的用户名单，每行一个 UID（`/sid` 获取）或 `BotID:UID`；留空继承全局名单 |
| `cooldown_seconds` | int | 冷却秒数，0=关闭本规则冷却；不填=继承全局 |
| `queue_interval_seconds` | int | 队列间隔秒数，0=立即转发；不填=继承全局 |
| `queue_max_size` | int | 本规则队列积压上限，0 或不填=不限制（仅受全局总上限约束） |
| `download_media_before_send` | string | 发送前先下载媒体：`inherit` / `true` / `false` |
| `content_types` | list | 本规则转发的内容类型（可填中文名，含「指令」），空=继承全局默认；勾上「指令」才转指令消息 |
| `at_nickname_lookup` | string | 高级 @ 开关：`inherit` / `true` / `false` |
| `use_proxy` | bool | 媒体下载是否走代理；开启后地址留空走 AstrBot 自带代理 |
| `proxy_url` | string | 代理地址（仅 `use_proxy` 开启时生效），如 `http://127.0.0.1:7890` |

</details>

### `platform_names` 格式

每项格式 `原始平台名=显示名`，默认已包含全部常见平台映射（`default=默认`、`aiocqhttp=QQ`、`qq_official=QQ官方机器人`、`weixin_oc=个人微信`、`telegram=Telegram`、`discord=Discord`、`lark=飞书`、`dingtalk=钉钉`、`kook=KOOK`、`slack=Slack`、`vocechat=VoceChat`、`line=LINE`、`satori=Satori`、`matrix=Matrix`、`misskey=Misskey`、`mattermost=Mattermost`、`wecom=企业微信`、`wecom_ai_bot=企微AI机器人`、`weixin_official_account=微信公众号`、`qq_official_webhook=QQ官方机器人(Webhook)`）。删空列表则全部平台显示原名。

### `header_template` 可用变量

| 变量 | 含义 |
| ------ | ------ |
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

---

## 四、其它

### 转发效果

来源会话的消息转发到目标会话时，会按规则补上来源信息头（可在规则里隐藏）；图片、语音、视频、文件照常送达；聊天记录可正常展开并保留原始发送时间与发送者头像；目标平台不支持的类型降级为文字占位，不会丢消息。

### 运行环境

AstrBot 4.0.0 及以上（Python 3.10+），无需额外安装依赖。

### 平台兼容性

支持 AstrBot 官方全部平台适配器；各平台之间「文字 / 图片 / 语音 / 动图 / 视频 / 合并消息 / 文件」的实测结果见 **[平台兼容性测试](docs/platform-compatibility.md)**。

### 贡献

欢迎提交 Bug 报告、功能建议、PR（适配更多平台、扩展规则能力）与测试记录（注明平台、会话与规则）。

### 许可证

本插件以 **AGPL-3.0** 开源（AGPL-3.0 文件见 [LICENSE](LICENSE)）：插件在 AstrBot（AGPL）基础上开发，并派生自 `astrbot_plugin_msg_transfer`，因此沿用同一许可证。

### 上游来源

- 框架：[AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）
- 前身项目：[Siaospeed/astrbot_plugin_msg_transfer](https://github.com/Siaospeed/astrbot_plugin_msg_transfer)
- 本仓库：[XTsat/astrbot_plugin_msg_forward_cc](https://github.com/XTsat/astrbot_plugin_msg_forward_cc)（作者：XTsat）
