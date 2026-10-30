# config_store —— 配置持久化（L1 基础服务）

**作用**：运行态参数（音量等）的 NVS 读写唯一入口；编译期参数（WiFi / WS / device_id / token）走 Kconfig，由使用方直接读 `CONFIG_AI_*`，本组件不包一层。

## 对外接口

| 接口 | 说明 |
|---|---|
| `cfg_init()` | 独占 `nvs_flash_init()`（分区写满/版本不兼容时擦除重建）+ 打开命名空间 `cfg`；重复调用返回 `ESP_ERR_INVALID_STATE` |
| `cfg_get_int(key, &out)` | 读 int32；未设置 → `ESP_ERR_NVS_NOT_FOUND`（由调用方决定缺省） |
| `cfg_get_int_or(key, fallback, &out)` | 同上，未设置回退 fallback 且返回 `ESP_OK` |
| `cfg_set_int(key, value)` | 写 int32 + commit |
| `cfg_get_str(key, buf, len)` | 读字符串（含 `\0`）；缓冲不足 → `ESP_ERR_NVS_INVALID_LENGTH` |
| `cfg_set_str(key, value)` | 写字符串 + commit（日志只打长度，不打内容） |
| `cfg_erase_key(key)` | 删除键（恢复缺省）；未设置 → `ESP_ERR_NVS_NOT_FOUND` |
| `cfg_get_volume(&out)` | 读音量 0~100；未设置用 `CONFIG_CFG_VOLUME_DEFAULT`；越界值 clamp + WARN |
| `cfg_set_volume(volume)` | 写音量：先 clamp 0~100 + WARN 再落盘 |
| `cfg_get_usage(&used, &free)` | NVS 分区条目统计（供 `diag` 的 `cfg info`） |

## 持久化契约

- 命名空间：`"cfg"`；当前键：`volume`（int32，0~100）。
- 键名/命名空间属于持久化契约，**改名等于让老设备配置消失**，改动需评估迁移；新增键（如 T3.4 的 `muted`）在此表登记。
- 初始化顺序：`cfg_init()` 必须在任何使用方之前，且全工程只调用一次（`nvs_flash_init` 由本组件独占，其它模块不得再调）。

## 依赖

- IDF：`nvs_flash`（`nvs.h` 由该组件提供）。
- 组件：无（L1 只依赖 IDF；不得反向依赖 L2/L3，也不依赖 `diag`）。

## Kconfig

| 配置 | 默认 | 说明 |
|---|---|---|
| `CONFIG_CFG_VOLUME_DEFAULT` | 60 | NVS 无 `volume` 键时的缺省音量；须与 ARCHITECTURE §3.5 一致 |

## 关键约束

- **禁止在音频实时任务里调用**：NVS 写会触发 Flash 操作并阻塞（CONVENTIONS §3）；音量变更只在 console/会话任务侧调用。
- 不加锁：NVS 自身线程安全；语义上"先读后写"的多步操作若将来出现，需要调用方自己保证顺序。
- 写失败必须返回错误码 + ERROR 日志，不允许静默降级。
- 分区不可用（`ESP_ERR_NVS_NOT_INITIALIZED`/`NO_FREE_PAGES` 等）由 `cfg_init()` 处理并返回，`app_main` 用 `ESP_ERROR_CHECK` 暴露；**擦除重建是唯一允许丢配置的路径，且必打 WARN**。

## 调试

`diag` 提供命令（底层调试口，`cfg set` **不做**范围/语义校验，重启才生效）：

```
cfg info                          # NVS 条目用量 + 当前音量
cfg get <key>                     # int 优先，其次 str；未设置明确提示
cfg set <key> <int>               # 写 int（不改业务状态）
cfg setstr <key> <value>          # 写字符串
cfg erase <key>                   # 删除键，恢复缺省
```

> 运行中调音量需要 `audio vol`（ARCHITECTURE §10.2，T5.1 实现）；本任务只保证"持久化 + 启动时应用"。
>
> `cfg get <key>` 与 `cfg set <key>` 都是**存储层语义**：`get` 返回 NVS 原始值（手工写入的 150 会原样显示），
> `set` 不做范围/语义校验；clamp 只发生在 `cfg_get_volume()` / `cfg info` 这一层（验证读取侧兜底用）。

## 后续

- T3.3 音量体系：与 `audio_player` 的默认音量 Kconfig 合并，音量变更实时保存；
- T3.4：新增 `muted` 键（长按静音状态持久化）；
- 若出现配置项增多：评估键名前缀/版本号，避免 NVS 键散乱。
