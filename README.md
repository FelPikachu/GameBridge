# GameBridge

GameBridge 是一款面向 Steam Deck / SteamOS 的非官方 Decky Loader 插件，用于将第三方平台游戏集中添加到 Steam 游戏库，并提供安装、启动、封面和兼容性管理。

> 当前版本：`v0.19.0-beta.3`。这是公开测试版，部分游戏和 SteamOS 更新后的行为仍需实机验证。

## 当前支持

### Epic Games

- 通过官方 Epic 登录流程连接账号
- 同步、安装、更新和卸载游戏
- 创建并维护 Steam 非 Steam 游戏卡片
- 使用 UMU / Proton 兼容环境启动游戏
- 对 Epic 官方支持的游戏自动下载与上传云存档，冲突时停止覆盖并保留本地备份
- 管理 SteamGridDB 封面素材
- 记录、导出和导入游戏时长及最后运行日期
- 提供小黄鸭和 Decky Framegen 启动预设

### 米哈游 / HoYoPlay

- 米哈游国服、B 服和 HoYoPlay 国际服区域选择
- 发现官方启动器和已安装游戏
- 使用稳定的 Steam 卡片在不同区域间切换
- 在官方启动器中管理或卸载游戏
- 恢复 GameBridge 启动选项
- 提供小黄鸭、FSR4 和组合启动预设
- 记录、导出和导入游戏时长及最后运行日期

并非所有游戏都已通过实机验证。GameBridge 不会绕过 DRM、账号登录、地区限制或反作弊。

## 安装

1. 在 Steam Deck 上安装并启用 [Decky Loader](https://decky.xyz/)。
2. 从 [Releases](https://github.com/FelPikachu/GameBridge/releases) 下载最新的 GameBridge ZIP。
3. 在 Decky Loader 的开发者设置中选择从 ZIP 安装插件。
4. 安装完成后重新加载 Decky Loader。
5. 打开 GameBridge，根据界面提示安装或连接所需平台。

测试版建议先备份重要的游戏启动选项和 GameBridge 游戏记录。

## 卸载与数据

默认执行“卸载前清理”时，GameBridge 会清理其创建的 Steam 卡片、素材、兼容数据和缓存，但保留游戏文件及米哈游官方启动器。

只有主动勾选危险选项后，清理流程才会删除经过验证、位于 GameBridge 管理范围内的游戏和启动器数据。执行前请仔细阅读确认窗口。

## 问题反馈

请通过 [GitHub Issues](https://github.com/FelPikachu/GameBridge/issues) 提交问题，并尽量提供：

- GameBridge 和 SteamOS 版本
- 游戏名称、平台和区域
- 可复现步骤
- 截图、录像或相关日志

公开日志前请删除账号、Token、Cookie、授权码和本机私人路径等敏感信息。

## 本地开发

需要 Python 3.11+、Node.js 和 pnpm。

```bash
pnpm install
pnpm typecheck
pnpm build
python3 -m pytest
```

Decky 分发包必须以 `GameBridge/` 为顶层目录，并包含 `dist/index.js`、`main.py`、`plugin.json`、`package.json`、`gamebridge/` 和许可证文件。

## 声明与许可

GameBridge 是独立开发的非官方项目，与 Valve、Epic Games、米哈游及其他平台方或游戏发行商不存在隶属、授权或背书关系。所有商标、游戏名称和品牌资源归各自权利人所有。

项目源码依据 [Mozilla Public License 2.0](LICENSE) 发布。
