# workbuddy_auto_checkin

WorkBuddy「Buddy 加油站」的每日自动化：**签到领积分**、**派猫猫旅行并领回礼物**，结果推送到飞书。

跑在 GitHub Actions 上，按 cron 每天定时执行，不需要任何常开设备。

> **两种运行方式，签到逻辑完全一致，二选一：**
>
> | | GitHub Actions 版（本 README） | [WPS 金山文档版](wps/README.md) |
> | --- | --- | --- |
> | 适合 | 习惯 GitHub 的用户 | **不想碰 GitHub / 零命令行** 的用户 |
> | 托管 | GitHub Actions 免费定时任务 | 金山文档智能表格脚本 + 免费定时任务 |
> | 配置 | Fork 仓库 + 填两个 Secret | 建表格 → 粘脚本 → 填两个单元格 |
> | 结果 | Actions 日志 / 飞书卡片 | 直接回写表格 / 飞书卡片 |
>
> 两种版本的 token 都由本机导出一次（[第 4 步](#第-4-步导出登录凭证二选一)）：
> 装了 Python 就跑 `refresh_token.py`；没装 Python 直接下载仓库里的
> **`refresh_token.exe`** 双击运行，无需任何环境。

## 特性

- **云端定时** — 免费 runner + `schedule` 触发，本机关机照常执行
- **一次做两件事** — 签到与成长中心旅行在同一次运行里完成
- **幂等** — 先查状态再决定是否领取；已签不重领，已派不重派
- **结论解耦** — 猫猫模块任何异常都不影响签到结论；整体成败只看签到
- **零明文入库** — 仓库不含任何凭据；token 由本机脚本导出后写入 GitHub Secret

## 工作原理

```text
本机（一次性）                      GitHub                           外部
┌──────────────────┐    gh secret   ┌────────────────────┐
│ refresh_token.py │ ─────────────► │ Secret:            │
│  读登录态 → 解密 │                │  WB_ACCESS_TOKEN   │
└──────────────────┘                │  FEISHU_WEBHOOK    │
                                    └─────────┬──────────┘
                                              │ 每天 cron
                                    ┌─────────▼──────────┐        ┌──────────────┐
                                    │ Actions: wb_auto.py│ ─────► │ copilot.     │
                                    │  签到 / 领奖 / 派出│ ◄───── │ tencent.com  │
                                    └─────────┬──────────┘        └──────────────┘
                                              │ webhook
                                    ┌─────────▼──────────┐
                                    │ 飞书群机器人卡片    │
                                    └────────────────────┘
```

### 为什么 token 必须在本机导出

桌面端把会话写在 `workbuddy-desktop.info` 里，其中 `accessToken` 是 Electron `safeStorage` 保护的 `$wbEncrypted` 信封（AES-256-GCM，密钥由客户端运行时持有）。云端 runner 既没有这份文件，也没有解开它所需的运行时，因此**无法在云端现取 token**。

`refresh_token.py` 的做法是：读出加密信封 → 以 `ELECTRON_RUN_AS_NODE=1` 起一个 WorkBuddy 子进程 → 把信封通过 stdin 递进去，由客户端自己的运行时解密 → 从 stdout 取回明文 token。解密材料全程不落盘。

## 快速开始

### 开始前要有

- 一个 GitHub 账号
- 一台装过 WorkBuddy **并且登录过**的电脑（只在配置时用一次）
- 一个飞书群（自己建一个只有自己的也行），用来收消息
- 电脑上有 Python 3.8+（没有也行——用仓库里的 `refresh_token.exe`，见第 4 步）

### 容易搞混的一点，先说清楚

下面所有命令和配置里出现的仓库名，指的都是**你自己**的那个仓库（`你的用户名/workbuddy_auto_checkin`），**不是**原作者的 `FXmonkey/workbuddy_auto_checkin`。原作者的仓库只是让你 Fork 的源头，你的凭证和定时任务都配在你自己的仓库里。

### 第 1 步：把项目复制到自己账号下

两种方式都行，**推荐 Fork**，一键搞定：

**方式 A：Fork（推荐）**

1. 打开项目页 `https://github.com/FXmonkey/workbuddy_auto_checkin`
2. 点右上角 **Fork** → **Create fork**
3. 完成后你就有了 `你的用户名/workbuddy_auto_checkin`
4. **fork 完必须多做一步**：进你 fork 出来的仓库 → 点上方 **Actions** 页签 → 页面中间会有一行提示，点 **I understand my workflows, go ahead and enable them** 启用。**不启用的话定时任务不会跑。**

**方式 B：新建仓库再上传**

1. GitHub 右上角 **+** → **New repository**，名字随便填，选 **Private**，点 **Create repository**
2. 仓库页点 **Add file → Upload files**
3. 把整个项目文件夹拖进去（**必须包含 `.github` 这个文件夹**，它是定时任务的定义），点 **Commit changes**

> 两种方式的取舍：Fork 少一步，但要多做上面第 4 小步的启用；新建仓库没有这个坑，但网页上传时容易漏掉 `.github` 这种以点开头的文件夹。二选一即可。

### 第 2 步：把脚本下载到自己电脑

后面要在你电脑上跑一次脚本导凭证，所以先把代码拿到本地。两种都行：

- 网页下载：你的仓库页 → 绿色 **Code** 按钮 → **Download ZIP** → 解压
- 或用 git：`git clone https://github.com/FXmonkey/workbuddy_auto_checkin.git`

### 第 3 步：拿一个飞书机器人地址

1. 在飞书里建一个群（只有你自己也行）
2. 群设置 → **群机器人** → **添加机器人** → **自定义机器人**
3. 复制它给你的地址，形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`

### 第 4 步：导出登录凭证（二选一）

**先选运行形式：**

- **没装 Python？** 直接双击仓库里的 **`refresh_token.exe`**（用法与 `.py` 完全一致，把下面命令里的 `python refresh_token.py` 换成 `refresh_token.exe` 即可）。首次运行 Windows SmartScreen 可能拦截，点 **更多信息 → 仍要运行**。
- 装了 Python：继续往下按命令操作（脚本零第三方依赖，只需 Python 3.8+）。

**方式 A：电脑上装了 gh CLI —— 自动写入，最省事**

在项目文件夹里运行：

```
python refresh_token.py --gh-repo 你的用户名/workbuddy_auto_checkin
```

脚本会解密并直接把 token 写进你仓库的 Secret，屏幕显示「已写入」即可跳到第 5 步。

**方式 B：没装 gh CLI —— 先打印出来，再手动粘贴**

在项目文件夹里运行：

```
python refresh_token.py
```

屏幕上会打印一长串字符（下图涂黑的部分就是它），**完整复制 `----- 以下为 token -----` 到 `----- 以上为 token -----` 之间**的内容，下一步手动粘进 GitHub。

![](assets/refresh_token输出.jpg)



### 第 5 步：把两个 Secret 填进仓库

打开**你自己的仓库** → **Settings** → 左侧 **Secrets and variables** → **Actions** → 点绿色的 **New repository secret**：

![](assets/github_配置action的飞书、wb_access_token.png)

需要填两条：

| Name | Secret 里填什么 |
| --- | --- |
| `WB_ACCESS_TOKEN` | 第 4 步方式 B 复制的那串字符 |
| `FEISHU_WEBHOOK` | 第 3 步拿到的飞书机器人地址 |

> 方式 A 已经自动写入 `WB_ACCESS_TOKEN` 的，这里只需要再填 `FEISHU_WEBHOOK` 一条。
> 填完这个页面应该能看到两条记录（如上面截图所示）。

### 第 6 步：手动跑一次验证

1. 打开你的仓库 → 点上方 **Actions** 页签
2. 左侧选 **Buddy 加油站每日自动化**
3. 右侧点 **Run workflow** 下拉，再点绿色的 **Run workflow**
4. 等十几秒，刷新页面，飞书应该收到一条消息

> 这个页面显示绿色不代表签到一定成功（猫猫出问题不影响整体结论）。**以飞书收到的消息为准。**

## 配置项

### Actions 侧（`wb_auto.py`）

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `WB_ACCESS_TOKEN` | 是 | — | 访问令牌。缺失则直接退出（exit 2） |
| `FEISHU_WEBHOOK` | 建议 | — | 飞书自定义机器人地址。缺失则不推送，仅打印日志 |
| `WB_ENDPOINT` | 否 | `https://copilot.tencent.com` | 接口地址 |
| `WB_RESULT_FILE` | 否 | — | 写入 Markdown 汇总表，供 Actions 的 step summary 使用 |

### 本机侧（`refresh_token.py`）

| 变量 | 说明 |
| --- | --- |
| `WORKBUDDY_AUTH_FILE` | 覆盖凭据文件路径。用于非默认安装或凭据位置变动 |
| `WORKBUDDY_EXE` | 覆盖客户端可执行文件路径。自动探测失败时使用 |

凭据文件按 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` 探测；客户端按 `WORKBUDDY_EXE` → `%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe` → 常见盘根目录 → 注册表 `DisplayIcon` 的顺序探测。

## 行为矩阵

| 情况 | 行为 | `result` |
| --- | --- | --- |
| 今天未签到 | 调用领取接口 | `CLAIMED` |
| 今天已签到 | 不重复领取，读状态汇报 | `ALREADY` |
| 活动未开启 | 直接汇报，不做写操作 | `INACTIVE` |
| 猫猫已到家 | 先领这趟的旅行积分，再判断派出 | `DEPARTED` / `LIMIT_REACHED` |
| 猫猫在途中 | 汇报位置与预计到家时间，不派出 | `IN_TRIP` |
| 猫猫空闲且名额未用完 | 派出一趟 | `DEPARTED` |
| 今日名额已用完 | 读服务端 `daily_limit_reached`，不派出 | `LIMIT_REACHED` |
| 认证被拒（401/403） | 提示重新运行 `refresh_token.py` | `AUTH` |
| 网络不可达 | 有限重试后汇报，不误报成功 | `NETWORK` |
| 猫猫模块异常 | 捕获后单独汇报，不影响签到 | `ERROR` |
| 旅行状态取值未知 | 原样汇报，不做写操作 | `UNKNOWN_STATE` |
| 领取返回无法识别 | 汇报原始返回，不臆断成功 | `UNKNOWN` |

派出名额的判断读服务端下发的 `daily_limit_reached`，而不是硬撞 `depart` 接口等它报错。

## 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 签到成功 |
| `1` | 签到未达成（猫猫失败不在此列） |
| `2` | 缺少 `WB_ACCESS_TOKEN` |

workflow 中脚本以 `exit 0` 收尾，只对未完成项发 `::warning::`，因此一次运行显示绿色不代表签到一定成功——**以日志、step summary 与飞书卡片为准**。

## 接口

均从客户端 `app.asar` 中核对并经线上实测，非网络流传版本。所有响应统一是 `{code, msg, data}` 信封，业务字段在 `data` 内，脚本用 `unwrap()` 统一解包。

```text
POST /v2/billing/meter/checkin-activity-status   查询今日签到状态
POST /v2/billing/meter/daily-checkin             领取签到积分（已签返回 code 10001）

GET  /v2/activity/growth/buddy/travel/status     查询旅行状态
POST /v2/activity/growth/buddy/travel/claim      领取已到家的旅行积分
GET  /v2/activity/growth/buddy/travel/config     获取可派目的地
POST /v2/activity/growth/buddy/travel/depart     派出一趟
```

时间戳字段（`arrive_at` / `depart_at` / `server_now`）为**秒级**，与毫秒混用会算出错误的剩余时间。

## 项目结构

```text
wb_auto.py                    云端执行主体：签到、领奖、派出、推送
refresh_token.py              本机一次性操作：导出 token / 写入 Secret
refresh_token.exe             上者的免 Python 打包版（Windows x64，双击即用）
.github/workflows/daily.yml   cron 定义与执行步骤
wps/                          WPS 金山文档版（脚本 + 说明，见 wps/README.md）
```

> `refresh_token.exe` 由 `refresh_token.py` 经 PyInstaller 打包（零第三方依赖），
> 每次改动 `.py` 后需要重新构建。未做代码签名，SmartScreen 的提示属正常现象。

## 安全设计

- 仓库内不含任何凭据；token 只存在于 GitHub Secret 与运行时内存
- 解密助手只含固定代码，加密信封走 stdin，不经过 argv；密钥用后清零，不写临时文件
- 明文 token 不进入命令行、环境变量或日志；`--gh-repo` 模式下经 stdin 交给 `gh`
- 所有响应打印前过 `masked()` 脱敏，长字符串仅保留首尾若干字符
- 写操作仅在网络异常时有限重试，依赖服务端幂等避免重复提交

## 常见问题

**令牌过期** — token 由客户端签发，有效期约 28 天（`expiresIn` 2419200）。日志或飞书出现「认证被拒」时，本机重跑 `python refresh_token.py --gh-repo OWNER/REPO` 覆盖即可（`OWNER/REPO` 换成你自己的仓库；exe 同理：`refresh_token.exe --gh-repo OWNER/REPO`）。

**fork 之后 Actions 页签是空的** — fork 来的仓库默认不启用 Actions。进 Actions 页签，点页面中间的 **I understand my workflows, go ahead and enable them** 启用一次即可。

**Actions 页签看不到 workflow** — 确认 `.github/workflows/daily.yml` 已入库，且仓库的 Actions 功能未被禁用。

**飞书没收到卡片** — 网络异常与 5xx 会自动重试 3 次；仍失败时看日志中 `[飞书]` 行的原因：
`19001` 表示 webhook 地址无效或复制不完整（检查 Secret `FEISHU_WEBHOOK`）；机器人若开启了
**签名校验**，推送会被拒（重试无用），建机器人时不要勾选。当天 cron 有三个时段，某次失败
后下一次运行会自动再推一遍当天结果（脚本幂等，不会重复领）。

**偶发 302** — 高频轮询旅行状态时网关会短暂跳转，请求层已对 3xx 退避重试。按日频执行不会触发。

**非默认安装路径** — 用 `WORKBUDDY_EXE` 指定客户端，用 `WORKBUDDY_AUTH_FILE` 指定凭据文件。

## 已知限制

- 只覆盖签到与猫猫旅行；**不含**断登补签、连登兑换、盲盒、任务领取等成长中心其它玩法
- 依赖客户端私有接口，客户端升级可能改变接口或凭据格式，届时需更新脚本
- 仅支持 `sym-v1` / `suite 1` 格式的加密信封；其它格式会明确返回 `UNSUPPORTED_ENVELOPE` 而非发送无效请求
