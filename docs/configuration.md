# 配置与运行参考

每个项目独立保存 `.skillorbit.json`。所有扫描根与输出路径都必须位于这个项目中；使用全局 `--project PATH` 切换项目。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `version` | `1` | 配置格式版本。 |
| `roots` | `[".agents/skills"]` | 一个或多个相对扫描根；递归查找准确命名的 `SKILL.md`。 |
| `output` | `"SKILLS.md"` | Markdown 输出的相对路径。 |
| `interval` | `2` | 检查间隔，单位秒，最少 0.2。 |
| `project_rules` | `[]` | 按顺序匹配路径的归属规则，首条命中生效。 |
| `descriptions` | `{}` | 按技能名设置描述覆盖；支持固定字符串或源描述哈希绑定的导入简介。 |

`match` 使用 Python `fnmatchcase` 的 glob 语义，路径统一为 `/`，`*` 可跨目录。规则示例：

```json
{"match": ".agents/skills/skills-codex*/*", "project": "ARIS"}
```

归属优先级：配置规则 → skill 的 `project` → `metadata.project` → `metadata.upstream_suite` → 最近的 `.git/config` 中的 origin → 未归属。读取本地 Git 配置不需要 Git 命令或联网；Git worktree 的 `.git` 指针不自动解析，可用显式规则指定归属。

技能主键是 `[项目, 名称]`。同主键的多个路径被合并，以目录层级最短、路径排序最先的文件作为主要描述来源。不同项目的同名技能保持独立，写备注时用 `--owner` 消除歧义。

## 描述更新

默认从 YAML `description` 提取首句，保留原文语言，最多 180 个字符。文本概括采用确定性规则，不调用模型。需要经过编辑的专业简介时，添加固定覆盖：

```json
"descriptions": {"paper-reader": "将论文整理为来源可追溯的中文精读稿。"}
```

`--import-md` 会保存 `{ "text": "旧简介", "source_hash": "..." }` 形式的覆盖。源描述变化后停止使用这份旧简介，下一轮自动显示新的源描述首句。固定字符串覆盖会一直使用，需要手动更新。

## 备注与删除

可以直接编辑备注列，也可以运行 `skillorbit note NAME TEXT`。管道符用 `\|` 或 `&#124;`，换行使用 `<br>`。同步时读取表格备注，名称或项目列的手动改动会在下一次同步中被扫描结果覆盖。

移除 skill 时，表格删除对应行，状态文件保留备注。相同主键恢复时备注恢复。更换名称或归属会形成新主键；工具不会猜测它与旧主键的关系。

不要删除或复制生成区标记。表格结构损坏时应先恢复正确的四列表格。重新生成目录的安全办法是保留 `.skillorbit/state.json`，将旧 Markdown 改名，然后运行 `sync`。

## 扫描与运行边界

- 读取 YAML 使用 `safe_load`；skill 正文作为数据读取，不执行正文里的命令。
- 文件缺少有效名称、描述或 YAML 时，整轮同步失败并保留成功版本。
- 扫描根不存在代表该目录已移除；对应技能从当前目录消失。
- 只跟随项目内部的目录链接；循环链接按真实路径去重，外部链接跳过。
- 忽略 `.git`、`node_modules`、`__pycache__`、`.venv` 和 `.skillorbit`。
- `sync --check` 不更新目录、备注或清单状态；仅可能创建用于协调进程的锁文件。
- 一轮扫描发现文件变化后进行完整解析。后台检查使用路径、文件大小和纳秒修改时间；若外部程序刻意保留这些值，请执行 `sync` 强制读取内容。
- `.skillorbit/watch.log` 仅记录状态与错误路径，可在停止进程后自行轮换；变更历史只保留最近 100 条。

## 后台与登录自启

`start` 使用当前 Python 环境创建脱离终端的本地进程。`stop` 用项目内的停止请求让进程自行退出，避免按过期 PID 杀死其他进程。`status` 检查进程和心跳。

macOS 的 `service install` 将当前项目专属 plist 放入 `~/Library/LaunchAgents/io.skillorbit.<项目哈希>.plist`，通过用户级 launchd 启动。其他系统使用 `start` 或把 `watch` 集成进已有的用户服务。登录服务绑定解释器与项目绝对路径，升级或迁移后重新安装。

## 常见问题

| 现象 | 处理 |
|---|---|
| “Output already exists without managed markers” | 使用 `init --import-md` 迁入旧清单，或选择其他输出路径。 |
| “Already initialized” | 编辑配置并执行 `sync`；无需再次 `init`。 |
| 新 skill 显示“未归属” | 为路径添加 `project_rules`，或在 skill 元数据写项目名。 |
| 中文旧简介变为英文 | 原 skill 描述已更新，哈希绑定简介失效；可设置固定中文覆盖。 |
| 日志显示 YAML 错误 | 修复对应文件，监控会自动恢复。 |
| 搬迁项目后不自启 | 在新路径和有效的虚拟环境中重新安装登录服务。 |
| 停止服务后下次登录又启动 | 运行 `service uninstall` 移除登录自启。 |
