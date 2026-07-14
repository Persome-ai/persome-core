# Import existing knowledge

On first onboarding, Persome checks Obsidian's local desktop vault registry. If
an active vault is present, one confirmation imports its Markdown notes and
builds the first personal model. The import is read-only: Persome neither
writes to the vault nor installs an Obsidian plugin. Hidden directories such as
`.obsidian`, symlinks, non-text attachments, and files larger than 2 MiB are
excluded.

This is the lowest-friction first import because an Obsidian vault is already a
local folder of Markdown files. A plugin is not required for migration; a
future plugin can add continuous, event-driven sync inside Obsidian.

The same source-agnostic entry point is available after onboarding:

```bash
# Auto-detect the currently open registered Obsidian vault.
persome import-data --source obsidian

# Import any local Markdown or UTF-8 text folder.
persome import-data --source folder --path ~/Documents/notes

# Import an unpacked Notion Markdown export.
persome import-data --source notion --path ~/Downloads/notion-export
```

Files are keyed by source path and SHA-256 content hash. Unchanged files are
not modeled twice; changed files retain a new provenance session. Imported
content enters the existing timeline/session writer and model-build pipeline,
so it follows the same evidence and idempotency rules as live context. Use
`--no-build` to stage changed documents without immediately running model
formation.

The current generic importer accepts `.md`, `.markdown`, and `.txt`. Unpack a
Notion export before selecting its folder. Other structured cloud sources can
be added behind the same importer contract without changing onboarding.
