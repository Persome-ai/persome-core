# Import existing knowledge

On first onboarding, Persome shows a multi-select import step. A local folder is
always available. Obsidian appears only when Persome finds a registered local
vault, and Notion appears only when its desktop app is installed. Sources that
are not present on the Mac are not shown. The user may select any combination
or skip the step entirely; selected folders use the native macOS folder picker.

The import is read-only: Persome neither writes to the source nor installs an
Obsidian plugin. Hidden directories such as `.obsidian`, symlinks, non-text
attachments, and files larger than 2 MiB are excluded. All selected sources are
staged first, then one shared model build creates the first personal model.
Files are opened with the platform's no-follow flag, bounded to 2 MiB, and
checked before and after reading; a note being edited during import is skipped
and can be picked up safely on the next run. Persome also refuses to import its
own private data directory, preventing a generated-model feedback loop.

Notion's application cache is never inspected or modified. Detecting
`Notion.app` only makes the option relevant in onboarding; the user then chooses
an unpacked export copy with the native folder picker. Obsidian discovery reads
its vault registry, but content import excludes the entire `.obsidian` tree.

## Onboarding states

The existing native onboarding surface follows four states:

1. **Choose sources.** Local folder is always present. Obsidian appears only
   for a real registered vault; Notion appears only when its desktop app is
   installed. The user may select multiple sources or choose **Not Now**.
2. **Choose folders.** Obsidian needs no second choice. Local folder and Notion
   export use the macOS folder picker, making the exact read boundary visible.
3. **Import and build.** Status lines name each source as read-only. All sources
   stage idempotently, then share one model build through the production writer.
4. **Receipt.** Onboarding reports new/changed, unchanged, and skipped files.
   A failed build remains resumable because its pending sessions are retained.

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
