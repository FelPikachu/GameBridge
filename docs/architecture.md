# 架构基线

依赖方向固定为：Decky UI → Application API → Domain Core → Provider SDK → Platform Adapters。

Provider 不操作 UI；UI 不调用外部 CLI；所有命令以参数数组执行；安装任务每次状态变更都在
SQLite 事务中落盘。认证材料不会写入核心数据库，后续通过独立凭证存储适配器保存引用。

首个真实 Provider 将从 Epic/Legendary 开始，但在启用账号登录和下载前，必须完成固定版本、
来源验证、结构化输出适配、Token 脱敏以及中断恢复测试。

## 游戏启动修饰器分层

启动修饰器不得作为一串 shell wrapper 无区别传递，而应按生命周期分层：

```text
Steam
  → LSFG（外层环境/Vulkan 层）
  → GameBridge 启动器
  → Legendary / UMU
  → Framegen（已知真实游戏目录后执行）
  → 游戏
```

LSFG 需保留 `LSFG_PROCESS`、用户 `XDG_CONFIG_HOME` 与 `XDG_DATA_HOME`；GameBridge 即使为
Legendary 隔离 `HOME` 也不得隐藏用户的 Vulkan implicit layer 目录。Framegen 的安装器和
卸载器必须在 `STEAM_COMPAT_INSTALL_PATH` 和真实 EXE 可用后执行。

“恢复启动选项”是用户在 Decky 插件改写启动项后触发的标准化步骤，不承诺在 GameBridge
启动之前已被 shell 解析破坏的任意字符串能够自动自救。

## 稳定 Steam 卡片生命周期

支持公开目录的 Provider 应优先在安装前创建持久 Steam 卡片，并在游戏状态变化时原地
更新同一快捷方式，不得把“下载完成”实现为删除旧卡片后重新创建。只要 Steam 接口允许，
卡片应在等待安装、安装中、已安装、更新、存储离线和卸载后等待重装等状态之间保持同一
App ID，以保留素材、收藏、分类、控制器布局、游戏时间和 CompatData 关联。

卡片生命周期可以跨 Provider 统一，但安装后的启动目标不能强制统一为裸 EXE。Provider
必须返回结构化启动方案，Application API 校验后由 Steam 适配层原地应用：能够独立完成
正版授权且已经实机验证的游戏可以直接指向真实可执行文件；需要平台授权、云存档、临时
参数或第三方账号关联的游戏继续指向 GameBridge、外部 CLI 或官方启动器。Epic 默认保留
Legendary 启动链；不得把米哈游国服《原神》的直启结论扩展到其他平台或未验证游戏。

转换前必须重新读取 Provider 安装状态，避免详情页缓存旧 App ID、旧 EXE 或旧启动参数。
转换失败、兼容层缺失、存储未挂载或文件系统不安全时保留原卡片并回退到安装/官方客户端
入口，不删除游戏目录、Prefix、认证状态或用户已有快捷方式。
