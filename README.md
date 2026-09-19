# ⚡ WB Switch

WorkBuddy 供应商管理器 + 用量监控。解决 **WorkBuddy 更新后第三方模型容易丢失** 的问题——把供应商配置独立保存为预设，一键恢复、一键切换，并把模型用量（token、缓存命中率、按官方价的费用）统计清楚。

> 样式仿 [cc-switch](https://github.com/farion1231/cc-switch)：侧边栏 + 供应商卡片 + 启用按钮 + 用量多维筛选。

![面板预览](https://img.shields.io/badge/面板-127.0.0.1:5276-blue) ![平台](https://img.shields.io/badge/平台-Windows%20%7C%20macOS-lightgrey) ![依赖](https://img.shields.io/badge/依赖-零（仅标准库）-green)

---

## ✨ 功能

### 🔌 供应商管理
- **预设保存 / 一键切换**：多套供应商配置（方舟、第三方中转、自建……）保存为卡片，点「启用」即写入 WorkBuddy。
- **四层配置一次写对**：切换时同时写 `models.json` / `settings.json` / 偏好文件 / 会话数据库，新会话默认模型直接生效（带 `custom-local:` 运行时前缀，不会回落官方"快速"档）。
- **「所有模型」开关**：把全部供应商的模型同时注入 WorkBuddy 模型列表，不用来回切换。
  - ⚠️ WorkBuddy 按**模型 ID** 识别模型：不同供应商用相同 ID（如多家都用 `glm-5.3-flash`）时，同名条目会互相覆盖，只有排最前的（当前供应商）生效。想多家独立可用，给每家选不同模型 ID。
- **从供应商拉取模型清单**：填 API 地址和 Key 后可一键加载该供应商的全部模型（如 OpenAI 兼容中转通常有上百个）。

### 🛡️ 守护模式
- 每 2 秒巡检 `models.json`，丢失 / 损坏自动从快照恢复（2 秒内）。
- 启动器每次运行自动接管端口，永远加载最新代码。

### 📊 用量统计（1:1 复刻 CC Switch 风格）
- **四维筛选**：时间 / 类别（官方·积分 / 第三方·Token）/ 供应商 / 模型，全部图表联动。
- **缓存命中率**：总命中率 + 每日趋势折线 + 供应商命中率汇总表（≥60% 绿 / ≥30% 橙 / 以下红）——命中率越高越省钱。
- **费用按官方价计**：内置 201 个模型的价格表（`model_pricing.json`），输入 / 输出 / 缓存命中三档分开计价。
  - 国内模型（GLM / 豆包 / Qwen / Kimi / MiniMax / DeepSeek…）显示 **¥**
  - 国外模型（GPT / Claude / Gemini / Grok…）显示 **$**
  - 官方模型按 **credits** 显示
- **总用量**：≥1万 显示 `x.x万`，≥1亿 显示 `x.x亿`。
- 请求日志（最近 200 条）、会话明细、模型/供应商分布图、汇总战报（可一键复制文本）。

### 🚀 WorkBuddy 控制
- 面板内一键启动 / 强制重启 WorkBuddy，实时显示运行状态。
- 切换配置后提示重启，重启脚本自动对齐四层配置。

### ❓ 内置使用说明
右上角「? 使用说明」弹窗：软件用途、每页用法、指标解释、免责声明。功能更新时同步更新。

---

## 🚀 快速开始

**要求**：Python 3.8+（零第三方依赖），本机装有 WorkBuddy。

### Windows
1. 把整个 `wb-switch` 文件夹放到任意位置（如 `~/.workbuddy/wb-switch/`）。
2. 双击 **`WB Switch.bat`** —— 自动接管端口、启动面板、打开浏览器。
   - 也可把 bat 发送到桌面快捷方式。

### macOS
1. 把整个 `wb-switch` 文件夹放到任意位置。
2. 终端运行（或双击）：
   ```bash
   cd wb-switch
   chmod +x "WB Switch.command"   # 首次需要
   ./WB\ Switch.command
   ```
   - 若被 Gatekeeper 拦截：`xattr -d com.apple.quarantine "WB Switch.command"`
3. 面板地址 [http://127.0.0.1:5276](http://127.0.0.1:5276)，日志在同目录 `wb-switch-panel.log`。

---

## 📁 文件说明

| 文件 | 作用 |
|---|---|
| `wb_switch.py` | 主程序（单文件，含 Web UI） |
| `model_pricing.json` | 模型官方价格表（201 个模型，每百万 token 单价） |
| `WB Switch.bat` | Windows 启动器 |
| `WB Switch.command` | macOS 启动器 |

运行时产生的数据都在 `~/.workbuddy/wb-switch/`（预设、快照备份、状态、日志），删除该目录即完全卸载。

---

## ⚠️ 已知限制

- 同名模型 ID 覆盖：WorkBuddy 机制限制（见上文「所有模型」开关说明）。
- 费用为按公开官方价的**估算值**，实际以各供应商账单为准。
- 火山方舟 Agent Plan 无动态模型清单接口，模型列表从官方文档页实时抓取（缓存 6 小时）。

---

## 📄 免责声明

本工具为第三方开源辅助工具，与 WorkBuddy 官方无关。它只读写本机 WorkBuddy 配置文件与本地数据库（统计只读，**不上传任何数据**）。使用本工具产生的一切后果由使用者自行承担。

---

## License

MIT
