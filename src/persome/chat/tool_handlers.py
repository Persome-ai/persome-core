"""Chat tool handlers — each takes the raw args dict and returns a Python object."""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..mcp import captures as captures_mod
from ..store import fts
from . import history as chat_history

# ─── memory tools ─────────────────────────────────────────────────────────


def tool_search_memory(args: dict[str, Any]) -> Any:
    from ..retrieval import associative as assoc_mod

    with fts.cursor() as conn:
        # §5 read cutover: the associative entrance (slot-less queries degrade to
        # search_hybrid byte-identically; kill-switch [search] associative_read_enabled)
        hits = assoc_mod.associative_read(
            conn,
            query=args["query"],
            top_k=args.get("top_k", 5),
            since=args.get("since"),
            until=args.get("until"),
        )
    return [
        {
            "id": h.id,
            "path": h.path,
            "timestamp": h.timestamp,
            "content": h.content,
            "rank": h.rank,
        }
        for h in hits
    ]


def tool_list_memories(args: dict[str, Any]) -> Any:
    with fts.cursor() as conn:
        rows = fts.list_files(conn)
    return [
        {
            "path": r.path,
            "description": r.description,
            "tags": r.tags.split() if r.tags else [],
            "status": r.status,
            "entry_count": r.entry_count,
            "updated": r.updated,
        }
        for r in rows
    ]


def tool_read_memory(args: dict[str, Any]) -> Any:
    from ..store import files as files_mod

    p = files_mod.memory_path(args["path"])
    if not p.exists():
        return {"error": f"file not found: {args['path']}"}
    parsed = files_mod.read_file(p)
    entries = parsed.entries
    tail_n = args.get("tail_n")
    if tail_n and tail_n > 0:
        entries = entries[-tail_n:]
    with fts.cursor() as conn:
        fts.increment_retrieval_counts(conn, (e.id for e in entries))
    return {
        "path": args["path"],
        "description": parsed.description,
        "tags": parsed.tags,
        "entries": [{"id": e.id, "timestamp": e.timestamp, "body": e.body} for e in entries],
    }


def tool_recent_activity(args: dict[str, Any]) -> Any:
    with fts.cursor() as conn:
        hits = fts.recent(
            conn,
            since=args.get("since"),
            limit=args.get("limit", 20),
        )
    return [
        {
            "id": h.id,
            "path": h.path,
            "timestamp": h.timestamp,
            "content": h.content,
        }
        for h in hits
    ]


# ─── capture / context tools ──────────────────────────────────────────────


def tool_current_context(args: dict[str, Any]) -> Any:
    return captures_mod.current_context(
        app_filter=args.get("app_filter"),
    )


def tool_search_captures(args: dict[str, Any]) -> Any:
    return captures_mod.search_captures(
        query=args["query"],
        app_name=args.get("app_name"),
        since=args.get("since"),
        until=args.get("until"),
        limit=args.get("limit", 10),
    )


# ─── file / shell tools ───────────────────────────────────────────────────


def tool_run_command(args: dict[str, Any]) -> Any:
    cwd = args.get("cwd") or str(Path.home())
    timeout = args.get("timeout", 30)
    try:
        # shell=True is intentional: this handler is invoked via LLM tool-call from
        # the local chat scope; users expect shell features (pipes, globs, $VAR).
        # Do not "harden" this to a list — that breaks the contract.
        proc = subprocess.run(
            args["command"],
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "en_US.UTF-8"},
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-10000:] if len(proc.stdout) > 10000 else proc.stdout,
            "stderr": proc.stderr[-5000:] if len(proc.stderr) > 5000 else proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}


def tool_read_file(args: dict[str, Any]) -> Any:
    p = Path(args["path"]).expanduser()
    if not p.exists():
        return {"error": f"file not found: {args['path']}"}
    if not p.is_file():
        return {"error": f"not a file: {args['path']}"}
    text = p.read_text(errors="replace")
    lines = text.splitlines(keepends=True)
    offset = args.get("offset", 1) - 1
    limit = args.get("limit", len(lines))
    selected = lines[max(0, offset) : offset + limit]
    numbered = "".join(f"{i + offset + 1:>5} | {line}" for i, line in enumerate(selected))
    return {
        "path": str(p),
        "total_lines": len(lines),
        "showing": f"{offset + 1}-{offset + len(selected)}",
        "content": numbered[-20000:],
    }


