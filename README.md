# 音乐下载自动化管理

把音乐平台「我喜欢」里的歌，定时补齐到本地曲库的 Windows 小工具。

仓库名 `music-download-manager`，Python 包名 `music_sync`（沿用旧名，不改 import）。
不联网上传、不碰你的账号密码，只是拿你自己已登录的会话去读歌单、下歌。

## 它能干什么

- **对账补缺**：拉你歌单里的曲目，和本地曲库比一遍，缺哪首下哪首，已有的直接跳过。
- **多平台**：网易云音乐、QQ音乐、酷狗音乐可以下载；汽水音乐只有占位适配器（见「已知限制」）。
- **少手动**：网易云直接用客户端本地的登录态；QQ音乐/酷狗可以一键读浏览器里已经登录的状态，实在不行才手动粘。
- **定时跑**：注册 Windows 计划任务，每周固定一天 / 每天 / 每隔 N 小时跑一次；跑完右下角弹一个小窗告诉你下了几首。
- **跨平台补源**：主平台没版权或者下不动的歌，只要能换源，会用其它已登录平台的同名曲补上。
- **归置文件**：客户端（网易云/QQ音乐）自己下载的歌，默认放在它们的下载目录里，可以自动搬进你的曲库。
- **能自检**：一条命令查依赖、目录、网络、登录态、计划任务有没有装好。

## 环境要求

- Windows 10 / 11（只支持 Windows，脚本和计划任务都基于 Windows）
- Python 3.9 或更高（开发和测试用的是 3.13）
- 能联网，装依赖时需要网络

依赖只有两个：`requests`、`cryptography`（后者用于读网易云客户端加密过的登录数据）。

## 三步装好

### 1. 装依赖

```bat
cd music-sync
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

不需要 `pip install` 本仓库——直接在项目目录里用 `python -m music_sync` 运行。

### 2. 打开设置，填一遍

```bat
python -m music_sync settings
```

窗口里从上到下依次填：

1. **音乐平台 / 歌单 / 曲库目录**：歌单可以填 id 也可以直接粘链接；曲库目录点「浏览…」选。
2. **音质**：320 / 192 / 128 / 无损。无损只有部分平台支持，选了不支持的平台，下面会有一行小字提醒会自动降级。
3. **平台登录态**：先试「自动获取」——它会去读你网易云客户端或浏览器里已经登录好的状态；不行再点「手动粘贴」。
4. **定时与提醒**：要不要定时、多久一次、几点、结果弹窗几秒后自动关。
5. 点 **保存**。

### 3. 注册计划任务

设置界面的「计划任务名称」要和脚本的 `-TaskName` 一致，然后用脚本注册（推荐）：

```powershell
# 每周日 21:00
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Weekly -Weekday Sunday -Time 21:00

# 每天 21:00
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Daily -Time 21:00

