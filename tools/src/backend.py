

# ============================================================== Gala: a gala audited while it runs
# Files arrive during the gala in an inbox folder (the scanner, the Gen7 computer, or the browser save there).
# The portal sorts each one into its session: data/sessions/S<n>/{meet,gen7,scans,ocr,reports}. From each session's
# Meet Manager entries export it knows the events and heats to expect; when an event's lane sheet pack and all its
# heats' Gen7 race PDFs are in, it asks gala-claude (a question on the board) to transcribe and audit that event.
SESSION_DIRS = [
    ("meet", "Meet Manager files", "Entries, merge results and final exports (.zip / .hy3), Meet Manager prints"),
    ("gen7", "Gen7 race PDFs", "One PDF per race, as Gen7 prints them"),
    ("scans", "Scans", "Lane sheet packs (one per event), DQ slips, the ETD programme"),
    ("ocr", "OCR (Claude)", "One JSON per lane sheet, DQ slip or ETD page, and dq-codes.json"),
    ("reports", "Reports (Claude)", "Event and session audit reports, merge results .hy3, final audit"),
]
SESSION_KEYS = [d[0] for d in SESSION_DIRS]
SKIP_NAME = re.compile(r"^[.~]|\.(part|partial|tmp|crdownload|download|lock)$|^(thumbs\.db|desktop\.ini)$", re.I)
SESSION_RX = re.compile(r"(?i)(?:\bsession|\bsess|\bses)\s*[-_#.]?\s*(\d{1,2})(?!\d)|(?<![a-z0-9])s(\d{1,2})(?![0-9])")
EVENT_RX = re.compile(r"(?i)(?:\bevent|\bevt|\bev|(?<![a-z0-9])e)\s*[-_#.]?\s*(\d{1,3})(?!\d)")
GEN7_RX = re.compile(r"Event:\s*(\d+)\s+Heat:\s*(\d+)\s+Race\s*#\s*(\d+)")   # \s also matches line breaks
GEN7_SESSION = re.compile(r"Session:\s*(\d+)")
OCR_LANE = re.compile(r"-event0*(\d+)-lane(\d+)-p\d+\.json$", re.I)
OCR_SLIP = re.compile(r"-event0*(\d+)-heat\d+-lane\d+-p\d+\.json$", re.I)
OCR_PAGE = re.compile(r"-p(\d{3,4})\.json$", re.I)
MEET_EXT = (".zip", ".hy3", ".cl2", ".ev3", ".hyv")
CLAUDE_WHO = "gala-claude"
# Merge results files carry the date and time they were written in their name; the questions tell Claude so.
STAMP = "<YYYY-MM-DD_HHMM>"
STAMP_NOTE = ("Put the date and time you write it in the file name in place of `%s` (e.g. `2026-11-08_1532`, from "
              "`date +%%Y-%%m-%%d_%%H%%M`), and use that same file name below." % STAMP)
SETTLE = 6   # seconds a new file's size and time must stay the same before it is taken (it may still be copying)


def _first(m):
    return int(next(g for g in m.groups() if g)) if m else None


