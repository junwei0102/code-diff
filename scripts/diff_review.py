#!/usr/bin/env python3
"""diff-review: read an agent's diff in the browser, ask an AI to explain anything you
don't understand, then approve it or send it back for a rewrite.

Standard library only (Python >= 3.7). Works on a laptop and on remote / HPC login
nodes; there it listens on a private Unix socket that you reach with an SSH forward.

  start   start the review server for a repo, or push a new round to a running one
  wait    block until the reviewer submits the current round, then print the review
  result  print the latest submitted review
  status  show the server running for a repo
  stop    stop it
  serve   run the server in the foreground (`start` launches this in the background)
"""
import sys

if sys.version_info < (3, 7):
    sys.exit("diff-review needs Python >= 3.7 (on Alliance clusters: module load python)")

import argparse
import fnmatch
import getpass
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler

APP = "diff-review"
SCRIPT = os.path.abspath(__file__)
UI_FILE = os.path.join(os.path.dirname(SCRIPT), "ui.html")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
COOKIE = "dr_token"

MAX_UNTRACKED_FILES = 200       # untracked files beyond this are listed as skipped
MAX_UNTRACKED_DIR_FILES = 50    # an untracked directory with more files is not expanded
MAX_TEXT_BYTES = 1 << 20        # untracked files larger than this are shown without content
FILE_DIFF_BUDGET = 60000        # characters of one file's diff in an explainer prompt
WHOLE_DIFF_BUDGET = 120000      # characters of the whole diff in an overview prompt
MAX_ANCHOR_LINES = 400
MAX_ANNOTATE_ROWS = 400
MAX_PARALLEL_JOBS = 3
JOB_TIMEOUT = 600               # seconds before an explainer run is killed

EXPLAINER_TOOLS = "Read,Grep,Glob"
DENIED_TOOLS = "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch"
# Set by a parent Claude Code session; the nested `claude -p` must not inherit them.
PARENT_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_CODE_SSE_PORT",
)
LANG_NAMES = {"zh": "Simplified Chinese", "en": "English"}
STATUS_WORDS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied"}
VERDICTS = {
    "approve": "APPROVED — 通过",
    "comment": "COMMENT — 只有问题，不要求改代码",
    "changes": "CHANGES REQUESTED — 打回修改",
}

NOT_A_REPO = """%s is not inside a git repository.
diff-review shows a git work tree against a base revision. Either
  - run it inside a git repo (tip: commit a baseline before the agent starts editing,
    so its changes show up as a diff), or
  - pass --patch FILE with a unified diff (for example from `diff -ruN old/ new/`)."""

DENIED_PAGE = """<!doctype html><meta charset="utf-8"><title>diff-review</title>
<body style="font:15px/1.6 system-ui,sans-serif;max-width:560px;margin:15vh auto;padding:0 16px">
<h2>需要 token · Token required</h2>
<p>请打开 <code>diff_review.py start</code> 打印出来的完整链接（带 <code>?token=…</code>）。这个页面能让 AI 读你的代码，所以没有 token 不让进。</p>
<p>Open the full link printed by <code>diff_review.py start</code> (it ends with <code>?token=…</code>). This page lets an AI read your code, so it requires the token.</p></body>"""


# ----------------------------------------------------------------------------- small helpers

def now():
    return time.time()


def app_dir(xdg_var, fallback):
    return os.path.join(os.environ.get(xdg_var) or os.path.expanduser(fallback), APP)


def private_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


