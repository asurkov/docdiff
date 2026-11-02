---
name: docdiff
description: "Show a repo's uncommitted Markdown changes (or changes since a given commit) as one rendered page: each changed doc in full, additions and deletions marked inline, with next/prev navigation, opened in the built-in browser. Use when the user invokes /docdiff, asks to see doc changes or proposed wording rendered \"on the text\", or wants an htmldiff-style view of .md edits."
argument-hint: "[repo path] [base revision] [paths…]"
---

# docdiff

Renders old = the file at a base revision (default `HEAD`) and new = the working tree (staged,
unstaged and untracked `.md` files) with pandoc, compares the two renders paragraph by paragraph (headings,
list items and tables count as paragraphs), and writes one page. Added paragraphs show as plain text with
a green bar; removed ones fold to a one-line summary with a red bar; a lightly edited one is word-diffed
inline; consecutive added and removed paragraphs within a section are one change. The page has
a sticky header with per-file change counts, "change N of M · file", ↑ prev / next change ↓ buttons and
`n`/`p` (or `j`/`k`) keys, and a view switch (`v` cycles): **To review** (the default: the working tree
against the accepted copy, so only what changed since the last accepts), **Accepted** (base against the
accepted copy: what the user has signed off), **Diff vs base** (everything, read-only), and **Old** /
**New**, each version clean, for rewrites too large to read inline; switching keeps the reader at the
same place in the doc.
Whitespace-only rewraps do not count as changes. Relative images are embedded.

**Accept** (`a`, or the ✓ button; "accept file" on each file's bar) moves the current change into the
accepted copy, and the page reloads without it; in Accepted, the same key (↺) takes a change back out.
Clicking a change makes it current and opens a small popup under it: ✓ (or ↺) and ✎ revisit; selecting
text pops up ✎ alone. **Revisit** (`r`, or the
button) takes the selected text, or the current change, and a request typed under it; Send (↵; ⇧↵ adds a line) copies a
ready prompt to the clipboard and queues the request ([Accepts and revisits](#accepts-and-revisits)).

## Arguments

Read them from the invocation, in any order:

- a directory → the repo (default: the current working directory's repo). For the WM docs, that is
  `~/projects/webmonetization/docs` or `~/projects/webmonetization/wm-design-docs`.
- a revision (`HEAD~1`, a hash, a branch) → the base. Default `HEAD`.
- file paths → limit to those, as pathspecs relative to the repo root.

## Steps

1. Build and serve in one background command (`run_in_background: true`):

   ```bash
   python3 -I ~/.claude/skills/docdiff/docdiff.py --repo <repo> --base <rev> --serve [paths…]
   ```

   It prints `<k> changed Markdown file(s) … → <out>/index.html`, then `URL: http://127.0.0.1:<port>/index.html`.
   The output dir defaults to `$TMPDIR/docdiff-<repo name>`. If a server for that dir is already running,
   the script only rebuilds the page, prints the existing URL and exits, so rerunning after more edits
   is cheap: reload the tab.
2. Read the background command's output file once for the `URL:` line. With `0 changed Markdown file(s)`
   there is nothing to show: say so and stop.
3. Open the URL in the built-in browser (`navigate`, a new tab if the current one shows something else).
   Use http, never `file://`: a local file there renders as a static snapshot with no scripts, so the
   navigation does not work.
4. Tell the user the files and change counts in one or two lines, and that `n`/`p` step through changes,
   `a` accepts one and `r` revisits a selection.

## Accepts and revisits

The server writes both into the output dir:

- `accepted/<file>.html` — the accepted copy: the file as the user has accepted it, rendered. It starts as
  the base; each accept moves one change in, each unaccept takes one out, and the server rebuilds the page.
  An edit to accepted text then shows in To review as just that edit. (`accepted.json`, the older store of
  accepted change keys, seeds the copy once with the changes that still match.)
- `revisit.jsonl` — one request per line: `time`, `file`, `section`, `quote`, `view`, `prompt`, and `text`
  (the prompt as copied to the clipboard). When the user pastes one or says they sent revisits, read the
  lines since the last one handled, apply each to the file, and rerun docdiff.

**Watching for revisits.** While the user reviews, keep one background Bash command (`run_in_background`)
waiting for the next request. It has no time limit, unlike Monitor, and it touches `<out>/watcher` every
second: on Send the page checks that file, says "sent, Claude is watching" while it is fresh, and
otherwise copies the prompt for the user to paste. The command exits with the new request; handle it,
then start the next wait at once.

```bash
sh -c 'd=<out>; f=$d/revisit.jsonl; touch "$f"; n=$(($(wc -l < "$f"))); while [ $(($(wc -l < "$f"))) -le $n ]; do touch "$d/watcher"; sleep 1; done; tail -n +$((n+1)) "$f"'
```

## Proposing wording with it

When proposing edits to docs, apply them to the real files (uncommitted), run docdiff, and let the user
review the rendered page; a rejected edit is a `git restore` of that file. Never commit or stage as part
of this skill.

## Limits

- Needs `pandoc` (`brew install pandoc`).
- Links between docs and section anchors are not rewritten; the page is for reading changes, not
  for following links.
- A deleted file shows struck through as a whole; a renamed file diffs against its old path.
- Accepting and revisiting need the page served by docdiff (`--serve`); two identical changes with the
  same words around them in one file share a key, so accepting one accepts both.
