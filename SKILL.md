---
name: diff-review
description: 在浏览器里审 agent 写的代码改动（git diff）：起一个 web UI，逐文件看 diff；看不懂的行点一下就让 AI 逐行解释、可以追问；代码整体太挫看不懂就「打回重写」，review 结果自动交回给 agent 修改，一轮一轮直到用户通过。支持本机，也支持远程机器和 HPC 集群（Alliance / CCDB 的 Nibi、Fir、Narval、Rorqual 等，经 SSH 端口转发访问）。凡是用户想 review、看懂或批准 agent 刚做的改动，都用它——比如「review 一下你的改动」「打开 diff 界面」「我看不懂你改了什么」「逐行解释这个 diff」「改完先让我审一下」「/diff-review」，或者用户要求代码改动必须经他审核才算完。Use it whenever the user wants to review, understand, or approve code an agent changed (diff review UI, line-by-line explanations, send back for a rewrite). 不用于：自动找 bug 的代码审查（那是 /code-review）、给 GitHub PR 留评论。
---

# Diff Review：先让用户看懂，再让用户批准

工作流是这样的：agent 改完代码 → 用户在 web UI 里看 diff → 一有看不懂的地方就点一下，让 AI 逐行解释 → 代码整体太挫看不懂，就打回重写 → agent 按意见改 → 下一轮，直到用户通过。

**你在这里是「作者」**，要做三件事：启动 UI 并写一份作者说明；等用户提交 review；按意见改代码、回答用户留给你的问题，然后推送下一轮。

UI 里还有一个**解释器**。它是另起的 `claude -p`，只有只读工具，只能读这个仓库，用户提问时当场回答，不占你的上下文。解释器看不到你和用户的对话，所以它对「为什么这么改」的理解全部来自你写的作者说明。用户也可以把问题标成「问作者」，这类问题会在下一轮交给你回答，因为只有你知道当时为什么这么写。

下文 `<skill>` 指这个 SKILL.md 所在的目录（通常是 `~/.claude/skills/diff-review`）。所有操作都是这一个脚本，只依赖 Python ≥ 3.7 标准库和 git：

```bash
python3 <skill>/scripts/diff_review.py <start|wait|result|status|stop> --repo <仓库根目录> ...
```

---

## 第 1 步：确定对比基准

先 `git status --short` 看一眼。默认对比 `HEAD`，也就是工作区里所有未提交的改动，加上未跟踪的新文件。

- 如果你在这次任务里 **commit 过**，就用 `--base <你第一次 commit 之前的 sha>`，否则已经 commit 的改动不会出现在 diff 里。
- 只想审某几个 commit：`--base A..B`。这种情况下不包含未提交的改动和未跟踪的文件。
- 如果工作区里有不是你写的改动（用户自己没提交的修改），要在说明里讲清楚哪些是你的，免得用户把他自己的代码当成你的来审。
- 生成文件、日志、checkpoint 混进来了，就用 `--exclude 'outputs/*' --exclude '*.ckpt'` 排除。超过 50 个文件的未跟踪目录会自动折叠成一行。
- 不是 git 仓库：可以用 `--patch FILE` 审一个 unified diff；或者建议用户以后在你开始改代码之前先 `git init && git add -A && git commit -m baseline`。

## 第 2 步：写作者说明（最重要）

作者说明会显示在 UI 最上面，解释器回答每个问题时也会读。写到一个临时文件里（集群上放在 `$TMPDIR` 或 `~/.cache` 下就行），格式如下：

```markdown
一段话：这次改动要解决什么，整体思路，关键取舍。

## path/to/file.py
这个文件改了什么，为什么；哪些地方看着奇怪但是有意为之。

## other/file.py
...
```

- `## <仓库相对路径>` 标题下的内容会挂到对应文件上，其余内容都算整体说明。
- 写 diff 里看不出来的东西：动机、约束、考虑过但放弃的方案。不要复述 diff。
- 要诚实：临时方案、偷懒的地方、没测到的地方、你自己也不满意的写法，都写出来。这些用户迟早会发现，你先说了，他就能把精力花在真正需要他判断的地方。

## 第 3 步：启动

```bash
python3 <skill>/scripts/diff_review.py start --repo <root> --notes <notes.md> [--base <ref>]
```

