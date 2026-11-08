# CGA Gala Audit Portal

A local web page for auditing a swimming gala **while it runs**, with Claude Code. As the gala goes on, the
lane sheet packs, Gen7 race PDFs and Meet Manager exports land in an inbox folder; the portal sorts them by
session, and as soon as an event's lane sheets and Gen7 races are in, a Claude Code session on this computer
transcribes and audits that event with the
[gala-ocr plugin](https://github.com/XRayA4T/Gala-AI-Plugins/tree/main/plugins/gala-ocr) (1.6.0 or later),
so problems reach the officials while they're still at the pool. It is built from the Claude QA Board, so it also has questions and answers, documents, a live
view of Claude's screen and progress bars.

Everything stays on this computer. The page only listens on `127.0.0.1`.

## Start

```bash
git clone https://github.com/XRayA4T/Gala-Audit.git "CGA Gala Audit"
cd "CGA Gala Audit"
bash start-portal.sh
```

It:

1. checks for python3, tmux, Claude Code and poppler (`pdfinfo`, `pdftoppm`, `pdftotext`);
2. makes the `data/` folders and a private key in `.portal/key`;
3. sets up Claude Code for this folder: the gala-ocr plugin, the portal as an MCP server (`.mcp.json`),
   permissions and the inbox hook (`.claude/settings.local.json`);
4. starts the portal at `http://127.0.0.1:9000`, Claude Code in tmux (it restarts itself and carries on if it
   exits), and the watcher that hands new questions to Claude and mirrors its screen on the **Live** tab;
5. opens the portal in your browser and puts this terminal into the Claude session.

**The first time**, answer Claude Code's questions in the terminal: trust this folder, use the `portal` MCP
server, and install the plugin from the `gala-ai-plugins` marketplace. If Claude isn't signed in yet, run
`claude` once on its own first.

Options: `--port 9000`, `--inbox DIR` (default `data/inbox`; e.g. a shared folder the scanner and the Gen7
computer save into), `--name "Gala name"`, `--no-browser`, `--no-attach`.
Leave the terminal with **Ctrl-b d**; Claude keeps running. Back in: `tmux -L gala-portal attach -t claude`.

```bash
bash stop-portal.sh              # stops the page, the watcher and Claude (data/ and the questions are kept)
```

## During the gala

**Files in.** Save the gala's files into the inbox folder (from the scanner, the Gen7 computer, or by dropping
them on the **Gala** tab). Within a few seconds each one is sorted into its session:

