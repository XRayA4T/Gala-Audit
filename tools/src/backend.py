

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
GEN7_RX = re.compile(r"Event:\s*(\d+)\s+Heat:\s*(\d+)\s+Race\s*#\s*(\d+)")
GEN7_SESSION = re.compile(r"Session:\s*(\d+)")
OCR_LANE = re.compile(r"-event0*(\d+)-lane(\d+)-p\d+\.json$", re.I)
OCR_SLIP = re.compile(r"-event0*(\d+)-heat\d+-lane\d+-p\d+\.json$", re.I)
OCR_PAGE = re.compile(r"-p(\d{3,4})\.json$", re.I)
MEET_EXT = (".zip", ".hy3", ".cl2", ".ev3", ".hyv")
CLAUDE_WHO = "gala-claude"


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
        self.seen = {}          # inbox file -> (size, mtime) at the last look, to wait until it is fully written
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
                return {"events": [], "sheets": 0}
            events, sheets = set(), 0
            for page in txt.split("\f"):
                ev = re.search(r"(?m)^\s*Event (\d+) ", page)
                if ev and re.search(r"(?m)^\s*Lane \d+\s+Heat \d+", page):
                    events.add(int(ev.group(1)))
                    sheets += 1
            return {"events": sorted(events), "sheets": sheets}
        return self._cached("lanesprint", path, run) or {"events": [], "sheets": 0}

    def gen7(self, path):
        """The Gen7 races in a PDF: {session, races: [{race, event, heat, desc}]} (empty races: not a Gen7 PDF)."""
        def run(p):
            try:
                txt = subprocess.run(["pdftotext", p, "-"], capture_output=True, text=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                txt = ""
            lines = [ln.strip() for ln in txt.splitlines()]
            races = []
            for i, ln in enumerate(lines):
                m = GEN7_RX.search(ln)
                if m:
                    desc = next((lines[j] for j in range(i - 1, max(i - 4, -1), -1) if lines[j]), "")
                    races.append({"event": int(m.group(1)), "heat": int(m.group(2)), "race": int(m.group(3)),
                                  "desc": "" if GEN7_SESSION.search(desc) else desc[:80]})
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
        return self.hy3(os.path.join(self.sdir(n, "meet"), files[-1]["name"])) if files else {}

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
            for n in reversed(have):
                if event in self.entries(n) or event in {r["event"] for r in self.gen7_races(n)}:
                    return n
        return max(have) if have else 1

    def sort_inbox(self):
        """Move every finished file in the inbox to its session. A file is finished once its size and time haven't
        changed for one look and it is a few seconds old."""
        now = time.time()
        found = {}
        for dirpath, dirs, files in os.walk(self.inbox):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in files:
                if SKIP_NAME.search(n):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                found[p] = (st.st_size, st.st_mtime)
        ready = [p for p, sig in found.items() if self.seen.get(p) == sig and now - sig[1] > 3 and sig[0] > 0]
        self.seen = found
        for p in sorted(ready, key=lambda p: found[p][1]):
            try:
                self.file_one(p)
            except Exception as e:  # never let one bad file stop the rest
                sys.stderr.write("inbox: %s: %s\n" % (p, e))
                try:
                    dest = os.path.join(self.root, "unsorted", _unique(os.path.join(self.root, "unsorted"),
                                                                       os.path.basename(p)))
                    shutil.move(p, dest)
                    self.log("Couldn't sort %s (%s): moved to unsorted" % (os.path.basename(p), e),
                             name=os.path.basename(p), to="unsorted")
                except OSError:
                    pass

    def file_one(self, path, force_session=None, force_folder=None):
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
        shutil.move(path, dest)
        self.log("%s → %s%s" % (name, label, note), name=name, to=label, kind=kind)
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
        lanes_done = {}
        for o in ocr:
            m = OCR_LANE.search(o)
            if m:
                lanes_done.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
        gen7_by_event, desc = {}, {}
        for r in races:
            gen7_by_event.setdefault(r["event"], {}).setdefault(r["heat"], r)
            if r["desc"]:
                desc.setdefault(r["event"], r["desc"])
        printed, prints = {}, []
        for f in meet["lanesprint"]:
            info = self.lane_print(os.path.join(self.sdir(n, "meet"), f["name"]))
            prints.append({"name": f["name"], "events": info["events"], "sheets": info["sheets"]})
            for e in info["events"]:
                printed.setdefault(e, []).append(f["name"])
        packs = {}
        for s in scans:
            if s["kind"] == "lanes":
                for e in ([s["event"]] if s["event"] is not None else s["events"]):
                    packs.setdefault(e, []).append(s)
        asked = self.state["asked"]
        events = []
        for e in sorted(set(expected) | set(gen7_by_event) | set(packs) | set(lanes_done)):
            x = expected.get(e, {})
            heats = x.get("heats") or max(gen7_by_event.get(e, {0: 0}))
            have = sorted(gen7_by_event.get(e, {}))
            pk = packs.get(e, [])
            report = next((f for f in reports if f["name"].lower().endswith(".html")
                           and re.search(r"(?i)\bevent\s*0*%d\b" % e, f["name"])), None)
            inputs = [(p["name"], p["mtime"]) for p in pk] + [(r["file"], r["mtime"]) for r in races if r["event"] == e]
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
                           "expected": bool(x), "lanes": len(x.get("lanes", [])) or None,
                           "packs": [p["name"] for p in pk], "printed": printed.get(e, []), "ocrLanes": len(lanes_done.get(e, ())),
                           "report": report["name"] if report else None, "ready": ready, "sig": sig,
                           "status": status, "asked": a})
        merged = [f["name"] for f in reports if f["name"].lower().endswith(".hy3")]
        finals = [f["name"] for f in reports if f["name"].lower().endswith(".html") and "final" in f["name"].lower()]
        sess_report = next((f["name"] for f in reports if f["name"].lower().endswith(".html")
                            and re.search(r"(?i)session", f["name"]) and "final" not in f["name"].lower()), None)
        return {"n": n, "meet": {k: [f["name"] for f in v] for k, v in meet.items()}, "lanePrints": prints,
                "expected": {"events": len(expected), "heats": sum(v["heats"] for v in expected.values())},
                "races": len(races), "scans": scans, "events": events, "merged": merged, "finalReports": finals,
                "sessionReport": sess_report, "dqCodes": "dq-codes.json" in ocr,
                "folders": {k: _ls(self.sdir(n, k)) if k != "ocr" else
                            sorted(_ls(self.sdir(n, k)), key=lambda f: -f["mtime"])[:12] for k in SESSION_KEYS},
                "ocrCount": len(ocr)}

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

    def question(self, key, n):
        """(title, body) for a piece of work, or None if it doesn't apply (yet)."""
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
                        % ", ".join("`%s`" % self.P(n, "meet", f) for f in ev["printed"]))
            else:
                orig = ("There are no printed lane sheets for this event yet, so read the printed fields from the scan "
                        "(and say so in your answer).")
            missing = ("\n\nGen7 heats not in yet: %s. Audit what is there and say which heats are missing."
                       % ", ".join(map(str, ev["gen7Missing"]))) if ev["gen7Missing"] else ""
            body = (("Event %d is in: lane sheets %s and Gen7 races for heats %s (of %s) in `%s`.%s\n\n"
                     "1. **Transcribe** the lane sheets with the **gala-ocr** skill into `%s/` (skip pages already done%s). %s\n"
                     "2. **Audit the event** with the **gala-audit** skill:\n"
                     "   `audit_report.py %s %s \"%s/Event %02d audit.html\" --events %d --scans %s`\n"
                     "3. **Update the session report** (every event transcribed so far):\n   %s\n"
                     "4. **Answer** with what the officials should look at now (pads, times in doubt, DQs, unsigned "
                     "sheets, anything unreadable), with links to both reports.")
                    % (e, packs, ", ".join(map(str, ev["gen7"])) or "none", ev["heats"] or "?", gen7, missing,
                       ocr, "; a rescanned pack replaces the old one and its old OCR has been set aside, so read all its pages"
                       if redo else "", orig,
                       ocr, gen7, rep, e, e, scans, session_audit))
            return title, body
        if kind == "scan":
            name = key.split(":", 2)[2]
            sc = next((x for x in s["scans"] if x["name"] == name), None)
            if not sc:
                return None
            if sc["kind"] == "dq":
                return ("Session %d: read the DQ slips in %s" % (n, name),
                        "New DQ slips: `%s`.\n\n1. Read them with the **gala-ocr** skill into `%s/` (skip slips already done).\n"
                        "2. Give each slip its DQ code in `%s/dq-codes.json` (**gala-audit** skill); ask the referee here "
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
            return ("Session %d: write the merge results for Meet Manager" % n,
                    "Meet Manager's merge results export for session %d is in: `%s` (entries: `%s`).\n\n"
                    "1. Check every event of the session has been transcribed and audited and every DQ slip has a code "
                    "(`%s/dq-codes.json`); list anything missing.\n"
                    "2. Write the merge results with the **gala-audit** skill (unzip the entries' Hfile*.hy3 into a temp "
                    "folder if it's a zip):\n   `merge_results.py %s %s <entries>.hy3 %s \"%s/Merge Meet Results-S%d-audit.hy3\"`\n"
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
                self.auto_ask()
            except Exception as e:
                sys.stderr.write("gala loop: %r\n" % (e,))
            time.sleep(every)

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