# 每隔 24 小时
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Hours 24
```

嫌 PowerShell 麻烦的话，命令行入口也包了一层：

```bat
python -m music_sync task --weekly --weekday Sunday --time 21:00
```

脚本是幂等的（先删再建），改时间就再跑一次。想先看看会下什么、但不下：

```bat
python -m music_sync sync --dry-run
python -m music_sync check        :: 环境自检
python -m music_sync platforms    :: 列出平台和登录态
```

## 每个平台怎么登录

| 平台 | 能不能下载 | 登录怎么做 |
| --- | --- | --- |
| 网易云音乐 | 能 | 打开网易云客户端登录一次就够了，工具会自己去读本地登录状态，**不用填任何东西**。实在读不到时，浏览器登录后点「自动获取」当兜底。 |
| QQ音乐 | 能（接口未在真机账号验证） | 浏览器登录 QQ音乐网页 → 设置里点「自动获取」。还不行就「手动粘贴」：F12 → Network → 刷新 → 任意请求的 Request Headers 里找 `Cookie`，整行值复制进去。 |
| 酷狗音乐 | 能（接口未在真机账号验证） | 同上，浏览器登录后点「自动获取」。 |
| 汽水音乐 | **不能**（只有占位适配器） | 没有公开接口，登录和下载都不可用，只保留平台位方便以后补。 |

命令行也能查/取登录态：

```bat
python -m music_sync login            :: 所有平台过一遍
python -m music_sync login netease    :: 只取网易云（会自动写入配置文件）
```

登录态会以 `cookie` 字符串存在 `config.json` 里。这个文件等于你的登录凭证，别发给别人、别提交到 git。

## 配置文件字段

默认路径 `~/.music-sync/config.json`（`~` 是你的用户目录）。两种改法：

- 环境变量 `MUSIC_SYNC_HOME` 换配置目录；
- 命令行/快捷方式加 `--config D:\somewhere\config.json` 指定文件。

缺字段会自动补默认值，手改坏了大不了删掉重来。

### 顶层

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `platform` | `"netease"` | 用哪个平台当主来源：`netease` / `qqmusic` / `kugou` / `soda` |
| `playlist` | `"8508302371"` | 歌单 id 或整条歌单链接 |
| `library_dir` | `"F:\\FFOutput"` | 曲库目录，下载和判重都在这里 |
| `client_download_dir` | `"D:\\CloudMusic"` | 客户端下载目录；留空表示不归置 |
| `quality` | `"320"` | 音质：`320` / `192` / `128` / `lossless` |
| `auto_move` | `true` | 是否把客户端下载目录里的歌搬进 `library_dir` |
| `ignore_ids` | `[]` | 不想再同步的曲目 id 列表（手动排除某些歌） |

### `schedule`（定时）

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用定时（设置界面里的复选框） |
| `mode` | `"weekly"` | `weekly` 每周 / `daily` 每天 / `hours` 每隔 N 小时 |
| `weekday` | `"sunday"` | `weekly` 时用，`monday`…`sunday` |
| `time` | `"21:00"` | `weekly`/`daily` 时用，24 小时制 `HH:MM` |
| `hours` | `168` | `mode=hours` 时的间隔小时数，1~8760 |
| `task_name` | `"MusicSync"` | 计划任务名，要和 `install_task.ps1 -TaskName` 一致 |

### `notify`（跑完的提示窗）

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `toast` | `true` | 同步结束后要不要弹右下角结果窗 |
| `timeout` | `40` | 提示窗几秒后自动关，5~300 |

### `download`（下载细节）

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `ext` | `"mp3"` | 保存的文件扩展名 |
| `retries` | `3` | 单曲最多重试几次 |
| `sleep` | `0.4` | 每首之间等多少秒（别把人家服务器打爆） |
| `overwrite` | `false` | 已存在同名文件要不要覆盖 |

### `platforms`（各平台登录信息）

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `platforms.netease.cookie` | `""` | 网易云登录凭证（一般留空由工具自动读取） |
| `platforms.netease.client_app_dir` | `""` | 网易云客户端数据目录，留空＝用默认安装位置 |
| `platforms.qqmusic.cookie` | `""` | QQ音乐登录凭证 |
| `platforms.kugou.cookie` | `""` | 酷狗登录凭证 |
| `platforms.soda.cookie` | `""` | 汽水音乐预留字段（暂无实现） |

## 常见问题

**Q：跑完了但没弹结果窗？**
多半是被安全软件拦了。火绒 → 防护中心 → 弹窗拦截 → 添加程序，把 `pythonw.exe`（和你的 python.exe）加进白名单。命令行自检也能提示这一点：`python -m music_sync check`。

**Q：明明下过了，还重复下 / 歌名对不上？**
工具认的文件名是 **`歌手 - 歌名.mp3`**（中间是空格-空格）。原来的文件名是 `歌名 - 歌手` 或者纯歌名，它就会当成没下过。有两种办法：把本地文件名改成上面的格式，或者在设置里把 `overwrite`/判重相关的东西想清楚再跑 `--dry-run` 看看清单。版本词（`(Live)`、`(Remix)`、`(伴奏)`）会被当成不同的曲子，别随手删。

**Q：网易云报 404 是什么？**
这首没有版权，或者需要会员/付费才能听。工具会跳过它并把歌名列进失败清单，不会重试到天荒地老。

**Q：提示 `No module named 'cryptography'`？**
依赖没装全，回去跑一遍 `pip install -r requirements.txt`。没有它读不了网易云客户端的登录数据，其它平台不受影响。

**Q：计划任务到点了没反应？**
先看任务有没有跑过：`schtasks /Query /TN MusicSync /V /FO LIST`。注意任务的「工作目录」必须是项目根目录——用 `scripts\install_task.ps1` 注册就自动设好了，手动在任务计划程序里建容易漏。电脑当时在睡眠/关机也没关系，勾了「错过就开始」会在下次开机后补跑。

**Q：QQ音乐/酷狗一直显示未登录？**
这两个平台靠浏览器会话。先在 Chrome/Edge 里登录对应网页版，回到设置窗口点「自动获取」。浏览器用的是别的账号或者开了无痕窗口，工具就读不到。

## 卸载

```powershell
# 1. 删计划任务
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1 -TaskName MusicSync -Remove

# 2. 删程序目录
#    直接把 music-sync 文件夹删掉（如果建了 .venv 也一起删）

# 3. 删配置和日志（可选，想重来就先删它）
#    删掉 C:\Users\<你的用户名>\.music-sync 整个文件夹
```

没有注册表项、没有开机自启、没有服务，删完就干净了。

## 已知限制

说清楚，免得白折腾：

- **汽水音乐只有占位适配器**，不能登录、不能下载，只占个平台位。选它当主平台会直接报错。
- **QQ音乐 / 酷狗音乐的下载接口没有在真机账号上验证过**。代码是按公开接口写的，能跑通对账和登录检测，但真正下单/取地址的环节可能因为对方接口改动而失效，出问题请以日志为准。
- 只支持 **Windows**（依赖 Windows 计划任务、DPAPI 解密、`pythonw.exe`）。
- 歌单里需要会员才能听的歌，没有会员账号就是下不到，这不是 bug。
- 请只用来管理**你自己账号**的歌单，别拿去做批量分发或商业用途。

## 目录结构

```
music-sync/
├─ music_sync/            主程序包
│  ├─ config.py           配置读写
│  ├─ platforms/          各平台适配器（netease/qqmusic/kugou/soda）
│  ├─ matching.py         歌名对账
│  ├─ sync.py             同步主流程
│  ├─ login.py            登录态获取（客户端 → 浏览器 → 配置）
│  ├─ browser_cookies.py  读 Chrome/Edge 等浏览器的 cookie 库
│  ├─ selfcheck.py        环境自检
│  ├─ cli.py / __main__.py  命令行入口
│  └─ ui/                 设置界面、结果提示窗
├─ scripts/
│  └─ install_task.ps1    注册/更新/删除计划任务
├─ docs/
│  └─ devlog-entry.md     发布说明稿
├─ requirements.txt
└─ LICENSE
```

## 许可

MIT，见 [LICENSE](LICENSE)。
