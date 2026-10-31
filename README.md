# code-diff：先看懂 AI 写的代码，再决定收不收

一个 [Claude Code](https://docs.claude.com/en/docs/claude-code) skill（名字叫 `diff-review`）。Claude 改完代码后，会在浏览器里给你打开一个 review 页面，有点像老师批改作业：

- 像 GitHub 那样一个文件一个文件看改动：红色是删掉的，绿色是新加的。
- **哪行看不懂，点一下行号就能问 AI**：「这段在干嘛？」「为什么这么写？」「有没有更简单的写法？」回答直接贴在代码下面，还能接着追问。
- **逐行讲解**：让 AI 每几行配一句解释。
- **忘了某个参数、公式是什么意思，选中它就行**：双击一个词，或者拖选一个表达式，旁边会弹出一段从最基本讲起的白话解释。解释里还有不懂的词，接着选。
- **写得太绕就打回**：对某几行提修改意见，或者把整个文件标成「需重写」。
- 你提交以后，Claude 会自动收到意见去改。改完页面自己刷新到下一轮，你接着看，直到点「通过」为止。

灵感来自一条评论：「我做了一套让 agent 给我解释 diff 的 web UI，一有不懂的地方就逐行解释，代码整体太挫难以理解就打回重写。」

只需要 Python ≥ 3.7 和 git，不用装任何包。在自己电脑上能用，在远程服务器和 HPC 集群（Alliance / CCDB 的 Nibi、Fir 等）上也能用。

## 安装

在自己电脑上：

```bash
git clone git@github.com:junwei0102/code-diff.git ~/.claude/skills/diff-review
```

目录名必须是 `diff-review`。以后要更新，就在这个目录里 `git pull`。

装到集群或远程服务器上（在你自己电脑上运行，一条命令可以装好几台）：

```bash
~/.claude/skills/diff-review/scripts/install_remote.sh nibi fir
```

前提是那台机器上已经装好 Claude Code 并且登录过。

## 怎么用

### 1. 让 Claude 开 review

Claude 改完代码后，跟它说「review 一下你的改动」「我看不懂你改了什么」，或者直接输入 `/diff-review`。
Claude 会先写一份「作者说明」，讲清楚这次为什么这么改，然后给你一个链接。

### 2. 打开链接

- 在自己电脑上：直接打开就行。
- 在集群上：Claude 还会给你一条 `ssh -fN -L ...` 端口转发命令。在**你自己电脑**的终端里运行一次，再打开链接。详见下面的「在集群上用」。

### 3. 在页面上看、问、提意见

| 想做什么 | 怎么做 |
|---|---|
| 先了解全貌 | 看最上面的「作者说明」，或者点右上角「📖 整体讲解」 |
| 问某几行 | 点行号选中（按住 Shift 再点或者拖动可以多选），然后点「💬 这段在干嘛？」或「❓ 提问…」 |
| 让 AI 一行行讲 | 在每段改动的标题栏点「📖 逐行讲解这段」 |
| 忘了某个参数 / 公式 / 函数是什么意思 | 直接选中它（双击一个词，或者拖选一个表达式、几行代码），旁边会弹出白话解释。代码、AI 的回答、作者说明里都能选，解释里不懂的词也能接着选。想留着就点「📌 留在页面上」。不想自动弹出，可以在右上角 ⚙ 里关掉 |
| 问写代码的 Claude 本人 | 提问时勾选「交给作者回答」，或者在 AI 的回答下面点「转给作者」 |
| 这里要改 | 选中代码，点「🚩 这里要改…」，写下你希望怎么改 |
| 整个文件都不行 | 文件标题栏的「✗ 需重写」 |
| 这个文件没问题 | 文件标题栏的「✓ 看懂了」（会自动折叠，跳到下一个文件） |
| 切换中文 / English | 右上角的 EN / 中文 按钮（之后 AI 的回答也会改用这个语言） |

「问 AI」和「问作者」有什么区别：AI 会马上回答，但它只看得到代码，「为什么这么写」是它猜的。「问作者」要等下一轮，由写这段代码的 Claude 亲自回答，它知道当时为什么这么写。

### 4. 提交

点右上角「提交 review」，三选一：

- ✅ **通过**：看懂了，收下。
- 💬 **只提问**：先回答我的问题，代码先别动。
- ↩️ **打回修改**：按我的意见改。整体太挫就写「重写」。

提交以后 Claude 会自动收到你的 review：改代码，逐条回复你的意见，然后推送下一轮。页面会自己刷新，它的回复就挂在对应的讨论下面。这样一轮一轮来，直到你选「通过」。

## 在集群上用（远程服务器 / HPC 集群）

集群的登录节点是很多人共用的。为了安全，服务不开普通端口，而是监听一个只有你本人能访问的 Unix socket。所以你要在**自己电脑**上做一次端口转发，Claude 启动服务时会把完整命令打印出来，形如：

```bash
ssh -fN -L 36948:/run/user/<你的uid>/diff-review/current.sock <用户>@<服务所在节点的地址>
```

- **为什么要直接连那个节点：** 集群的登录地址后面通常有好几个登录节点，你每次 `ssh` 会落到其中随便一个，已经开着的主连接也一样。socket 只存在于服务所在的那个节点上，转发一旦走到别的节点就会失败。所以命令会直接指向那个节点；节点没有公网地址时，会经 `-J <登录地址>` 跳过去。
- 转发一次就行，之后每一轮、换别的仓库都能接着用。第一次可能要过一次二次验证（MFA），并确认一次主机指纹。
- **打不开：** 最常见的原因是本地端口被旧的转发占着（比如 Claude 换了节点，旧转发还连着原来那个节点）。先跑 `pkill -f -- "-L <端口>:"` 关掉旧转发，再重跑命令。agent 每次给链接时都会附上这条命令和其他排错提示。
- **一劳永逸：** 如果你总在同一个节点上跑 Claude（比如开在 tmux 里），可以把 ssh config 固定到那个节点，并自动带上转发，同一个链接以后一直能用。做法见 [references/remote-clusters.md](references/remote-clusters.md) 的「固定登录节点」。
- 用 VS Code 或 Cursor 远程开发：启动时加 `--listen tcp`，它们会自动帮你转发端口。

计算节点、排错等更多细节见 [references/remote-clusters.md](references/remote-clusters.md)。

## 更新

skill 本身就是一个 git 仓库（`~/.claude/skills/diff-review`）。改完以后：

1. **自己电脑**：改动 commit 之后 `git push`。其他电脑在 `~/.claude/skills/diff-review` 里 `git pull`。
2. **集群**：在自己电脑上再跑一次 `~/.claude/skills/diff-review/scripts/install_remote.sh <集群1> <集群2>`，会把本机最新的文件覆盖过去。集群上不需要能访问 GitHub。
3. **正在跑的 review 服务**：
   - 只改了网页（`ui.html`）：刷新浏览器页面就生效。
   - 改了 `diff_review.py`：先 `stop` 再 `start`，已经打开的页面会自动重新加载。
4. **Claude 会话**：新开的会话会用新版的 SKILL.md。

## 不经过 Claude，自己用

脚本可以单独运行，用来审任何 git 仓库里的改动（比如别的 AI 工具写的代码）：

```bash
S=~/.claude/skills/diff-review/scripts/diff_review.py
python3 $S start --repo .     # 启动，打印链接
python3 $S result --repo .    # 看你提交的 review
python3 $S stop --repo .      # 关掉
```

常用参数：

- `--base <commit>`：跟哪个版本比，默认是 HEAD。
- `--notes notes.md`：作者说明。
- `--exclude 'outputs/*'`：隐藏不想看的文件。
- `--model sonnet`：解释用的模型。

## 常见问题

- **解释代码的 AI 是什么？** 是你电脑或集群上的 Claude Code（`claude -p`）。它只有读文件的权限，而且只能读这个仓库。每次提问都会用掉一些你的 Claude 额度；右上角 ⚙ 可以换成更快的 Sonnet。
- **数据存在哪？** 讨论记录在 `~/.cache/diff-review/`，访问 token 在 `~/.config/diff-review/token`。不会往你的仓库里写任何东西。
- **服务会一直开着吗？** 6 小时没人用会自动关掉，也可以手动 `stop`。
- **页面打不开，或者提示「需要 token」？** 用 Claude 给的完整链接（末尾带 `?token=…`）。在集群上的话，先检查端口转发做了没有。

## 文件

```
SKILL.md                       给 Claude 看的工作流程说明
scripts/diff_review.py         后台服务 + 命令行（start / wait / result / status / stop）
scripts/ui.html                网页（纯 HTML/JS，无需构建）
scripts/install_remote.sh      装到远程机器
references/remote-clusters.md  集群上的细节和排错
```
