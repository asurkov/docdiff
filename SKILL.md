---
name: docdiff
description: "Show a repo's uncommitted Markdown changes (or changes since a given commit, or between two commits such as a PR) as one rendered page: each changed doc in full, additions and deletions marked inline, with next/prev navigation, opened in the built-in browser. ReSpec specs (an index.html with a respecConfig and its includes) are rendered and diffed too. Use when the user invokes /docdiff, asks to see doc changes or proposed wording rendered \"on the text\", wants an htmldiff-style view of .md edits, or wants the rendered diff of a spec PR (e.g. a WICG/webmonetization PR whose pr-preview Diff is empty), or how a Google Doc differs from its Markdown before a /gdocsync push."
argument-hint: "[repo path] [base revision] [head revision | PR] [paths…]"
---

# docdiff

Renders old = the file at a base revision (default `HEAD`) and new = the working tree (staged,
unstaged and untracked `.md` files) with pandoc, compares the two renders paragraph by paragraph (headings,
list items and tables count as paragraphs), and writes one page. Added paragraphs show as plain text with
a green bar; removed ones sit under a one-line summary with a red bar, open up to 200 words and folded
past that; a lightly edited one is word-diffed inline; consecutive added and removed paragraphs within a
section are one change, all its old text first, then all its new, each in one box with one bar,
paragraphs and list items together; only a change of one-to-one rewrites keeps each old paragraph above its new one. The page has
a sticky header with per-file change counts, "change N of M · file", ↑ prev / next change ↓ buttons and
`n`/`p` (or `j`/`k`) keys, and a view switch (`v` cycles): **To review** (the default: the working tree
against the accepted copy, so only what changed since the last accepts), **Accepted** (base against the
accepted copy: what the user has signed off), **Diff vs base** (everything, read-only), and **Old** /
**New**, each version clean, for rewrites too large to read inline; switching keeps the reader at the
same place in the doc. `n`/`p` step from the current change; with none, from where the reader last clicked
or selected, while that is on screen, and otherwise from the top of the screen. The header's first line says what is compared
(Uncommitted edits, Commits, GitHub PR or Google Docs vs Markdown, with the repo) and names both sides:
the base revision with its commit, and `--head`'s or the working tree (`--new-label` names it otherwise).
Whitespace-only rewraps do not count as changes. Relative images are embedded. With `--head <rev>`, new
is the files at that revision instead of the working tree (a PR, say). A changed `.html` file under an
`index.html` that holds a `respecConfig` stands for that ReSpec spec, which shows as one doc
([ReSpec specs](#respec-specs)).

**Accept** (`a`, or the ✓ button; "accept file" on each file's bar) moves the current change into the
accepted copy, and the page reloads without it; in Accepted, the same key (↺) takes a change back out.
Clicking a change makes it current and opens a small popup under it: ✓ (or ↺), ✎ revisit and the buttons
plugins add, such as 💬 comment ([Review comments](#review-comments)) and GitHub ↗ for a PR
([Plugins](#plugins)); selecting text pops up ✎ and 💬. **Revisit** (`r`, or the
button) takes the selected text, or the current change, and a request typed under it; Send (↵; ⇧↵ adds a line) copies a
ready prompt to the clipboard and queues the request ([Accepts and revisits](#accepts-and-revisits)).

**Reviewing commits** (`--head`: a PR, say), where there is nothing to edit or publish, the same actions
read as a review: accept is **✓ looks good** (`a`) and ↺ **not reviewed**, the Accepted view is
**Reviewed** ("22 to review · 28 reviewed"), and revisit is **✎ ask** (`r`), a question to Claude about
the change; 💬 comment (`c`) is the remark for the PR's author. On a change you commented on, accept is
**✓ reviewed** instead: it leaves To review without saying it looks good. With `--pr` and your login
known, a comment you wrote on GitHub counts as your 💬 does, here and in Reviewed below
([GitHub PR](#github-pr)); other people's comments do not. Either one resolves the
answered threads on that change alone; ↺ not reviewed reopens those ([Review comments](#review-comments)).

Reviewed shows reviewed changes **settled**: the new text only, no strike-through or highlight, with a
thin neutral bar beside a block that changed; your commented wording has a dotted amber underline (the
comment's quote, or the change's text when the comment is on the whole change) and its 💬 beside it, and
a commented change its amber ✓ 💬 N. A removed block stays its one-line "removed: …" stub with its marks.
Clicking a change shows its diff again; **± diffs** in the header, remembered per viewer, shows them all,
green ✓ chips included. The header counts the reviewed changes that carry comments or open questions
("· 3 with comments · 1 open question"), each count stepping through them there. To review and Diff
always show diffs.

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
   `a` accepts one, `r` revisits a selection and `c` comments on it.

## Accepts and revisits

The server writes both into the output dir:

- `accepted/<file>.html` — the accepted copy: the file as the user has accepted it, rendered. It starts as
  the base; each accept moves one change in, each unaccept takes one out, and the server rebuilds the page.
  An edit to accepted text then shows in To review as just that edit. When the base moves (a commit, a
  new export), the accepted changes that still read the same carry over onto it and the others come back
  to review, so the copy never keeps text the new base dropped (`<file>.base` records the base commit).
  (`accepted.json`, the older store of accepted change keys, seeds the copy once with the changes that
  still match.)
- `revisit.jsonl` — one request per line: `time`, `file`, `section`, `quote`, `view`, `prompt`, `key` (the
  change's, for a revisit of a change) and `text` (the prompt as copied to the clipboard). One asked from a
  thread ([Review comments](#review-comments)) also has `thread`, its thread's first revisit's time: a
  follow-up (Ask more) quotes the thread's text, a revision of a drafted comment also has `revises` (the
  draft's id) and quotes the draft. Older lines have neither. When the user pastes one or says they sent
  revisits, read the lines since the last one handled, apply each to the file, and rerun docdiff; with
  `--head` (commits, a PR) a revisit is a question (its prompt starts "Ask about"): answer it, editing
  nothing. Before answering one with `thread`, read the thread so far: the revisit.jsonl lines whose
  `time` or `thread` is that time (or whose quote is its quote), and their answers in answers.jsonl, and
  answer the new question in that light. A revision asks to rework the draft it quotes: answer with the
  new draft as a blockquote.
  Open each reply with the request as sent, since the chat does not show it: the section and the quote
  as a blockquote, then the prompt, e.g. `> **Terms** · "visits a monetized document."` and
  `> monetized document or monetized page?`.
- `answers.jsonl` — Claude's answers to revisits, one per line: `{"time": <the revisit's time>, "answer":
  <Markdown>}`. After answering a revisit in chat, append the answer here (the reply without the quoted
  request), and the page shows it under the request ([Review comments](#review-comments)); until then the
  request shows as pending. Put a comment drafted for the PR in the answer as a blockquote (`> …`, a blank
  line between two), as the page offers each blockquote as a comment to make:

  ```bash
  python3 -c 'import json,sys; print(json.dumps({"time": sys.argv[1], "answer": sys.stdin.read().strip()}, ensure_ascii=False))' "<time>" >> <out>/answers.jsonl <<'EOF'
  <the answer>
  EOF
  ```
- `review.json` — the user's review comments, and which revisits they resolved or read
  ([Review comments](#review-comments)). When the user says they left comments, read them there.

**Watching for revisits.** While the user reviews, keep one background Bash command (`run_in_background`)
waiting for the next request. It has no time limit, unlike Monitor, and it touches `<out>/watcher` every
second: on Send the page checks that file, says "sent, Claude is watching" while it is fresh, and
otherwise copies the prompt for the user to paste. The command exits with the new request; handle it,
append the answer to `answers.jsonl`, then start the next wait at once.

```bash
sh -c 'd=<out>; f=$d/revisit.jsonl; touch "$f"; n=$(($(wc -l < "$f"))); while [ $(($(wc -l < "$f"))) -le $n ]; do touch "$d/watcher"; sleep 1; done; tail -n +$((n+1)) "$f"'
```

## ReSpec specs

`plugins/respec.py`. A spec renders in the page itself, not in the script: the first load shows the
spec's bar with "rendering with ReSpec…" while each side loads in a hidden frame, and a few seconds later
the page reloads into the diff. So the page must be open in the built-in browser, with network access
(ReSpec, xref and specref load online). Renders are kept in `<out>/rendered/<sources id>.html`, by the hash of the spec's directory
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
to its line on GitHub, and `--me <login>` makes your comments there your review ([GitHub PR](#github-pr)).

## Google Doc vs Markdown

For all of a repo's docs at once, `gdocsync.py --repo <repo> browse` exports them and runs docdiff as
below, with `--docs` and every mapped doc named, so one in step shows too (with its open comments, out
of the header's list): a table above the diff lists every Markdown file, its name a link to the doc
below, with a link to its Google Doc, where the two stand and its open comments
(`plugins/gdoc_docs.py`). For one doc, by hand:

What a `/gdocsync` push would change in a Google Doc, with the doc's open comments on it:

```bash
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> export [file.md…]
```

writes `~/.gdocsync/<repo name>/<repo name>-google/`, a git repo whose tag `google` holds each Google
Doc's text and whose working tree holds its Markdown's. Then step 1 with `--repo <that dir> --base google
--comments <that dir>/comments.json --new-label "Markdown of <repo>, as exported"`. Old is the Google Doc
(the header shows when it was read, from the export's commit), new the Markdown, both as the plain text a
push compares: inline styles and link targets do not show, since a push sets them from the Markdown.

Each open comment (`plugins/gdoc_comments.py`) is marked where its quote reads in the old text: amber,
underlined red when the change deletes all of its anchor and dashed when it edits it, the thread in the
tooltip; ⌘- or Ctrl-click on the mark opens the comment in the Google Doc. A comment whose quote no
longer reads there (the Google Doc was edited after it was made) is listed under the file's bar, each
a link to it in the Google Doc.

A revisit there names the export's file: apply it to the same file in the real repo, export again and
rerun docdiff.

**Publishing accepted changes.** Add `--publish` with gdocsync's push of the accepted copy, then its
export:

```bash
--publish "python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> push {file} --target {target} && python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> export {file}"
```

While any file has accepted changes, the header has "⇪ publish accepted (N)". It opens a panel at the
bottom that lists each such file and its count; on Publish the server publishes them in turn, the
button saying which one ("publishing 1 of 2…"): for each, it writes the accepted copy as Markdown
(`{target}`) and runs the command. The panel then shows what each printed, and Done reloads the page
with the published changes in the old side and the rest still to review. A push refused because the
Google Doc or the Markdown changed since the export publishes nothing for that file: export again,
rerun docdiff (accepts that still apply carry over), and publish again; a file already published has
nothing accepted left, so it is not published twice. The click is the user's
go: never press it on a doc other people read.

## Plugins

Source-specific features are plugins: the Python modules in `plugins/` next to `docdiff.py`, each loaded
on every run and used where it applies.

- `respec.py`: ReSpec specs ([ReSpec specs](#respec-specs)).
- `gdoc_comments.py`: `--comments`, a Google Doc's comments marked on the old side
  ([Google Doc vs Markdown](#google-doc-vs-markdown)).
- `gdoc_docs.py`: `--docs`, the repo's docs and their Google Docs listed above the diff.
- `github_pr.py`: `--pr`, a link from each change to its line in a GitHub PR, and the PR's comments read
  from GitHub ([GitHub PR](#github-pr)).
- `review.py`: review comments, and revisits with Claude's answers, on the page
  ([Review comments](#review-comments)).

A plugin sees docdiff itself as `docdiff` (`git()`, `read()` and the like) and may define the hooks
below; `load_plugins()` in `docdiff.py` has their exact shapes. `run` is the run's arguments as
`<out>/.run.json` keeps them, `repo` and `out` as full paths.

- `add_args(parser)`: its command-line arguments, kept in `.run.json` with the rest, so the server's
  rebuilds see them.
- `PATHS` and `doc(…)`: the files it makes docs of beside `*.md`, and the doc a changed one stands for
  (a spec's `index.html` for a section file).
- `htmls(…)`: its docs rendered, both sides, or while they are not, what the file's section shows instead.
- `section(run, name)`: words for a file's bar and data for the page.
- `title(run)`: what the page compares, for the header's first line, with a link or none: `github_pr.py`'s
  names the PR, `gdoc_comments.py`'s a Google Doc against its Markdown.
- `top(run)`: html above the docs, such as `gdoc_docs.py`'s table; with it the page is served even when
  no doc changed.
- `actions(run)`: links in a change's popup, `[(label, key, href)]` per change, made from the file's diff
  as text: per block (paragraph, list item, heading…) a list of `(change key or None, old text, new
  text)`. A link opens `href` in a new tab, and so does `key` on the current change, so the key must not
  be one of docdiff's own (`n p j k a r v`).
- `place(run, old, new, side, before, after)`: for other plugins, where text of a file's block is in its
  source; `github_pr.py`'s gives review comments their file and line.
- `remarks(run, out, refresh)`: for other plugins, comments made on the docs elsewhere; `github_pr.py`'s
  reads the PR's on GitHub, which review comments show.
- `STYLE`, `SCRIPT`: CSS and JavaScript for the page, after docdiff's own. An element of class `panel`
  holds off the reload after a rebuild while it shows, and clicks in it leave the doc alone; so do clicks
  in an element of class `ui` (a card or side panel), which does not hold off the reload.
- `get(run, path, query)`, `post(out, path, req)`: the server's routes; the page rebuilds after a post,
  unless the path is in the plugin's `IN_PLACE`.

### GitHub PR

`plugins/github_pr.py`. With `--pr <PR URL | owner/repo#N | N>` (N alone takes the repo from the clone's
`origin` remote; needs `--head`), each change's popup gets **GitHub ↗** (`g`): the change in the PR's
Files changed view at its line, where review comments go:
`https://github.com/<owner>/<repo>/pull/<N>/files#diff-<sha256 of the path>R<line>`, the anchor GitHub's
own line links use. The line is the head file's line of the change's first added word, or, when the
change only removes, `L<line>` in the base file. GitHub highlights the line but does not scroll to it,
so the button names the file and line: **GitHub ↗ terms.html:23**, with "(old)" for a base line.

To find it, the change's block (up to 40 words on each side of it) is matched word by word against the
files the PR changes, for a spec everything it changes in the spec's directory: the file holding the
longest run of the block's words wins, and aligning the two puts the change's word on a line. Tags and
punctuation do not count, so ReSpec's rewrites (`[=term=]`, `{{IDL}}`, `|var|`) cost only the words they
change; a `title` attribute counts, as ReSpec shows a note's or an example's title as its caption. A
Markdown file's words take their lines from pandoc's source positions (`gfm+sourcepos`). A change with no
run of 4 words anywhere, text ReSpec makes such as the references, links to its file without a line, or to
the PR's files when a spec has several. `place()` finds other text of a block the same way, for review
comments.

### Review comments

`plugins/review.py`. Both popups, a change's and a selection's, get **💬** (`c`): a comment on that change
or that text, typed in a panel like the revisit one (↵ saves, ⇧↵ adds a line). It is kept in
`<out>/review.json` with its file, view, change key (none on a selection), quote (its text as it reads),
the text of its block around it, section, body and time; with `--pr`, also `github`: its file and line in
the PR, found as its change's GitHub ↗ button finds them, and found again when the run's head moves.

Each comment is a 💬 mark on its change in every view that has the change, and elsewhere where its quote
reads, the text before it picking among repeats: so it stays on a change once that is accepted, and shows
on the same text in Old or New. Where a view has neither (an accepted change, removed words), the mark
ends the paragraph or list item holding the text before it in its block (or else after it); only a view
without that block goes without the mark. When no view has its text any more (it was rewritten since),
the mark sits on its section's heading. A quote that does not read as it is is matched with spaces
ignored, as a selection across paragraphs keeps no space between them. In To review, a mark on text already accepted (on a change in
Accepted, on none in To review) is muted, still a click from its comment; marks never count as changes or
take n/p. A mark on removed words sits after them, so settled Reviewed shows it. Clicking the mark edits
or deletes it. The header's **💬 N** opens the Review panel: the comments and revisits in the order they
come in the doc, each a click from its place, comments with edit and delete, and **Copy as Markdown**,
which copies per comment its `path:line` (linked to the line on GitHub; without `--pr`, the file and
section), the quote and the comment, to paste into GitHub. Saving a comment shows it in place, with no
rebuild; comments outlast rebuilds, accepts and server restarts.

Revisits show the same way, as ✎ marks, one per thread: the revisits of one change, or of one quote, each
request with Claude's answers from `answers.jsonl` under it. Asking again on the same change or quote
adds to its thread. One on selected text keeps only its quote, so a view without it takes the text around
it from a view that has it. A thread is **pending** (dashed) while its last request has no answer,
**new** (filled and bold, counted in the header) while an answer is unread, until its card is opened, and
**resolved** once the user says so: ✓ Resolve on its card or in the Review panel, or, on commits, by
accepting (✓ looks good or ✓ reviewed) the change it is on, which resolves its answered threads; a thread
whose last question has no answer yet stays open, and so does one on text outside a change or across
several. ↺ not reviewed reopens the threads its accept resolved, not ones resolved by hand. On your own
docs accepting resolves nothing. Asking again reopens a thread. A resolved thread folds to a faint dot in
the text, which opens its card to ↺ Reopen, and in the panel into a folded Resolved group under the open
ones (Open / Resolved / All filter). A thread's card keeps its header, the state with ✎ Ask more and ✓
Resolve (↺ Reopen), at its top while a long thread scrolls under it; in the Review panel each entry has
them on its first line, and a long entry folds to a few lines with Show more. On commits, a card whose
thread is on one change still in To review leads with that change's accept, worded as the toolbar's (✓
looks good, or ✓ reviewed on a commented change; `a` while the card is open) and doing the same: the
change is reviewed and its answered threads resolve, this one with them unless its last question has no
answer yet, as the button's title then says. ✓ Resolve is the smaller choice there, done asking but not
done reviewing: it resolves this thread alone and leaves the change in To review. A card on unchanged
text, across several changes or on a reviewed change has only Resolve or Reopen. **Ask more** (or `r`
while the card is open) opens the revisit panel on the thread's own text, saying which thread it goes on
from: the question joins that thread whatever text is selected, and reopens it if it was resolved.

Each drafted comment in an answer (a blockquote) is a **proposal**, shown once per thread however many
answers repeat it, with its actions under it. **💬 Use** makes it a comment on the thread's change or
quote and opens that to edit (edits go in the comment, never in the proposal); the proposal then shows ✓
used with 💬 Open comment. **✎ Revise** opens the revisit panel on the draft, in its thread ("shorter",
"as a question"); the draft in that revision's answer replaces it, which folds to a summary, still
readable. **Dismiss** folds it to a stub. **↶ Undo** takes back Use (deleting the comment, after asking
if it was edited) or Dismiss. An answer with several proposed drafts also offers 💬 Use all as one
comment. Using a draft resolves nothing: once every draft in a thread is used, dismissed or replaced, its
header says "drafts handled" and urges Resolve, where the card has no change to accept.

`review.json` keeps each revisit's state by its time, `revisits: {<time>: {resolved, resolved_by, seen}}`,
`resolved_by` `accept` when its change's accept resolved it, `seen` how many of its answers were read, and each proposal's by the time of the revisit that first proposed it and
the draft's hash, `proposals: {"<time>#<hash>": {state: used | dismissed, comment}}`; deleting the comment
frees the draft again. `revisit.jsonl` and `answers.jsonl` are only ever appended to.

The page asks the server every 2 seconds, so a request or an answer shows without a reload. A server
started before `review.py` existed has no route for any of this (💬 N greys out): stop it and rerun
docdiff.

**The PR's comments on GitHub** (`github_pr.py`, with `--pr`; read only, nothing is posted). The page
reads the PR's review comments, its own comments and its reviews' bodies from GitHub's API on load and
from ↻ GitHub in the Review panel, which says when they were read and, offline or rate limited, that it
shows the last copy. They are kept in `<out>/github.json` with each page's ETag; a page load asks again
only when the copy is over 5 minutes old, as without a token each ask costs 3 of GitHub's 60 requests an
hour (a 304 included). `GITHUB_TOKEN`, if set, is sent and never written down. `--pr-comments FILE`
reads them from a file instead (`{review, issue, reviews}` as the API lists them), offline or for a test.
`--me <login>` names you on GitHub; without it, `GITHUB_TOKEN`'s user (`GET /user`, once, its login kept
in `github.json`). With neither, every GitHub comment is someone else's.

A line comment is placed on the diff by its `path`, `side` and `line`, the inverse of a change's GitHub
button: the words of that source line, with the words around it, are matched against the diff's blocks,
and the block whose matching run passes through the line holds it, on the change there or in unchanged
text. Each thread (a comment and its replies) is a neutral speech-bubble mark with its count, next to
✎ and 💬, in every view where its text reads, or else its block, as a 💬 is; clicking it opens it read only: author, time, the body as
Markdown (rendered as answers are), the replies, and Open on GitHub ↗. Outdated comments, ones that
could not be placed, the PR's own comments and review bodies are listed in the Review panel under
On GitHub. A 💬 of yours whose text a GitHub comment has (spacing aside, or one holding the other) shows
as posted, linked to it.

**Yours** (your login known): a thread you wrote in, first or as a reply, is your comment on its change,
as a 💬 is: accept there reads ✓ reviewed, the change counts in "with comments", and in settled Reviewed
its wording has the dotted amber underline. Its mark is amber, still read only with Open on GitHub ↗, and
the Review panel names you "(you)". A 💬 is posted only by a comment of yours, and that comment then shows
as the 💬 alone: a thread of nothing else has no mark, one with replies counts the replies.

**Pushing, later** (not built). `review.json` as a pending review through `gh api`
(`POST /repos/<owner>/<repo>/pulls/<N>/reviews` with `commit_id` the head and `comments: [{path, line,
side, body}]`, no `event`, so it stays pending for the user to submit on GitHub), keeping each comment's
GitHub id in `review.json` so a second push updates instead of duplicating; comments without a line go
in the review's body. Submitting stays the user's click.

## Proposing wording with it

When proposing edits to docs, apply them to the real files (uncommitted), run docdiff, and let the user
review the rendered page; a rejected edit is a `git restore` of that file. Never commit or stage as part
of this skill.

## Limits

- Needs `pandoc` (`brew install pandoc`), and for specs the built-in browser.
- Links between docs and section anchors are not rewritten; the page is for reading changes, not
  for following links. A click on a link in the doc does nothing, so it never scrolls away from the
  change being read; a web link opens in a new tab, and a modifier key follows any link as usual.
- A deleted file shows struck through as a whole; a renamed file diffs against its old path.
- Accepting and revisiting need the page served by docdiff (`--serve`); two identical changes with the
  same words around them in one file share a key, so accepting one accepts both.