def tool_write_file(args: dict[str, Any]) -> Any:
    p = Path(args["path"]).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"])
    return {"path": str(p), "bytes_written": len(args["content"])}


def tool_edit_file(args: dict[str, Any]) -> Any:
    p = Path(args["path"]).expanduser()
    if not p.exists():
        return {"error": f"file not found: {args['path']}"}
    text = p.read_text()
    old = args["old_string"]
    if old not in text:
        return {"error": "old_string not found in file"}
    count = text.count(old)
    if count > 1:
        return {"error": f"old_string found {count} times, must be unique"}
    new_text = text.replace(old, args["new_string"], 1)
    p.write_text(new_text)
    return {"path": str(p), "replaced": True}


def tool_grep_search(args: dict[str, Any]) -> Any:
    search_path = args.get("path", ".")
    cmd = ["grep", "-rn", "--color=never"]
    if args.get("include"):
        cmd.extend(["--include", args["include"]])
    cmd.extend([args["pattern"], search_path])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(Path.home()),
        )
        all_lines = proc.stdout.strip().splitlines()
        lines = all_lines[:50]
        return {
            "matches": lines,
            "count": len(lines),
            "truncated": len(all_lines) > 50,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Search timed out"}


def tool_list_dir(args: dict[str, Any]) -> Any:
    p = Path(args.get("path", ".")).expanduser()
    if not p.exists():
        return {"error": f"path not found: {p}"}
    entries = []
    for item in sorted(p.iterdir()):
        if item.name.startswith("."):
            continue
        kind = "dir" if item.is_dir() else "file"
        size = item.stat().st_size if item.is_file() else None
        entries.append({"name": item.name, "type": kind, "size": size})
    return {"path": str(p), "entries": entries[:100]}


# ─── web tools ────────────────────────────────────────────────────────────


def tool_web_search(args: dict[str, Any]) -> Any:
    from ddgs import DDGS

    with DDGS() as ddgs:
        raw = list(ddgs.text(args["query"], max_results=args.get("max_results", 5)))
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
        }
        for r in raw
    ]


def tool_fetch_page(args: dict[str, Any]) -> Any:
    import httpx
    from bs4 import BeautifulSoup

    max_len = args.get("max_length", 10000)
    resp = httpx.get(
        args["url"],
        follow_redirects=True,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Persome/0.1"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [line for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)
    if len(text) > max_len:
        text = text[:max_len] + "\n...(truncated)"
    return {
        "url": args["url"],
        "title": soup.title.string if soup.title else "",
        "content": text,
    }


# ─── chat-history tools ───────────────────────────────────────────────────


def tool_search_chat_history(args: dict[str, Any]) -> Any:
    return chat_history.search_chat_history(args["query"], args.get("limit", 10))


def tool_list_chat_sessions(args: dict[str, Any]) -> Any:
    return chat_history.list_chat_sessions()


# ─── registry ─────────────────────────────────────────────────────────────


def tool_set_user_name(args: dict[str, Any]) -> Any:

    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name cannot be empty"}

    from ..store import entries as entries_mod
    from ..store import fts

    profile_name = "user-profile"
    with fts.cursor() as conn:
        with contextlib.suppress(FileExistsError):
            entries_mod.create_file(
                conn,
                name=profile_name,
                description="User's identity, background, and long-term stable basic information",
                tags=["identity", "background"],
            )
        entries_mod.append_entry(
            conn,
            name=profile_name,
            content=f"Name: {name}",
            tags=["identity", "name"],
        )
    return {"ok": True, "name": name}


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "set_user_name": tool_set_user_name,
    "search_memory": tool_search_memory,
    "list_memories": tool_list_memories,
    "read_memory": tool_read_memory,
    "recent_activity": tool_recent_activity,
    "current_context": tool_current_context,
    "search_captures": tool_search_captures,
    "run_command": tool_run_command,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "grep_search": tool_grep_search,
    "list_dir": tool_list_dir,
    "web_search": tool_web_search,
    "fetch_page": tool_fetch_page,
    "search_chat_history": tool_search_chat_history,
    "list_chat_sessions": tool_list_chat_sessions,
}
