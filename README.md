# 我要涩涩增强版 (v2.2.0)

一个对接 AstrBot 的涩图插件，基于 [Lolicon API](https://api.lolicon.app) 获取随机涩图，支持数量参数、标签搜索、本地缓存池、图片压缩、多数据源降级，全部参数 WebUI 可视化配置。

> **衍生声明**：本仓库 fork 自 [ttq7/astrbot_plugin_Lolicon](https://github.com/ttq7/astrbot_plugin_Lolicon)（作者 hello七七），在原版基础上重写了缓存机制并增加了 WebUI 可视化配置。部分增强功能（数量解析、速率限制、图片压缩、多源降级）参考了 [FlanChanXwO/astrbot_plugin_setu](https://github.com/FlanChanXwO/astrbot_plugin_setu) 的实现。感谢原作者与社区的工作。

## 功能特点

- **数量参数**：支持"来三份"中文数字解析，`max_count` 配置单次上限
- **标签搜索 + 别名**：从消息提取标签，`tag_alias` 配置中文→API 标签映射（如 `白丝=white_pantyhose`）
- **PID 回显**：`show_pid` 开启后在回复文字后附带图片 Pixiv PID，多图逗号分隔，方便按图索源
- **触发词匹配可配置**：完整短语 / 子串 / 正则三种模式
- **本地图片缓存池**：预下载图片，请求时直接取缓存，响应快且降低 API 压力
- **多数据源 + 降级**：lolicon / nyan / all（多源故障转移，按 failover/random/round_robin 策略）
- **图片压缩**：超阈值自动压缩（质量+尺寸阶梯），防 OneBot 单帧超限，Pillow 软依赖
- **速率限制**：按用户加锁防并发刷，TTL 防泄漏
- 支持 R18 / 非 R18 / 混合（仅 lolicon 数据源）
- 自动清理已发送图片缓存
- 全部参数 WebUI 可视化配置，无需改代码
- 图片存储路径规范化（AstrBot 官方 data 目录）

## 安装方法

将本仓库克隆到 AstrBot 的 `plugins` 目录下，重启机器人即可自动加载：

```bash
cd AstrBot/plugins
git clone https://github.com/lolicin/astrbot_plugin_lolicon
```

或在 AstrBot 插件市场搜索安装。

## 配置

插件目录下的 `_conf_schema.json` 定义了所有配置项。AstrBot 会自动在 **WebUI → 插件管理** 中生成可视化配置面板（枚举字段为下拉选项），直接编辑即可，无需修改代码。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `match_mode` | string | `contains` | 触发词匹配方式：`exact`/`contains`/`regex` |
| `trigger_words` | list | `["色图","涩图","瑟图"]` | 触发词列表，触发后解析数量与标签 |
| `reply_style` | string | `plain` | 回复风格：`plain`/`playful` |
| `r18` | int | `0` | 0=非R18 / 1=仅R18 / 2=混合（仅 lolicon） |
| `data_source` | string | `lolicon` | `lolicon`/`nyan`/`all`（多源降级） |
| `provider_strategy` | string | `failover` | 多源降级策略：`failover`/`random`/`round_robin`（仅 all） |
| `exclude_ai` | bool | `true` | 排除 AI 生成图（仅 lolicon） |
| `aspect_ratio` | string | `gt1` | 宽高比：留空/`gt1`/`lt1`/`eq1`（仅 lolicon） |
| `max_count` | int | `5` | 单次最大图片数（1-10），支持中文数字 |
| `multi_tag_mode` | string | `ignore_tag` | 多图带标签时：`ignore_tag`(忽略标签走缓存)/`fetch_by_tag`(按标签实时下载) |
| `max_image_bytes` | int | `10485760` | 图片压缩字节阈值，超过则压缩；0=不压缩；默认 10MB |
| `tag_alias` | text | `""` | 标签别名映射，逗号或换行分隔，格式 `白丝=white_pantyhose` |
| `show_pid` | bool | `false` | 回复文字末尾附带图片 PID，多图英文逗号分隔（仅 lolicon 源有 PID） |
| `pid_label` | string | `"PID: "` | PID 列表前的提示文字，可留空只显示数字（仅 `show_pid` 开启时生效） |
| `pid_newline` | bool | `true` | PID 是否另起一行，`false` 则与回复文字同行（仅 `show_pid` 开启时生效） |
| `cache_size` | int | `10` | 缓存池目标数量 |
| `refill_threshold` | int | `5` | 触发补充的阈值 |
| `refill_interval` | int | `300` | 后台巡检间隔（秒） |

## 使用说明

发送包含触发词的消息即可，默认触发词为 `色图` / `涩图` / `瑟图`（子串匹配）。可在消息中带数量和标签：

```
用户：来一份色图
机器人：[图片] 给你涩图~ x1

用户：来三份色图
机器人：[图片][图片][图片] 给你涩图~ x3

用户：白丝涩图
机器人：[图片] 给你涩图~ x1

用户：来三份白丝涩图
机器人：[图片][图片][图片] 多图已忽略标签，走本地缓存（更快）
（multi_tag_mode=ignore_tag 时，默认）

用户：来三份白丝涩图
机器人：[图片][图片][图片] 给你涩图~ x3
（multi_tag_mode=fetch_by_tag 时）

用户：来9份萝莉色图
机器人：一次最多 5 张哦

用户：色图
机器人：[图片] 给你涩图~ x1
```

开启 `show_pid` 后（默认 `pid_newline=true`，PID 换行显示）：

```
用户：色图
机器人：[图片] 给你涩图~ x1
        PID: 123456789

用户：来三份色图
机器人：[图片][图片][图片] 给你涩图~ x3
        PID: 111111,222222,333333
```

- 数量支持中文数字（一二三...十、十三）和阿拉伯数字，量词支持 份/个/张/片
- 标签按空格/逗号/顿号分隔，经 `tag_alias` 别名映射后传给 Lolicon API
- **多图带标签的处理**（`multi_tag_mode` 配置）：`ignore_tag`=忽略标签走本地缓存（快，图可能不符标签）；`fetch_by_tag`=按标签实时下载多张（慢，图符合标签）。默认 `ignore_tag`
- 无标签时从本地缓存取（快，支持多图）；单图带标签时实时按标签下载（精准）
- **PID 回显**（`show_pid`）：PID 从缓存文件名 `{pid}_p{页码}.{扩展名}` 解析，仅 lolicon 数据源带 PID；`nyan` 源返回的是图片二进制、文件名为随机串，无 PID 可显示，此时回复不会附加任何 PID 文本。同一作品的多页（p0/p1）PID 相同，会自动去重；仅统计**实际发送成功**的图片。可用 `https://www.pixiv.net/artworks/<PID>` 查看原作
- 若想还原原版"必须完整输入 我要涩涩 才触发"的行为：`match_mode=exact`、`trigger_words=["我要色色","我要色图","我要涩涩"]`、`reply_style=playful`

## 配置要求

- Python 3.8+
- 依赖：`aiohttp`、`aiofiles`
- **可选依赖**：`Pillow`（图片压缩功能需要，未安装则原样发送不影响使用）：`pip install Pillow`
- 需要连接互联网访问数据源 API

## 注意事项

- 请遵守相关法律法规，禁止传播非法内容
- Lolicon API 存在请求频率限制（约 5 秒 / 次），缓存池机制可有效规避
- 图片缓存在发送后自动删除，存储于 AstrBot data 目录的 `plugin_data/astrbot_plugin_lolicon/imgs`
- `data_source=all` 时若 lolicon 失败会自动降级到 nyan，反之亦然
- 部分图片可能因网络问题加载失败

## License

MIT，见 [LICENSE](LICENSE)。衍生自 ttq7/astrbot_plugin_Lolicon（原作者 hello七七）。

## 贡献

欢迎提交 Pull Request。建议提交前先创建 Issue 讨论新特性。
