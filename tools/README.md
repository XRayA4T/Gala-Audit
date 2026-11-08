# Building portal/portal.py

`portal/portal.py` is the Claude QA Board's `qaboard.py` with the gala pieces added. To rebuild it after changing
anything in `src/`:

```bash
python3 tools/patch_gala.py path/to/Claude-QA-Board/server/qaboard.py portal/portal.py
```

| File | |
|---|---|
| `src/backend.py` | The `Gala` class: the inbox sorter, sessions, events, what Claude is asked and when |
| `src/handler.py` | The HTTP routes under `/api/gala/` |
| `src/section.html`, `src/gala.css`, `src/gala.js` | The Gala and Checklist tabs |
| `patch_gala.py` | Puts them into `qaboard.py` (and renames, rebrands and adds the command-line options) |
