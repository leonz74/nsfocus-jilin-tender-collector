# 标书采集平台

**NSFOCUS 吉林代表处 · v0.3.0**

在本机按客户需求查询招标采购平台，汇总项目公告、采购单位、金额和文件线索，并导出带下载 URL 的表格。支持全国 → 省份 → 城市 → 区县选择、行业与关键词查询、采集进度、分页，以及采集期间导出已取得的结果。

## 安装与一键运行

普通使用者请进入本仓库 [Releases → v0.3.0](https://github.com/leonz74/nsfocus-jilin-tender-collector/releases/tag/v0.3.0)，下载安装包；GitHub 的“Code → Download ZIP”是开发源码，不包含运行环境。

| 系统 | 安装包 | 首次启动 |
|---|---|---|
| Mac Apple Silicon / Intel，macOS 11 及以上 | Mac ZIP | 完整解压，双击“标书采集平台.app” |
| Windows 10 / 11，Intel / AMD 64 位 | Windows ZIP | 右键“全部解压”，双击“安装并启动.cmd” |

安装包内置 Python 和依赖，自动放置程序、创建桌面入口并打开网页；不需要预装 Python 或 Node。之后双击桌面“标书采集平台”即可运行，双击“停止标书采集平台”退出。关闭网页时后台采集继续；停止平台会保留已采集结果。

详细操作、数据位置与更新说明见 [安装说明](docs/INSTALL.md)，校验值见 [SHA256SUMS.txt](docs/SHA256SUMS.txt)。Mac 包未经过 Apple 公证，首次打开可能需要按系统“隐私与安全性”提示允许运行。

## 主要功能

- 按日期、地区、行业、关键词和公告类别向平台检索，展示当前查询条件与各来源进度。
- 汇总采购单位、项目金额、联系人、中标单位、原公告 URL 和附件线索，支持每页 10 / 20 / 50 / 100 条。
- 导出所选公告或全部筛选结果的 CSV；一个公告有多个附件时分别占行。采集尚未结束也可导出当前已取得的结果。
- 可选 AI 分类和 AI 防漏检测；按服务地址与 Key 获取可用模型，再测试连接。
- 用户明确选择后按需下载文件，并保留来源、状态和获取说明。
- 桌面分发版支持独立数据目录、单实例运行、端口冲突处理，以及停止后保留结果。

## 平台与登录

| 平台 | 用途 |
|---|---|
| 吉林省公共资源交易平台 | 吉林省政府采购、工程建设等公告 |
| 中国政府采购网检索 | 按地区、时间与关键词检索 |
| 中国政府采购网归档 | 静态归档补充查询 |
| 全国公共资源交易平台 | 跨地区公告检索与补充线索 |
| 招标采购导航网（OKCIS） | 商业平台项目与附件入口，需自行登录并具备相应权限 |
| 官方链接补录 / 自定义网站 | 补充指定站点的公告线索 |

首次使用数据库为空，AI 默认关闭。OKCIS 已预置但默认未启用，需在来源配置中启用并登录自己的账号。网站登录采集需要 Microsoft Edge 或 Google Chrome；普通主界面也可使用 Safari。网站验证、会员权限、限流或访问受阻时，按页面提示处理。

Mac 支持把 AI Key 保存到系统钥匙串。当前 Windows 版使用临时输入或 `TENDER_AI_API_KEY` 环境变量，尚未实现系统凭据持久保存。不要把真实 Key、账号密码、Cookie 或数据库提交到仓库。

## 结果的含义

公告和附件 URL 都保留来源及获取说明。“发现下载链接”不等于文件已下载或已验证可访问；需要登录、会员权限或人工处理的入口会保留对应状态。来源受阻、翻页上限、归档保留范围等可能造成遗漏；AI 防漏检测也不能保证零遗漏。城市、行业和项目字段由规则与可选 AI 提取，正式使用前应结合原公告复核。

## 从源码运行

源码运行需要 Python 3.11 及以上；建议使用 Python 3.13。Mac 可执行：

```bash
bash start-ui-mac.sh
```

Windows 开发环境可以执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[pdf]"
Copy-Item config.example.json config.json
.\.venv\Scripts\python.exe -m tender_downloader web --config config.json --port 8765
```

`Copy-Item` 仅在首次运行、尚无 `config.json` 时执行；从源码启动时请检查日期。源码脚本与自带运行环境的桌面安装包是两个入口。

## 测试与打包

```bash
python -m unittest discover -s tests -q
node --test tests/*.js
```

Node 仅用于前端测试，程序运行不依赖 Node。

在 Mac 上使用 Python 3.12 及以上构建两个分发包：

```bash
mkdir -p build/cache dist
python packaging/build_release.py --cache build/cache --output dist
```

构建脚本下载并校验固定版本运行环境及依赖，用白名单复制应用代码，并生成清洁的 Mac、Windows ZIP。Mac 构建会使用系统 `codesign` 做本地完整性签名，不执行 Apple 公证。安装包放在 GitHub Release 附件中，避免把运行环境二进制写入 Git 历史。

v0.3.0 的原有 356 项 Python 测试、新增 7 项安装与启动测试和 32 项前端测试已通过。Mac 已实际验证安装、采集、采集中导出、停止、重启及保留数据的更新；Windows 包完成静态检查，**尚未在 Windows 实机验证**。具体范围见 [打包验收说明](docs/RELEASE_VALIDATION.md)。

## 目录与第三方组件

- `src/tender_downloader/`：采集、解析、AI、导出与本地网页。
- `src/tender_downloader/data/`：离线行政区划数据及原始许可；这是必须提交的应用数据。
- `packaging/`：桌面启动器与分发包构建脚本。
- `tests/`：测试代码及少量固定测试样例，不包含真实登录状态。
- `config.example.json`：无账号与 Key 的示例配置。

第三方运行环境和依赖的地址、校验值及许可随安装包保留。行政区划数据说明见 [数据来源与许可](src/tender_downloader/data/china-regions-README.md)。
