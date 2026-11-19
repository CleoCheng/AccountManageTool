# 🔐 KeyVault · 全平台账号与凭据管家 (KeyVault Account & Credential Vault)

一款轻量、100% 本地离线存储、支持 **ChatGPT、Google、WorkBuddy、Qcoder** 及自定义平台的全能多账号凭据管理与防关联指纹工作台。

> [!WARNING]
> **使用前请阅读文末「⚠️ 免责声明」。** 本项目仅供学习研究及管理您本人合法拥有的账号。

---

## ✨ 核心特性

- 📦 **快递地址式智能识别导入**
  - 支持直接粘贴 `----` 或 `|` 分隔格式（如 `账号----密码----2FA秘钥` 或 `账号|密码|2FA`）、6 段式 Google 或带购买/停用日期与 `新2FA` 的多行块。
  - 自动修复跨行断行文本，支持单条实时拆解回填与多条一键批量导入。
- 🔑 **原生 TOTP (2FA) 动态验证码 & 自动清洗**
  - 内置离线 RFC 6238 TOTP 引擎，粘贴或输入 2FA 秘钥时**自动去除空格/连字符并转大写**。
  - 仅在 30 秒整点边界计算验证码，倒计时由前端时间戳驱动，后台静置几乎不占 CPU。
- 🛡️ **安全状态标记 & 生命周期统计**
  - 独立标记 **「密码已改 / 初始密码」** 与 **「2FA已改 / 初始2FA」**，支持一键切换及保留初始 2FA 秘钥备份。
  - 根据购买日期与停用日期自动计算账号状态（`使用中` / `已停用`）与累计使用天数。
- 🌐 **三引擎账号专属浏览器环境与 Google 纯原生守护**
  - 每个账号使用独立持久化的浏览器配置目录（`data/browsers/acc_<id>`）。
  - 支持三种启动引擎切换：内置扩展模式（`inpage_edge` / Edge）、Chrome 无扩展隐身注入（`inpage_chrome_cdp`）、以及 **Pure C++ 纯原生模式（`pure_native`）**。
  - **Google 账号强制纯原生保护**：始终以本机官方 Chrome 为首选（禁用 `--load-extension` / `--remote-debugging-port` 与 JS 注入），启动时静默清理历史扩展残留并保留已认证 `SID` Cookie，通过外部 WinForms 紫盾领航栏（HUD）与硬件扫描码粘贴提供账号/密码/2FA 填写与安全核对闭环。
- 🔑 **外部紫盾领航栏（HUD）& 登录工具**
  - 从账号的「登录工具」查看动态验证码，或复制账号、密码和动态验证码。Windows 下从 KeyVault 启动账号浏览器后，WinForms 紫盾领航栏会停靠在对应前台浏览器内部顶部（支持全屏自适应、收起为迷你胶囊、极速改密/2FA 浮窗与常见维护直链）。关闭工具条后，可在 KeyVault 主窗口点击「贴到浏览器」重新打开。
  - 在 Google 账号的「养护」里记录本人核对的结果，支持 RT 4 步探测、Antigravity 客户端注入、本地 Loopback OAuth 2.0 + PKCE 授权，以及凭据变更在 Google 生效后确认归档；另支持 Windows Named Pipe 外链接管（仅拦截认证/验证链接，普通网页透传回原生浏览器）。
- 🔀 **独立 Sidecar Mihomo 代理与 16 槽位节点隔离**
  - **Clash / Mihomo 生态**：Clash Verge Rev、Clash Verge、Mihomo Party、Clash Nyanpasu、FlClash、Clash for Windows、Mihomo / Clash Meta（支持从订阅 YAML 与 `proxy_provider` 读取节点，自动拉起独立 `KeyVault` Mihomo 内核分配 `kv-slot-0..15` 独立端口，**零修改本地 Clash Verge 运行态**）。
  - **其他客户端**：v2rayN、Sing-box、NekoRay、Shadowsocks 等常见本地端口及系统代理自动探测；也可手动输入任意 `http://` / `socks5://` / `host:port:user:pass` 代理地址。
  - 可为单个账号**锁定专属代理端口与节点**，未解锁前禁止变更；支持启动前出口 IP 归属地与时区校验。

---

## 💻 平台支持

| 平台 | 状态 |
|---|---|
| **Windows 10 / 11** | ✅ 主要开发与测试平台（含 `KeyVault.exe` 一键启动器） |
| macOS / Linux | ⚠️ 未充分测试。核心代码做了跨平台路径适配，但指纹 User-Agent 目前固定为 Windows，与真实系统不一致 |

---

## 🚀 快速开始

### 1. 环境要求
- **Python** `3.10+`
- **浏览器**：Google Chrome、Microsoft Edge、Brave 或 Chromium 任一
- **Node.js**（可选，仅运行测试需要：`tests/test_browser.py` 会用 Node 校验 JS 版 TOTP 实现）

### 2. 安装依赖
```bash
python -m venv .venv
.venv\Scripts\activate          # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
# 如需运行自动化测试或 Playwright UI 测试：
pip install -r requirements-dev.txt
```

### 3. 启动
- **命令行**：`python main.py`（支持 `--verify-init` 无界面自检）
- **Windows 无黑框启动**：双击根目录的 `KeyVault.exe`。它会按以下顺序寻找 Python：项目内 `.venv` / `venv` → `PATH` 中的 `pythonw.exe`（自动跳过微软商店占位）→ 注册表 `PythonCore` → 常见安装目录（按版本号数字降序）→ `pyw.exe`。

