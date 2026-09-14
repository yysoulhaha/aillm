# AI-LLM 打包说明（README-打包）

产物矩阵

| 平台   | 产物                                        | 装法                              | 构建位置      | 备注 |
| ------ | ------------------------------------------- | --------------------------------- | ------------- | ---- |
| Windows| `dist/AI-LLM-Setup-<版本>.exe` (Inno)         | 双击向导安装（per-user 免管理员）   | Windows / Wine | PyInstaller+ISCC |
| Windows| `dist/AI-LLM-Windows-<版本>.zip`（onedir 免安装）| 解压运行 `AI-LLM\AI-LLM.exe`      | Windows / Wine | PyInstaller |
| Ubuntu | `dist/ai-llm-gateway_<版本>_all.deb`         | `sudo apt install ./xxx.deb`      | Linux / Windows | venv 法：postinst 自动建 .venv 装依赖 |
| Linux  | `dist/AI-LLM-Linux-<版本>.tar.gz`（onedir）  | 解压运行 `AI-LLM/AI-LLM`          | Linux         | PyInstaller |

> 本仓库已能在 Linux 上用 **Wine 10 + Windows Python 3.12 + PyInstaller + Inno Setup 6.7.3**
> 交叉产出 Windows 安装包（shell 里 `wine 'C:\Python312\python.exe' ...`），
> 仍推荐在真 Windows 机跑 `packaging\build_windows.ps1` 以取得签名/图标等发布闭环。

关键约束
- **PyInstaller 不能跨平台**：Windows 的 exe 只能在 Windows 构建，Linux 版只能在
  Linux 构建。生成 .deb 的 `make_deb.py` 是纯 Python，**任何系统都能跑**。
- .deb 采用「venv 法」：包里只放源码，postinst 在安装机建 `/opt/AI-LLM/.venv`
  并 `pip install -r requirements.txt`，无需预先编译、任何架构可用（Architecture: all）。
- **版本号只改一处**：`server.py` 的 `APP_VERSION`；`make_deb.py` 自动读取它生成
  `control` 的 Version 与文件名。Inno 安装器版本在 `packaging/installer.iss`。
- **严禁把发布者密钥打进包**：个人凭证（发布机 Gitee/GitHub token 等）只存在于本机
  环境，绝不进任何分发物（打包脚本默认只收 server.py/client.py/core/web/requirements.txt，天然排除 data/ 与 .env）。

## 一、Windows 构建（必须 Windows 机）

```powershell
# 一键（推荐）：在仓库根目录
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# 产物：dist\AI-LLM\（onedir） + dist\AI-LLM-Setup-<版本>.exe（装了 Inno 时）
```

或分步手动：
```powershell
# 1) 依赖（Python 3.12）
pip install psutil pyinstaller

# 2) 应用本体（onedir + 无控制台 + tiktoken 词表）
python -X utf8 -m PyInstaller packaging\AI-LLM.spec --noconfirm --distpath dist --workpath build

# 3) 安装包（Inno Setup 6+，winget install JRSoftware.InnoSetup）
& "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
# 产物：dist\AI-LLM-Setup-<版本>.exe
```

## 二、Ubuntu 构建（切换 Ubuntu 后）

```bash
bash AI-LLM/packaging/build_ubuntu.sh
# = 自动：装系统依赖 → 建 build-venv → 装依赖 → 出 .deb → (可选)PyInstaller onedir
# 只出 .deb（快）：SKIP_PYINSTALLER=1 bash AI-LLM/packaging/build_ubuntu.sh
```

单独出 .deb（任何机器，含 Windows）：
```bash
python3 AI-LLM/packaging/make_deb.py            # → AI-LLM/dist/ai-llm-gateway_<版本>_all.deb
```

## 三、Ubuntu 安装 .deb 后

```bash
sudo apt install ./ai-llm-gateway_<版本>_all.deb     # postinst 自动建 venv+装依赖（需联网）
ai-llm --open                                        # 启动并打开面板(127.0.0.1:18123)
systemctl --user enable --now ai-llm                 # 可选：开机自启（用户级服务）
# 数据：~/.local/share/ai-llm/；卸载 sudo apt remove ai-llm-gateway（保留数据）
```

## 五、普通用户环境常见问题（Windows 安装包已内置的解决方案）

| 现象 | 状态 |
| ---- | ---- |
| 系统没装 Python | ✅ 无需：onedir 已内嵌 `python312.dll` 解释器，零系统依赖 |
| 没装 git | ✅ 无需：模型源走各厂商官方 HTTPS 接口，不调系统 git |
| 缺 VC++ 运行库报错 | ✅ 已内嵌 `VCRUNTIME140.dll/VCRUNTIME140_1.dll`（Win10/11 自带 UCRT） |
| 需要管理员权限才能装 | ✅ 免管理员：per-user 装到 `%LocalAppData%\Programs\AI-LLM` |
| 杀软/SmartScreen 拦截 | ⚠️ 安装包未签名会弹「Windows 已保护你的电脑」→ 点「更多信息→仍要运行」；正式分发建议买代码签名证书后重签 |
| 双击没反应/以为没启动 | 无控制台窗口属正常；面板 http://127.0.0.1:18123/（安装器装完自动弹出）。日志在 `%LocalAppData%\AI-LLM\logs\` |
| 防火墙弹窗/想从手机访问 | ✅ 默认只监听 `127.0.0.1`（本机），免防火墙弹窗；要局域网访问在 `config.json` 设 `"bindHost":"0.0.0.0"`（页面/文档给指引） |
| 端口 18123 被占用 | 修改 `config.json` 的 `port`；程序检测到已在运行会直接打开现有面板 |
| tiktoken 词表需联网 | 内置兜底：词表下载失败自动降级 len//4 启发式估算，路由/压缩照常跑，仅精度略降 |
| 卸载 | 只删程序目录，`%LocalAppData%\AI-LLM` 数据（配置/密钥/日志）全部保留 |
| 开机自启 | 安装向导可选；写入当前用户 HKCU Run，卸载自动移除 |

## 六、包内到底装了什么

- **零预置渠道、零注册码、零会员体系**：默认空配置，用户自行通过 UI「添加模型源」填入自己的 API 接口地址和密钥。
  支持 OpenAI / Claude / Gemini / DeepSeek 等主流官方接口，也支持 OpenAI 兼容的任意自建/第三方接口（接入责任由用户自行承担）。
- 运行期数据全部写用户目录（Windows `%LocalAppData%\AI-LLM`，Linux `~/.local/share/ai-llm`），
  安装目录只读可升级/卸载，用户数据不动。