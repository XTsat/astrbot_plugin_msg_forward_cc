# MsgForward —— AstrBot 跨平台消息转发插件

一个 **简洁、稳健、可扩展** 的 AstrBot 跨平台消息转发插件，用于在不同聊天平台之间同步消息、桥接群聊，让多个平台之间能够像“同一个群”一样互动。

本插件理论上适用于任意 AstrBot 支持的平台（如 QQ、微信、Telegram、Discord 等）。

> **许可证：AGPL-3.0**  
> 本插件在 AstrBot（AGPL）基础上开发，因此以 AGPL 开源。  
> 本插件基于 [Siaospeed/astrbot_plugin_msg_transfer](https://github.com/Siaospeed/astrbot_plugin_msg_transfer) 修改而来，添加了WebUI配置, 支持对转发规则设置来源信息显示状态。

---

## ✨ 特性

- **多平台互通**：支持 AstrBot 的任意平台适配器。
- **消息来源标注**：支持自动在消息前增加来源信息（例如 UMO / 平台名 / 发送者）。
- **可持久化存储**：自动保存转发规则，不会因重启丢失。
- **可拓展性高**：所有逻辑模块化，便于二次开发。

额外功能：
- **WebUI配置**：支持通过 AstrBot WebUI 进行可视化配置，无需了解配置格式。
- **单命令配置**：支持通过单命令单窗口配置转发规则，无需 WebUI 或手动绑定。
- **来源信息控制**：支持对转发规则设置来源信息显示状态，允许显示或禁止显示来源信息，支持自定义来源信息显示模板。
- **消息过滤**：支持对转发规则设置消息过滤模式（off/blacklist/whitelist），并配置过滤正则表达式或关键词。

---

## 🚀 快速开始
1. **下载插件**：通过 AstrBot 的插件市场直接下载，或从本仓库的 Release 下载 `astrbot_plugin_msg_forward_cc` 的 `.zip` 文件，在 AstrBot WebUI 中的插件页面中选择 `从文件安装` 。
2. **重启 AstrBot**：我们推荐在安装本插件后手动重启一次 AstrBot。

---

## ⚙️ 配置

插件支持通过 AstrBot WebUI 进行可视化配置，配置项定义在 `_conf_schema.json` 中。

| 配置项 | 类型 | 默认值 | 说明 |
|-------|------|--------|------|
| `rules` | template_list | `[]` | 转发规则列表，可在 WebUI 直接增删改 |
| `default_hide_header` | bool | `false` | 新建规则时默认是否隐藏来源信息头 |
| `platform_name_map` | object | 见下方 | 自定义各平台在来源信息中的显示名称 |
| `header_template` | text | 见下方 | 来源信息头模板，支持变量替换 |
| `filter_mode` | string | `off` | 消息过滤模式：off/blacklist/whitelist |
| `filter_patterns` | template_list | `[]` | 全局过滤规则列表，每条一条规则（regex:xxx=正则，否则=关键词） |

### `rules` 每条规则包含

| 字段 | 类型 | 说明 |
|-----|------|------|
| `source_umo` | string | 消息来源会话标识 |
| `target_umo` | string | 消息目标会话标识 |
| `hide_header` | bool | 是否隐藏来源信息头 |
| `filter_mode` | string | 过滤模式（inherit=继承全局），支持 inherit/off/blacklist/whitelist |
| `filter_patterns` | list | 过滤规则列表（留空=继承全局），每项格式：`regex:表达式` 或 关键词 |

### `platform_name_map` 默认值
```json
{
   "default": "默认",
  "aiocqhttp": "QQ",
  "wechatpadpro": "微信",
  "telegram": "Telegram",
  "discord": "Discord"
}
```

### `header_template` 可用变量
- `{sender_name}` — 发送者昵称
- `{sender_id}` — 发送者 ID
- `{platform}` — 平台名称
- `{msg_type}` — 消息类型（群组/私聊/未知类型）
- `{conversation_id}` — 会话 ID

默认模板：
```
[转发] {sender_name} ({sender_id})
来自 {platform} 的 {msg_type}（ID: {conversation_id}）消息
```

---

## 💬 指令列表
| 指令           | 说明               |
|--------------|------------------|
| `mf add`     | 创建一则消息转发绑定的请求    |
| `mf bind`    | 接受一则消息转发绑定的请求    |
| `mf bindraw` | 直接创建转发绑定（例：#mf bindraw qq 654321 wx 123456） |
| `mf del`     | 删除一条转发规则         |
| `mf list`    | 列出与当前会话相关的所有转发规则（可用于获取当前群号） |
| `mf listall` | 列出所有转发规则         |
| `mf hide`      | 切换规则的来源信息显示状态     |
| `mf hidelist`    | 列出当前会话规则的来源信息状态 |
| `mf hidelistall` | 列出所有规则的来源信息状态   |
| `mf filter` | 查看当前过滤配置（模式+正则） |
| `mf help`    | 显示该插件帮助信息        |

### `mf bindraw` 平台简写映射
| 简写 | 完整平台名 | 对应平台 |
|------|-----------|---------|
| `df` | `default` | 默认 |
| `qq` | `aiocqhttp` | QQ |
| `wx` | `wechatpadpro` | 微信 |
| `tg` | `telegram` | Telegram |
| `dc` | `discord` | Discord |

> 省略平台时默认为 `default`。平台简写或群号后加 `s` 表示私聊。
>
> 用法示例：
> - `#mf bindraw qq 654321 wx 123456` → QQ 群 654321 → 微信 123456
> - `#mf bindraw 654321 wx 123456` → 默认群 654321 → 微信 123456（省略源平台）
> - `#mf bindraw s 123456 654321` → 默认私聊 123456 → 默认群 654321（省略源平台单独 s）
> - `#mf bindraw 654321s 123456` → 默认私聊 654321 → 默认群 123456（群号后加 s）
> - `#mf bindraw qq 114514 wxs 123456` → QQ 群 114514 → 微信私聊 123456（平台后加 s）

---

## 🧩 项目结构
项目结构示例：

```
astrbot/
└─ data/
   └─ plugins/
      └─ astrbot_plugin_msg_forward_cc/
         ├─ LICENSE
         ├─ logo.png
         ├─ _conf_schema.json
         ├─ main.py
         ├─ metadata.yaml
         ├─ README.md
         └─ requirements.txt
```

同时，插件会建立 `astrbot/data/plugin_data/msg_forward_cc` 目录以存储持久化数据：

```
astrbot/
└─ data/
   └─ plugin_data/
      └─ msg_forward_cc/
         └─ pending.json
```
---

## 📦 功能概念

### **1. 来源信息控制**

插件支持对转发规则设置来源信息显示状态：

- **允许显示**：显示来源信息头（默认）
- **禁止显示**：隐藏来源信息头，消息直接转发

**使用方法**：
```
#mf hide <规则ID>            # 切换规则的来源信息显示状态
#mf hidelist                 # 列出当前会话规则的来源信息状态
#mf hidelistall              # 列出所有规则的来源信息状态
```

`#mf list` 命令会显示每个规则的状态标记：
- 🔓 表示允许显示来源信息
- 🔒 表示禁止显示来源信息

### **2. UMO（Unified Message Origin）**
一个唯一标识会话的字符串，格式为 `平台名:消息类型:会话ID`。消息类型有两种：
- `GroupMessage` — 群聊
- `FriendMessage` — 私聊

示例：
```
aiocqhttp:GroupMessage:654321       # QQ 群 654321
wx:GroupMessage:123456              # 微信 123456
your_name:FriendMessage:114514      # 私聊 114514
```

UMO 能让插件知道“某条消息来自哪个平台的哪个会话”，确保转发到正确目标。

---

### **3. 转发规则（Rules）**

插件允许你创建规则（可通过 WebUI 配置或命令）：

```
由 mf add / mf bind 创建，或直接在 WebUI 配置页面添加。
```

即来自端点 A 的消息会自动同步并转发给端点 B。

每条规则包含：

| 字段 | 说明 |
|-----|------|
| `source_umo` | 消息来源会话标识 |
| `target_umo` | 消息目标会话标识 |
| `hide_header` | 是否隐藏来源信息头 |
| `filter_mode` | 过滤模式（inherit=继承全局） |
| `filter_patterns` | 过滤规则列表（留空=继承全局），每项 `regex:表达式` 或关键词 |

所有规则保存在 AstrBot 的插件配置中（通过 WebUI 编辑 `_conf_schema.json` 配置项）。

### **4. 消息过滤（Filter）**

支持**关键词**和**正则表达式**两种匹配方式，统一在 `filter_patterns` 中管理。分**全局**和**逐规则**两层：

- **全局**：顶层 `filter_mode` + `filter_patterns`（template_list 类型，逐条添加规则，`regex:xxx`=正则，否则=关键词），支持 JSON 批量导入导出
- **逐规则**：每条转发规则可覆写 `filter_mode`（`inherit`=继承全局）和 `filter_patterns`（空=继承全局），每项格式：`regex:正则表达式` 或 关键词

三种模式：
- **off**：不过滤（默认），所有匹配规则的消息都会转发
- **blacklist**：命中就**不转发**
- **whitelist**：命中才转发，未命中则跳过

> 全局用 WebUI 的 template_list 增删改，规则级在 WebUI 中填 JSON 数组字符串。通过 `#mf filter` 可查看当前配置。

### **5. 消息链（MessageChain）处理**

插件会在转发前自动构造一条带来源信息的消息链，例如：

```
[转发] 张三 (1919810)
a:GroupMessage:11451419 -> a:GroupMessage:14191981

​这是一条示例消息
```

文字、图片、表情等组件都会被完整复制到目标平台。

## 🔄 转发行为示例
假设你有两条 UMO：

- `aiocqhttp:GroupMessage:654321`
- `your_name:FriendMessage:114514`

创建规则后：
- `aiocqhttp` 群 `654321` 的消息，会自动转发至 `your_name` 的好友 `114514`
- 会自动加上消息链前的来源标注

## 🤝 贡献

欢迎提交：

- Bug 报告
- 新特性建议
- PR（支持适配更多平台、更多规则类型）

插件完全开源，希望它能够成为 AstrBot 最好用的“跨群桥接插件”。
