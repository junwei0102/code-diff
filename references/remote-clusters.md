# 在远程机器和 Alliance / CCDB 集群上用 diff-review

## 原理

在远程机器上（检测到 `SSH_CONNECTION` 或 `CC_CLUSTER`），服务不开 TCP 端口，而是监听一个 Unix socket：

- socket 在 `$XDG_RUNTIME_DIR/diff-review/`，没有这个变量时用 `/tmp/diff-review-<uid>/`。权限是 0600，所在目录 0700，登录节点上的其他用户连不上。
- `current.sock` 永远指向最近一次启动的 review。所以用户只要做一次端口转发，之后的每一轮、甚至换个仓库，都能接着用。
- 页面和 API 还额外要求一个 token（保存在 `~/.config/diff-review/token`），防止本机上其他程序或网页冒用这个端口。

为什么不用 TCP：登录节点上所有人都能连 `127.0.0.1`，而解释器能以用户的身份读代码。

## 集群上的准备（每个集群一次）

1. 集群上已经装好 Claude Code 并登录过：解释器调用的就是集群上的 `claude -p`。
2. 把 skill 拷到集群上。在你自己电脑上运行：
   ```bash
   ~/.claude/skills/diff-review/scripts/install_remote.sh nibi fir
   ```
   脚本会把整个 skill 目录拷到每台机器的 `~/.claude/skills/diff-review`。更新 skill 之后再跑一次即可。
3. 需要 `python3` ≥ 3.7。登录节点默认就有一个 python3；如果版本太老，先 `module load python/3.11`。不用装任何包。

## 端口转发（用户在自己电脑上做）

`start` 会把这几行打印出来，端口和 socket 路径都已经填好：

```bash
# 推荐：复用已经打开的 ControlMaster 连接。不用再过 MFA，而且一定落在 Claude 所在的那个登录节点
ssh -O forward -L <端口>:<socket>/current.sock nibi

# 取消转发
ssh -O cancel  -L <端口>:<socket>/current.sock nibi
```

- Alliance 官方推荐在 `~/.ssh/config` 里开 ControlMaster（`ControlMaster auto`、`ControlPath ~/.ssh/cm-%r@%h:%p`、`ControlPersist`），这样后续连接不会反复要 Duo。开了之后，`ssh -O forward` 会给已有连接追加一条转发，走的正是你运行 Claude 的那个 SSH 会话，自然就是同一个登录节点。
- 没开 ControlMaster：用 `ssh -N -L <端口>:<socket> nibi` 新开一个隧道（会要 MFA）。注意 `nibi.alliancecan.ca` 后面有好几个登录节点（l1…l5），`fir.alliancecan.ca` 是 3 个 IP 轮询，新连接可能落到别的节点上，那个节点上没有你的 socket。这时 ssh 会报 `connect failed: No such file or directory`，可以先在隧道里 `hostname` 确认一下。最省事的办法是开 ControlMaster，然后从同一个主连接派生出所有会话。
- 想永久生效：在 `~/.ssh/config` 对应集群的 `Host` 段里加一行 `LocalForward <端口> <socket目录>/current.sock`。端口由 uid 和集群名算出来，每个集群固定，所以 nibi 和 fir 可以同时转发，不会撞端口。
- VS Code 或 Cursor 的 Remote-SSH 只会自动转发 TCP 端口。要用它们，就用 `start --listen tcp` 启动（安全性稍弱：登录节点上的其他人能连到这个端口，只靠 token 挡住）。另外 Alliance 文档说 Fir 的登录节点禁止 VS Code。

## 在计算节点上跑 Claude（salloc 交互作业）

Nibi 和 Fir 的计算节点能上网，所以 Claude Code 也可以在交互作业里跑；Narval、Rorqual、tamIA 的计算节点不能上网，Claude 只能在登录节点上跑。在计算节点上跑时，服务也在计算节点上，转发要多跳一下：

```bash
ssh -J nibi -N -L <端口>:<socket>/current.sock <计算节点名>   # 需要你在那个节点上有正在运行的作业
```

如果这条走不通，退路是 `start --listen tcp --host 0.0.0.0`，再 `ssh -O forward -L <端口>:<计算节点名>:<端口> nibi`。这样同一集群的其他用户也能连到这个端口，只有 token 挡着，用完要记得 `stop`。

## 登录节点礼仪

- 服务空闲时基本不占 CPU。解释器只在用户提问时短暂跑一个 `claude -p`，最多 3 个并行。
- 默认 6 小时没人用就自动退出（`--idle-hours` 可调）。review 结束后最好 `stop`。
- 登录节点偶尔会重启，后台进程会被一起杀掉；重新 `start` 就行，讨论记录保存在 `~/.cache/diff-review/` 里，不会丢。

## 排错清单

1. `status --repo <root>`：服务在不在、在哪个节点、socket 路径。
2. 在你自己电脑上，用 `ssh -O check nibi` 看主连接还在不在。
3. 浏览器显示「需要 token」：打开的是没带 `?token=` 的地址。用 `start` 或 `status` 打印的完整链接。
4. 页面显示「AI 解释不可用」：集群上 PATH 里没有 `claude`，或者 Claude Code 没登录。重启时用 `--claude ~/.local/bin/claude`（或你的安装路径）。
5. 日志在 `~/.cache/diff-review/<仓库>-<hash>/server.log`。