### 4. 域名邮箱与 WorkBuddy 员工开户

1. 切换到 **域名邮箱**，点击添加并粘贴 `账号----密码`，每行一个邮箱（也支持导入 `accountData/` 下的 Excel `.xlsx` / `.csv`）。密码中的分隔符和首尾空格会完整保留；登录地址为 `https://mail.ggb6.com`。
2. 勾选邮箱后点击 **批量注册WorkBuddy**。队列采用 **双浏览器并发 + 每节点 2 账号配额**，通过独立 Sidecar 代理内核与隔离 CDP（不启用 `Runtime.enable`）驱动标准流程，支持自动提取 GitHub 验证码与 WorkBuddy2API 登录凭据 JSON 导出。
3. 识别到滑块或人机验证时自动暂停，也可手动点击暂停；完成验证并点击恢复后继续。暂停和退出程序会保留进度，完成、跳过或取消时关闭并清理当前工作会话。

### 5. 可选环境变量

| 变量 | 作用 |
|---|---|
| `KEYVAULT_BROWSER_PATH` | 指定浏览器可执行文件路径（自动探测失败时使用） |
| `KEYVAULT_CLASH_CONFIG` | 指定 Clash / Mihomo 配置文件路径（优先于自动探测） |
| `KEYVAULT_MIHOMO_PATH` | 指定独立 Sidecar Mihomo 内核可执行文件路径 |

---

## 🔨 关于 `KeyVault.exe`

`KeyVault.exe` 只是一个极简启动器（约 80 行 C#），作用是用 `pythonw.exe` 无窗口运行 `main.py`，**不包含任何业务逻辑**。完整源码见 [`tools/launcher/KeyVaultLauncher.cs`](tools/launcher/KeyVaultLauncher.cs)。

如不信任仓库内的二进制文件，可以删除后自行编译（使用 Windows 自带的 .NET Framework 编译器，无需安装 Visual Studio）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\launcher\build.ps1
```

> 未签名的小型启动器偶尔会被杀毒软件误报，自行编译或直接使用 `python main.py` 均可。

---

## 📂 目录结构

```text
.
├── data/                    # [本地私有 · 已被 .gitignore 排除] 首次运行自动创建
│   ├── accounts.db          # SQLite 数据库 (WAL 模式)
│   ├── settings.json        # 界面配置
│   ├── runtime/             # WebView2 运行缓存
│   └── browsers/            # 各账号独立的浏览器配置目录 (acc_1, acc_2 ...)
├── src/
│   ├── api.py                 # pywebview 前后端桥接 API
│   ├── browser_service.py     # 浏览器三引擎、Google 纯原生清理与环境配置
│   ├── browser_window.py      # Win32 浏览器窗口绑定、DPI 与屏幕越界救援
│   ├── crash_logger.py        # pythonw 与 WinForms/CLR 异常日志轮转
│   ├── db_service.py          # SQLite WAL 存储、历史归档与 Rolling 备份
│   ├── github_mail.py         # IMAP SSL 验证码读取（支持纯文本/HTML）
│   ├── login_tools_panel.py   # 浏览器前台会话与 HUD 领航栏调度器
│   ├── login_tools_strip.py   # WinForms 紫盾领航栏与极速改密/2FA 浮窗
│   ├── maintenance_service.py # RT 诊断、OAuth PKCE 监听、本地代理中继与 GeoIP
│   ├── parser.py              # 智能粘贴与分栏解析器
│   ├── proxy_service.py       # 代理配置探测与节点区域分类
│   ├── sidecar_proxy.py       # 独立 Mihomo 内核与 16 槽位端口隔离
│   ├── totp_service.py        # 2FA 秘钥清洗、脱敏与 TOTP 计算
│   ├── url_dispatcher.py      # Windows 外链接管与 Named Pipe 单例分发
│   ├── window_state.py        # 主窗口位置与最大化状态持久化
│   ├── workbuddy.py           # WorkBuddy 双浏览器并发队列与配额调度
│   └── workbuddy_browser.py   # WorkBuddy CDP 无侵入浏览器控制器
├── web/                       # 纯本地前端资源（无外部 CDN）
├── tools/launcher/            # KeyVault.exe 源码与编译脚本
├── tests/                     # Pytest 测试
├── main.py                    # 应用入口
├── requirements.txt           # 运行时核心依赖
└── requirements-dev.txt       # 测试与 Playwright UI 验证依赖
```

> **🔒 隐私**：所有账号数据、2FA 秘钥及浏览器 Cookie 仅保存在本地 `data/` 目录，应用不包含任何统计上报或云端上传逻辑。备份 / 迁移只需拷贝 `data/` 文件夹。**`data/` 中为明文存储，请自行保管好设备与备份。**

---

## 🧪 运行测试

```bash
python -m pytest -v
```

---

## ⚠️ 免责声明

- 本项目仅供**学习研究**以及**管理您本人合法拥有的账号**使用。
- Google、OpenAI 等平台的服务条款可能限制自动化登录、多账号操作或规避风控检测的行为。**使用者须自行了解并遵守相关平台条款及所在地法律法规**，因使用本项目导致的账号封禁、数据丢失或其他任何损失，由使用者自行承担。
- 严禁将本项目用于盗用他人账号、批量注册、账号倒卖、欺诈等任何违法违规用途。
- 本项目按「现状」提供，不提供任何明示或暗示的担保，详见 [LICENSE](LICENSE)。

---

## 📄 开源协议

本项目基于 [Apache License 2.0](LICENSE) 开源。
