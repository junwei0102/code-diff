# 在远程机器和 HPC 集群上用 diff-review

## 原理

在远程机器上（检测到 `SSH_CONNECTION` 或 `CC_CLUSTER`），服务不开 TCP 端口，而是监听一个 Unix socket：

- socket 在 `$XDG_RUNTIME_DIR/diff-review/`，没有这个变量时用 `/tmp/diff-review-<uid>/`。socket 权限是 0600，所在目录 0700，登录节点上的其他用户连不上。
- `current.sock` 永远指向最近一次启动的 review。所以端口转发只要做一次，之后的每一轮、甚至换个仓库，都能接着用。
- 页面和 API 还要求一个 token（保存在 `~/.config/diff-review/token`），防止本机上的其他程序或网页冒用这个端口。

为什么不用 TCP：登录节点上所有人都能连 `127.0.0.1`，而解释器能以用户的身份读代码。

**socket 只存在于服务所在的那个节点上。** 这是下面所有端口转发问题的根源。

## 集群上的准备（每个集群一次）

1. 集群上已经装好 Claude Code 并登录过。解释器调用的就是集群上的 `claude -p`。
2. 把 skill 拷到集群上。在你自己电脑上运行（主机名换成你 ssh config 里的名字）：
   ```bash
   ~/.claude/skills/diff-review/scripts/install_remote.sh <集群1> <集群2>
   ```
   脚本会把整个 skill 目录拷到每台机器的 `~/.claude/skills/diff-review`。更新 skill 之后再跑一次即可。
3. 需要 `python3` ≥ 3.7。大多数登录节点默认就有；版本太老就先 `module load python`。不用装任何包。

## 端口转发（用户在自己电脑上做）

集群的登录地址后面通常有好几个登录节点。可能是 DNS 轮询（比如 `fir.alliancecan.ca` 背后是 login1–3），也可能是负载均衡（比如 nibi 背后是 l1–l5）。每次新开的 SSH 连接会落到其中随便一个节点，**已经开着的 ControlMaster 主连接也一样**：Claude 可能一直在 login1 的 tmux 里跑，而你今天的主连接落在了 login3。转发一旦走到别的节点，那里没有这个 socket，就会失败。

所以 `start` 打印的转发命令会直接指向服务所在的节点：

```bash
# 节点有公网地址（服务自己验证过这个地址确实指向本机）
ssh -fN -L <端口>:<socket目录>/current.sock <用户>@<节点的公网地址>

# 节点没有公网地址（只有集群内部才能解析的名字，计算节点也是这样）：经登录地址跳过去
ssh -fN -J <用户>@<登录地址> -L <端口>:<socket目录>/current.sock <用户>@<节点名>
```

- `-fN` 让这条转发在后台常驻，之后每一轮都不用再跑。第一次可能要过一次二次验证（MFA），并确认一次主机指纹。
- 节点的公网地址是怎么找的：服务会试本机的完整域名，以及去掉其中一段之后的名字（内部名和公网名常常只差一段，比如 `login3.int.example.org` 和 `login3.example.org`），还会试当前 SSH 会话连进来的那个 IP。只有解析出的地址是公网地址、并且确实配在本机网卡上，才会采用。
- 登录地址（跳转用）：在集群上设环境变量 `DIFF_REVIEW_SSH_HOST` 就能指定，任何集群都适用。没设的话，在 Alliance 集群上会按 `CC_CLUSTER` 推出 `<集群>.alliancecan.ca`；其他集群就留一个 `<登录地址>`，让你自己填。
- 取消一条后台转发：`pkill -f -- "-L <端口>:"`。
- VS Code 或 Cursor 的 Remote-SSH 只会自动转发 TCP 端口。要用它们，就用 `start --listen tcp` 启动。这样安全性稍弱：同一节点上的其他用户能连到这个端口，只靠 token 挡着。

## 固定登录节点（一次设置，以后都不用管）

