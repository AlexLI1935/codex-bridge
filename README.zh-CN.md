# Codex Bridge

[English](README.md) | [简体中文](README.zh-CN.md)

给本机 Codex CLI 加上一层轻量的任务管理：先检查额度，在后台执行，保存结果，需要时接着原来的会话做。

你只要给它项目目录和任务，之后就能用任务 ID 查看进度。调用它的 Agent
断开了，任务记录也还在。不需要绑定某一种 Agent，也不需要额外启动常驻服务。

**本项目不包含 Codex。** 请先安装并登录 [Codex CLI](https://learn.chatgpt.com/docs/cli)，
并准备 Python 3.10 或更新版本。Bridge 本身只用 Python 标准库。
当前版本 **1.0.0**，采用 MIT 许可证。

## 开始使用

```powershell
git clone https://github.com/AlexLI1935/codex-bridge.git
cd codex-bridge
codex --version
python codex_bridge.py init
python codex_bridge.py environment
python codex_bridge.py usage-refresh
```

如果提示需要认证，请自行运行 `codex login`，登录后重试。登录和 token
续期全部交给 Codex；Bridge 不读取或复制认证文件，也不实现 OAuth。
Windows 请使用原生 `codex.exe`。如果 PATH 找到的是 npm 的 `.cmd` 包装脚本，
用环境变量 `CODEX_BRIDGE_CODEX` 指定原生可执行文件。

把下面的目录占位符换成一个已经存在的绝对路径：

```powershell
python codex_bridge.py run --workdir "<项目绝对路径>" --mode write --prompt "修复失败的测试，运行测试，并说明改动。"
python codex_bridge.py status <任务ID>
python codex_bridge.py resume <任务ID> --prompt "再加一个边界测试并运行。"
python codex_bridge.py cancel <任务ID>
```

`run` 和 `resume` 默认在后台执行，立即返回任务 ID。想在前台等结果，加
`--wait`。长任务说明可以用 `run --prompt-file <文件>` 传入。
`status` 只读本地状态，不会反复查询额度。
取消请求会保存到磁盘，运行器通常在很短时间内处理；取消不会撤销已经做过的修改。
需要继续时，可以明确调用 `resume`。

默认 `read_only` 模式适合审查和调查。正常开发用 `--mode write`，采用 Codex
原生 `workspace-write` 沙箱，并关闭逐条交互审批。
只有用户明确授权更大范围时才用 `--mode full`；它对应 `danger-full-access`，
Bridge 不会自动升级权限。具体隔离能力仍取决于 Codex、操作系统和管理策略。
你也可以先自己建立 Git worktree，再把它的路径交给 Bridge；它不会主动创建分支、推送或部署。

## 额度怎么管理

每次真正启动或续跑之前，运行器都会读取配置，确认 **5 小时和周额度都还有余量**。
默认在 5H 使用达到 **90%**、或周使用达到 **95%** 时停止启动新任务，
尽量分别留出约 10% 和 5% 给你手动使用。这是启动门槛，不能保证运行中的任务绝不超出预留线。

`usage` 读取本地数据。`usage-refresh` 优先通过一个短暂运行的本机
`codex app-server` 标准输入输出进程，调用官方 `account/rateLimits/read`。
这一步不发起模型任务，也不留下常驻服务。旧 CLI 不支持时，才会在项目外的临时目录
运行一个极小的只读 `exec` probe，再重新读取 session 快照；这条备用路线会消耗少量额度。
Bridge 不抓取 TUI，也不会自动兑换额度重置机会。

默认复用不足五分钟的快照，刷新尝试也共用持久化的五分钟冷却时间。
数据过期或未知时，会刷新一次再读取；仍然拿不到，就如实报告并暂停派发。
两个窗口根据时长 300 / 10080 分钟识别，不依赖 primary/secondary 名称。
只有一个窗口时只报告那一个，但自动派发仍要求两个窗口都有新鲜数据。
重置前获取的旧快照不能拿来证明重置后已经恢复。

额度来源依赖当前 CLI、账号接口和 session 格式，**不是保证永久稳定的公共计费 API**。
格式不认识就返回 UNKNOWN，不编百分比。只用 API key 的账号可能没有订阅窗口数据。
同一运行目录应对应同一账号；切换账号后请刷新额度。

达到门槛后，任务保存为 `WAITING_USAGE_RESET`，记录阻塞窗口、使用比例、真实 reset
和本地显示时间。两个窗口同时阻塞时取较晚的 reset，`resume_after_epoch` 再加上配置的宽限秒数。
到时由调用方再次执行 `resume`，Bridge 会重新检查额度。这里没有自动唤醒服务，
需要自动恢复的调用方可以安排一次后续调用。

运行中真正耗尽额度，也会保存等待状态和原生 session ID。恢复后沿用原会话，
不会把整段任务从头重跑。如果 CLI 没提供可用的绝对 reset，就留空并等待后续查询，
绝不猜成“从现在起再等五小时”。没有 session ID 的中断任务需要人工处理；
认证失效则明确报告 `AUTH_REQUIRED`。

## 配置和时间

真实配置在运行目录的 `config.json`，不在 Git checkout 里：

- Windows 默认 `%LOCALAPPDATA%/codex-bridge`
- 其它系统默认 `~/.codex-bridge`
- 用 `CODEX_BRIDGE_HOME` 可以更换运行目录

[config.example.json](config.example.json) 是配置示例，不是运行真值，也不保存凭据。

```powershell
python codex_bridge.py config show
python codex_bridge.py config validate
python codex_bridge.py config set usage.five_hour_max_pct 85
python codex_bridge.py run --workdir "<项目绝对路径>" --prompt "检查这次改动。" --profile complex --model <模型ID> --reasoning-effort high
```

模型默认是 `null`，沿用你当前 Codex 的模型。两个 profile 默认分别使用
`medium` / `high` 推理强度；每个任务都能单独覆盖，不修改全局 Codex 设置。
也可以直接编辑配置，非法数值和未知字段会被拒绝。

`init` 或第一次派发时，会获取可信 HTTPS 响应中的 UTC 时间，读取本机墙钟，
把两者差值写成 `observed_utc_offset_minutes`，同时记录 OS 时区标签，不猜服务器所在地。
已有配置自洽时会跨启动复用；时区、offset、时钟发生明显变化，或运行
`init --refresh-time` 时才重新观察。联网失败会报告 UNVERIFIED，不编 offset。
系统 UTC 偏差超过 120 秒会报 `CLOCK_SKEW`，请先修正系统时钟。

reset 和等待逻辑始终以 **UTC / epoch** 为真值，执行超时用单调时钟。
观察到的 offset 只用于当前本地时间显示，不能代表未来的夏令时规则。
本项目没有本地周期日程引擎。

## 结果和进程保护

任务状态保存在 `tasks/<ID>.json`；每次执行的日志、`report.md` 和报告哈希凭据
保存在 `results/<ID>/attempt-N/`。这些文件可能含项目内容，请放在私人目录，别提交到 Git。

只有进程正常退出、JSON 流合法、有完成事件和最终回复，才会标记 `COMPLETED`。
`report_validated` 表示报告能与凭据匹配，不代表代码必然正确；仍应查看 diff 和测试。
其它状态包括 `QUEUED`、`RUNNING`、`FAILED`、`NEEDS_INPUT`、`WAITING_USAGE_RESET`、`CANCELLED`。
运行器意外中断后会报告 `NEEDS_INPUT`，不会悄悄重跑原任务。
POSIX 系统强杀运行器可能留下子进程；请先检查并停止它原本创建的进程树，
再清除任务记录中的 `cleanup_required` 标志，明确调用续跑。

同一个任务有内核锁防重复；不同任务可以并行，建议分别使用 worktree。
取消和超时只处理自己创建的进程树：Windows 用 Job Object，其它系统用进程组。
不会按进程名扫描和杀掉所有 Codex，用户正在使用的桌面 Codex 不属于清理目标。
停滞诊断会比较输出、对应 session 和项目文件活动，不会仅因长时间没输出就杀进程；
明确设置的执行超时仍然有效。

CLI 退出码：0 表示请求已接受、读取成功或任务完成；1 表示任务处于未成功状态；
2 表示操作或参数错误。后台任务请继续用 `status` 查看最终结果。
给 Agent 使用的操作说明见 [SKILL.md](SKILL.md)。

## 开发验证

```powershell
python -m unittest discover -s tests -v
```

离线测试使用合成事件和自己创建的子进程，不调用付费模型。
Windows 真实 smoke 覆盖后台写入、原 session 续跑和只读限制；Linux/macOS 的进程组行为仍需当地验证。
[DISCOVERY.md](DISCOVERY.md) 记录了已测 CLI 版本和公开接口来源。

## 作者

Created and maintained by [Alex Li](https://github.com/AlexLI1935).

[MIT License](LICENSE)，Copyright (c) 2026 Alex Li。
