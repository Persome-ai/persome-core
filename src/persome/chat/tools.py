"""Chat tool schemas (OpenAI function-calling format)."""

from __future__ import annotations

from typing import Any

# ─── tool definitions (OpenAI function-calling schema) ────────────────────

CHAT_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Search the user's long-term memory entries (people, projects, tools, "
                "topics, events, preferences, etc.) by keyword. Returns the most relevant "
                "entries ranked by BM25."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords, e.g. 'DeepSeek API' or 'weekly meeting'.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return (default 5).",
                        "default": 5,
                    },
                    "since": {
                        "type": "string",
                        "description": "Only include entries after this ISO timestamp, e.g. '2026-05-01'.",
                    },
                    "until": {
                        "type": "string",
                        "description": "Only include entries before this ISO timestamp.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": (
                "List all memory files (user profile, projects, people, tools, topics, etc.). "
                "Use this to discover what memories exist before searching."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": (
                "Read the full content of a specific memory file by its filename, "
                "e.g. 'user-profile.md' or 'project-persome.md'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Memory filename, e.g. 'user-profile.md'.",
                    },
                    "tail_n": {
                        "type": "integer",
                        "description": "Only return the last N entries (latest first).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_activity",
            "description": (
                "Get the user's recent activity entries from memory, ordered by time "
                "(newest first). Useful for questions like 'what did I do today/recently'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO timestamp, e.g. '2026-05-15' or '2026-05-15T09:00'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (default 20).",
                        "default": 20,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_context",
            "description": (
                "Get a snapshot of what's currently on the user's screen: recent captures, "
                "active apps, window titles, and recent timeline blocks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_filter": {
                        "type": "string",
                        "description": "Filter by app name, e.g. 'Chrome' or 'VS Code'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_captures",
            "description": (
                "Search raw screen captures (AX tree snapshots) by keyword. "
                "Returns matched snippets with app, window title, URL, and timestamp. "
                "Useful for finding specific things the user saw on screen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords.",
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Filter by app name.",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO timestamp lower bound.",
                    },
                    "until": {
                        "type": "string",
                        "description": "ISO timestamp upper bound.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    # ─── coding / terminal tools ──────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a shell command in the user's terminal and return stdout/stderr. "
                "Use for: running scripts, git commands, installing packages, checking system state, "
                "compiling code, running tests, etc. Commands run in the user's default shell."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory. Defaults to the user's home directory.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30).",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file from the local filesystem. "
                "Supports text files of any kind: source code, config, logs, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Start reading from this line number (1-based).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines to read (default: entire file).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file. Creates the file if it doesn't exist, "
                "overwrites if it does. Creates parent directories as needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace a specific string in a file with new content. "
                "Use for targeted edits without rewriting the whole file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact string to find and replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement string.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": (
                "Search file contents by regex or literal string. "
                "Returns matching lines with file paths and line numbers. "
                "Useful for finding code, config values, or text across a project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (regex supported).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in. Defaults to current directory.",
                    },
                    "include": {
                        "type": "string",
                        "description": "File glob pattern to include, e.g. '*.py' or '*.ts'.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List files and directories at a given path. "
                "Useful for exploring project structure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path. Defaults to current directory.",
                    },
                },
            },
        },
    },
    # ─── web tools ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo. Returns titles, URLs, and snippets. "
                "Use for current events, documentation lookups, or anything not in local memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Fetch a web page and extract its main text content. "
                "Returns cleaned text without HTML tags. Use after web_search to read a specific page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch.",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Max characters to return (default 10000).",
                        "default": 10000,
                    },
                },
                "required": ["url"],
            },
        },
    },
    # ─── chat history tools ───────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "search_chat_history",
            "description": (
                "Search across all past chat sessions (including archived/compressed ones) "
                "for messages matching a keyword. Returns matching messages with session ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_chat_sessions",
            "description": (
                "List all past chat sessions with turn count and first message preview. "
                "Use to find a specific past conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

CHAT_SCHEMA_NAMES = {t["function"]["name"] for t in CHAT_SCHEMAS}


def to_anthropic_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI-style function-calling schemas to Anthropic tool format.

    Anthropic uses 'input_schema' instead of 'parameters'.
    """
    out = []
    for s in schemas:
        fn = s.get("function", s)
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", fn.get("input_schema", {})),
            }
        )
    return out