def write_atomic(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def one_line(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def excerpt(text, limit):
    """The start of a long text, cut at a line break when possible."""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut if cut > limit // 2 else limit].rstrip() + "\n…"


def load_token():
    """Per-user secret that every request must carry (stable across rounds and repos)."""
    token = os.environ.get("DIFF_REVIEW_TOKEN", "").strip()
    if token:
        return token
    path = os.path.join(private_dir(app_dir("XDG_CONFIG_HOME", "~/.config")), "token")
    try:
        token = read_text(path).strip()
    except OSError:
        token = ""
    if len(token) < 16:
        token = secrets.token_urlsafe(18)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    return token


def default_port():
    """A stable per-user (and per-cluster) port, so SSH forwards and bookmarks keep working."""
    if os.environ.get("DIFF_REVIEW_PORT"):
        return int(os.environ["DIFF_REVIEW_PORT"])
    cluster = os.environ.get("CC_CLUSTER", "")
    return 20000 + (os.getuid() + zlib.crc32(cluster.encode("utf-8"))) % 20000 if cluster else 20000 + os.getuid() % 20000


def is_remote():
    return any(os.environ.get(k) for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "CC_CLUSTER"))


def socket_dir():
    """A node-local directory only this user can enter, for the Unix socket."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        return private_dir(os.path.join(base, APP))
    path = "/tmp/%s-%d" % (APP, os.getuid())
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit("refusing to use %s: it is not a directory owned by you" % path)
    os.chmod(path, 0o700)
    return path


def child_env():
    env = dict(os.environ)
    for key in PARENT_SESSION_VARS:
        env.pop(key, None)
    return env


def git(args, cwd, check=True):
    proc = subprocess.run(["git", "-c", "core.quotepath=off"] + list(args), cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout.decode("utf-8", "replace")


def git_ok(args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def read_optional(path):
    return read_text(path) if path else None


def read_replies(path):
    if not path:
        return None
    try:
        data = json.loads(read_text(path))
    except ValueError as e:
        raise SystemExit("--replies %s is not valid JSON: %s" % (path, e))
    if not isinstance(data, dict):
        raise SystemExit('--replies must be a JSON object like {"T3": "reply"}')
    return data


# ----------------------------------------------------------------------------- unified diff parsing

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_c(text):
    """Undo git's C-style quoting of unusual paths: "caf\\303\\251" -> café."""
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return text
    body, out, i = text[1:-1], bytearray(), 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            if re.match(r"[0-7]{3}$", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
            else:
                out.extend(bytes([C_ESCAPES[body[i + 1]]]) if body[i + 1] in C_ESCAPES
                           else body[i + 1].encode("utf-8"))
                i += 2
            continue
        out.extend(body[i].encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def split_quoted(text):
    """(first token, rest) when `text` starts with a C-quoted string, else (None, text)."""
    text = text.lstrip()
    if text.startswith('"'):
        i = 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                return unquote_c(text[:i + 1]), text[i + 1:]
            i += 1
    return None, text


def marker_path(raw):
    """Path from a '--- x' or '+++ x' line; None for /dev/null."""
    token, _ = split_quoted(raw)
    if token is None:
        token = raw.split("\t", 1)[0].rstrip()  # `diff -u` appends "\t<timestamp>"
    return None if token == "/dev/null" else token


def header_paths(rest):
    """Both paths of a 'diff --git a/x b/y' line, still carrying their a/ and b/ prefixes."""
    first, tail = split_quoted(rest)
    if first is not None:
        second, _ = split_quoted(tail)
        return first, second if second is not None else tail.strip()
    n = len(rest)
    if n % 2 == 1:  # the common case "a/P b/P"
        left, right = rest[:n // 2], rest[n // 2 + 1:]
        if left[2:] == right[2:]:
            return left, right
    k = rest.find(" b/")
    return (rest[:k], rest[k + 1:]) if k > 0 else (rest, rest)


def strip_ab(path):
    return path[2:] if path and path[:2] in ("a/", "b/") else path


def new_file(path=None):
    return {"path": path, "oldPath": None, "status": "M", "binary": False, "untracked": False,
            "hunks": [], "additions": 0, "deletions": 0, "meta": [], "skipped": None}


def parse_patch(text):
    """Parse `git diff` output (or a plain `diff -u` patch) into files -> hunks -> rows.

    A row is [marker, old_line, new_line, text], marker being "+", "-" or " ", plus a
    trailing 1 when that line has no newline at the end of the file."""
    git_style = ("\n" + text).find("\ndiff --git ") >= 0
    files, cur, hunk, last_row, minus = [], None, None, None, None
    rem_old = rem_new = old_no = new_no = 0
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if hunk is not None and (rem_old > 0 or rem_new > 0):
            tag = line[:1]
            if tag in (" ", "") and rem_old > 0 and rem_new > 0:
                last_row = [" ", old_no, new_no, line[1:]]
                old_no, new_no, rem_old, rem_new = old_no + 1, new_no + 1, rem_old - 1, rem_new - 1
            elif tag == "-" and rem_old > 0:
                last_row = ["-", old_no, None, line[1:]]
                old_no, rem_old = old_no + 1, rem_old - 1
                cur["deletions"] += 1
            elif tag == "+" and rem_new > 0:
                last_row = ["+", None, new_no, line[1:]]
                new_no, rem_new = new_no + 1, rem_new - 1
                cur["additions"] += 1
            elif tag == "\\":
                if last_row is not None and len(last_row) == 4:
                    last_row.append(1)
                continue
            else:
                hunk = None  # malformed hunk; read the line as a header below
            if hunk is not None:
                hunk["rows"].append(last_row)
                continue
        if line.startswith("\\"):
            if last_row is not None and len(last_row) == 4:
                last_row.append(1)
            continue
        m = HUNK_RE.match(line)
        if m and cur is not None:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            rem_old = int(m.group(2)) if m.group(2) is not None else 1
            rem_new = int(m.group(4)) if m.group(4) is not None else 1
            hunk = {"header": line, "oldStart": old_no, "oldLen": rem_old,
                    "newStart": new_no, "newLen": rem_new, "rows": []}
            cur["hunks"].append(hunk)
            last_row = None
            continue
        hunk = None
        if line.startswith("diff --git "):
            old, new = header_paths(line[11:])
            cur = new_file(strip_ab(new))
            cur["oldPath"], cur["_header"] = strip_ab(old), True
            files.append(cur)
            minus = None
            continue
        if line.startswith("--- "):
            minus = line[4:]
            continue
        if line.startswith("+++ ") and minus is not None:
            old, new = marker_path(minus), marker_path(line[4:])
            if git_style:
                old, new = strip_ab(old), strip_ab(new)
            if cur is None or cur["hunks"] or not cur.get("_header") or cur.get("_markers"):
                cur = new_file()
                files.append(cur)
            cur["_markers"] = True
            if old is None:
                cur["status"], cur["path"] = "A", new
            elif new is None:
                cur["status"], cur["path"] = "D", old
            else:
                cur["path"], cur["oldPath"] = new, old
            minus = None
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["status"] = "A"
        elif line.startswith("deleted file mode"):
            cur["status"] = "D"
        elif line.startswith("rename from "):
            cur["status"], cur["oldPath"] = "R", unquote_c(line[12:])
        elif line.startswith("rename to "):
            cur["status"], cur["path"] = "R", unquote_c(line[10:])
        elif line.startswith("copy from "):
            cur["status"], cur["oldPath"] = "C", unquote_c(line[10:])
        elif line.startswith("copy to "):
            cur["status"], cur["path"] = "C", unquote_c(line[8:])
        elif line.startswith(("old mode", "new mode", "similarity index", "dissimilarity index")):
            cur["meta"].append(line)
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur["binary"] = True
    for f in files:
        f.pop("_header", None)
        f.pop("_markers", None)
        if not git_style:
            tidy_plain_paths(f)
        if f["status"] not in ("R", "C"):
            f["oldPath"] = None
    return [f for f in files if f["path"]]


def tidy_plain_paths(f):
    """`diff -ruN old/ new/` names files old/x and new/x: show them as x."""
    old, new = f.get("oldPath"), f["path"]
    tail = lambda p: p.split("/", 1)[1] if p and "/" in p else p  # noqa: E731
    if old and new and old != new and tail(old) == tail(new):
        f["path"] = tail(new)
    hunks = f["hunks"]
    if len(hunks) == 1 and hunks[0]["oldLen"] == 0 and hunks[0]["oldStart"] == 0:
        f["status"] = "A"
    elif len(hunks) == 1 and hunks[0]["newLen"] == 0 and hunks[0]["newStart"] == 0:
        f["status"] = "D"


def flat_rows(f):
    return [row for h in f["hunks"] for row in h["rows"]]


def count_files(top, limit):
    n = 0
    for _dir, _subdirs, names in os.walk(top):
        n += len(names)
        if n >= limit:
            break
    return n


# ----------------------------------------------------------------------------- the repository

class Repo:
    """Where the diff comes from: a git work tree (the default) or a patch file."""

    def __init__(self, path=None, base=None, patch=None, untracked=True, excludes=()):
        start = os.path.abspath(os.path.expanduser(path or "."))
        if os.path.isfile(start):
            start = os.path.dirname(start)
        self.patch = os.path.abspath(os.path.expanduser(patch)) if patch else None
        self.root, self.git_dir = start, None
        try:
            self.root = git(["rev-parse", "--show-toplevel"], start).strip()
            self.git_dir = git(["rev-parse", "--absolute-git-dir"], start).strip()
        except (RuntimeError, OSError):
            if not self.patch:
                raise SystemExit(NOT_A_REPO % start)
        self.name = os.path.basename(self.root.rstrip(os.sep)) or self.root
        self.untracked = untracked
        self.excludes = [e for e in excludes or () if e]
        self.set_base(base)

    def key(self):
        ident = self.patch or self.git_dir
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", self.name)[:20]
        return "%s-%s" % (slug, hashlib.sha1(ident.encode("utf-8")).hexdigest()[:10])

    def set_base(self, base):
        self.base = (base or "").strip()
        self.range = ".." in self.base
        if self.patch or not self.git_dir:
            self.base_ref = None
        elif self.base:
            self.base_ref = self.base
        elif git_ok(["rev-parse", "--verify", "-q", "HEAD"], self.root):
            self.base_ref = "HEAD"
        else:
            self.base_ref = EMPTY_TREE  # a repo without commits: everything is new

    def describe(self):
        d = {"name": self.name, "root": self.root, "host": socket.gethostname(), "patch": self.patch,
             "isGit": bool(self.git_dir), "base": self.base or "HEAD", "range": self.range,
             "emptyBase": self.base_ref == EMPTY_TREE, "branch": None, "baseSha": None}
        if self.git_dir and not self.patch:
            d["branch"] = git(["symbolic-ref", "--short", "-q", "HEAD"], self.root, check=False).strip() or "detached HEAD"
            if not self.range and self.base_ref != EMPTY_TREE:
                d["baseSha"] = git(["rev-parse", "--short", self.base_ref], self.root, check=False).strip() or None
        return d

    def load(self):
        """The current diff as (files, skipped)."""
        if self.patch:
            files = parse_patch(read_text(self.patch))
        else:
            files = parse_patch(git(["diff", "--no-color", "--no-ext-diff", "-M", "--src-prefix=a/",
                                     "--dst-prefix=b/", self.base_ref, "--"], self.root))
        skipped = []
        if self.git_dir and not self.patch and not self.range and self.untracked:
            paths, skipped = self.untracked_paths()
            files.extend(self.untracked_file(p) for p in paths)
        if self.excludes:
            kept = []
            for f in files:
                name = os.path.basename(f["path"])
                if any(fnmatch.fnmatch(f["path"], g) or fnmatch.fnmatch(name, g) for g in self.excludes):
                    skipped.append({"path": f["path"], "code": "excluded"})
                else:
                    kept.append(f)
            files = kept
        files.sort(key=lambda f: f["path"])
        for f in files:
            sig = json.dumps([f["status"], f["oldPath"], f["binary"], f["skipped"], f["hunks"]])
            f["hash"] = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
        return files, skipped

    def untracked_paths(self):
        out = git(["ls-files", "--others", "--exclude-standard", "--directory", "--no-empty-directory", "-z"], self.root)
        paths, skipped = [], []
        for entry in filter(None, out.split("\0")):
            if not entry.endswith("/"):
                paths.append(entry)
                continue
            if count_files(os.path.join(self.root, entry), MAX_UNTRACKED_DIR_FILES + 1) > MAX_UNTRACKED_DIR_FILES:
                skipped.append({"path": entry, "code": "big-dir", "n": MAX_UNTRACKED_DIR_FILES})
                continue
            inner = git(["ls-files", "--others", "--exclude-standard", "-z", "--", entry], self.root)
            paths.extend(p for p in inner.split("\0") if p)
        if len(paths) > MAX_UNTRACKED_FILES:
            extra = paths[MAX_UNTRACKED_FILES:]
            skipped.append({"path": "", "code": "too-many", "n": len(extra), "example": extra[0]})
            paths = paths[:MAX_UNTRACKED_FILES]
        return paths, skipped

    def untracked_file(self, rel):
        f = new_file(rel)
        f["status"], f["untracked"] = "A", True
        full = os.path.join(self.root, rel)
        try:
            if os.path.islink(full):
                data = os.readlink(full).encode("utf-8")
                f["meta"].append("symlink")
            else:
                size = os.path.getsize(full)
                if size > MAX_TEXT_BYTES:
                    f["skipped"] = {"code": "too-large", "mb": round(size / 1048576.0, 1)}
                    return f
                with open(full, "rb") as fh:
                    data = fh.read()
        except OSError as e:
            f["skipped"] = {"code": "unreadable", "detail": str(e.strerror or e)}
            return f
        if b"\0" in data[:8192]:
            f["binary"] = True
            return f
        text = data.decode("utf-8", "replace")
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        rows = [["+", None, i + 1, line] for i, line in enumerate(lines)]
        if rows and not text.endswith("\n"):
            rows[-1].append(1)
        if rows:
            f["hunks"] = [{"header": "@@ -0,0 +1,%d @@" % len(rows), "oldStart": 0, "oldLen": 0,
                           "newStart": 1, "newLen": len(rows), "rows": rows}]
        f["additions"] = len(rows)
        return f


def state_dir(repo):
    return private_dir(os.path.join(app_dir("XDG_CACHE_HOME", "~/.cache"), repo.key()))


# ----------------------------------------------------------------------------- author notes

def parse_notes(markdown, paths):
    """Split the author's notes into an overall summary and per-file notes.

    A '## <path>' heading naming a changed file starts that file's note; everything
    else (other headings included) belongs to the summary."""
    summary, per_file, current = [], {}, None
    for line in (markdown or "").splitlines():
        m = re.match(r"^#{2,3}\s+(.+?)\s*#*\s*$", line)
        if m:
            current = match_path(m.group(1).strip().strip("`*_").strip(), paths)
            if current:
                per_file.setdefault(current, [])
                continue
        (per_file[current] if current else summary).append(line)
    notes = {p: "\n".join(v).strip() for p, v in per_file.items()}
    return {"summary": "\n".join(summary).strip(), "files": {p: v for p, v in notes.items() if v}}


def match_path(name, paths):
    if name in paths:
        return name
    hits = [p for p in paths if p.endswith("/" + name) or name.endswith("/" + p)]
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------------------- persistent state

class Store:
    """Threads, verdicts and rounds of one repo's review, kept in a JSON file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.dirty = False
        try:
            data = json.loads(read_text(path))
        except (OSError, ValueError):
            data = {}
        defaults = {"version": 0, "round": 1, "rounds": {}, "threads": {}, "annotations": {},
                    "verdicts": {}, "deleted": {}, "next": {},
                    "settings": {"model": "", "lang": "zh", "effort": ""}}
        for key, value in defaults.items():
            data.setdefault(key, value)
        for key, value in defaults["settings"].items():
            data["settings"].setdefault(key, value)
        self.data = data

    def bump(self, obj=None):
        """Advance the version (clients poll for changes since a version)."""
        self.data["version"] += 1
        if obj is not None:
            obj["v"] = self.data["version"]
        self.dirty = True
        return self.data["version"]

    def new_id(self, prefix):
        n = self.data["next"].get(prefix, 1)
        self.data["next"][prefix] = n + 1
        return "%s%d" % (prefix, n)

    def delete(self, kind, item_id):
        if self.data[kind].pop(item_id, None) is not None:
            self.data["deleted"][item_id] = self.bump()

    def save(self, force=False):
        with self.lock:
            if self.dirty or force:
                write_atomic(self.path, json.dumps(self.data, ensure_ascii=False))
                self.dirty = False


# ----------------------------------------------------------------------------- the explainer (claude -p)

PERSONA = """You are the explainer inside a diff-review web UI. A developer is reviewing a change \
that an AI coding agent made to the repository in the current working directory. They want to \
truly understand it before accepting it, and they send code back for a rewrite when it is too \
convoluted to understand.

How to answer:
- Write in {lang}: the reviewer picked it with the UI's language switch, so it overrides any other \
language preference (for example one in CLAUDE.md). Keep code identifiers as they are.
- Open with a direct one- or two-sentence answer, then give the details. Prefer short paragraphs, \
bullet lists and small code snippets. No filler, and don't restate the diff.
- Explain what the code does, why it is (probably) written this way (use the author's notes when \
they help), and how it fits into the rest of the codebase. Read, Grep and Glob files in the \
repository whenever that makes your answer more accurate; you have read-only access.
- Refer to lines as L123 (new version) or "old L45" (removed lines), and to other code as path/to/file.py:123.
- Be candid. If the code is convoluted, over-engineered, buggy, or inconsistent with the rest of \
the codebase, say so plainly and sketch a simpler alternative. Don't defend code just because it exists.
- You cannot edit files or run commands, so never say you changed anything."""

DIFF_LEGEND = "Columns: old line | new line | marker (+ added, - removed, blank = unchanged) | code"

OVERVIEW_TASK = """# Task: a guided tour of this change
Write it in {lang}, scannable, with file paths and L-numbers so the reviewer can jump there:
1. One or two sentences: what the change does and why.
2. Suggested reading order: the files / functions to read, in order; for each, what changed, why, \
and how it connects to the previous step.
3. What deserves the closest scrutiny (subtle behavior changes, risky logic, missing tests).
4. Anything that looks convoluted, redundant or inconsistent with the codebase, with a simpler alternative."""

ANNOTATE_TASK = """# Task: line-by-line walkthrough of rows r{a}-r{b}
The reviewer finds these rows hard to follow. Walk them through.
- Split the rows into consecutive groups, each one logical step (usually 1-6 rows). For each group \
write 1-3 sentences in {lang}: what it does and why. When added (+) rows replace removed (-) rows, \
say what changed and why.
- Cover every added and removed row, but fold blank lines, imports and other trivial rows into a neighbouring \
group instead of giving them their own note. Unchanged rows only need a note when they matter.
- Groups must not overlap and must stay within r{a}-r{b}. Read other files if you need context.
Reply with ONLY a JSON array in a ```json fence, nothing before or after it:
```json
[{{"from": "r{a}", "to": "r{a}", "note": "..."}}]
```"""


def row_line(row, rid=None):
    mark, old, new, text = row[0], row[1], row[2], row[3]
    prefix = "" if rid is None else "r%-4d " % rid
    return "%s%5s %5s %s | %s" % (prefix, "" if old is None else old, "" if new is None else new, mark, text)


def diff_text(f, focus=None, budget=FILE_DIFF_BUDGET, ids=False):
    """A file's diff as numbered text; when too long, only the hunks near `focus` rows."""
    chunks, idx = [], 0
    for h in f["hunks"]:
        body = [row_line(r, idx + k if ids else None) for k, r in enumerate(h["rows"])]
        chunks.append((idx, idx + len(h["rows"]) - 1, "\n".join([h["header"]] + body)))
        idx += len(h["rows"])
    text = "\n".join(c[2] for c in chunks)
    if len(text) <= budget:
        return text
    if focus is not None:
        a, b = focus
        text = "\n".join(c[2] for c in chunks if c[1] >= a - 150 and c[0] <= b + 150)
    return text[:budget] + "\n... (diff truncated; Read the file for the rest)"


def describe_tool(block, root):
    """What the explainer looked at, as {tool, arg, where}; the UI words it in the reader's language."""
    name, args = block.get("name") or "tool", block.get("input") or {}

    def rel(p):
        p = str(p or "")
        return os.path.relpath(p, root) if p.startswith(root + os.sep) else p

    if name == "Read":
        return {"tool": "read", "arg": rel(args.get("file_path"))}
    if name == "Grep":
        return {"tool": "grep", "arg": str(args.get("pattern", "")),
                "where": rel(args.get("path")) if args.get("path") else ""}
    if name == "Glob":
        return {"tool": "glob", "arg": str(args.get("pattern", ""))}
    return {"tool": name, "arg": ""}


def find_claude(explicit=None):
    """Locate a working Claude Code CLI; returns (path, version) or (None, "")."""
    candidates = [explicit, os.environ.get("DIFF_REVIEW_CLAUDE"), os.environ.get("CLAUDE_CODE_EXECPATH"),
                  shutil.which("claude"), "~/.local/bin/claude", "~/.claude/local/claude",
                  "~/.npm-global/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    seen = set()
    for cand in candidates:
        if not cand:
            continue
        path = os.path.expanduser(cand)
        if path in seen or not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        seen.add(path)
        try:
            out = subprocess.run([path, "--version"], env=child_env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=60).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
        if "claude code" in out.lower():
            return path, out.strip()
    return None, ""


class Job:
    def __init__(self, kind, item_id, msg_id, prompt, settings):
        self.kind, self.item_id, self.msg_id, self.prompt = kind, item_id, msg_id, prompt
        self.settings = dict(settings)
        self.key = "%s:%s" % (item_id, msg_id or "")


class Explainer:
    """Answers questions about the diff with `claude -p`, read-only and confined to the repo."""

    def __init__(self, app, explicit=None):
        self.app = app
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(MAX_PARALLEL_JOBS)
        self.running, self.cancelled = {}, set()
        self.active = 0
        self.caps, self.error, self.error_code = {}, None, None
        self.bin, self.version = find_claude(explicit)
        if not self.bin:
            self.error_code = "no-claude"
            self.error = "no working claude CLI found (pass --claude /path/to/claude or set DIFF_REVIEW_CLAUDE)"
            return
        try:
            help_text = subprocess.run([self.bin, "--help"], env=child_env(), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, timeout=60).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError) as e:
            self.error_code = "help-failed"
            self.error = "claude --help failed: %s" % e
            return
        for flag in ("--include-partial-messages", "--tools", "--disallowedTools", "--no-session-persistence",
                     "--strict-mcp-config", "--settings", "--effort"):
            self.caps[flag] = flag in help_text
        self.caps["dontAsk"] = "dontAsk" in help_text
        # A settings file (not inline JSON) works with every CLI version that has --settings.
        self.settings_file = os.path.join(app.state_dir, "explainer-settings.json")
        write_atomic(self.settings_file, json.dumps({"disableAllHooks": True}))

    def ready(self):
        return bool(self.bin) and not self.error

    def describe(self):
        return {"available": self.ready(), "bin": self.bin, "version": self.version, "error": self.error,
                "errorCode": self.error_code}

    def command(self, settings):
        caps = self.caps
        cmd = [self.bin, "-p", "--output-format", "stream-json", "--verbose"]
        if caps.get("--include-partial-messages"):
            cmd.append("--include-partial-messages")
        if caps.get("--tools"):
            cmd += ["--tools", EXPLAINER_TOOLS]
        if caps.get("--disallowedTools"):
            cmd += ["--disallowedTools", DENIED_TOOLS]
        if caps.get("dontAsk"):  # anything not allowed by default (e.g. reading outside the repo) is denied
            cmd += ["--permission-mode", "dontAsk"]
        if caps.get("--no-session-persistence"):
            cmd.append("--no-session-persistence")
        if caps.get("--strict-mcp-config"):
            cmd.append("--strict-mcp-config")
        if caps.get("--settings"):
            cmd += ["--settings", self.settings_file]
        if settings.get("model"):
            cmd += ["--model", settings["model"]]
        if settings.get("effort") and caps.get("--effort"):
            cmd += ["--effort", settings["effort"]]
        return cmd

    def submit(self, job):
        threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def cancel(self, key):
        with self.lock:
            self.cancelled.add(key)
            proc = self.running.get(key)
        if proc is not None:
            self._kill(proc)

    def stop_all(self):
        with self.lock:
            procs = list(self.running.values())
        for proc in procs:
            self._kill(proc)

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass

    def _run(self, job):
        with self.slots:
            with self.lock:
                skip = job.key in self.cancelled
                if skip:
                    self.cancelled.discard(job.key)
                else:
                    self.active += 1
            if skip:
                self.app.job_done(job, None, None, cancelled=True)
                return
            try:
                self._exec(job)
            except Exception as e:  # never leave a message stuck in "pending"
                traceback.print_exc()
                self.app.job_done(job, None, self.app.tr("explainer 出错：%s", "explainer error: %s") % e)
            finally:
                with self.lock:
                    self.active -= 1
                    self.running.pop(job.key, None)
                    self.cancelled.discard(job.key)

    def _exec(self, job):
        proc = subprocess.Popen(self.command(job.settings), cwd=self.app.repo.root, env=child_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        with self.lock:
            self.running[job.key] = proc
        errors = []
        err_reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        err_reader.start()
        try:
            proc.stdin.write(job.prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        timer = threading.Timer(JOB_TIMEOUT, self._kill, args=(proc,))
        timer.daemon = True
        timer.start()
        final = error = cost = None
        has_text = False
        partial = self.caps.get("--include-partial-messages")
        try:
            for raw in iter(proc.stdout.readline, b""):
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "stream_event":
                    e = ev.get("event") or {}
                    if e.get("type") == "message_start" and has_text:
                        self.app.job_text(job, "\n\n")
                    elif e.get("type") == "content_block_delta":
                        delta = e.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            has_text = True
                            self.app.job_text(job, delta["text"])
                elif kind == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") == "tool_use":
                            self.app.job_tool(job, describe_tool(block, self.app.repo.root))
                        elif block.get("type") == "text" and block.get("text") and not partial:
                            self.app.job_text(job, ("\n\n" if has_text else "") + block["text"])
                            has_text = True
                elif kind == "result":
                    cost = ev.get("total_cost_usd")
                    if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
                        error = str(ev.get("result") or ev.get("subtype") or "unknown error")
                    else:
                        final = ev.get("result") or ""
        finally:
            timer.cancel()
        proc.wait()
        err_reader.join(2)
        with self.lock:
            cancelled = job.key in self.cancelled
        if cancelled:
            self.app.job_done(job, None, None, cancelled=True)
            return
        if final is None and error is None:
            tail = b"".join(errors).decode("utf-8", "replace").strip()[-1500:]
            error = self.app.tr("claude 退出了（exit %s）", "claude exited (exit %s)") % proc.returncode
            if tail:
                error += "\n" + tail
        self.app.job_done(job, final, error, cost=cost)


def parse_annotation(text, a, b):
    """Turn the explainer's JSON walkthrough into [{from, to, note}] row ranges within [a, b]."""
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text or "", re.S) or re.search(r"(\[\s*\{.*\}\s*\])", text or "", re.S)
    if not m:
        return None
    try:
        items = json.loads(m.group(1))
    except ValueError:
        return None

    def row_no(value):
        hit = re.match(r"^r?(\d+)$", str(value).strip())
        return int(hit.group(1)) if hit else None

    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        start, end = row_no(it.get("from")), row_no(it.get("to", it.get("from")))
        note = str(it.get("note") or "").strip()
        if start is None or not note:
            continue
        end = start if end is None or end < start else end
        start = max(a, min(b, start))
        out.append({"from": start, "to": max(start, min(b, end)), "note": note})
    out.sort(key=lambda x: (x["from"], x["to"]))
    return out or None


# ----------------------------------------------------------------------------- the application

class ApiError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def clean_anchor(anchor):
    if not isinstance(anchor, dict):
        return None
    try:
        return {"rowStart": int(anchor["rowStart"]), "rowEnd": int(anchor["rowEnd"]),
                "side": "old" if anchor.get("side") == "old" else "new",
                "start": None if anchor.get("start") is None else int(anchor["start"]),
                "end": None if anchor.get("end") is None else int(anchor["end"]),
                "lines": [str(x)[:2000] for x in anchor.get("lines") or []][:MAX_ANCHOR_LINES]}
    except (KeyError, TypeError, ValueError):
        return None


def needs_author(t):
    """A change request or author question whose latest reviewer message has no author reply yet."""
    if t.get("resolved") or not (t.get("kind") == "change" or t.get("askAuthor")):
        return False
    roles = [m["role"] for m in t["messages"]]
    last_user = max([i for i, r in enumerate(roles) if r == "user"] or [-1])
    last_author = max([i for i, r in enumerate(roles) if r == "author"] or [-1])
    return last_user > last_author


def anchor_label(anchor):
    if not anchor or anchor.get("start") is None:
        return "whole file"
    start, end = anchor["start"], anchor["end"]
    span = "L%d" % start if start == end else "L%d-L%d" % (start, end)
    return ("old " + span) if anchor.get("side") == "old" else span


def thread_where(t):
    if not t.get("file"):
        return "the change as a whole"
    if not t.get("anchor"):
        return "`%s` (whole file)" % t["file"]
    return "`%s` %s" % (t["file"], anchor_label(t["anchor"]))


class App:
    def __init__(self, opts):
        self.opts = opts
        self.repo = Repo(opts.repo, opts.base, opts.patch, not opts.no_untracked, opts.exclude)
        self.state_dir = state_dir(self.repo)
        self.reviews_dir = private_dir(os.path.join(self.state_dir, "reviews"))
        self.store = Store(os.path.join(self.state_dir, "state.json"))
        self.token = load_token()
        self.server_id = secrets.token_hex(4)
        self.last_activity = now()
        self.listen, self.httpd = {}, None
        self.files, self.skipped, self.by_path = [], [], {}
        self.notes = {"summary": "", "files": {}}
        self.diff_version = 0
        self.explainer = Explainer(self, opts.claude)
        if opts.model is not None:
            self.store.data["settings"]["model"] = opts.model
        self.refresh(notes=read_optional(opts.notes), replies=read_replies(opts.replies))

    # -- diff, rounds and author replies

    def refresh(self, notes=None, replies=None, base=None, new_round=None):
        """Reload the diff. Starts a new round when the current one was submitted."""
        if replies is not None and not isinstance(replies, dict):
            raise ApiError(400, "replies must be an object like {\"T3\": \"...\"}")
        with self.store.lock:
            st = self.store.data
            previous_base = self.repo.base
            if base is not None:
                self.repo.set_base(base)
            try:
                self.files, self.skipped = self.repo.load()
            except RuntimeError as e:
                self.repo.set_base(previous_base)
                raise ApiError(400, str(e))
            self.by_path = {f["path"]: f for f in self.files}
            rnd = st["round"]
            cur = st["rounds"].get(str(rnd))
            if new_round is None:
                new_round = bool(cur and cur.get("submitted"))
            started = cur is None or bool(new_round)
            if started:
                if cur is not None:
                    rnd = st["round"] = rnd + 1
                cur = st["rounds"][str(rnd)] = {"n": rnd, "startedAt": now(), "submitted": None,
                                                "notes": cur["notes"] if cur else ""}
            if notes is not None:
                cur["notes"] = notes
            cur["base"] = self.repo.base or "HEAD"
            for path, verdict in list(st["verdicts"].items()):  # a verdict only holds for the code it was given on
                f = self.by_path.get(path)
                if f is None or f["hash"] != verdict.get("hash"):
                    del st["verdicts"][path]
            unknown = []
            for tid, text in (replies or {}).items():
                t = st["threads"].get(str(tid))
                if t is None or not str(text).strip():
                    unknown.append(str(tid))
                    continue
                t["messages"].append(self.message("author", str(text).strip()))
                t["resolved"] = False
                self.store.bump(t)
            self.notes = parse_notes(cur.get("notes", ""), list(self.by_path))
            self.diff_version = self.store.bump()
            self.store.save(force=True)
            return {"round": rnd, "newRound": started, "unknownReplies": unknown, "stats": self.stats()}

    def tr(self, zh, en):
        """A message in the language the reviewer picked in the UI."""
        return en if self.store.data["settings"].get("lang") == "en" else zh

    def ai_unavailable(self):
        return self.tr("AI 解释不可用：", "AI explanations are unavailable: ") + (self.explainer.error or "?")

    def message(self, role, text, **extra):
        msg = {"id": self.store.new_id("m"), "role": role, "text": text, "ts": now(),
               "round": self.store.data["round"]}
        msg.update(extra)
        return msg

    def stats(self):
        return {"files": len(self.files), "additions": sum(f["additions"] for f in self.files),
                "deletions": sum(f["deletions"] for f in self.files)}

    # -- read endpoints

    def ping(self):
        with self.store.lock:
            st = self.store.data
            cur = st["rounds"].get(str(st["round"])) or {}
            return {"ok": True, "serverId": self.server_id, "pid": os.getpid(), "name": self.repo.name,
                    "round": st["round"], "submitted": cur.get("submitted"), "stats": self.stats(),
                    "jobs": self.explainer.active}

    def bootstrap(self):
        with self.store.lock:
            return {"serverId": self.server_id, "repo": self.repo.describe(), "files": self.files,
                    "skipped": self.skipped, "notes": self.notes, "diffVersion": self.diff_version,
                    "stats": self.stats(), "ai": self.explainer.describe(),
                    "listen": {"mode": self.listen.get("mode"), "host": self.listen.get("host")},
                    "state": self.state_since(0)}

    def state_since(self, since):
        with self.store.lock:
            st = self.store.data
            full = since <= 0 or since > st["version"]

            def changed(items):
                return {k: v for k, v in items.items() if full or v.get("v", 0) > since}

            return {"serverId": self.server_id, "version": st["version"], "diffVersion": self.diff_version,
                    "round": st["round"], "rounds": st["rounds"], "verdicts": st["verdicts"],
                    "settings": st["settings"], "threads": changed(st["threads"]),
                    "annotations": changed(st["annotations"]), "jobs": self.explainer.active, "full": full,
                    "deleted": [k for k, v in st["deleted"].items() if full or v > since]}

    def file_content(self, path, side):
        f = self.by_path.get(path)
        if f is None:
            raise ApiError(404, "file not in the diff")
        repo = self.repo
        try:
            if side == "old":
                if not repo.git_dir or repo.patch or f["status"] == "A" or repo.base_ref == EMPTY_TREE:
                    raise ApiError(404, self.tr("这个文件没有旧版本", "this file has no older version"))
                left = repo.base_ref.split("..")[0] or "HEAD"
                if "..." in repo.base_ref:
                    left = git(["merge-base", left, repo.base_ref.split("...")[1] or "HEAD"], repo.root).strip()
                text = git(["show", "%s:%s" % (left, f["oldPath"] or f["path"])], repo.root)
            else:
                if f["status"] == "D":
                    raise ApiError(404, self.tr("这个文件被删除了", "this file was deleted"))
                if repo.range:
                    right = repo.base_ref.split("..")[-1].lstrip(".") or "HEAD"
                    text = git(["show", "%s:%s" % (right, f["path"])], repo.root)
                else:
                    full = os.path.realpath(os.path.join(repo.root, f["path"]))
                    if not full.startswith(os.path.realpath(repo.root) + os.sep):
                        raise ApiError(403, "path is outside the repository")
                    if os.path.getsize(full) > 4 * MAX_TEXT_BYTES:
                        raise ApiError(413, self.tr("文件太大", "file too large"))
                    text = read_text(full)
        except (RuntimeError, OSError) as e:
            raise ApiError(404, str(e))
        return {"path": path, "side": side, "text": text}

    def review_text(self, rnd):
        path = os.path.join(self.reviews_dir, "round-%d.md" % rnd)
        if not os.path.exists(path):
            raise ApiError(404, "no review for round %d" % rnd)
        return {"round": rnd, "markdown": read_text(path)}

    # -- threads

    def ask(self, body):
        question = str(body.get("question") or "").strip()
        use_ai = bool(body.get("ai"))
        if not question and not use_ai:
            raise ApiError(400, "empty message")
        if use_ai and not self.explainer.ready():
            raise ApiError(400, self.ai_unavailable())
        job = None
        with self.store.lock:
            st = self.store.data
            tid = body.get("threadId")
            if tid:
                t = st["threads"].get(tid)
                if t is None:
                    raise ApiError(404, "thread %s not found" % tid)
                if body.get("askAuthor") is not None:
                    t["askAuthor"] = bool(body["askAuthor"])
            else:
                path = body.get("file")
                if path is not None and path not in self.by_path:
                    raise ApiError(400, "file not in the diff: %s" % path)
                t = {"id": self.store.new_id("T"), "file": path,
                     "anchor": clean_anchor(body.get("anchor")) if path else None,
                     "kind": "change" if body.get("kind") == "change" else "question",
                     "askAuthor": bool(body.get("askAuthor")), "overview": bool(body.get("overview")),
                     "resolved": False, "round": st["round"], "createdAt": now(), "messages": []}
                st["threads"][t["id"]] = t
            if question:
                t["messages"].append(self.message("user", question))
            t["resolved"] = False
            if use_ai:
                settings = st["settings"]
                pending = self.message("ai", "", status="pending", tools=[], model=settings.get("model") or "")
                t["messages"].append(pending)
                job = Job("ask", t["id"], pending["id"], self.ask_prompt(t), settings)
            self.store.bump(t)
        if job:
            self.explainer.submit(job)
        return {"thread": t}

    def update_thread(self, body):
        with self.store.lock:
            t = self.store.data["threads"].get(body.get("id"))
            if t is None:
                raise ApiError(404, "thread not found")
            if body.get("delete"):
                for m in t["messages"]:
                    if m.get("status") == "pending":
                        self.explainer.cancel("%s:%s" % (t["id"], m["id"]))
                self.store.delete("threads", t["id"])
                return {"ok": True}
            if "resolved" in body:
                t["resolved"] = bool(body["resolved"])
            if body.get("kind") in ("question", "change"):
                t["kind"] = body["kind"]
            if "askAuthor" in body:
                t["askAuthor"] = bool(body["askAuthor"])
            self.store.bump(t)
            return {"thread": t}

    def cancel(self, body):
        if body.get("annoId"):
            self.explainer.cancel("%s:" % body["annoId"])
        elif body.get("threadId") and body.get("msgId"):
            self.explainer.cancel("%s:%s" % (body["threadId"], body["msgId"]))
        else:
            raise ApiError(400, "nothing to cancel")
        return {"ok": True}

    # -- line-by-line walkthroughs

    def annotate(self, body):
        f = self.by_path.get(body.get("file"))
        if f is None:
            raise ApiError(400, "file not in the diff")
        rows = flat_rows(f)
        if not rows:
            raise ApiError(400, self.tr("这个文件没有可以讲解的文本改动", "this file has no text changes to explain"))
        try:
            a = max(0, int(body.get("rowStart", 0)))
            b = min(len(rows) - 1, int(body.get("rowEnd", len(rows) - 1)))
        except (TypeError, ValueError):
            raise ApiError(400, "bad row range")
        if b < a:
            raise ApiError(400, "empty row range")
        if b - a + 1 > MAX_ANNOTATE_ROWS:
            raise ApiError(400, self.tr("一次最多逐行讲解 %d 行；请选一段（点行号，Shift+点击扩展）或者按 hunk 讲解",
                                        "at most %d lines per walkthrough: select a range or walk through one hunk")
                           % MAX_ANNOTATE_ROWS)
        if not self.explainer.ready():
            raise ApiError(400, self.ai_unavailable())
        with self.store.lock:
            st = self.store.data
            for old in list(st["annotations"].values()):  # a new walkthrough replaces overlapping ones
                if old["file"] == f["path"] and not (old["rowEnd"] < a or old["rowStart"] > b):
                    self.explainer.cancel("%s:" % old["id"])
                    self.store.delete("annotations", old["id"])
            anno = {"id": self.store.new_id("A"), "file": f["path"], "rowStart": a, "rowEnd": b,
                    "hash": f["hash"], "status": "pending", "items": [], "tools": [], "error": None,
                    "round": st["round"], "createdAt": now()}
            st["annotations"][anno["id"]] = anno
            self.store.bump(anno)
            job = Job("annotate", anno["id"], None, self.annotate_prompt(f, a, b), st["settings"])
        self.explainer.submit(job)
        return {"annotation": anno}

    def update_annotation(self, body):
        with self.store.lock:
            anno = self.store.data["annotations"].get(body.get("id"))
            if anno is None:
                raise ApiError(404, "annotation not found")
            if body.get("delete"):
                self.explainer.cancel("%s:" % anno["id"])
                self.store.delete("annotations", anno["id"])
            return {"ok": True}

    # -- explainer callbacks (called from worker threads)

    def _target(self, job):
        st = self.store.data
        if job.kind == "annotate":
            return st["annotations"].get(job.item_id), None
        t = st["threads"].get(job.item_id)
        msg = next((m for m in t["messages"] if m["id"] == job.msg_id), None) if t else None
        return t, msg

    def job_text(self, job, text):
        with self.store.lock:
            obj, msg = self._target(job)
            if obj is None:
                return
            if job.kind == "annotate":
                obj["_raw"] = obj.get("_raw", "") + text  # JSON in the making: not worth showing
            elif msg is not None:
                msg["text"] += text
                self.store.bump(obj)

    def job_tool(self, job, description):
        with self.store.lock:
            obj, msg = self._target(job)
            if obj is None:
                return
            (obj if job.kind == "annotate" else msg or {}).setdefault("tools", []).append(description)
            self.store.bump(obj)

    def job_done(self, job, final, error, cost=None, cancelled=False):
        with self.store.lock:
            obj, msg = self._target(job)
            if obj is None:
                return
            if job.kind == "annotate":
                raw = final or obj.pop("_raw", "")
                obj.pop("_raw", None)
                if cancelled:
                    obj["status"] = "cancelled"
                elif error:
                    obj["status"], obj["error"] = "error", error[:2000]
                else:
                    items = parse_annotation(raw, obj["rowStart"], obj["rowEnd"])
                    if items is None and raw.strip():  # not the requested JSON: show the text as one note
                        items = [{"from": obj["rowStart"], "to": obj["rowEnd"], "note": raw.strip()}]
                        obj["unstructured"] = True
                    obj["items"], obj["status"] = items or [], "done" if items else "error"
                    if not items:
                        obj["error"] = self.tr("解释器没有返回内容", "the explainer returned nothing")
            elif msg is not None:
                if final:
                    msg["text"] = final
                msg["status"] = "cancelled" if cancelled else ("error" if error else "done")
                if error and not cancelled:
                    msg["error"] = error[:2000]
                if cost is not None:
                    msg["cost"] = cost
            self.store.bump(obj)

    # -- prompts

    def lang(self):
        return LANG_NAMES.get(self.store.data["settings"].get("lang"), "Simplified Chinese")

    def context(self):
        repo = self.repo
        lines = ["Repository: %s (%s)" % (repo.name, repo.root)]
        if repo.patch:
            lines.append("Diff source: patch file %s" % repo.patch)
        elif repo.range:
            lines.append("Diff: commits %s" % repo.base_ref)
        else:
            lines.append("Diff: working tree (uncommitted and untracked files included) vs %s" % (repo.base or "HEAD"))
        listing = ["%s (%s, +%d -%d)" % (f["path"], STATUS_WORDS.get(f["status"], "?"), f["additions"], f["deletions"])
                   for f in self.files[:80]]
        lines.append("Changed files (%d): %s%s" % (len(self.files), "; ".join(listing), " ..." if len(self.files) > 80 else ""))
        if self.notes["summary"]:
            lines += ["", "## Author's notes (from the agent that wrote this change)", self.notes["summary"]]
        return "\n".join(lines)

    def whole_diff(self):
        out, used = [], 0
        per_file = max(4000, WHOLE_DIFF_BUDGET // max(1, len(self.files)))
        for i, f in enumerate(self.files):
            body = diff_text(f, budget=per_file) if f["hunks"] else ("(binary file)" if f["binary"] else "(no textual diff)")
            chunk = "### %s (%s)\n%s" % (f["path"], STATUS_WORDS.get(f["status"], "?"), body)
            if used + len(chunk) > WHOLE_DIFF_BUDGET:
                out.append("... (%d more files omitted; Read them if needed)" % (len(self.files) - i))
                break
            out.append(chunk)
            used += len(chunk)
        return "\n\n".join(out)

    def ask_prompt(self, t):
        lang = self.lang()
        parts = [PERSONA.format(lang=lang), "", "# Context", self.context()]
        f = self.by_path.get(t.get("file") or "")
        anchor = t.get("anchor")
        if f is not None:
            note = self.notes["files"].get(f["path"])
            if note:
                parts += ["", "## Author's note on %s" % f["path"], note]
            focus = (anchor["rowStart"], anchor["rowEnd"]) if anchor else None
            parts += ["", "## Diff of %s (%s)" % (f["path"], STATUS_WORDS.get(f["status"], "changed")),
                      DIFF_LEGEND, "```diff", diff_text(f, focus) or "(no textual diff)", "```"]
            if anchor:
                parts += ["", "## The lines the reviewer selected (%s)" % anchor_label(anchor),
                          "```diff", "\n".join(anchor["lines"]), "```"]
            else:
                parts += ["", "The reviewer is asking about this file as a whole."]
        else:
            if t.get("file"):
                parts += ["", "(%s is no longer part of the diff.)" % t["file"]]
            parts += ["", "## The whole diff", DIFF_LEGEND, "```diff", self.whole_diff(), "```"]
        msgs = [m for m in t["messages"] if m["text"].strip()]
        question = msgs.pop()["text"] if msgs and msgs[-1]["role"] == "user" else ""
        if msgs:
            who = {"user": "Reviewer", "ai": "Explainer (you)", "author": "Author (the agent that wrote the code)"}
            parts += ["", "## Earlier messages in this thread"]
            parts += ["**%s:** %s" % (who[m["role"]], m["text"]) for m in msgs]
        if t.get("overview") and not any(m["role"] == "ai" and m.get("status") == "done" for m in t["messages"]):
            parts += ["", OVERVIEW_TASK.format(lang=lang)]
        else:
            parts += ["", "# The reviewer's question", question or "Explain the selected code."]
        return "\n".join(parts)

    def annotate_prompt(self, f, a, b):
        lang = self.lang()
        parts = [PERSONA.format(lang=lang), "", "# Context", self.context()]
        note = self.notes["files"].get(f["path"])
        if note:
            parts += ["", "## Author's note on %s" % f["path"], note]
        parts += ["", "## Diff of %s, with row ids" % f["path"], "Columns: row id | " + DIFF_LEGEND[9:],
                  "```diff", diff_text(f, (a, b), ids=True), "```", "", ANNOTATE_TASK.format(a=a, b=b, lang=lang)]
        return "\n".join(parts)

    # -- verdicts, settings, submission

    def set_verdict(self, body):
        f = self.by_path.get(body.get("file"))
        if f is None:
            raise ApiError(400, "file not in the diff")
        state = body.get("state") or ""
        if state not in ("", "ok", "rewrite"):
            raise ApiError(400, "state must be ok, rewrite or empty")
        comment = str(body.get("comment") or "").strip()
        created = None
        with self.store.lock:
            st = self.store.data
            if state:
                st["verdicts"][f["path"]] = {"state": state, "hash": f["hash"], "round": st["round"], "at": now()}
            else:
                st["verdicts"].pop(f["path"], None)
            if state == "rewrite" and comment:  # the reason travels to the author as a file-level change request
                created = {"id": self.store.new_id("T"), "file": f["path"], "anchor": None, "kind": "change",
                           "askAuthor": False, "overview": False, "resolved": False, "round": st["round"],
                           "createdAt": now(), "messages": [self.message("user", comment)]}
                st["threads"][created["id"]] = created
                self.store.bump(created)
            self.store.bump()
            return {"ok": True, "thread": created, "verdicts": st["verdicts"]}

    def set_settings(self, body):
        with self.store.lock:
            settings = self.store.data["settings"]
            if "model" in body:
                model = str(body.get("model") or "").strip()
                if model and not re.match(r"^[A-Za-z0-9._\[\]-]+$", model):
                    raise ApiError(400, "bad model name")
                settings["model"] = model
            if body.get("lang") in LANG_NAMES:
                settings["lang"] = body["lang"]
            if "effort" in body:
                effort = str(body.get("effort") or "")
                if effort not in ("", "low", "medium", "high", "xhigh", "max"):
                    raise ApiError(400, "bad effort level")
                settings["effort"] = effort
            self.store.bump()
            return {"settings": settings}

    def submit(self, body):
        verdict = body.get("verdict")
        if verdict not in VERDICTS:
            raise ApiError(400, "verdict must be approve, comment or changes")
        comment = str(body.get("comment") or "").strip()
        with self.store.lock:
            st = self.store.data
            cur = st["rounds"][str(st["round"])]
            if cur.get("submitted"):
                raise ApiError(409, self.tr("第 %d 轮已经提交过了", "round %d was already submitted") % st["round"])
            markdown, data = self.build_review(verdict, comment)
            stem = os.path.join(self.reviews_dir, "round-%d" % st["round"])
            write_atomic(stem + ".json", json.dumps(data, ensure_ascii=False, indent=2))
            write_atomic(stem + ".md", markdown)
            cur["submitted"] = {"verdict": verdict, "comment": comment, "at": now()}
            self.store.bump()
            self.store.save(force=True)
            return {"ok": True, "round": st["round"], "markdown": markdown}

    def build_review(self, verdict, comment):
        st = self.store.data
        rnd = st["round"]
        threads = sorted(st["threads"].values(), key=lambda t: int(re.sub(r"\D", "", t["id"]) or 0))
        changes = [t for t in threads if t["kind"] == "change" and needs_author(t)]
        questions = [t for t in threads if t["kind"] != "change" and needs_author(t)]
        puzzled = [t for t in threads if t["kind"] != "change" and not t.get("askAuthor") and not t.get("overview")
                   and t.get("round") == rnd and any(m["role"] == "user" for m in t["messages"])]
        verdicts = st["verdicts"]
        rewrite = [f["path"] for f in self.files if verdicts.get(f["path"], {}).get("state") == "rewrite"]
        ok = [f["path"] for f in self.files if verdicts.get(f["path"], {}).get("state") == "ok"]
        discussed = {t.get("file") for t in threads}
        unreviewed = [f["path"] for f in self.files if f["path"] not in verdicts and f["path"] not in discussed]
        repo, stats = self.repo, self.stats()
        md = ["# Diff review · round %d · %s" % (rnd, VERDICTS[verdict]), "",
              "- repo: `%s` · base: `%s` · %d files (+%d −%d) · %s" % (
                  repo.root, repo.base or "HEAD", stats["files"], stats["additions"], stats["deletions"],
                  time.strftime("%Y-%m-%d %H:%M"))]
        if comment:
            md += ["", "## Overall comment（整体意见）", ""] + [("> " + l) if l else ">" for l in comment.splitlines()]
        if changes:
            md += ["", "## Change requests — must address (%d)" % len(changes)]
            for t in changes:
                md += self.thread_md(t, rnd)
        if rewrite:
            md += ["", "## Files marked “needs rewrite” (%d)" % len(rewrite)]
            md += ["- `%s`" % p for p in rewrite]
        if questions:
            md += ["", "## Questions for you, the author — answer each one (%d)" % len(questions)]
            for t in questions:
                md += self.thread_md(t, rnd)
        if puzzled:
            md += ["", "## Code the reviewer had to ask the explainer about (%d)" % len(puzzled),
                   "Not change requests, but a sign this code is hard to read. Consider clearer names, "
                   "simpler structure or a short why-comment."]
            for t in puzzled:
                first = next(m["text"] for m in t["messages"] if m["role"] == "user")
                md.append("- %s · %s: %s" % (t["id"], thread_where(t), one_line(first, 200)))
        if ok or unreviewed:
            md.append("")
        if ok:
            md.append("- understood / OK: " + ", ".join("`%s`" % p for p in ok))
        if unreviewed:
            md.append("- not reviewed: " + ", ".join("`%s`" % p for p in unreviewed))
        md += ["", "## How to respond"] + self.respond_hint(verdict)
        data = {"round": rnd, "verdict": verdict, "comment": comment, "repo": repo.root, "base": repo.base or "HEAD",
                "changeRequests": [self.thread_brief(t) for t in changes],
                "questions": [self.thread_brief(t) for t in questions],
                "puzzled": [self.thread_brief(t) for t in puzzled],
                "files": {"rewrite": rewrite, "ok": ok, "notReviewed": unreviewed}}
        return "\n".join(md) + "\n", data

    def thread_md(self, t, rnd):
        out = ["", "### %s · %s%s" % (t["id"], thread_where(t), "" if t.get("round") == rnd else " · (round %d)" % t["round"])]
        lines = (t.get("anchor") or {}).get("lines") or []
        if lines:
            out += ["```diff"] + lines[:40] + (["... (%d more lines)" % (len(lines) - 40)] if len(lines) > 40 else []) + ["```"]
        for m in t["messages"]:
            text = m["text"].strip()
            if not text:
                continue
            if m["role"] == "user":
                out.append("- **reviewer:** " + text.replace("\n", "\n  "))
            elif m["role"] == "author":
                out.append("- **you (earlier reply):** " + text.replace("\n", "\n  "))
            else:
                out.append("- **explainer said** (excerpt):")
                out += ["  > " + line for line in excerpt(text, 700).splitlines()]
        return out

    @staticmethod
    def thread_brief(t):
        return {"id": t["id"], "file": t.get("file"), "where": thread_where(t),
                "lines": (t.get("anchor") or {}).get("lines") or [],
                "messages": [{"role": m["role"], "text": m["text"]} for m in t["messages"] if m["text"].strip()]}

    def respond_hint(self, verdict):
        repo = self.repo
        target = " --repo %s" % shlex.quote(repo.root) + (" --patch %s" % shlex.quote(repo.patch) if repo.patch else "")
        cmd = "python3 %s" % shlex.quote(SCRIPT)
        if verdict == "approve":
            return ["Approved: nothing left to change. Optionally stop the UI: `%s stop%s`" % (cmd, target)]
        first = ("1. Address every change request and every file marked “needs rewrite”. The reviewer will not "
                 "accept code they can't follow, so aim for code that reads clearly (direct control flow, fewer "
                 "layers, clear names, small functions, a short comment where the why isn't obvious), not "
                 "cosmetic edits. If you disagree with a request, don't silently skip it: explain in your reply."
                 if verdict == "changes" else
                 "1. Answer the questions. Change code only where an answer shows it is needed.")
        return [first,
                "2. Write a JSON file mapping every thread id above to your reply (the answer, or what you "
                'changed and why), e.g. {"T4": "..."}.',
                "3. Update the notes file (begin with what changed this round) and push the next round:",
                "   `%s start%s --notes NOTES.md --replies REPLIES.json`" % (cmd, target),
                "4. Wait for the next review (run in the background): `%s wait%s`" % (cmd, target)]

    def request_shutdown(self):
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        return {"ok": True}


# ----------------------------------------------------------------------------- HTTP

def query_int(query, name):
    try:
        return int((query.get(name) or ["0"])[0] or 0)
    except ValueError:
        raise ApiError(400, "%s must be an integer" % name)


ROUTES = {
    ("GET", "/api/ping"): lambda app, body, q: app.ping(),
    ("GET", "/api/bootstrap"): lambda app, body, q: app.bootstrap(),
    ("GET", "/api/state"): lambda app, body, q: app.state_since(query_int(q, "since")),
    ("GET", "/api/file"): lambda app, body, q: app.file_content((q.get("path") or [""])[0], (q.get("side") or ["new"])[0]),
    ("GET", "/api/review"): lambda app, body, q: app.review_text(query_int(q, "round")),
    ("POST", "/api/ask"): lambda app, body, q: app.ask(body),
    ("POST", "/api/thread"): lambda app, body, q: app.update_thread(body),
    ("POST", "/api/cancel"): lambda app, body, q: app.cancel(body),
    ("POST", "/api/annotate"): lambda app, body, q: app.annotate(body),
    ("POST", "/api/annotation"): lambda app, body, q: app.update_annotation(body),
    ("POST", "/api/verdict"): lambda app, body, q: app.set_verdict(body),
    ("POST", "/api/settings"): lambda app, body, q: app.set_settings(body),
    ("POST", "/api/submit"): lambda app, body, q: app.submit(body),
    ("POST", "/api/refresh"): lambda app, body, q: app.refresh(body.get("notes"), body.get("replies"),
                                                              body.get("base"), body.get("newRound")),
    ("POST", "/api/shutdown"): lambda app, body, q: app.request_shutdown(),
}
QUIET_ROUTES = {("GET", "/api/ping"), ("GET", "/api/state")}  # polling doesn't count as activity


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = APP
    sys_version = ""

    def log_message(self, fmt, *args):
        if self.server.app.opts.verbose:
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def address_string(self):
        return "client"

    def send_body(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_body(code, json.dumps(obj, ensure_ascii=False))

    def token_ok(self, value):
        return bool(value) and hmac.compare_digest(value.encode("utf-8"), self.server.app.token.encode("utf-8"))

    def cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        app = self.server.app
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if method == "GET" and url.path == "/":
                return self.page(query)
            if method == "GET" and url.path == "/favicon.ico":
                return self.send_body(204, b"", "image/x-icon")
            if not url.path.startswith("/api/"):
                return self.send_json({"error": "not found"}, 404)
            if not self.token_ok(self.headers.get("X-DR-Token", "")):
                return self.send_json({"error": "missing or wrong token"}, 401)
            body = self.read_json() if method == "POST" else {}
            handler = ROUTES.get((method, url.path))
            if handler is None:
                return self.send_json({"error": "not found"}, 404)
            if (method, url.path) not in QUIET_ROUTES:
                app.last_activity = now()
            with app.store.lock:
                payload = json.dumps(handler(app, body, query), ensure_ascii=False)
            self.send_body(200, payload)
        except ApiError as e:
            self.send_json({"error": str(e)}, e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except OSError:
                pass

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8 * 1024 * 1024:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ApiError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "expected a JSON object")
        return data

    def page(self, query):
        app = self.server.app
        token = (query.get("token") or [""])[0]
        if token:  # trade the URL token for a cookie, then drop it from the address bar
            if not self.token_ok(token):
                return self.send_body(403, DENIED_PAGE, "text/html; charset=utf-8")
            cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (COOKIE, app.token)
            return self.send_body(303, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
        if not self.token_ok(self.cookie_token()):
            return self.send_body(401, DENIED_PAGE, "text/html; charset=utf-8")
        app.last_activity = now()
        self.send_body(200, read_text(UI_FILE).replace("__DR_TOKEN__", app.token), "text/html; charset=utf-8")


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        http.client.HTTPConnection.__init__(self, "localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


def api_call(info, method, path, body=None, timeout=10):
    if info.get("mode") == "unix":
        conn = UnixHTTPConnection(info["socket"], timeout)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=timeout)
    try:
        conn.request(method, path, body=None if body is None else json.dumps(body).encode("utf-8"),
                     headers={"X-DR-Token": load_token(), "Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8") or "{}")
        if resp.status >= 400:
            raise RuntimeError(data.get("error") or "HTTP %d" % resp.status)
        return data
    finally:
        conn.close()


def unix_alive(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def point_symlink(link, target):
    tmp = "%s.%d.tmp" % (link, os.getpid())
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link)
    except OSError:
        pass


def bind_tcp(host, preferred):
    error = None
    for port in range(preferred, preferred + 50):
        try:
            return TCPServer((host, port), Handler), port
        except OSError as e:
            error = e
    raise SystemExit("no free port in %d-%d: %s" % (preferred, preferred + 49, error))


def housekeeping(app, httpd):
    idle_limit = app.opts.idle_hours * 3600
    while True:
        time.sleep(2)
        try:
            app.store.save()
        except OSError as e:
            sys.stderr.write("could not save state: %s\n" % e)
        if idle_limit > 0 and now() - app.last_activity > idle_limit and app.explainer.active == 0:
            sys.stderr.write("idle for %.1f h, shutting down\n" % app.opts.idle_hours)
            httpd.shutdown()
            return


# ----------------------------------------------------------------------------- commands

def cmd_serve(opts):
    app = App(opts)
    mode = opts.listen or ("unix" if is_remote() else "tcp")
    info = {"mode": mode, "pid": os.getpid(), "serverId": app.server_id, "host": socket.gethostname(),
            "repo": app.repo.root, "startedAt": now()}
    info.update(node_details() if is_remote() else {"remote": False})
    if mode == "unix":
        path = os.path.join(socket_dir(), app.repo.key() + ".sock")
        if os.path.exists(path):
            if unix_alive(path):
                raise SystemExit("another diff-review server already listens on %s" % path)
            os.unlink(path)
        umask = os.umask(0o177)
        try:
            httpd = UnixServer(path, Handler)
        finally:
            os.umask(umask)
        os.chmod(path, 0o600)
        current = os.path.join(os.path.dirname(path), "current.sock")
        point_symlink(current, path)
        info.update(socket=path, current=current, port=opts.port or default_port())
    else:
        httpd, port = bind_tcp(opts.host, opts.port or app.store.data.get("lastPort") or default_port())
        app.store.data["lastPort"] = port
        info.update(port=port, bindHost=opts.host)
    httpd.app, app.httpd, app.listen = app, httpd, info
    info_path = os.path.join(app.state_dir, "server.json")
    write_atomic(info_path, json.dumps(info, indent=2))
    threading.Thread(target=housekeeping, args=(app, httpd), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    print("diff-review: serving %s on %s" % (app.repo.root, info.get("socket") or "%s:%d" % (opts.host, info["port"])), flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.explainer.stop_all()
        app.store.save(force=True)
        try:
            if json.loads(read_text(info_path)).get("pid") == os.getpid():
                os.unlink(info_path)
        except (OSError, ValueError):
            pass
        if mode == "unix":
            try:
                os.unlink(info["socket"])
            except OSError:
                pass
            try:
                if os.path.realpath(info["current"]) == os.path.realpath(info["socket"]):
                    os.unlink(info["current"])
            except OSError:
                pass


def live_server(sdir):
    """The running server for a state dir (as recorded in server.json), or None."""
    try:
        info = json.loads(read_text(os.path.join(sdir, "server.json")))
    except (OSError, ValueError):
        return None
    if info.get("host") != socket.gethostname() or not pid_alive(info.get("pid")):
        return None
    try:
        info["ping"] = api_call(info, "GET", "/api/ping", timeout=5)
    except Exception:
        return None
    return info


def is_global_ip(ip):
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_global
    except ValueError:
        return False


def is_local_ip(ip):
    """True when `ip` is assigned to this machine (binding to it only works then)."""
    probe = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def node_address():
    """A public address of this very machine that the reviewer's computer can SSH to, or None.

    Clusters usually put several login nodes behind one login address, so a new SSH
    connection may land on a different node than the one this server runs on, while the
    Unix socket exists only here: the forward has to name this node. Candidates are the
    node's own DNS names (internal and public names often differ by one label, e.g.
    login3.int.example.org and login3.example.org) and the address the current SSH session
    came in on; one counts only if it is public and assigned to this machine."""
    fqdn, short = socket.getfqdn(), socket.gethostname().split(".")[0]
    domain = fqdn.split(".")[1:] if fqdn.split(".")[0] == short else []
    names = [fqdn]
    if len(domain) >= 3:  # drop one label, keeping at least "example.org"
        names += [".".join([short] + domain[:i] + domain[i + 1:]) for i in range(len(domain))]
    names += [".".join([short] + domain[i:]) for i in range(1, len(domain) - 1)]
    for name in dict.fromkeys(names):
        if name.endswith(".arpa"):
            continue
        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(name, None)}
        except OSError:
            continue
        if any(is_global_ip(ip) and is_local_ip(ip) for ip in addresses):
            return name
    for ip in (os.environ.get("SSH_CONNECTION") or "").split()[2:3]:
        if is_global_ip(ip) and is_local_ip(ip):
            try:  # prefer its DNS name when that resolves back to it
                name = socket.gethostbyaddr(ip)[0]
                if ip in {entry[4][0] for entry in socket.getaddrinfo(name, None)}:
                    return name
            except OSError:
                pass
            return ip
    return None


def login_address():
    """The address the reviewer logs in with, to hop from it to this node; None if unknown.

    DIFF_REVIEW_SSH_HOST sets it on any site. Digital Research Alliance clusters export
    CC_CLUSTER, and their login address is <cluster>.alliancecan.ca."""
    if os.environ.get("DIFF_REVIEW_SSH_HOST"):
        return os.environ["DIFF_REVIEW_SSH_HOST"]
    cluster = os.environ.get("CC_CLUSTER")
    return "%s.alliancecan.ca" % cluster if cluster else None


def node_details():
    """Where this server runs, for the SSH forwarding instructions."""
    return {"remote": True, "user": getpass.getuser(), "node": socket.gethostname().split(".")[0],
            "nodeAddress": node_address(), "loginAddress": login_address()}


def forward_command(info):
    """The SSH port forward the reviewer runs on their own computer to reach this server."""
    port = info["port"]
    target = info["current"] if info["mode"] == "unix" else "localhost:%d" % port
    user = info.get("user") or getpass.getuser()
    if info.get("nodeAddress"):  # straight to this machine
        return "ssh -fN -L %d:%s %s@%s" % (port, target, user, info["nodeAddress"])
    # no public address of its own: hop to it through the login address
    login = info.get("loginAddress") or "<登录地址>"
    jump = login if "@" in login or login.startswith("<") else "%s@%s" % (user, login)
    return "ssh -fN -J %s -L %d:%s %s@%s" % (jump, port, target, user, info.get("node") or info.get("host"))


def page_url(info):
    token = load_token()
    if info["mode"] == "unix":
        return "http://localhost:%d/?token=%s" % (info["port"], token)
    return "http://127.0.0.1:%d/?token=%s" % (info["port"], token)


def print_banner(info, ping, refreshed=False, result=None):
    stats = ping.get("stats") or {}
    if refreshed and result and result.get("newRound"):
        status = "已推送新一轮"
    else:
        status = "已刷新" if refreshed else "已启动"
    print("✅ diff-review %s · %s · 第 %d 轮 · %d 个文件 (+%d −%d)" % (
        status, ping.get("name"), ping.get("round", 1), stats.get("files", 0), stats.get("additions", 0),
        stats.get("deletions", 0)))
    if "remote" not in info:  # server.json written by an older version
        info.update(node_details() if is_remote() else {"remote": False})
    if not info["remote"] and info["mode"] == "tcp":
        print("   打开： " + page_url(info))
    else:
        node = info.get("node") or info["host"]
        kind = "Unix socket，只有你本人能连" if info["mode"] == "unix" else "TCP 端口 %d" % info["port"]
        print("   服务跑在 %s 上（%s）。" % (node, kind))
        print("   集群的登录地址后面常常有好几个登录节点，新开的 SSH 连接不一定落在 %s 上，所以转发要直接连到这台机器%s。"
              % (node, "" if info.get("nodeAddress") else "（它没有能从外面直接连的地址，所以经登录地址跳过去）"))
        print("   在【你自己电脑】的终端里跑一次（之后每一轮都不用再跑；这条转发已经在跑就跳过）：")
        print("     " + forward_command(info))
        if not info.get("nodeAddress") and not info.get("loginAddress"):
            print("   （<登录地址> 换成你平时 ssh 登录这里用的名字，或者在这台机器上设好 DIFF_REVIEW_SSH_HOST。）")
        print("   第一次可能要过一次二次验证（MFA）、确认一次主机指纹。")
        print("   然后打开： " + page_url(info))
        print("   如果你总在 %s 上跑 Claude（比如开在 tmux 里），可以在 ssh config 里把登录用的 Host 固定到这个节点并自动带上转发，" % node)
        print("   以后 ssh 登录就自带转发、同一个链接一直能用：见 diff-review skill 的 references/remote-clusters.md「固定登录节点」。")
    if refreshed:
        print("   已经打开的页面会自动刷新。")


def resolve_paths(opts):
    for name in ("notes", "replies", "patch"):
        value = getattr(opts, name, None)
        if value:
            setattr(opts, name, os.path.abspath(os.path.expanduser(value)))


def serve_args(opts, repo):
    args = ["--repo", repo.root]
    for flag, value in (("--patch", opts.patch), ("--base", opts.base), ("--notes", opts.notes),
                        ("--replies", opts.replies), ("--listen", opts.listen), ("--port", opts.port),
                        ("--host", opts.host), ("--model", opts.model), ("--claude", opts.claude),
                        ("--idle-hours", opts.idle_hours)):
        if value is not None:
            args += [flag, str(value)]
    for pattern in opts.exclude or []:
        args += ["--exclude", pattern]
    if opts.no_untracked:
        args.append("--no-untracked")
    if opts.verbose:
        args.append("--verbose")
    return args


def cmd_start(opts):
    resolve_paths(opts)
    repo = Repo(opts.repo, opts.base, opts.patch, not opts.no_untracked, opts.exclude)
    sdir = state_dir(repo)
    info = live_server(sdir)
    if info:
        body = {"notes": read_optional(opts.notes), "replies": read_replies(opts.replies)}
        if opts.base is not None:
            body["base"] = opts.base
        if opts.new_round:
            body["newRound"] = True
        result = api_call(info, "POST", "/api/refresh", body, timeout=180)
        if info["mode"] == "unix":
            point_symlink(info["current"], info["socket"])
        print_banner(info, api_call(info, "GET", "/api/ping"), refreshed=True, result=result)
        if result.get("unknownReplies"):
            print("⚠ replies 里这些 thread id 不存在或回复为空，已忽略：" + ", ".join(result["unknownReplies"]))
        return 0
    cmd = [sys.executable, SCRIPT, "serve"] + serve_args(opts, repo)
    log_path = os.path.join(sdir, "server.log")
    with open(log_path, "ab") as log:
        log.write(("\n--- %s\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        log.flush()
        proc = subprocess.Popen(cmd, cwd=repo.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True)
    deadline, info = now() + 60, None
    while now() < deadline and proc.poll() is None:
        info = live_server(sdir)
        if info and info.get("pid") == proc.pid:
            break
        info = None
        time.sleep(0.3)
    if not info:
        tail = read_text(log_path)[-3000:] if os.path.exists(log_path) else ""
        print("diff-review server did not come up. Log %s:\n%s" % (log_path, tail), file=sys.stderr)
        return 1
    print_banner(info, info["ping"])
    if opts.open and info["mode"] == "tcp":
        webbrowser.open(page_url(info))
    return 0


def review_path(sdir, rnd):
    return os.path.join(sdir, "reviews", "round-%d.md" % rnd)


def cmd_wait(opts):
    repo = Repo(opts.repo, patch=opts.patch)
    sdir = state_dir(repo)
    info = live_server(sdir)
    if not info:
        print("没有正在运行的 diff-review（先运行 start）。")
        return 2
    rnd = info["ping"]["round"]
    deadline = now() + opts.timeout if opts.timeout else None
    failures = 0
    while True:
        try:
            ping = api_call(info, "GET", "/api/ping", timeout=10)
            failures = 0
        except Exception:
            ping = None
            failures += 1
            if failures >= 3 and not pid_alive(info["pid"]):
                if os.path.exists(review_path(sdir, rnd)):
                    print(read_text(review_path(sdir, rnd)))
                    return 0
                print("diff-review 已经停了，第 %d 轮没有提交。" % rnd)
                return 3
        if ping:
            submitted = ping.get("submitted") if ping.get("round") == rnd else os.path.exists(review_path(sdir, rnd))
            if submitted:
                print(read_text(review_path(sdir, rnd)))
                return 0
            if ping.get("round") != rnd:
                print("已经进入第 %d 轮，第 %d 轮没有提交。" % (ping["round"], rnd))
                return 4
        if deadline and now() > deadline:
            print("第 %d 轮还没提交（等待超时）。再运行一次 wait，或者等用户说提交了再运行 result。" % rnd)
            return 5
        time.sleep(2)


def cmd_result(opts):
    repo = Repo(opts.repo, patch=opts.patch)
    reviews = os.path.join(state_dir(repo), "reviews")
    rounds = sorted(int(m.group(1)) for m in (re.match(r"round-(\d+)\.md$", n) for n in os.listdir(reviews)) if m) \
        if os.path.isdir(reviews) else []
    if not rounds:
        print("还没有提交过 review。")
        return 2
    rnd = opts.round or rounds[-1]
    if rnd not in rounds:
        print("第 %d 轮没有提交过 review（已提交：%s）。" % (rnd, ", ".join(map(str, rounds))))
        return 2
    print(read_text(review_path(state_dir(repo), rnd)))
    return 0


def cmd_status(opts):
    repo = Repo(opts.repo, patch=opts.patch)
    sdir = state_dir(repo)
    info = live_server(sdir)
    if not info:
        print("没有正在运行的 diff-review：%s（state: %s）" % (repo.root, sdir))
        return 1
    ping = info["ping"]
    print("diff-review 在运行：pid %s @ %s · 第 %d 轮%s" % (info["pid"], info["host"], ping["round"],
                                                    " · 已提交" if ping.get("submitted") else ""))
    print("  listen: %s" % (info.get("socket") or "127.0.0.1:%d" % info["port"]))
    print("  url:    %s" % page_url(info))
    if info.get("remote") or info["mode"] == "unix":
        if "remote" not in info:
            info.update(node_details())
        print("  ssh:    %s" % forward_command(info))
    print("  state:  %s" % sdir)
    return 0


def cmd_stop(opts):
    repo = Repo(opts.repo, patch=opts.patch)
    info = live_server(state_dir(repo))
    if not info:
        print("没有正在运行的 diff-review。")
        return 0
    try:
        api_call(info, "POST", "/api/shutdown", {})
    except Exception:
        os.kill(int(info["pid"]), signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(info["pid"]):
            break
        time.sleep(0.1)
    print("diff-review 已停止（pid %s）。" % info["pid"])
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="diff_review.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")

    def target(p):
        p.add_argument("--repo", default=".", help="any path inside the git repository (default: cwd)")
        p.add_argument("--patch", help="review a unified diff file instead of a git work tree")

    def server_options(p):
        p.add_argument("--base", help="revision to diff against (default HEAD); A..B reviews commits only")
        p.add_argument("--notes", help="markdown notes from the author: overall intent, then '## path' per file")
        p.add_argument("--replies", help='JSON {"T3": "reply", ...} answering review threads')
        p.add_argument("--no-untracked", action="store_true", help="leave untracked files out")
        p.add_argument("--exclude", action="append", help="glob of files to hide (repeatable)")
        p.add_argument("--listen", choices=("tcp", "unix"), help="default: unix over SSH/on clusters, tcp locally")
        p.add_argument("--port", type=int, help="TCP port (tcp) or suggested local port for the SSH forward (unix)")
        p.add_argument("--host", default="127.0.0.1", help="TCP bind address (default 127.0.0.1)")
        p.add_argument("--model", help="explainer model, e.g. sonnet / opus / haiku (default: your Claude Code default)")
        p.add_argument("--claude", help="path to the claude CLI")
        p.add_argument("--idle-hours", type=float, default=6, help="exit after this long without activity (0 = never)")
        p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("start", help="start the review server, or push a new round to the running one")
    target(p)
    server_options(p)
    p.add_argument("--new-round", action="store_true", help="start a new round even if this one wasn't submitted")
    p.add_argument("--open", action="store_true", help="open the page in the local browser")
    p = sub.add_parser("serve", help="run the server in the foreground")
    target(p)
    server_options(p)
    p = sub.add_parser("wait", help="block until the current round is submitted; print the review")
    target(p)
    p.add_argument("--timeout", type=float, default=0, help="give up after this many seconds (default: wait forever)")
    p = sub.add_parser("result", help="print the latest submitted review")
    target(p)
    p.add_argument("--round", type=int)
    for name, text in (("status", "show the running server"), ("stop", "stop the server")):
        target(sub.add_parser(name, help=text))

    opts = parser.parse_args(argv)
    commands = {"start": cmd_start, "serve": cmd_serve, "wait": cmd_wait, "result": cmd_result,
                "status": cmd_status, "stop": cmd_stop}
    if opts.command not in commands:
        parser.print_help()
        return 2
    return commands[opts.command](opts) or 0


if __name__ == "__main__":
    sys.exit(main())