- 命令几秒就返回，后台留一个小服务；6 小时没人用会自动退出。
- 把输出里的「打开」链接原样给用户。链接里的 token 是用户自己的，可以直接给。
- **在远程机器或集群上**（检测到 SSH 会话或 `CC_CLUSTER`），服务监听的是只有用户本人能连的 Unix socket。输出里会有一条端口转发命令，形如 `ssh -fN -L <端口>:<socket> <用户>@<节点>`：原样转给用户，请他在**自己电脑**的终端里跑一次，然后打开链接。这条转发之后每一轮都能接着用。
  - 这条命令直接指向服务所在的那个节点，**不要**改写成 `ssh -O forward … <集群名>` 这类连集群登录地址的形式。集群的登录地址后面通常有好几个登录节点，用户新开的连接（包括已经开着的主连接）不一定落在这个节点上，而 socket 只存在于这个节点。节点没有公网地址时，命令会经 `-J <登录地址>` 跳过去；如果命令里留着 `<登录地址>`，请用户换成他平时登录用的地址。
  - 用户总在固定节点上跑 Claude（比如 tmux 一直开在某个登录节点上），或者反复遇到连不上时，推荐 `references/remote-clusters.md` 里「固定登录节点」的 ssh config，设置一次以后就不用管了。
  - 用户说打不开，多半是转发落到了别的节点，或者本地端口被旧的转发占着。排错见 `references/remote-clusters.md`。
- 在本地，如果你有内置浏览器之类的工具，可以直接帮用户打开链接。

## 第 4 步：等用户提交

**放到后台运行**（`run_in_background`）。用户在页面上点「提交 review」后，它会打印 review 内容并退出，你会被自动唤醒：

```bash
python3 <skill>/scripts/diff_review.py wait --repo <root>
```

然后结束这一轮，用一两句话告诉用户怎么用：看不懂就点行号问 AI，看完点右上角「提交 review」，提交后你会自动接着干。如果你的环境收不到后台任务结束的通知，就请用户提交后在聊天里说一声，你再运行 `result --repo <root>` 读结果。

## 第 5 步：处理 review

`wait` / `result` 打印的是一份 markdown，包括：整体意见；修改意见（带代码和讨论记录）；留给你的问题；被标成「需重写」的文件；用户需要靠解释器才看懂的代码；以及接下来要跑的命令。按结论处理：

- **APPROVED（通过）**：简短总结。可以 `stop --repo <root>` 关掉服务。到此结束。
- **COMMENT（只提问）**：回答问题。只有回答本身说明代码确实该改时才改。
- **CHANGES REQUESTED（打回修改）**：处理每一条修改意见和每一个「需重写」的文件，怎么改见下一节。

然后推送下一轮：

1. 写 `replies.json`，**每个 thread id 都要回**：问题就给答案，修改意见就说你怎么改的、为什么这么改。
   ```json
   {"T3": "改成了显式的 if/else，参数改成具名的 …", "T5": "会丢掉最后一个 batch，因为 …"}
   ```
2. 更新作者说明，开头先写「这一轮改了什么」，逐条对上用户的意见。
3. 推送：
   ```bash
   python3 <skill>/scripts/diff_review.py start --repo <root> --notes <notes.md> --replies <replies.json>
   ```
   服务还在跑的话不会重启：直接开始新一轮，用户的页面会自动刷新，你的回复显示在对应的讨论里。
4. 再在后台跑一次 `wait`。这样循环，直到 APPROVED。

## 被打回时怎么改

用户打回，通常是因为**看不懂**。看不懂是代码的问题，不是用户的问题。目标是让人读一遍就懂：

- 控制流写直白些，少一层抽象，少一点「聪明」写法。能用 if/else 讲清楚的，就别压成一行表达式。
- 名字要说清楚它是什么。函数要短，只做一件事。只在「为什么」不明显的地方写简短注释。
- 先看看周边代码是怎么写的，跟仓库现有的写法保持一致。
- 用户说「整体太挫 / 重写」，可以推倒重来，但只撤销**你自己写的**部分，绝不能动用户原本未提交的改动。
- 不同意某条意见时，不要偷偷不改，也不要做表面文章（改个变量名不算改）。在 reply 里把理由讲清楚，让用户决定。
- review 里「用户靠解释器才看懂的地方」那一节不是硬性要求，但它说明那段代码读起来费劲，顺手改清楚通常是对的。

## 常见问题

| 现象 | 处理 |
|---|---|
| 页面提示「AI 解释不可用」 | 用户仍然可以把问题留给作者，或者直接打回。要恢复 AI 解释：重启时用 `--claude /path/to/claude` 或环境变量 `DIFF_REVIEW_CLAUDE` 指定 Claude Code 的路径 |
| `needs Python >= 3.7` | 集群上先 `module load python`。任意 3.x 都行，不用装任何包 |
| 用户在远程打不开页面 | 端口转发没做，或者转发落到了别的登录节点。见 `references/remote-clusters.md` |
| 想换 base，或者从头来 | 先 `stop` 再 `start`。`start --new-round` 可以强制开新一轮 |
| 查看运行状态 | `status --repo <root>`：显示链接、socket、当前轮次 |

解释器用的模型、思考力度和语言，可以在页面右上角 ⚙ 里改；`start --model sonnet` 用来设默认值。
