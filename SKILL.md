---
name: docdiff
description: "Show a repo's uncommitted Markdown changes (or changes since a given commit, or between two commits such as a PR) as one rendered page: each changed doc in full, additions and deletions marked inline, with next/prev navigation, opened in the built-in browser. ReSpec specs (an index.html with a respecConfig and its includes) are rendered and diffed too. Use when the user invokes /docdiff, asks to see doc changes or proposed wording rendered \"on the text\", wants an htmldiff-style view of .md edits, or wants the rendered diff of a spec PR (e.g. a WICG/webmonetization PR whose pr-preview Diff is empty), or how a Google Doc differs from its Markdown before a /gdocsync push."
argument-hint: "[repo path] [base revision] [head revision | PR] [paths…]"
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
same place in the doc. The header's last line names both sides: the base revision with its commit, and
`--head`'s or the working tree (`--new-label` names it otherwise).
Whitespace-only rewraps do not count as changes. Relative images are embedded. With `--head <rev>`, new
is the files at that revision instead of the working tree (a PR, say). A changed `.html` file under an
`index.html` that holds a `respecConfig` stands for that ReSpec spec, which shows as one doc
([ReSpec specs](#respec-specs)).

**Accept** (`a`, or the ✓ button; "accept file" on each file's bar) moves the current change into the
accepted copy, and the page reloads without it; in Accepted, the same key (↺) takes a change back out.
Clicking a change makes it current and opens a small popup under it: ✓ (or ↺), ✎ revisit and the buttons
plugins add, such as GitHub ↗ for a PR ([Plugins](#plugins)); selecting text pops up ✎ alone. **Revisit** (`r`, or the
button) takes the selected text, or the current change, and a request typed under it; Send (↵; ⇧↵ adds a line) copies a
ready prompt to the clipboard and queues the request ([Accepts and revisits](#accepts-and-revisits)).

## Arguments

Read them from the invocation, in any order:

- a directory → the repo (default: the current working directory's repo). For the WM docs, that is
  `~/projects/webmonetization/docs` or `~/projects/webmonetization/wm-design-docs`.
- a revision (`HEAD~1`, a hash, a branch) → the base. Default `HEAD`. A second revision → `--head`.
- a PR (URL or number) → base and head from the PR ([Spec PRs](#spec-prs)).
- file paths → limit to those, as pathspecs relative to the repo root.

## Steps

1. Build and serve in one background command (`run_in_background: true`):

   ```bash
   python3 -I ~/.claude/skills/docdiff/docdiff.py --repo <repo> --base <rev> [--head <rev>] --serve [paths…]
   ```

   It prints `<k> changed doc(s) … → <out>/index.html`, then `URL: http://127.0.0.1:<port>/index.html`.
   The output dir defaults to `$TMPDIR/docdiff-<repo name>`. If a server for that dir is already running,
   the script only rebuilds the page, prints the existing URL and exits, so rerunning after more edits
   is cheap: reload the tab.
2. Read the background command's output file once for the `URL:` line. With `0 changed doc(s)`
   there is nothing to show: say so and stop.
3. Open the URL in the built-in browser (`navigate`, a new tab if the current one shows something else).
   Use http, never `file://`: a local file there renders as a static snapshot with no scripts, so the
   navigation does not work. Open it on `127.0.0.1` as printed, never `localhost`: specs render on
   `localhost`, and the server takes posts only from the `127.0.0.1` page.
4. Tell the user the files and change counts in one or two lines, and that `n`/`p` step through changes,
   `a` accepts one and `r` revisits a selection.

## Accepts and revisits

The server writes both into the output dir:

- `accepted/<file>.html` — the accepted copy: the file as the user has accepted it, rendered. It starts as
  the base; each accept moves one change in, each unaccept takes one out, and the server rebuilds the page.
  An edit to accepted text then shows in To review as just that edit. When the base moves (a commit, a
  new export), the accepted changes that still read the same carry over onto it and the others come back
  to review, so the copy never keeps text the new base dropped (`<file>.base` records the base commit).
  (`accepted.json`, the older store of accepted change keys, seeds the copy once with the changes that
  still match.)
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

## ReSpec specs

A spec renders in the page itself, not in the script: the first load shows the spec's bar with "rendering
with ReSpec…" while each side loads in a hidden frame, and a few seconds later the page reloads into the
diff. So the page must be open in the built-in browser, with network access (ReSpec, xref and specref
load online). Renders are kept in `<out>/rendered/<sources id>.html`, by the hash of the spec's directory
on that side, so an unchanged side never renders twice. Before diffing, the frame drops the table of
contents, the index, section numbers and permalinks, and unwraps sections, so the diff pairs headings,
paragraphs and list items. On "not rendered: …" in the bar, reload the page to retry.

The spec's own scripts run in the frame, served from `localhost` (another origin than the page on
`127.0.0.1`), so a PR cannot script the page or post accepts or revisits; what the frame sends back is
stripped of scripts and handlers before it enters the page.

A revisit on a spec names the spec's `index.html` and the section; find the included source file that
holds the quote (`grep -rl`) and edit that.

### Spec PRs

For a GitHub PR, run against a clone of its repo (with none, a blobless one is enough:
`git clone --filter=blob:none --no-checkout <repo url> "$TMPDIR/<name>"`):

```bash
git -C <clone> fetch -q origin main
git -C <clone> fetch -q origin "pull/<N>/head"
head=$(git -C <clone> rev-parse FETCH_HEAD)
base=$(git -C <clone> merge-base origin/main "$head")
```

then step 1 with `--base $base --head $head --pr <owner>/<repo>#<N> --out "$TMPDIR/docdiff-<name>-<N>"`.
Base is the merge base, as on GitHub, so changes that landed on main since do not show as reverted. Pass
hashes, not `FETCH_HEAD`: the server re-resolves both on every rebuild. `--pr` gives each change a button
to its line on GitHub ([GitHub PR](#github-pr)).

## Google Doc vs Markdown

What a `/gdocsync` push would change in a Google Doc, with the doc's open comments on it:

```bash
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> export [file.md…]
```

writes `~/.gdocsync/<repo name>/<repo name>-google/`, a git repo whose tag `google` holds each Google
Doc's text and whose working tree holds its Markdown's. Then step 1 with `--repo <that dir> --base google
--comments <that dir>/comments.json --new-label "Markdown of <repo>, as exported"`. Old is the Google Doc
(the header shows when it was read, from the export's commit), new the Markdown, both as the plain text a
push compares: inline styles and link targets do not show, since a push sets them from the Markdown.

Each open comment is marked where its quote reads in the old text: amber, underlined red when the change
deletes all of its anchor and dashed when it edits it, the thread in the tooltip. A comment whose quote
no longer reads there (the Google Doc was edited after it was made) is listed under the file's bar.

A revisit there names the export's file: apply it to the same file in the real repo, export again and
rerun docdiff.

**Publishing accepted changes.** Add `--publish` with gdocsync's push of the accepted copy, then its
export:

```bash
--publish "python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> push {file} --target {target} && python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> export {file}"
```

Each file with accepted changes then has "publish accepted" on its bar. After a confirm, the server
writes the accepted copy as Markdown (`{target}`), runs the command and shows what it printed; on
success the page reloads with the published changes in the old side and the rest still to review. A
push refused because the Google Doc or the Markdown changed since the export publishes nothing: export
again, rerun docdiff (accepts that still apply carry over), and publish again. The click is the user's
go: never press it on a doc other people read.

## Plugins

Source-specific features are plugins: the Python modules in `plugins/` next to `docdiff.py`, each loaded
on every run and used where it applies. A plugin may define:

- `add_args(parser)`: its command-line arguments. Their values go into `<out>/.run.json` with the rest,
  so the server's rebuilds see them.
- `actions(run)`, `run` being that `.run.json` as a dict: `None` where the plugin does not apply, or else a
  function `(old path, new path, blocks)` that returns `{change key: [(label, key, href)]}`, called per
  file and view. `blocks` is the file's diff as text, per block (paragraph, list item, heading…) a list of
  `(change key or None, old text, new text)`. Each action is a link in the change's popup that opens
  `href` in a new tab; `key` does the same on the current change, so it must not be one of docdiff's own
  (`n p j k a r v`).

That is all the seam holds so far: arguments and per-change links.

### GitHub PR

`plugins/github_pr.py`. With `--pr <PR URL | owner/repo#N | N>` (N alone takes the repo from the clone's
`origin` remote; needs `--head`), each change's popup gets **GitHub ↗** (`g`): the change in the PR's
Files changed view at its line, where review comments go:
`https://github.com/<owner>/<repo>/pull/<N>/files#diff-<sha256 of the path>R<line>`, the anchor GitHub's
own line links use. The line is the head file's line of the change's first added word, or, when the
change only removes, `L<line>` in the base file.

To find it, the change's block (up to 40 words on each side of it) is matched word by word against the
files the PR changes, for a spec everything it changes in the spec's directory: the file holding the
longest run of the block's words wins, and aligning the two puts the change's word on a line. Tags and
punctuation do not count, so ReSpec's rewrites (`[=term=]`, `{{IDL}}`, `|var|`) cost only the words they
change; a `title` attribute counts, as ReSpec shows a note's or an example's title as its caption. A
Markdown file's words take their lines from pandoc's source positions (`gfm+sourcepos`). A change with no
run of 4 words anywhere, text ReSpec makes such as the references, links to its file without a line, or to
the PR's files when a spec has several.

## Proposing wording with it

When proposing edits to docs, apply them to the real files (uncommitted), run docdiff, and let the user
review the rendered page; a rejected edit is a `git restore` of that file. Never commit or stage as part
of this skill.

## Limits

- Needs `pandoc` (`brew install pandoc`), and for specs the built-in browser.
- Links between docs and section anchors are not rewritten; the page is for reading changes, not
  for following links.
- A deleted file shows struck through as a whole; a renamed file diffs against its old path.
- Accepting and revisiting need the page served by docdiff (`--serve`); two identical changes with the
  same words around them in one file share a key, so accepting one accepts both.