如果你总在同一个节点上跑 Claude（比如 tmux 一直开在某个登录节点上），可以把 ssh config 固定到这个节点，并让连接自动带上转发。端口、socket 路径和 token 都不会变，`current.sock` 永远指向最新一轮 review，所以同一个链接以后一直能用，可以加进收藏夹。

1. 先关掉现有的主连接：它可能连在别的节点上，还占着这个端口。这会断开通过它开着的终端，但集群上的 tmux 会话不受影响。
   ```bash
   ssh -O exit <别名>
   ```
2. 把 `~/.ssh/config` 里这个集群的 `Host` 段改成下面这样（尖括号里的值都在 `start` 的输出里）：
   ```
   Host <别名>
     HostName <节点的公网地址>          # 节点没有公网地址时，改成两行：HostName <节点名> 和 ProxyJump <用户>@<登录地址>
     HostKeyAlias <登录地址>             # 可选：各登录节点共用同一把 host key 时，沿用 known_hosts 里已有的记录，不再弹指纹确认
     User <集群用户名>
     ControlMaster auto
     ControlPath ~/.ssh/cm-%C
     ControlPersist 12h
     ServerAliveInterval 60
     LocalForward <端口> <socket目录>/current.sock

   Host <别名>-any                        # 那个节点维护或宕机时，用它登到别的节点
     HostName <登录地址>
     User <集群用户名>
   ```
3. 以后的用法：
   - 照常 `ssh <别名>`，转发会自动建立。
   - 只要转发、不要终端：`ssh -fN <别名>`。
   - 所有终端共用一条连接，不会再要 MFA，也不会报端口被占用。
   - 那个节点挂了：用 `ssh <别名>-any` 登到别的节点。这时 tmux 会话也没了，Claude 要在新节点上重开；新的 `start` 会打印新节点的转发命令，等原节点恢复再换回来。

## 在计算节点上跑 Claude（交互作业）

计算节点能不能上网取决于集群。Alliance 的 Nibi、Fir 能，Narval、Rorqual、tamIA 不能；不能上网的话，Claude 只能在登录节点上跑。在计算节点上跑时，服务也在计算节点上。计算节点没有公网地址，`start` 打印的就是经登录地址跳过去的那条命令（`-J`）。这需要你在那个节点上有正在运行的作业。

如果跳转走不通，退路是 `start --listen tcp --host 0.0.0.0`，再 `ssh -fN -L <端口>:<计算节点名>:<端口> <登录地址>`。这样同一集群的其他用户也能连到这个端口，只有 token 挡着，用完记得 `stop`。

## 登录节点礼仪

- 服务空闲时基本不占 CPU。解释器只在用户提问时短暂跑一个 `claude -p`，最多 3 个并行。
- 默认 6 小时没人用就自动退出（`--idle-hours` 可调）。review 结束后最好 `stop`。
- 登录节点偶尔会重启，后台进程会被一起杀掉；重新 `start` 就行，讨论记录保存在 `~/.cache/diff-review/` 里，不会丢。

## 排错清单

1. 在集群上运行 `status --repo <root>`：看服务在不在、在哪个节点、socket 路径，以及该用的转发命令（`ssh:` 那一行）。
2. ssh 报 `connect failed: No such file or directory` 或 `open failed`：转发落到了别的节点。改用 `start` 或 `status` 打印的那条直连节点的命令，或者按上面「固定登录节点」配置。
3. ssh 报 `bind [127.0.0.1]:<端口>: Address already in use`：本地端口被旧的转发占着（比如一条连在别的节点上的主连接）。先 `ssh -O exit <别名>`，或者 `pkill -f -- "-L <端口>:"`，再重跑。
4. 浏览器显示「需要 token」：打开的是没带 `?token=` 的地址。用 `start` 或 `status` 打印的完整链接。
5. 页面显示「AI 解释不可用」：集群上 PATH 里没有 `claude`，或者 Claude Code 没登录。重启时用 `--claude ~/.local/bin/claude`（或你的安装路径）。
6. 日志在 `~/.cache/diff-review/<仓库>-<hash>/server.log`。