| What | How it's recognised | Goes to |
|---|---|---|
| Meet entries export (`Meet Entries-…zip` / `Hfile…hy3`) | `.zip` / `.hy3`, "entr" in the name | `sessions/S<n>/meet` |
| Merge results export / final export | "result" / "final" in the name | `sessions/S<n>/meet` |
| Printed lane sheets (Meet Manager's PDF, not the scan) | its text (Event, Lane, Heat) | `sessions/S<n>/meet` |
| Gen7 race PDF | its text (Event, Heat, Race #, Session) | `sessions/S<n>/gen7` |
| Lane sheet pack | a scanned PDF, "Event 3" in the name | `sessions/S<n>/scans` |
| DQ slips / ETD programme | a scanned PDF with "DQ" / "ETD" in the name | `sessions/S<n>/scans` |

Events numbered by session in hundreds (101–1xx session 1, 201–2xx session 2) are split into sessions
automatically, so one meet-wide entries export or one printed lane sheet PDF covers every session. Heats come
from the entries export, or, when it is a pre-gala export with no heats yet, from the printed lane sheets.

The session comes from "Session 2" or "S2" in the name, the Gen7 PDF's own session, or the session whose
entries have that event. A file with the same name as one already there replaces it (a rescan or a reprint;
the old copy is kept in `.replaced`). **A folder another computer saves into** (e.g. the Gen7 computer's PDF folder on a share): put a symbolic
link to it in the inbox, `ln -s "/path/to/Gen7 PDFs" data/inbox/gen7`. Files in a linked folder are **copied**,
not moved, so that folder is left alone; each file is copied once, and again only if it changes (a reprint).
Every PDF already in it is copied on the first look, so link a folder for this gala only.

The portal **looks** at the inbox every few seconds rather than waiting to be told of changes, so it works on a
Windows (SMB) share, where Linux gets no change notifications. A new file is taken once its size and time have
stayed the same for 6 seconds (it may still be copying), timed by this computer's clock, so a Windows clock
that's ahead or behind doesn't matter. Mount the share read-only, for example:

```bash
sudo apt install cifs-utils
sudo mkdir -p /mnt/gen7
sudo mount -t cifs "//GEN7-PC/Gen7 Reports" /mnt/gen7 \
     -o ro,vers=3.0,username=WINUSER,uid=$(id -u),gid=$(id -g),actimeo=1
ln -s /mnt/gen7 data/inbox/gen7
```

To mount it at every start, put the same in `/etc/fstab` with a credentials file
(`credentials=/home/YOU/.smb-gen7`, mode 600, holding `username=`, `password=`, `domain=`). A share opened from
the file manager (`/run/user/<uid>/gvfs/smb-share:server=…,share=…`) works too, but is slower.

Files the portal can't place are listed under **Not sorted**, with a
Move button.

**Claude works event by event.** From the session's entries export the portal knows the events and heats. When
an event's lane sheet pack and every heat's Gen7 race are in, it asks `gala-claude` (a question on the
**Questions** tab) to transcribe the pack, audit that event and update the session report, and Claude writes the
event's merge results (`Event NN merge results.hy3`, to import into Meet Manager) and its Quick Fix list,
which shows under the event on the Checklist tab to tick off swim by swim. **Regenerate** (in the event's
Merge column) rewrites the event's merge results and quick fixes from what is already transcribed, with
every DQ slip read since; no scans are read again, and answers with what the
officials should look at. DQ slip batches and the ETD programme are handed over as they arrive.
At the end of the session, once every event is audited and Meet Manager's merge results export is in, Claude
writes the session's merge results `.hy3`; when the final export arrives, it runs the final audit. Untick
**Ask Claude automatically** to do it by hand with the **Ask Claude** buttons instead.

**Gala tab:** the inbox drop zone, the sessions, each session's progress (events audited, with Claude, ready,
waiting), OCR progress per scan, its folders, and the activity log.

**Checklist tab:** per session, in order: the entries export; the printed lane sheets (Claude copies the
names, ages, teams and seed times from them rather than reading them off the scans); a row per event (printed ✓, lane sheets, Gen7 heats in and
missing, OCR lanes, audit report, status, Ask Claude); then DQ slips, ETD, DQ codes, the merge results export,
our merge results, the final export and the final audit. Tick items off once you've checked them; ticks are
shared. Drop a file on a row to add it.

Watch Claude work on the **Live** tab; long jobs show a progress bar under the tabs.

## The data folder

```
data/inbox/                  new files arrive here (or --inbox DIR)
data/sessions/S1/meet/       Meet Manager exports and prints
data/sessions/S1/gen7/       Gen7 race PDFs
data/sessions/S1/scans/      lane sheet packs, DQ slips, ETD
data/sessions/S1/ocr/        Claude's transcriptions, dq-codes.json
data/sessions/S1/reports/    Event NN audit.html, Session N audit.html, merge results .hy3, final audit
data/unsorted/               files the portal couldn't place
data/state.json              ticks, what Claude was asked, the activity log
```

For the next gala, move the contents of `data/` to an archive folder (or use a copy of this folder).

**Privacy.** The files hold children's names, ages and times. They stay in `data/` on this computer.
`.gitignore` keeps `data/`, `.portal/` and the key out of git.

## Files

| File | |
|---|---|
| `start-portal.sh`, `stop-portal.sh` | Start and stop everything |
| `CLAUDE.md` | Claude's instructions: its role, the folders, the skills, how to use the portal |
| `portal/portal.py` | The portal (one Python file, standard library only; built from the QA Board) |
| `portal/board-watch.py` | The watcher |
| `portal/brand/` | The CGA look: `brand.css` (colours, header) and the logo. Edit it to change the look |
| `portal/claude-loop.sh`, `resume-prompt.txt`, `tmux.conf` | Claude's tmux session and restarts |
| `.portal/` | Made at start: key, database, logs |

Optional: install `ttyd` and the portal gets a web terminal (on port 9001) for typing to Claude from the
browser.
