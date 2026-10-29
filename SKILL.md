---
name: docdiff
description: "Show a repo's uncommitted Markdown changes (or changes since a given commit) as one rendered page: each changed doc in full, additions and deletions marked inline, with next/prev navigation, opened in the built-in browser. Use when the user invokes /docdiff, asks to see doc changes or proposed wording rendered \"on the text\", or wants an htmldiff-style view of .md edits."
argument-hint: "[repo path] [base revision] [paths…]"
---

# docdiff

Renders old = the file at a base revision (default `HEAD`) and new = the working tree (staged,
unstaged and untracked `.md` files) with pandoc, word-diffs the two HTML renders, and writes one page:
a sticky header with per-file change counts, "change N of M · file", ↑ prev / next change ↓ buttons and
`n`/`p` (or `j`/`k`) keys, and a view switch (`v` cycles): **Since last run** (against the previous docdiff
run of the same repo; the default when it differs), **Diff vs base**, and **Old** / **New**, each version clean,
for rewrites too large to read inline; switching keeps the reader at the same place in the doc.
Whitespace-only rewraps do not count as changes. Relative images are embedded.

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
4. Tell the user the files and change counts in one or two lines, and that `n`/`p` step through changes
   and **Since last run** shows only what changed since they last looked.

## Proposing wording with it

When proposing edits to docs, apply them to the real files (uncommitted), run docdiff, and let the user
review the rendered page; a rejected edit is a `git restore` of that file. Never commit or stage as part
of this skill.

## Limits

- Needs `pandoc` (`brew install pandoc`).
- Links between docs and section anchors are not rewritten; the page is for reading changes, not
  for following links.
- A deleted file shows struck through as a whole; a renamed file diffs against its old path.
