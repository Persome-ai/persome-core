"""Identity: git config, GitHub CLI auth, SSH host aliases, public-key owner."""

from __future__ import annotations

import re

from .base import Signal, collector, home, read_text, run_cmd


def _git(key: str) -> str | None:
    return run_cmd(["git", "config", "--global", key])


@collector("identity", "身份", "identity")
def collect() -> list[Signal]:
    signals: list[Signal] = []

    name = _git("user.name")
    email = _git("user.email")
    if name:
        signals.append(Signal("Git 名字", name))
    if email:
        signals.append(Signal("Git 邮箱", email))
    if _git("user.signingkey"):
        signals.append(Signal("Git 签名", "已配置 commit signing"))
    editor = _git("core.editor")
    if editor:
        signals.append(Signal("Git 编辑器", editor))
    default_branch = _git("init.defaultBranch")
    if default_branch:
        signals.append(Signal("默认分支", default_branch))

    # GitHub CLI — reveals account + org membership when present.
    gh_status = run_cmd(["gh", "auth", "status"], timeout=8.0)
    if gh_status:
        accounts = re.findall(r"account (\S+)", gh_status)
        hosts = re.findall(r"(?:Logged in to|account .* on) (\S+\.\S+)", gh_status)
        who = ", ".join(sorted(set(accounts)))
        if who:
            detail = f"host: {hosts[0]}" if hosts else ""
            signals.append(Signal("GitHub 账号", who, detail))

    # SSH config host aliases (just the alias names — what they routinely reach).
    ssh_cfg = read_text(home() / ".ssh" / "config", max_bytes=100_000)
    if ssh_cfg:
        hosts = []
        for line in ssh_cfg.splitlines():
            m = re.match(r"\s*Host\s+(.+)", line, re.IGNORECASE)
            if m:
                for h in m.group(1).split():
                    if h and "*" not in h:
                        hosts.append(h)
        if hosts:
            uniq = list(dict.fromkeys(hosts))[:12]
            signals.append(Signal("SSH 主机别名", uniq, f"{len(set(hosts))} 个"))

    # Public-key comment often carries an identity (e.g. user@host or email).
    ssh_dir = home() / ".ssh"
    if ssh_dir.is_dir():
        comments = []
        for pub in sorted(ssh_dir.glob("*.pub")):
            text = read_text(pub, max_bytes=10_000) or ""
            parts = text.split()
            if len(parts) >= 3:
                comments.append(parts[-1])
        if comments:
            signals.append(Signal("SSH 公钥标识", list(dict.fromkeys(comments))[:5]))

    return signals
