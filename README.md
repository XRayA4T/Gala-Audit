# CGA Gala Audit Portal

A local web page for auditing a swimming gala with Claude Code. You drop the gala's files on the page,
click what you want done, and a Claude Code session on this computer does it with the
[gala-ocr plugin](https://github.com/XRayA4T/Gala-AI-Plugins/tree/main/plugins/gala-ocr), answering on the
page as it goes. It is built from the Claude QA Board, so it also has questions and answers, documents, a live
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

Options: `--port 9000`, `--name "Gala name"`, `--no-browser`, `--no-attach`.
Leave the terminal with **Ctrl-b d**; Claude keeps running. Back in: `tmux -L gala-portal attach -t claude`.

```bash
bash stop-portal.sh              # stops the page, the watcher and Claude (data/ and the questions are kept)
```

## Use

The **Gala** tab:

- **Folders.** Drop files on a folder card (or *choose files*). Click a file to open it; ⤓ downloads it.
- **OCR progress.** Each scan's pages against the JSON files in `data/ocr`, the pages not done yet, and the
  files the OCR marked for checking.
- **Steps.** Each button opens the Ask form with a ready request for `gala-claude`, in the usual order:
  check the files → lane sheets → DQ slips → ETD → Gen7 report → DQ codes → audit report → merged results
  `.hy3` → final audit. Edit it if you like and **Post**.

Claude answers on the **Questions** tab and asks you there when it can't read something. Long jobs show a
progress bar under the tabs. Watch it work on the **Live** tab.

## The data folder

| Folder | What goes in it |
|---|---|
| `data/pdf/scanned/` | The scans: `Lane Sheets.pdf`, `DQs.pdf`, `ETD.pdf` |
| `data/pdf/original/` | The Meet Manager print and the Colorado Gen7 session report (`*Session*.pdf`) |
| `data/meet/` | Meet Manager entries and results `.hy3` (or the `.zip`) |
| `data/ocr/` | Written by Claude: one JSON per lane sheet, DQ slip or ETD page, plus `dq-codes.json` |
| `data/reports/` | Written by Claude: the audit report (HTML), merged `.hy3`, CSV |

Uploading a file that's already there asks first and keeps the old one as a hidden `.NAME.previous`.
For the next gala, move the contents of `data/` to an archive folder (or use a copy of this folder).

**Privacy.** The files hold children's names, ages and times. They stay in `data/` on this computer.
`.gitignore` keeps `data/`, `.portal/` and the key out of git.

## Files

| File | |
|---|---|
| `start-portal.sh`, `stop-portal.sh` | Start and stop everything |
| `CLAUDE.md` | Claude's instructions: its role, the folders, the skills, how to use the portal |
| `portal/portal.py` | The portal (one Python file, standard library only) |
| `portal/board-watch.py` | The watcher |
| `portal/claude-loop.sh`, `resume-prompt.txt`, `tmux.conf` | Claude's tmux session and restarts |
| `.portal/` | Made at start: key, database, logs |

Optional: install `ttyd` and the portal gets a web terminal (on port 9001) for typing to Claude from the
browser.