def _ls(folder):
    out = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return out
    for n in names:
        if n.startswith("."):
            continue
        p = os.path.join(folder, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": n, "dir": os.path.isdir(p), "size": st.st_size, "mtime": st.st_mtime})
    return out


def _unique(folder, name):
    base, ext = os.path.splitext(name)
    cand, i = name, 2
    while os.path.exists(os.path.join(folder, cand)):
        cand = "%s (%d)%s" % (base, i, ext)
        i += 1
    return cand


def _read_fixes(path):
    """The Quick Fix list audit_report.py --quick-fixes writes as JSON: [{event, heat, lane, inFile, checks, scans}]."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for x in d if isinstance(d, list) else []:
        if isinstance(x, dict):
            out.append({"heat": x.get("heat"), "lane": x.get("lane"), "inFile": str(x.get("inFile") or ""),
                        "checks": [str(c) for c in x.get("checks") or []][:12],
                        "scans": [{"label": str(sc.get("label") or ""), "scan": str(sc.get("scan") or ""),
                                   "page": sc.get("page")} for sc in x.get("scans") or [] if isinstance(sc, dict)][:4],
                        "times": x.get("times") if isinstance(x.get("times"), dict) else None})
    return out


def _read_times(path):
    """audit_report.py --times: [{event, heat, lane, tp, pb, sw, median, final, ...}] -> {"heat.lane": times}."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return {"%s.%s" % (x.get("heat"), x.get("lane")): x for x in d if isinstance(x, dict)} if isinstance(d, list) else {}


def _same_file(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(1 << 20), fb.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


class Gala:
    def __init__(self, root, inbox, store):
        self.root = os.path.abspath(root)
        self.inbox = os.path.abspath(inbox or os.path.join(self.root, "inbox"))
        self.store = store
        self.lock = threading.RLock()
        self.cache = {}
        # inbox file -> ((size, mtime), when this computer first saw it like that), to wait until it is fully
        # written. Timed by this computer's clock, not the file's mtime, which on a Windows share comes from
        # another computer's clock and may be ahead or behind.
        self.seen = {}
        for d in (self.root, self.inbox, os.path.join(self.root, "sessions"), os.path.join(self.root, "unsorted")):
            os.makedirs(d, exist_ok=True)
        self.state_file = os.path.join(self.root, "state.json")
        try:
            with open(self.state_file, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("auto", True)
        self.state.setdefault("log", [])
        self.state.setdefault("asked", {})
        self.state.setdefault("ticks", {})
        self.rechecks = {}

    # ---------------------------------------------------------------- state
    def save(self):
        with self.lock:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.state_file)

    def log(self, text, **kw):
        with self.lock:
            self.state["log"] = (self.state["log"] + [dict(kw, ts=time.time(), text=text)])[-300:]
            self.save()

    # ---------------------------------------------------------------- reading files (cached by size and mtime)
    def _cached(self, kind, path, fn):
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (kind, path)
        hit = self.cache.get(key)
        if hit and hit[0] == (st.st_mtime, st.st_size):
            return hit[1]
        val = fn(path)
        self.cache[key] = ((st.st_mtime, st.st_size), val)
        return val

    def pdf_pages(self, path):
        def run(p):
            try:
                out = subprocess.run(["pdfinfo", p], capture_output=True, text=True, timeout=30).stdout
                m = re.search(r"^Pages:\s+(\d+)", out, re.M)
                return int(m.group(1)) if m else None
            except (OSError, subprocess.SubprocessError):
                return None
        return self._cached("pages", path, run)

    def pdf_text(self, path):
        def run(p):
            try:
                return subprocess.run(["pdftotext", "-l", "6", p, "-"], capture_output=True, text=True,
                                      timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                return ""
        return self._cached("text", path, run) or ""

    def lane_print(self, path):
        """Meet Manager's printed (not scanned) lane sheets: {events: [...], sheets: N}; no events if it isn't one."""
        def run(p):
            try:
                txt = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, text=True,
                                     timeout=120).stdout
            except (OSError, subprocess.SubprocessError):
                return {"events": [], "sheets": 0, "heats": {}, "lanes": {}}
            sheets, heats, lanes = 0, {}, {}
            for page in txt.split("\f"):
                ev = re.search(r"(?m)^\s*Event (\d+) ", page)
                rows = re.findall(r"(?m)^\s*Lane (\d+)\s+Heat (\d+)", page)
                if ev and rows:
                    e = int(ev.group(1))
                    sheets += 1
                    heats[e] = max([heats.get(e, 0)] + [int(h) for _, h in rows])
                    lanes.setdefault(e, set()).update(int(ln) for ln, _ in rows)
            return {"events": sorted(heats), "sheets": sheets, "heats": heats,
                    "lanes": {e: sorted(v) for e, v in lanes.items()}}
        return self._cached("lanesprint", path, run) or {"events": [], "sheets": 0, "heats": {}, "lanes": {}}

    def all_prints(self):
        """Every printed lane sheet PDF in any session: [(session, name, info)]. A print can cover several sessions."""
        out = []
        for n in self.sessions():
            for f in self.meet_files(n)["lanesprint"]:
                out.append((n, f["name"], self.lane_print(os.path.join(self.sdir(n, "meet"), f["name"]))))
        return out

    @staticmethod
    def by_hundreds(events):
        """True when events are numbered by session in hundreds (101-1xx session 1, 201-2xx session 2, ...)."""
        return len({e // 100 for e in events if e >= 100}) >= 2 and all(e >= 100 for e in events)

    def gen7(self, path):
        """The Gen7 races in a PDF: {session, races: [{race, event, heat, desc}]} (empty races: not a Gen7 PDF)."""
        def run(p):
            try:
                txt = subprocess.run(["pdftotext", p, "-"], capture_output=True, text=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                txt = ""
            races = []
            # pdftotext may put "Event: 7", "Heat: 2" and "Race # 44" on lines of their own, so search the whole
            # text; the event title is the last line before "Event:".
            for m in GEN7_RX.finditer(txt):
                before = [ln.strip() for ln in txt[:m.start()].splitlines() if ln.strip()]
                desc = before[-1] if before else ""
                races.append({"event": int(m.group(1)), "heat": int(m.group(2)), "race": int(m.group(3)),
                              "desc": "" if GEN7_SESSION.search(desc) or "Event:" in desc else desc[:80]})
            s = GEN7_SESSION.search(txt)
            return {"session": int(s.group(1)) if s else None, "races": races}
        return self._cached("gen7", path, run) or {"session": None, "races": []}

    def hy3(self, path):
        """Events in a Meet Manager .hy3 (or the .hy3 in a .zip): {event: {heats, lanes, entries}}."""
        def run(p):
            data = b""
            try:
                if p.lower().endswith(".zip"):
                    import zipfile
                    with zipfile.ZipFile(p) as z:
                        names = [n for n in z.namelist() if n.lower().endswith(".hy3")]
                        names.sort(key=lambda n: (not os.path.basename(n).lower().startswith("hfile"), n))
                        if names:
                            data = z.read(names[0])
                else:
                    with open(p, "rb") as f:
                        data = f.read()
            except Exception:
                return {}
            ev, cur = {}, None
            for ln in data.decode("latin-1").replace("\r\n", "\n").split("\n"):
                if ln.startswith("E1") and len(ln) >= 42 and ln[38:42].strip().isdigit():
                    cur = int(ln[38:42])
                    e = ev.setdefault(cur, {"heats": 0, "lanes": set(), "entries": 0})
                    e["entries"] += 1
                elif ln.startswith("E2") and cur is not None and len(ln) >= 26:
                    h, l = ln[20:23].strip(), ln[23:26].strip()
                    if h.isdigit() and int(h) > 0:
                        ev[cur]["heats"] = max(ev[cur]["heats"], int(h))
                    if l.isdigit() and int(l) > 0:
                        ev[cur]["lanes"].add(int(l))
            return {k: {"heats": v["heats"], "lanes": sorted(v["lanes"]), "entries": v["entries"]} for k, v in ev.items()}
        return self._cached("hy3", path, run) or {}

    def ocr_info(self, path):
        def run(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                return {"bad": True}
            if not isinstance(d, dict):
                return {"bad": True}
            txt = json.dumps(d)
            return {"back": d.get("backPage") if isinstance(d.get("backPage"), int) else None,
                    "review": bool(re.search(r'"(needsReview|unsure|uncertain)"\s*:\s*(true|\[[^\]])', txt))}
        return self._cached("ocr", path, run) or {}

    # ---------------------------------------------------------------- sessions
    def sessions(self):
        out = []
        for f in _ls(os.path.join(self.root, "sessions")):
            m = re.fullmatch(r"S(\d{1,2})", f["name"])
            if f["dir"] and m:
                out.append(int(m.group(1)))
        return sorted(out)

    def sdir(self, n, sub=None):
        d = os.path.join(self.root, "sessions", "S%d" % n)
        for k in SESSION_KEYS:
            os.makedirs(os.path.join(d, k), exist_ok=True)
        return os.path.join(d, sub) if sub else d

    def meet_files(self, n):
        """The session's Meet Manager files by kind: entries, results, final, codes, print."""
        out = {"entries": [], "results": [], "final": [], "codes": [], "lanesprint": [], "print": []}
        for f in _ls(self.sdir(n, "meet")):
            if f["dir"]:
                continue
            out[self.meet_kind(os.path.join(self.sdir(n, "meet"), f["name"]))].append(f)
        for v in out.values():
            v.sort(key=lambda f: f["mtime"])
        return out

    def meet_kind(self, path):
        name = os.path.basename(path).lower()
        ext = os.path.splitext(name)[1]
        if ext in (".txt", ".csv"):
            return "codes"
        if ext == ".pdf":
            return "lanesprint" if self.lane_print(path)["events"] else "print"
        if "final" in name:
            return "final"
        if "result" in name:
            return "results"
        if "entr" in name or name.startswith("hfile"):
            return "entries"
        if ext == ".zip":
            try:
                import zipfile
                with zipfile.ZipFile(path) as z:
                    inner = " ".join(os.path.basename(n).lower() for n in z.namelist())
                if "result" in inner:
                    return "results"
            except Exception:
                pass
        return "entries"

    def entries(self, n):
        """Events expected in the session (from its newest entries export): {event: {heats, lanes, entries}}."""
        files = self.meet_files(n)["entries"]
        own = self.hy3(os.path.join(self.sdir(n, "meet"), files[-1]["name"])) if files else {}
        base = own
        if not base:   # an entries export for the whole meet, put in another session
            for m in self.sessions():
                f = self.meet_files(m)["entries"] if m != n else []
                if f:
                    base = self.hy3(os.path.join(self.sdir(m, "meet"), f[-1]["name"]))
                    break
        if base and self.by_hundreds(base):
            mine = {e: v for e, v in base.items() if e // 100 == n}
            return mine if mine or not own else own
        return own

    def scan_kind(self, name):
        low = name.lower()
        if re.search(r"dq|disq", low):
            return "dq"
        if re.search(r"etd|programme|program", low):
            return "etd"
        return "lanes"

    # ---------------------------------------------------------------- sorting the inbox
    def classify(self, path):
        """(kind, folder, session or None, event or None) for a new file."""
        name = os.path.basename(path)
        low = name.lower()
        ext = os.path.splitext(low)[1]
        sess = _first(SESSION_RX.search(name))
        event = _first(EVENT_RX.search(name))
        if ext in MEET_EXT or ext in (".txt", ".csv"):
            return self.meet_kind(path), "meet", sess, None
        if ext == ".pdf":
            g = self.gen7(path)
            if g["races"]:
                return "gen7", "gen7", g["session"] or sess, g["races"][0]["event"]
            txt = self.pdf_text(path)
            if len(re.sub(r"\s+", "", txt)) > 300:      # a generated PDF (e.g. a Meet Manager print), not a scan
                return self.meet_kind(path), "meet", sess, event
            return self.scan_kind(name), "scans", sess, event
        return "unknown", None, sess, event

    def pick_session(self, kind, path, sess, event):
        have = self.sessions()
        if sess:
            return sess
        if kind == "entries":
            evs = set(self.hy3(path))
            best = max(have, key=lambda n: len(evs & {r["event"] for r in self.gen7_races(n)}), default=None)
            if best and evs & {r["event"] for r in self.gen7_races(best)}:
                return best
            free = [n for n in have if not self.meet_files(n)["entries"]]
            return free[0] if free else (max(have) + 1 if have else 1)
        if kind in ("results", "final"):
            evs = set(self.hy3(path))
            known = lambda n: set(self.entries(n)) | {r["event"] for r in self.gen7_races(n)}  # noqa: E731
            best = max(have, key=lambda n: len(evs & known(n)), default=None)
            if best and evs & known(best):
                return best
            lacking = [n for n in have if self.meet_files(n)["entries"] and not self.meet_files(n)[kind]]
            return lacking[0] if lacking else (max(have) if have else 1)
        if kind == "lanesprint":
            evs = set(self.lane_print(path)["events"])
            known = lambda n: set(self.entries(n)) | {r["event"] for r in self.gen7_races(n)}  # noqa: E731
            best = max(have, key=lambda n: len(evs & known(n)), default=None)
            if best and evs & known(best):
                return best
            return max(have) if have else 1
        if event is not None:
            known = set()
            for n in have:
                f = self.meet_files(n)["entries"]
                if f:
                    known |= set(self.hy3(os.path.join(self.sdir(n, "meet"), f[-1]["name"])))
            if event >= 100 and known and self.by_hundreds(known):
                return event // 100
            for n in reversed(have):
                if event in self.entries(n) or event in {r["event"] for r in self.gen7_races(n)}:
                    return n
        return max(have) if have else 1

    def sort_inbox(self):
        """Move every finished file in the inbox to its session. A file is finished once its size and time haven't
        changed for one look and it is a few seconds old.
        A folder in the inbox that is a symbolic link (e.g. to the folder the Gen7 computer saves into) is watched
        too, but its files are copied, not moved, so that folder is left as it is; each file is copied once (again
        only if it changes)."""
        now = time.time()
        found, linked = {}, set()
        inbox_real = os.path.realpath(self.inbox)
        visited = set()
        for dirpath, dirs, files in os.walk(self.inbox, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in visited:          # a link loop
                dirs[:] = []
                continue
            visited.add(real)
            outside = not (real == inbox_real or real.startswith(inbox_real + os.sep))
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in files:
                if SKIP_NAME.search(n):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if outside or os.path.islink(p):
                    done = self.state.setdefault("copied", {}).get(os.path.realpath(p))
                    if done == [st.st_size, st.st_mtime]:
                        continue
                    linked.add(p)
                found[p] = (st.st_size, st.st_mtime)
        seen = {p: (sig, self.seen[p][1] if p in self.seen and self.seen[p][0] == sig else now)
                for p, sig in found.items()}
        ready = [p for p, (sig, since) in seen.items() if sig[0] > 0 and now - since >= SETTLE]
        self.seen = seen
        for p in sorted(ready, key=lambda p: found[p][1]):
            try:
                if p in linked:
                    self.file_one(p, copy=True)
                    with self.lock:
                        self.state["copied"][os.path.realpath(p)] = list(found[p])
                        self.save()
                    continue
                self.file_one(p)
            except Exception as e:  # never let one bad file stop the rest
                sys.stderr.write("inbox: %s: %s\n" % (p, e))
                try:
                    dest = os.path.join(self.root, "unsorted", _unique(os.path.join(self.root, "unsorted"),
                                                                       os.path.basename(p)))
                    if p in linked:
                        shutil.copy2(p, dest)
                        with self.lock:
                            self.state["copied"][os.path.realpath(p)] = list(found[p])
                    else:
                        shutil.move(p, dest)
                    self.log("Couldn't sort %s (%s): put in unsorted" % (os.path.basename(p), e),
                             name=os.path.basename(p), to="unsorted")
                except OSError:
                    pass

    def file_one(self, path, force_session=None, force_folder=None, copy=False):
        name = os.path.basename(path)
        kind, folder, sess, event = self.classify(path)
        if force_folder:
            folder = force_folder
        if not folder:
            dest_dir = os.path.join(self.root, "unsorted")
            label = "unsorted (unknown type)"
        else:
            n = force_session or self.pick_session(kind, path, sess, event)
            dest_dir = self.sdir(n, folder)
            label = "S%d/%s" % (n, folder)
        dest = os.path.join(dest_dir, name)
        note = ""
        if os.path.exists(dest):
            if _same_file(path, dest):
                if not copy:
                    os.unlink(path)
                self.log("%s was already in %s" % (name, label), name=name, to=label, kind=kind)
                return label
            # A file with the same name is a rescan or a reprint: it replaces the old one, which is kept in
            # .replaced/. For a scan, the OCR of the old scan is set aside too, so its pages are read again.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            old = os.path.join(dest_dir, ".replaced")
            os.makedirs(old, exist_ok=True)
            shutil.move(dest, os.path.join(old, "%s %s" % (stamp, name)))
            note = " (replaces the earlier copy, kept in .replaced"
            if folder == "scans" and name.lower().endswith(".pdf"):
                ocr = os.path.dirname(dest_dir) + os.sep + "ocr"
                stem = name[:-4] + "-"
                moved = 0
                for f in _ls(ocr):
                    if f["name"].startswith(stem) and f["name"].lower().endswith(".json"):
                        os.makedirs(os.path.join(ocr, ".replaced", stamp), exist_ok=True)
                        shutil.move(os.path.join(ocr, f["name"]), os.path.join(ocr, ".replaced", stamp, f["name"]))
                        moved += 1
                if moved:
                    note += "; its %d OCR files were set aside to be read again" % moved
            note += ")"
        if copy:
            shutil.copy2(path, dest)
        else:
            shutil.move(path, dest)
        self.log("%s → %s%s" % (name, label, note + (" (copied)" if copy else "")), name=name, to=label, kind=kind)
        return label

    def move(self, rel, session, folder):
        """Move a file from unsorted/ (or anywhere under sessions/) to S<session>/<folder>."""
        src = gala_path(self.root, rel)
        if not src or folder not in SESSION_KEYS:
            raise ValueError("no such file, or unknown folder")
        dest_dir = self.sdir(int(session), folder)
        dest = os.path.join(dest_dir, _unique(dest_dir, os.path.basename(src)))
        shutil.move(src, dest)
        self.log("%s → S%d/%s (by hand)" % (os.path.basename(src), int(session), folder),
                 name=os.path.basename(dest), to="S%d/%s" % (int(session), folder))

    # ---------------------------------------------------------------- what each session has
    def gen7_races(self, n):
        out = []
        d = self.sdir(n, "gen7")
        for f in _ls(d):
            if not f["dir"] and f["name"].lower().endswith(".pdf"):
                for r in self.gen7(os.path.join(d, f["name"]))["races"]:
                    out.append(dict(r, file=f["name"], mtime=f["mtime"]))
        return out

    def scans(self, n):
        d, ocr = self.sdir(n, "scans"), self.sdir(n, "ocr")
        ocr_files = [f["name"] for f in _ls(ocr) if f["name"].lower().endswith(".json")]
        out = []
        for f in _ls(d):
            if f["dir"] or not f["name"].lower().endswith(".pdf"):
                continue
            stem = f["name"][:-4]
            pages = self.pdf_pages(os.path.join(d, f["name"]))
            done, review, bad, files, events = set(), 0, 0, 0, set()
            for o in ocr_files:
                if not o.startswith(stem + "-"):
                    continue
                m = OCR_PAGE.search(o)
                if not m:
                    continue
                files += 1
                done.add(int(m.group(1)))
                me = OCR_LANE.search(o) or OCR_SLIP.search(o)
                if me:
                    events.add(int(me.group(1)))
                info = self.ocr_info(os.path.join(ocr, o))
                bad += 1 if info.get("bad") else 0
                review += 1 if info.get("review") else 0
                if info.get("back"):
                    done.add(info["back"])
            ev = _first(EVENT_RX.search(stem))
            missing = [p for p in range(1, (pages or 0) + 1) if p not in done]
            out.append({"name": f["name"], "kind": self.scan_kind(f["name"]), "event": ev,
                        "events": sorted(events), "pages": pages, "mtime": f["mtime"], "size": f["size"],
                        "done": len([p for p in done if not pages or p <= pages]), "files": files, "review": review,
                        "bad": bad, "missing": missing[:40], "missingCount": len(missing)})
        return out

    def session(self, n):
        meet = self.meet_files(n)
        expected = self.entries(n)
        races = self.gen7_races(n)
        scans = self.scans(n)
        ocr = [f["name"] for f in _ls(self.sdir(n, "ocr"))]
        reports = [f for f in _ls(self.sdir(n, "reports")) if not f["dir"]]
        lanes_done, slips_done = {}, {}
        for o in ocr:
            m = OCR_LANE.search(o)
            if m:
                lanes_done.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
            m = OCR_SLIP.search(o)
            if m:
                slips_done.setdefault(int(m.group(1)), []).append(o)
        gen7_by_event, desc = {}, {}
        for r in races:
            gen7_by_event.setdefault(r["event"], {}).setdefault(r["heat"], r)
            if r["desc"]:
                desc.setdefault(r["event"], r["desc"])
        hand = self.fixes(n)
        printed, prints, print_heats, print_lanes = {}, [], {}, {}
        mine = set(expected) | {r["event"] for r in races}
        for m, name, info in self.all_prints():
            evs = [e for e in info["events"] if e in mine] if mine else info["events"]
            if m != n and not evs:
                continue
            path = "sessions/S%d/meet/%s" % (m, name)
            prints.append({"name": name, "path": path, "events": evs or info["events"], "sheets": info["sheets"]})
            for e in info["events"]:
                printed.setdefault(e, []).append(path)
                print_heats[e] = max(print_heats.get(e, 0), info["heats"].get(e, 0))
                print_lanes.setdefault(e, set()).update(info["lanes"].get(e, []))
        packs = {}
        for s in scans:
            if s["kind"] == "lanes":
                for e in ([s["event"]] if s["event"] is not None else s["events"]):
                    packs.setdefault(e, []).append(s)
        asked = self.state["asked"]
        events = []
        for e in sorted(set(expected) | set(gen7_by_event) | set(packs) | set(lanes_done)):
            x = expected.get(e, {})
            heats = x.get("heats") or print_heats.get(e) or max(gen7_by_event.get(e, {0: 0}))
            have = sorted(gen7_by_event.get(e, {}))
            pk = packs.get(e, [])
            report = next((f for f in reports if f["name"].lower().endswith(".html")
                           and re.search(r"(?i)\bevent\s*0*%d\b" % e, f["name"])), None)
            inputs = [(p["name"], p["mtime"]) for p in pk] + [(r["file"], r["mtime"]) for r in races if r["event"] == e]
            # The merge results need the session's entries export and Meet Manager's empty merge results export;
            # when they arrive after the event was done, the event is asked again (they are part of its inputs).
            seeded = (x.get("heats") or 0) > 0
            merge_inputs = []
            if meet["entries"]:   # a pre-gala export (not seeded) works too: the plugin seeds from the lane sheets
                merge_inputs.append((meet["entries"][-1]["name"], meet["entries"][-1]["mtime"]))
            if meet["results"]:
                merge_inputs.append((meet["results"][-1]["name"], meet["results"][-1]["mtime"]))
            can_merge = len(merge_inputs) == 2
            if can_merge:
                inputs = inputs + merge_inputs
            ev_rx = re.compile(r"(?i)\bevent\s*0*%d\b" % e)
            merge_f = max((f for f in reports if f["name"].lower().endswith(".hy3") and ev_rx.search(f["name"])),
                          key=lambda f: f["mtime"], default=None)   # the newest: each one is named with its time
            merge_file = merge_f["name"] if merge_f else None
            qf_file = next((f for f in reports if f["name"].lower().endswith(".json") and "quick" in f["name"].lower()
                            and ev_rx.search(f["name"])), None)
            fixes = None
            if qf_file:
                fixes = self._cached("qf", os.path.join(self.sdir(n, "reports"), qf_file["name"]), _read_fixes)
                tf = os.path.join(self.sdir(n, "reports"), "Event %02d times.json" % e)
                tmap = self._cached("times", tf, _read_times) if os.path.exists(tf) else None
                fixes = [dict(f, row=self.sheet_row(n, e, f["heat"], f["lane"], ocr),
                              times=(tmap or {}).get("%s.%s" % (f["heat"], f["lane"])) or f.get("times"))
                         for f in self.qf_history(n, e, fixes or [])]
                # Times for every swim (TP, PB...) come from a check run by the portal: run one if there are none yet.
                if tmap is None and fixes and (n, e) not in self.rechecks and self.plugin_script("audit_report.py") \
                        and lanes_done.get(e):
                    try:
                        self.recheck(n, e, "portal")
                    except ValueError:
                        pass
            sig = hashlib.sha1(json.dumps(sorted(inputs)).encode()).hexdigest()[:12]
            newest = max([t for _, t in inputs] or [0])
            ready = bool(pk) and heats > 0 and all(h in have for h in range(1, heats + 1))
            a = asked.get("S%d:e%d" % (n, e))
            if report and report["mtime"] >= newest:
                status = "audited"
            elif report:
                status = "changed"
            elif a:
                status = "asked"
            elif ready:
                status = "ready"
            else:
                status = "waiting"
            events.append({"event": e, "desc": desc.get(e, ""), "heats": heats, "gen7": have,
                           "gen7Missing": [h for h in range(1, heats + 1) if h not in have],
                           "expected": bool(x), "lanes": len(x.get("lanes", [])) or len(print_lanes.get(e, ())) or None,
                           "packs": [p["name"] for p in pk], "printed": printed.get(e, []), "ocrLanes": len(lanes_done.get(e, ())),
                           "report": report["name"] if report else None, "ready": ready, "sig": sig,
                           "status": status, "asked": a, "merge": merge_file, "mergeAt": merge_f["mtime"] if merge_f else None,
                           "canMerge": can_merge,
                           "seeded": seeded, "fixes": fixes, "recheck": self.rechecks.get((n, e)), "dqSlips": len(slips_done.get(e, [])),
                           "fixesFile": qf_file["name"][:-5] + ".html" if qf_file else None,
                           "handFixes": {k.split("-", 1)[1].replace("-", "."): v for k, v in hand.items()
                                         if k.startswith("%d-" % e)}})
        merged = [f["name"] for f in sorted(reports, key=lambda f: f["mtime"])
                  if f["name"].lower().endswith(".hy3") and not re.search(r"(?i)\bevent\b", f["name"])]
        finals = [f["name"] for f in reports if f["name"].lower().endswith(".html") and "final" in f["name"].lower()]
        sess_report = next((f["name"] for f in reports if f["name"].lower().endswith(".html")
                            and re.search(r"(?i)session", f["name"]) and "final" not in f["name"].lower()), None)
        return {"n": n, "meet": {k: [f["name"] for f in v] for k, v in meet.items()}, "lanePrints": prints,
                "expected": {"events": len(expected), "heats": sum(e["heats"] for e in events if e["expected"])},
                "races": len(races), "scans": scans, "events": events, "merged": merged, "finalReports": finals,
                "sessionReport": sess_report, "dqCodes": "dq-codes.json" in ocr,
                "folders": {k: _ls(self.sdir(n, k)) if k != "ocr" else
                            sorted(_ls(self.sdir(n, k)), key=lambda f: -f["mtime"])[:12] for k in SESSION_KEYS},
                "ocrCount": len(ocr), "handFixCount": len(hand)}

    def overview(self):
        with self.lock:
            inbox_in_root = os.path.realpath(self.inbox).startswith(os.path.realpath(self.root) + os.sep)
            return {"root": self.root, "inbox": self.inbox, "inboxRel": os.path.relpath(self.inbox, self.root)
                    if inbox_in_root else None, "auto": self.state["auto"],
                    "log": self.state["log"][-40:][::-1],
                    "waiting": [os.path.relpath(p, self.inbox) for p in self.seen],
                    "unsorted": _ls(os.path.join(self.root, "unsorted")),
                    "sessions": [self.session(n) for n in self.sessions()],
                    "ticks": self.state["ticks"],
                    "tools": {t: bool(shutil.which(t)) for t in ("pdfinfo", "pdftoppm", "pdftotext")}}

    # ---------------------------------------------------------------- asking Claude
    def P(self, n, sub, name=""):
        return "data/sessions/S%d/%s%s" % (n, sub, ("/" + name) if name else "")

    FINA_CODES = "plugins/gala-ocr/skills/gala-audit/data/dqcodesFINA.txt"

    def question(self, key, n):
        """(title, body) for a piece of work, or None if it doesn't apply (yet). Every audit_report, merge_results
        and final_audit command gets --codes when the session has the meet's own DQ code list; otherwise the
        scripts use the full FINA list in the plugin."""
        q = self._question(key, n)
        if not q:
            return q
        q = (q[0], q[1].replace("{STAMPNOTE}", STAMP_NOTE))
        q = (q[0], re.sub(r"(`audit_report\.py [^`]*--events (\d+) [^`]*)`",
                          lambda m: '%s --times "data/sessions/S%d/reports/Event %02d times.json"`'
                          % (m.group(1), n, int(m.group(2))), q[1]))
        if os.path.exists(self.fixes_file(n)) and self.fixes(n):
            fx = "data/sessions/S%d/fixes.json" % n
            body = re.sub(r"(`(?:merge_results|audit_report)\.py [^`]*)`", lambda m: '%s --fixes "%s"`' % (m.group(1), fx), q[1])
            q = (q[0], body + ("\n\nFixes made in the portal: `%s` (%d swims). It is passed to merge_results.py and "
                               "audit_report.py as `--fixes` (plugin 1.6.3+): result fixes, watches accepted or captured "
                               "by hand, swimmers chosen for written-in names. Don't edit it." % (fx, len(self.fixes(n)))))
        own = self.meet_files(n)["codes"]
        if own:
            path = self.P(n, "meet", own[-1]["name"])
            body = re.sub(r"(`(?:audit_report|merge_results|final_audit)\.py [^`]*)`", lambda m: '%s --codes "%s"`' % (m.group(1), path), q[1])
            note = ("\n\nDQ codes: use the meet's own list `%s` (passed to the scripts as `--codes`)." % path)
        else:
            body = q[1]
            note = ("\n\nDQ codes: the full FINA list is in the plugin, `%s` (the gala-audit skill's `data/` folder; "
                    "the scripts use it by default)." % self.FINA_CODES)
        if re.search(r"(?i)\bDQ\b", body):
            body += note
        return q[0], body

    def _question(self, key, n):
        s = self.session(n)
        ocr, gen7, scans, rep = self.P(n, "ocr"), self.P(n, "gen7"), self.P(n, "scans"), self.P(n, "reports")
        done_events = ",".join(str(e["event"]) for e in s["events"] if e["ocrLanes"] or e["packs"])
        session_audit = ('`audit_report.py %s %s "%s/Session %d audit.html" --events %s --scans %s`'
                         % (ocr, gen7, rep, n, done_events or "<events>", scans))
        kind = key.split(":")[1]
        if kind.startswith("e") and kind[1:].isdigit():
            e = int(kind[1:])
            ev = next((x for x in s["events"] if x["event"] == e), None)
            if not ev:
                return None
            packs = ", ".join("`%s`" % self.P(n, "scans", p) for p in ev["packs"]) or "(no lane sheet pack yet)"
            redo = ev["status"] in ("changed", "audited") or (ev["asked"] and ev["asked"].get("sig") != ev["sig"])
            title = "Session %d · Event %d%s: %s" % (n, e, (" " + ev["desc"]) if ev["desc"] else "",
                                                     "new files, audit it again" if redo else "transcribe and audit it")
            if ev["printed"]:
                orig = ("Take the printed fields (names, ages, teams, seed times) from Meet Manager's printed lane sheets "
                        "%s with the skill's `original_sheets.py` (pass the PDF and the event)."
                        % ", ".join("`data/%s`" % f for f in ev["printed"]))
            else:
                orig = ("There are no printed lane sheets for this event yet, so read the printed fields from the scan "
                        "(and say so in your answer).")
            missing = ("\n\nGen7 heats not in yet: %s. Audit what is there and say which heats are missing."
                       % ", ".join(map(str, ev["gen7Missing"]))) if ev["gen7Missing"] else ""
            mfile = "%s/Event %02d merge results %s.hy3" % (rep, e, STAMP)
            qfile = "%s/Event %02d quick fixes.html" % (rep, e)
            if ev["canMerge"]:
                ent = self.P(n, "meet", s["meet"]["entries"][-1])
                tpl = self.P(n, "meet", s["meet"]["results"][-1])
                merge_step = ("2. **Write the event's merge results** for Meet Manager with the **gala-audit** skill "
                              "(unzip the `Hfile*.hy3` from `%s` into a temp folder if it's a zip). {STAMPNOTE}\n"
                              "   `merge_results.py %s %s <entries>.hy3 \"%s\" \"%s\" --events %d`\n"
                              % (ent, ocr, gen7, tpl, mfile, e))
                merge_opt = ' --merge "%s"' % mfile
            else:
                why = []
                if not s["meet"]["entries"]:
                    why.append("the session's entries export isn't in")
                if not s["meet"]["results"]:
                    why.append("Meet Manager's empty merge results export isn't in")
                merge_step = ("2. **Merge results: not yet**, because %s. Skip this step and say so in your answer; "
                              "you'll be asked again when they arrive.\n" % " and ".join(why))
                merge_opt = ""
            body = (("Event %d is in: lane sheets %s and Gen7 races for heats %s (of %s) in `%s`.%s\n\n"
                     "1. **Transcribe** the lane sheets with the **gala-ocr** skill into `%s/` (skip pages already done%s). %s\n"
                     "%s"
                     "3. **Audit the event** with the **gala-audit** skill, with its Quick Fix list (the portal turns the "
                     "`.json` it writes next to the `.html` into the event's quick-fix checklist):\n"
                     "   `audit_report.py %s %s \"%s/Event %02d audit.html\" --events %d --scans %s%s --quick-fixes \"%s\"`\n"
                     "4. **Update the session report** (every event transcribed so far):\n   %s\n"
                     "5. **Answer** with what the officials should look at now (pads, times in doubt, DQs, unsigned "
                     "sheets, anything unreadable), how many quick fixes there are, and links to the reports and the "
                     "merge results file.")
                    % (e, packs, ", ".join(map(str, ev["gen7"])) or "none", ev["heats"] or "?", gen7, missing,
                       ocr, "; a rescanned pack replaced the old one and its old OCR was set aside, so read its pages again"
                       if redo and any(x["name"] in ev["packs"] and x["pages"] and x["done"] < x["pages"] for x in s["scans"])
                       else "; if they are all done, go straight to the next step", orig, merge_step,
                       ocr, gen7, rep, e, e, scans, merge_opt, qfile, session_audit))
            return title, body
        if kind == "scan":
            name = key.split(":", 2)[2]
            sc = next((x for x in s["scans"] if x["name"] == name), None)
            if not sc:
                return None
            if sc["kind"] == "dq":
                return ("Session %d: read the DQ slips in %s" % (n, name),
                        "New DQ slips: `%s`.\n\n1. Read them with the **gala-ocr** skill into `%s/` (skip slips already done).\n"
                        "2. Give each slip its DQ code in `%s/dq-codes.json` (**gala-audit** skill, from the DQ code list below); ask the referee here "
                        "about any code you're unsure of.\n3. Update the session report:\n   %s\n"
                        "4. Answer with the DQs, their codes and anything that doesn't match the lane sheets or Gen7."
                        % (self.P(n, "scans", name), ocr, ocr, session_audit))
            if sc["kind"] == "etd":
                return ("Session %d: read the ETD programme %s" % (n, name),
                        "The ETD programme is in: `%s`.\n\n1. Read the race numbers with the **gala-ocr** skill into `%s/`.\n"
                        "2. Check them against the Gen7 races (`check_etd.py %s %s`).\n3. Update the session report:\n   %s\n"
                        "4. Answer with combined events, re-started heats and race numbers that don't match."
                        % (self.P(n, "scans", name), ocr, ocr, gen7, session_audit))
            return None
        if kind == "merge":
            if not s["meet"]["results"] or not s["meet"]["entries"]:
                return None
            ent, res = self.P(n, "meet", s["meet"]["entries"][-1]), self.P(n, "meet", s["meet"]["results"][-1])
            return ("Session %d: write the whole session's merge results for Meet Manager" % n,
                    "Every event of session %d is audited. Write one merge results file for the whole session from "
                    "Meet Manager's empty merge results export `%s` and the entries `%s`.\n\n"
                    "1. Check every event of the session has been transcribed and audited and every DQ slip has a code "
                    "(`%s/dq-codes.json`); list anything missing.\n"
                    "2. Write the merge results with the **gala-audit** skill (unzip the entries' Hfile*.hy3 into a temp "
                    "folder if it's a zip). {STAMPNOTE}\n   `merge_results.py %s %s <entries>.hy3 %s \"%s/Merge Meet Results-S%d-audit <YYYY-MM-DD_HHMM>.hy3\"`\n"
                    "3. Run the session audit again with `--merge` and `--quick-fixes \"%s/Session %d quick fixes.html\"`.\n"
                    "4. Answer with what was written, the Quick Fix list and anything you were unsure of."
                    % (n, res, ent, ocr, ocr, gen7, res, rep, n, rep, n))
        if kind == "final":
            if not s["meet"]["final"] or not s["merged"]:
                return None
            fin = self.P(n, "meet", s["meet"]["final"][-1])
            ours = self.P(n, "reports", s["merged"][-1])
            return ("Session %d: final audit" % n,
                    "Meet Manager's final merge results for session %d are in: `%s`.\n\nRun the **gala-audit** final audit:\n"
                    "   `final_audit.py %s %s \"%s/Session %d final audit.html\" --quick-fixes \"%s/Session %d quick fixes.json\"`\n"
                    "and answer with every manual change and the Quick Fix swims left as they were." % (n, fin, ours, fin, rep, n, rep, n))
        if kind == "remerge":
            e = int(key.split(":")[2])
            ev = next((x for x in s["events"] if x["event"] == e), None)
            if not ev or not ev["canMerge"] or not ev["ocrLanes"]:
                return None
            ent = self.P(n, "meet", s["meet"]["entries"][-1])
            tpl = self.P(n, "meet", s["meet"]["results"][-1])
            mfile = "%s/Event %02d merge results %s.hy3" % (rep, e, STAMP)
            unread = [x["name"] for x in s["scans"] if x["kind"] == "dq" and x["pages"] and x["done"] < x["pages"]]
            codes = "%s/dq-codes.json" % ocr
            return ("Session %d · Event %d: regenerate the merge results" % (n, e),
                    ("Regenerate event %d's merge results from what is **already transcribed**. **Don't read any scans "
                     "again**: no gala-ocr, no rendering pages. The lane sheets and DQ slips are only read when their "
                     "scans arrive.\n\n"
                     "Event %d has %d DQ slip%s transcribed in `%s/`.%s\n\n"
                     "1. **DQ codes:** every DQ slip of event %d in `%s/` must have a code in `%s`. Give any slip "
                     "without one its code from the transcribed infraction and the DQ code list below. "
                     "Don't change codes already given.\n"
                     "2. **Write the merge results** with the **gala-audit** skill (unzip the `Hfile*.hy3` from `%s` "
                     "into a temp folder if it's a zip), as a new file (the older ones stay). {STAMPNOTE}\n"
                     "   `merge_results.py %s %s <entries>.hy3 \"%s\" \"%s\" --events %d`\n"
                     "3. **Refresh the audit and quick fixes:**\n"
                     "   `audit_report.py %s %s \"%s/Event %02d audit.html\" --events %d --scans %s --merge \"%s\" "
                     "--quick-fixes \"%s/Event %02d quick fixes.html\"`\n"
                     "4. **Answer** with what changed in the merge file (DQs added, times changed) and the quick fixes "
                     "left.")
                    % (e, e, ev["dqSlips"], "" if ev["dqSlips"] == 1 else "s", ocr,
                       (" DQ scans not fully transcribed yet (they'll be read on their own, not here; say so): %s."
                        % ", ".join("`%s`" % u for u in unread)) if unread else "",
                       e, ocr, codes, ent, ocr, gen7, tpl, mfile, e,
                       ocr, gen7, rep, e, e, scans, mfile, rep, e))
        if kind == "check":
            return ("Session %d: check the files" % n,
                    "Look through `data/sessions/S%d/` and the portal's Checklist for session %d: which events and "
                    "heats are expected (the entries export), which lane sheet packs and Gen7 races are in, what is "
                    "transcribed and audited, and what is missing. Don't start transcribing; just report." % (n, n))
        return None

    def ask(self, key, by=None, force=False):
        """Post the question for a piece of work to gala-claude (once per key and input files, unless forced)."""
        n = int(key.split(":")[0][1:])
        q = self.question(key, n)
        if not q:
            return None
        with self.lock:
            sig = None
            if re.fullmatch(r"S\d+:e\d+", key):
                ev = next((x for x in self.session(n)["events"] if "S%d:e%d" % (n, x["event"]) == key), None)
                sig = ev and ev["sig"]
            prev = self.state["asked"].get(key)
            if prev and not force and prev.get("sig") == sig:
                return None
            row = self.store.post(q[0], q[1], by or "portal", CLAUDE_WHO)
            self.state["asked"][key] = {"ts": time.time(), "qid": row["id"], "sig": sig, "by": by or "portal"}
            self.save()
            self.log("Asked Claude: %s (question %d)" % (q[0], row["id"]), qid=row["id"])
            return row

    def refile(self):
        """Move Gen7 race PDFs that were filed as Meet Manager files (portal 0.3.3 and earlier didn't recognise
        Gen7's own layout) to the session's gen7 folder."""
        for n in self.sessions():
            d = self.sdir(n, "meet")
            for f in _ls(d):
                p = os.path.join(d, f["name"])
                if not f["dir"] and f["name"].lower().endswith(".pdf") and self.gen7(p)["races"]:
                    g = self.sdir(n, "gen7")
                    dest = os.path.join(g, f["name"])
                    if os.path.exists(dest):
                        if _same_file(p, dest):
                            os.unlink(p)
                            continue
                        dest = os.path.join(g, _unique(g, f["name"]))
                    shutil.move(p, dest)
                    self.log("%s → S%d/gen7 (it is a Gen7 race, not a Meet Manager file)" % (f["name"], n),
                             name=f["name"], to="S%d/gen7" % n, kind="gen7")

    def auto_ask(self):
        if not self.state["auto"]:
            return
        for n in self.sessions():
            s = self.session(n)
            for ev in s["events"]:
                key = "S%d:e%d" % (n, ev["event"])
                prev = self.state["asked"].get(key)
                if ev["ready"] and (not prev or prev.get("sig") != ev["sig"]):
                    self.ask(key)
            for sc in s["scans"]:
                if sc["kind"] in ("dq", "etd"):
                    key = "S%d:scan:%s" % (n, sc["name"])
                    if key not in self.state["asked"]:
                        self.ask(key)
            all_audited = s["events"] and all(e["status"] == "audited" for e in s["events"])
            if s["meet"]["results"] and s["meet"]["entries"] and all_audited:   # the end of the session
                key = "S%d:merge" % n
                prev = self.state["asked"].get(key)
                if not prev or prev.get("file") != s["meet"]["results"][-1]:
                    if self.ask(key, force=True):
                        self.state["asked"][key]["file"] = s["meet"]["results"][-1]
                        self.save()
            if s["meet"]["final"] and s["merged"]:
                key = "S%d:final" % n
                prev = self.state["asked"].get(key)
                if not prev or prev.get("file") != s["meet"]["final"][-1]:
                    if self.ask(key, force=True):
                        self.state["asked"][key]["file"] = s["meet"]["final"][-1]
                        self.save()

    def run(self, every=4):
        while True:
            try:
                self.sort_inbox()
                if not getattr(self, "_refiled", False):
                    self.refile()
                    self._refiled = True
                self.auto_ask()
            except Exception as e:
                sys.stderr.write("gala loop: %r\n" % (e,))
            time.sleep(every)

    # ---------------------------------------------------------------- quick fixes done in the portal
    def sheet_row(self, n, e, h, ln, ocr_names):
        """The transcribed lane sheet row of a swim, for the quick-fix forms: name, notes, the watch readings."""
        rx = re.compile(r"-event0*%d-lane%d-p\d+\.json$" % (int(e), int(ln)), re.I)
        name = next((o for o in ocr_names if rx.search(o) and not OCR_SLIP.search(o)), None)
        if not name or not h:
            return None

        def run(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return None
        d = self._cached("sheet", os.path.join(self.sdir(n, "ocr"), name), run)
        rows = (d or {}).get("rows") or []
        if int(h) > len(rows):
            return None
        r = rows[int(h) - 1]
        notes = str(r.get("notes") or "")
        return {"name": r.get("swimmerName") or "", "notes": notes, "status": r.get("status") or "",
                "writtenIn": bool(re.search(r"\bname\b.*crossed out.*\bwritten\b|\bname written in\b", notes)),
                "watches": [{"column": t.get("column"), "written": t.get("written") or "", "normalized":
                             t.get("normalized") or "", "confidence": t.get("confidence") or "",
                             "corrected": bool(t.get("corrected"))} for t in r.get("handTimes") or []]}

    def swimmers(self, n):
        """Every swimmer in the session's entries export (or the meet-wide one): id, name, age, team, events."""
        files = self.meet_files(n)["entries"]
        path = os.path.join(self.sdir(n, "meet"), files[-1]["name"]) if files else None
        if not path:
            for m in self.sessions():
                f = self.meet_files(m)["entries"]
                if f:
                    path = os.path.join(self.sdir(m, "meet"), f[-1]["name"])
                    break
        if not path:
            return []

        def run(p):
            data = b""
            try:
                if p.lower().endswith(".zip"):
                    import zipfile
                    with zipfile.ZipFile(p) as z:
                        names = sorted((x for x in z.namelist() if x.lower().endswith(".hy3")),
                                       key=lambda x: (not os.path.basename(x).lower().startswith("hfile"), x))
                        data = z.read(names[0]) if names else b""
                else:
                    with open(p, "rb") as f:
                        data = f.read()
            except Exception:
                return []
            out, team = {}, ""
            for ln in data.decode("latin-1").replace("\r\n", "\n").split("\n"):
                tag = ln[:2]
                if tag == "C1":
                    team = ln[2:7].strip()
                elif tag == "D1" and len(ln) > 48:
                    sid = ln[2:8].strip()
                    age = ln[96:99].strip() if len(ln) > 99 else ""
                    out.setdefault(sid, {"id": sid, "surname": ln[8:28].strip(), "first": ln[28:48].strip(),
                                         "preferred": ln[48:68].strip(),
                                               "sex": ln[2:3], "age": age, "team": team, "events": []})
                elif tag == "E1" and len(ln) > 42 and ln[38:42].strip().isdigit():
                    s = out.get(ln[2:8].strip())
                    if s is not None:
                        s["events"].append(int(ln[38:42]))
            return list(out.values())
        return self._cached("swimmers", path, run) or []

    def find_swimmers(self, n, q, event=None, limit=6):
        import difflib
        norm = lambda x: re.sub(r"[^a-z ]", "", x.lower().replace(",", " ")).split()  # noqa: E731
        words = norm(q or "")
        out = []
        for s in self.swimmers(n):
            full = norm(s["surname"] + " " + s["first"] + " " + s.get("preferred", ""))
            if words:
                q2 = " ".join(words)
                score = max(difflib.SequenceMatcher(None, q2, " ".join(norm(s["surname"] + " " + x))).ratio()
                            for x in (s["first"], s.get("preferred") or s["first"]))
                score += 0.25 * sum(1 for w in words if any(f.startswith(w) or w.startswith(f) for f in full)) / len(words)
            else:
                score = 0
            entered = bool(event) and int(event) in s["events"]
            name_score = score
            score += 0.3 if entered else 0
            if (words and name_score >= 0.5) or (not words and entered):
                out.append((score, dict(s, entered=entered, name="%s, %s" % (s["surname"], s["first"]))))
        out.sort(key=lambda x: -x[0])
        return [dict(x, score=round(sc, 2)) for sc, x in out[:limit]]

    def qf_history(self, n, e, current):
        """Every quick fix the event has ever had, so none is lost when a re-check or regenerate settles it: the
        current ones, and the ones no longer listed ("cleared": True, with when). Kept in state.json."""
        hist = self.state.setdefault("qf", {}).setdefault("S%d:e%d" % (n, e), {})
        before = json.dumps(hist, sort_keys=True)
        now = time.time()
        # Quick fixes ticked before this history was kept: known only by their tick (heat and lane).
        for tk, tv in self.state["ticks"].items():
            m = re.fullmatch(r"S%d:e%d:qf:(\d+)\.(\d+)" % (n, e), tk)
            if m and "%s.%s" % m.groups() not in hist:
                hist["%s.%s" % m.groups()] = {"event": e, "heat": int(m.group(1)), "lane": int(m.group(2)),
                                              "inFile": "", "checks": ["(what it said wasn't kept: it was ticked before "
                                                                       "the portal kept a history of quick fixes)"],
                                              "scans": [], "times": None, "cleared": True,
                                              "clearedAt": tv.get("at"), "seen": tv.get("at")}
        keys = set()
        for f in current:
            k = "%s.%s" % (f["heat"], f["lane"])
            keys.add(k)
            old = hist.get(k) or {}
            hist[k] = dict(f, cleared=False, seen=old.get("seen") or now)
        for k, f in hist.items():
            if k not in keys and not f.get("cleared"):
                f["cleared"], f["clearedAt"] = True, now
        if json.dumps(hist, sort_keys=True) != before:
            self.save()
        return sorted(hist.values(), key=lambda f: (int(f["lane"]), int(f["heat"])))   # by lane, then heat

    def fixes_file(self, n):
        return os.path.join(self.sdir(n), "fixes.json")

    def fixes(self, n):
        """Fixes made in the portal: {"E-H-L": {time, status, code, note, by, at}}, passed to merge_results --fixes."""
        try:
            with open(self.fixes_file(n), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_fix(self, n, e, h, ln, fix, who):
        if n not in self.sessions():
            raise ValueError("no such session")
        status = str(fix.get("status") or "").upper()
        t = str(fix.get("time") or "").strip()
        code = str(fix.get("code") or "").strip()[:6]
        note = str(fix.get("note") or "").strip()[:300]
        if status not in ("", "OK", "NS", "DQ"):
            raise ValueError("result must be OK, NS or DQ")
        if t and not re.fullmatch(r"(?:\d{1,2}:)?\d{1,2}\.\d{2}", t):
            raise ValueError("time must look like 1:05.32 or 32.10")
        if status == "DQ" and not code:
            raise ValueError("a DQ needs its code")
        watches = {}
        for col, w in (fix.get("watches") or {}).items():
            if not str(col).isdigit() or not isinstance(w, dict):
                continue
            wt = str(w.get("time") or "").strip()
            if wt:
                if not re.fullmatch(r"(?:\d{1,2}:)?\d{1,2}\.\d{2}", wt):
                    raise ValueError("timer %s: the time must look like 1:05.32 or 32.10" % col)
                watches[str(col)] = {"time": wt}
            elif w.get("accepted"):
                watches[str(col)] = {"accepted": True}
        sw = fix.get("swimmer") if isinstance(fix.get("swimmer"), dict) else None
        swimmer = ({"id": str(sw.get("id") or "")[:12], "name": str(sw.get("name") or "")[:80]}
                   if sw and sw.get("name") else None)
        add = bool(fix.get("add")) and bool(swimmer)
        key = "%d-%d-%d" % (int(e), int(h), int(ln))
        with self.lock:
            d = self.fixes(n)
            if not (status or t or note or watches or swimmer):
                d.pop(key, None)
                text = "cleared"
            else:
                d[key] = {"status": status, "time": t, "code": code if status == "DQ" else "", "note": note,
                          "by": str(who or "someone")[:60], "at": time.time()}
                if watches:
                    d[key]["watches"] = watches
                if swimmer:
                    d[key]["swimmer"] = swimmer
                if add:
                    d[key]["add"] = True   # put the swim in the merge file for this swimmer (plugin 1.6.4+)
                text = ", ".join(x for x in (status and {"OK": "swam", "NS": "no-show", "DQ": "DQ " + code}[status],
                                             t, ", ".join("timer %s %s" % (c, w.get("time") or "accepted")
                                                          for c, w in watches.items()),
                                             swimmer and "swimmer " + swimmer["name"], note and "note") if x)
            tmp = self.fixes_file(n) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1, sort_keys=True)
            os.replace(tmp, self.fixes_file(n))
            # A fix doesn't tick the quick fix: a person ticks it once it is checked (and done in Meet Manager).
        self.log("Session %d event %d heat %d lane %d fixed in the portal: %s" % (n, int(e), int(h), int(ln), text))

    # ---------------------------------------------------------------- re-running an event's check in the portal
    @staticmethod
    def plugin_script(name):
        """The newest installed copy of one of the gala-audit skill's scripts, or None."""
        import glob
        found = glob.glob(os.path.expanduser("~/.claude/plugins/**/gala-audit/scripts/%s" % name), recursive=True)
        return max(found, key=os.path.getmtime) if found else None

    def recheck(self, n, e, who=None):
        """Re-run the event's audit from what is already transcribed, with the fixes made in the portal, and write
        its Quick Fix list again (no scans are read; the event's audit report is rewritten on its next regenerate).
        Runs in the background; its state is in self.rechecks[(n, e)]."""
        script = self.plugin_script("audit_report.py")
        if not script:
            raise ValueError("the gala-ocr plugin isn't installed for this user (no gala-audit/scripts/audit_report.py)")
        if n not in self.sessions():
            raise ValueError("no such session")
        key = (n, int(e))
        if self.rechecks.get(key, {}).get("state") == "running":
            return
        prev = self.rechecks.get(key)
        self.rechecks[key] = {"state": "running", "at": time.time(), "by": who}   # before session(): it may ask again
        s = self.session(n)
        ev = next((x for x in s["events"] if x["event"] == int(e)), None)
        if not ev or not ev["ocrLanes"]:
            if prev:
                self.rechecks[key] = prev
            else:
                self.rechecks.pop(key, None)
            raise ValueError("event %s has no transcribed lane sheets" % e)
        threading.Thread(target=self._recheck, args=(n, int(e), ev, script, who), daemon=True).start()

    def _recheck(self, n, e, ev, script, who):
        key = (n, e)
        rep = self.sdir(n, "reports")
        qf = os.path.join(rep, ev["fixesFile"]) if ev.get("fixesFile") else os.path.join(rep, "Event %02d quick fixes.html" % e)
        tmp = tempfile.mkdtemp(prefix="gala-recheck-")
        cmd = [sys.executable, "-I", script, self.sdir(n, "ocr"), self.sdir(n, "gen7"), os.path.join(tmp, "audit.html"),
               "--events", str(e), "--scans", tmp, "--quick-fixes", qf,
               "--times", os.path.join(rep, "Event %02d times.json" % e)]
        if ev.get("merge"):
            cmd += ["--merge", os.path.join(rep, ev["merge"])]
        if os.path.exists(self.fixes_file(n)):
            cmd += ["--fixes", self.fixes_file(n)]
        own = self.meet_files(n)["codes"]
        if own:
            cmd += ["--codes", os.path.join(self.sdir(n, "meet"), own[-1]["name"])]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            ok = r.returncode == 0
            out = (r.stdout + r.stderr).strip().splitlines()
            m = re.search(r"(\d+) swims? in Quick Fix", "\n".join(out))
            msg = ("%s still flagged" % m.group(1)) if m else (out[-1] if out else "")
        except (OSError, subprocess.SubprocessError) as ex:
            ok, msg = False, str(ex)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.rechecks[key] = {"state": "done" if ok else "failed", "at": time.time(), "by": who, "msg": msg[-300:]}
        self.log("Session %d event %d re-checked%s: %s" % (n, e, " by " + who if who else "", msg[-200:] if msg else
                                                          ("done" if ok else "failed")))

    def code_list(self, n):
        """The DQ codes to choose from: the meet's own list in meet/ if there is one, else the plugin's FINA list."""
        own = self.meet_files(n)["codes"]
        if own:
            path = os.path.join(self.sdir(n, "meet"), own[-1]["name"])
        else:
            import glob
            found = glob.glob(os.path.expanduser("~/.claude/plugins/**/gala-audit/data/dqcodesFINA.txt"), recursive=True)
            path = max(found, key=os.path.getmtime) if found else None
        if not path:
            return {"source": None, "codes": []}

        def run(p):
            import csv
            out = []
            try:
                with open(p, encoding="utf-8", errors="replace", newline="") as f:
                    for row in csv.reader(f):
                        if len(row) >= 2 and row[0].strip():
                            out.append([row[0].strip(), row[1].strip(), row[2].strip() if len(row) > 2 else ""])
            except OSError:
                pass
            return out
        return {"source": os.path.basename(path), "codes": self._cached("codes", path, run) or []}

    def tick(self, key, checked, who):
        if not re.fullmatch(r"S\d{1,2}:[\w.:=() \-]{1,200}", key or ""):
            raise ValueError("bad checklist key")
        with self.lock:
            if checked:
                self.state["ticks"][key] = {"by": str(who or "someone")[:60], "at": time.time()}
            else:
                self.state["ticks"].pop(key, None)
            self.save()

    def set_auto(self, on):
        with self.lock:
            self.state["auto"] = bool(on)
            self.save()
            self.log("Asking Claude automatically: %s" % ("on" if on else "off"))


def gala_path(root, rel):
    """The real path of rel inside root, or None if it escapes the data folder or doesn't exist."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or any(part in ("", ".", "..") for part in rel.split("/")) or "\x00" in rel:
        return None
    p = os.path.realpath(os.path.join(root, rel))
    r = os.path.realpath(root)
    if not (p == r or p.startswith(r + os.sep)) or not os.path.isfile(p):
        return None
    return p
