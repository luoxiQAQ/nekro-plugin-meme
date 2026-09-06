# nekro-plugin-meme（meme 表情包工作坊）

Nekro Agent 表情包插件，移植自 Yunzai 的 meme-plugin（admilkjs 改版 + 自定义修改），
对接 [MemeCrafters/meme-generator](https://github.com/MemeCrafters/meme-generator) API 服务，
提供 800+ 表情包模板（petpet / 摸头 / 贴贴 / 亲亲 / 举牌……），支持指令触发、事件触发与 AI 主动使用。

## 功能一览

- **`bq关键词` 指令**：与 Yunzai meme-plugin（forceSharp=true）用法一致，支持 @头像 / 图片 / 文字 / 引用回复 / `#参数名 参数值`
- **随机表情包**：聊天命令（原版 random.js），随机挑模板用你的头像生成并展示指令
- **戳一戳随机表情教学**：戳一下机器人，按概率用它生成一张表情包并教你对应用法（原版 poke.js + pokeProbability）
- **表情回应**：关键词触发时给消息加表情回应 66（可关闭）
- **AI 主动使用**：通过提示词注入引导 Agent 在聊天中主动生成表情包（如想抱对方时自己发 hug）
- **Agent 沙盒方法**：搜索模板 / 查看详情 / 生成 / 随机，供 AI 按需调用

## 安装

### 方式一：WebUI 插件市场

在 Nekro WebUI「插件」页面选择「从 Git 仓库安装」，填入本仓库地址：

```
https://github.com/luoxiQAQ/nekro-plugin-meme
```

### 方式二：手动安装

```bash
cd ${NEKRO_DATA_DIR}/plugins/packages
git clone https://github.com/luoxiQAQ/nekro-plugin-meme.git nekro_plugin_meme
```

### 方式三：内置插件挂载（进阶）

把仓库放到数据目录后，用 docker-compose 只读挂载进容器（容器重建 / 镜像更新都不丢，WebUI 中显示为 [内置]）：

```yaml
# docker-compose.yml -> nekro_agent.volumes
- ${NEKRO_DATA_DIR}/patches/nekro_plugin_meme:/app/plugins/builtin/nekro_plugin_meme:ro
```

## 部署依赖：meme-generator API

```bash
docker run -d --name meme-generator -p 2233:2233 ghcr.io/memecrafters/meme-generator:latest
```

首次启动会自动下载表情包资源。插件默认通过 docker bridge 网关访问 `http://172.17.0.1:2233`；
若 meme-generator 容器加入了 nekro 的 docker 网络，也可在插件配置里改成 `http://meme-generator:2233`。

## 触发方式

**1. 关键词指令（默认 `bq` 前缀）**

```
bq摸头 @某人            # 取被@用户头像生成
bq摸头                  # 不@则用自己头像
bq贴 @A @B              # 多头像模板（双人模板需要两张头像）
bq举牌 文字1/文字2      # 文字模板，多条文字用 / 分隔
bq举牌 #text 晚安       # 附加参数（#参数名 参数值，与原版语法一致）
回复某人的消息 + bq拍头  # 用被回复者头像
bq贴贴 123456           # 直接发 QQ 号也行
随机表情包              # 随机表情命令，无需前缀
```

关键词是各模板的中文别名，可以直接问 AI（「有哪些摸头类的表情包」）。

**2. 戳一戳**：戳一下机器人，按 `POKE_PROBABILITY`（默认 0.7）概率触发。

**3. 自然语言**：@机器人说「给这位来一张 petpet」；开启 `PROMPT_GUIDE_ENABLE` 后，
AI 也会在聊天中想抱抱 / 贴贴时主动发表情包。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `MEME_API_BASE` | `http://172.17.0.1:2233` | meme-generator API 地址 |
| `REQUEST_TIMEOUT` | 90 | 请求超时（秒） |
| `AVATAR_SIZE` | 640 | QQ 头像尺寸（40/100/140/640） |
| `DEFAULT_USER_NAME` | 用户 | 模板 user_infos 默认用户名 |
| `COMMAND_ENABLE` | 开 | bq 关键词指令开关 |
| `COMMAND_FORCE_PREFIX` | `bq` | 指令前缀；留空则裸关键词触发（易误触） |
| `REACTION_ENABLE` | 关 | 触发时表情回应 66 |
| `POKE_ENABLE` | 开 | 戳一戳随机表情 |
| `POKE_PROBABILITY` | 0.7 | 戳一戳触发概率（0~1） |
| `PROMPT_GUIDE_ENABLE` | 开 | AI 主动表情包引导提示词注入 |

## Agent 沙盒方法

| 方法 | 说明 |
| --- | --- |
| `search_meme(keyword, limit)` | 按关键词搜索模板（中文别名模糊匹配） |
| `get_meme_info(key)` | 查看模板图片/文字数量要求与附加参数 |
| `generate_meme(key, user_ids, image_paths, texts, ...)` | 生成表情包，`user_ids` 支持 `"me"` 代表机器人自己 |
| `random_meme(user_ids)` | 随机选一个头像模板生成 |

## 致谢

- [ClarityJS/meme-plugin](https://github.com/ClarityJS/meme-plugin) 及 [admilkjs/meme-plugin](https://github.com/admilkjs/meme-plugin)（Yunzai 原插件与改版）
- [MemeCrafters/meme-generator](https://github.com/MemeCrafters/meme-generator)（表情包生成服务）

## License

[MIT](LICENSE)
