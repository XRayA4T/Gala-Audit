#!/usr/bin/env python3
"""
Gala Audit Portal - the QA Board with a Gala tab (data folder, OCR progress, reports) for a local
Claude Code that runs the gala-ocr plugin.  Based on the QA Board:

QA Board - a tiny shared question & answer board and document store for a small team and their Claudes.

One file, Python 3.8+ standard library only (no pip installs).

  Run the board (on one machine on the LAN):
      python3 qaboard.py serve --port 8787 [--db qaboard.db] [--key SECRET] [--max-doc-mb 20]

  Things it serves:
      http://HOST:8787/                 web page for humans (questions + documents)
      http://HOST:8787/mcp?who=NAME     MCP endpoint for Claude Code (HTTP transport)
      http://HOST:8787/api/digest?to=N  plain-text "what's waiting for N" (for hooks)
      http://HOST:8787/api/docs         document store (POST raw bytes to upload, see below)

  Upload a file from a shell (e.g. Claude Code running curl):
      curl --data-binary @report.pdf "http://HOST:8787/api/docs?filename=report.pdf&author=dev-claude&key=KEY"

  Documents are stored next to the database in docs/<id>/; every upload of a new version is kept.

  Connect Claude Desktop (stdio -> HTTP bridge, no Node needed):
      python3 qaboard.py bridge "http://HOST:8787/mcp?who=NAME"
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
STATUSES = ("open", "answered", "resolved", "on_hold")

TEXT_MIMES = {
    "application/json", "application/xml", "application/javascript", "application/x-javascript",
    "application/x-yaml", "application/yaml", "application/x-sh", "application/sql",
    "application/x-python", "application/toml", "image/svg+xml",
}
EXTRA_TYPES = {".md": "text/markdown", ".yaml": "application/yaml", ".yml": "application/yaml",
               ".toml": "application/toml", ".ts": "text/plain", ".tsx": "text/plain",
               ".jsx": "text/plain", ".sql": "application/sql", ".log": "text/plain",
               ".env": "text/plain", ".csv": "text/csv", ".py": "text/x-python"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
INLINE_SAFE = IMAGE_MIMES | {"image/svg+xml", "application/pdf"}   # served inline (under a sandbox CSP: an SVG
#                                                                  can't run scripts); text/* is served as text/plain


# Built-in icon: a speech bubble with a "Q" (original artwork). To use your own, put favicon.svg,
# favicon.png or favicon.ico in the board's data folder (next to qaboard.db); no restart needed.
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="{ACCENT}"/>
<path d="M14 18a6 6 0 0 1 6-6h24a6 6 0 0 1 6 6v18a6 6 0 0 1-6 6H30l-10 9v-9a6 6 0 0 1-6-6z" fill="#fff"/>
<circle cx="32" cy="27" r="8.5" fill="none" stroke="{ACCENT}" stroke-width="4"/>
<path d="M35 31l6 6" stroke="{ACCENT}" stroke-width="4" stroke-linecap="round"/>
</svg>"""
BRAND_TYPES = {".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
               ".otf": "font/otf", ".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
               ".webp": "image/webp"}
FAVICON_FILES = (("favicon.svg", "image/svg+xml"), ("favicon.png", "image/png"), ("favicon.ico", "image/x-icon"))


def is_text_mime(mime):
    return mime.startswith("text/") or mime in TEXT_MIMES


def guess_mime(filename, head=b""):
    ext = os.path.splitext(filename)[1].lower()
    if ext in EXTRA_TYPES:
        return EXTRA_TYPES[ext]
    mime = mimetypes.guess_type(filename)[0]
    if mime:
        return mime
    try:
        head.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def safe_filename(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return (name or "file")[:120]


# --------------------------------------------------------------------------- storage

class Store:
    def __init__(self, path, max_doc_bytes=20 * 1024 * 1024):
        self.lock = threading.Lock()
        self.max_doc_bytes = max_doc_bytes
        self.attention = None   # {"reason", "by", "ts"} while the session is waiting for a person
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.docdir = os.path.join(os.path.dirname(os.path.abspath(path)), "docs")
        self.tmpdir = os.path.join(self.docdir, ".tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS questions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL,
                to_whom TEXT NOT NULL DEFAULT 'anyone',
                status TEXT NOT NULL DEFAULT 'open',
                created REAL NOT NULL,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS answers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL REFERENCES questions(id),
                body TEXT NOT NULL,
                author TEXT NOT NULL,
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                question_id INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deployments(
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS users(
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'person',
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS document_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL REFERENCES documents(id),
                version INTEGER NOT NULL,
                filename TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                author TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL,
                created REAL NOT NULL);
            """
        )
        self.db.commit()
        self._seed_users()
        # Leftovers from interrupted uploads
        for f in os.listdir(self.tmpdir):
            try:
                os.remove(os.path.join(self.tmpdir, f))
            except OSError:
                pass

    # ---------------------------------------------------------------- questions
    def _answers(self, qid):
        rows = self.db.execute(
            "SELECT id, body, author, created FROM answers WHERE question_id=? ORDER BY id", (qid,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _question(self, row):
        q = dict(row)
        q["to"] = q.pop("to_whom")
        q["answers"] = self._answers(q["id"])
        q["documents"] = [
            {"id": r["id"], "title": r["title"], "filename": r["filename"] or "", "mime": r["mime"] or ""}
            for r in self.db.execute(
                "SELECT d.id, d.title, v.filename, v.mime FROM documents d LEFT JOIN document_versions v "
                "ON v.doc_id=d.id AND v.version=(SELECT MAX(version) FROM document_versions WHERE doc_id=d.id) "
                "WHERE d.question_id=? AND d.archived=0 ORDER BY d.id", (q["id"],))
        ]
        return q

    def post(self, title, body, author, to):
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        now = time.time()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO questions(title, body, author, to_whom, status, created, updated) "
                "VALUES(?,?,?,?, 'open', ?, ?)",
                (title, (body or "").strip(), clean_name(author), clean_name(to or "anyone"), now, now),
            )
            self.db.commit()
            return self.get(cur.lastrowid)

    def answer(self, qid, body, author):
        body = (body or "").strip()
        if not body:
            raise ValueError("answer text is required")
        now = time.time()
        with self.lock:
            q = self.db.execute("SELECT status FROM questions WHERE id=?", (qid,)).fetchone()
            if not q:
                raise KeyError("no question #%s" % qid)
            self.db.execute(
                "INSERT INTO answers(question_id, body, author, created) VALUES(?,?,?,?)",
                (qid, body, clean_name(author), now),
            )
            new_status = "answered" if q["status"] == "open" else q["status"]
            self.db.execute("UPDATE questions SET status=?, updated=? WHERE id=?", (new_status, now, qid))
            self.db.commit()
            return self.get(qid)

    def set_status(self, qid, status):
        """status: open, answered, resolved, on_hold - or reopen: back to answered if it has answers,
        otherwise open (used to take a question off hold or reopen a resolved one)."""
        if status == "reopen":
            with self.lock:
                n = self.db.execute("SELECT COUNT(*) FROM answers WHERE question_id=?", (qid,)).fetchone()[0]
            status = "answered" if n else "open"
        if status not in STATUSES:
            raise ValueError("status must be one of %s, or reopen" % ", ".join(STATUSES))
        with self.lock:
            cur = self.db.execute(
                "UPDATE questions SET status=?, updated=? WHERE id=?", (status, time.time(), qid)
            )
            self.db.commit()
            if cur.rowcount == 0:
                raise KeyError("no question #%s" % qid)
            return self.get(qid)

    # -- deployments: progress reports (the whole state each time; a report replaces the earlier one)
    def put_deployment(self, dep_id, state):
        state = clean_deployment(dep_id, state)
        now = time.time()
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO deployments(id, state, updated) VALUES(?,?,?)",
                            (dep_id, json.dumps(state), now))
            self.db.execute("DELETE FROM deployments WHERE id NOT IN "
                            "(SELECT id FROM deployments ORDER BY updated DESC LIMIT 50)")
            self.db.commit()
        # "waitingFor" raises the board's Needs-you banner, and clears the one it raised when it's gone
        wf, att = state.get("waitingFor"), self.attention
        if isinstance(wf, dict) and wf.get("reason"):
            new = clean_attention(wf.get("reason"), wf.get("url"), wf.get("code"), "deployment")
            new["deployment"] = dep_id
            if not att or any(att.get(k) != new.get(k) for k in ("reason", "url", "code", "deployment")):
                self.attention = new
        elif att and att.get("deployment") == dep_id:
            self.attention = None
        state["received"] = now
        return state

    def deployments(self, limit=20, dep_id=None):
        with self.lock:
            if dep_id:
                rows = self.db.execute("SELECT * FROM deployments WHERE id=?", (dep_id,)).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM deployments ORDER BY updated DESC LIMIT ?",
                                       (max(1, min(int(limit or 20), 50)),)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["state"])
            d["received"] = r["updated"]
            out.append(d)
        return out

    def get(self, qid):
        row = self.db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if not row:
            raise KeyError("no question #%s" % qid)
        return self._question(row)

    def list(self, status="unresolved", to=None, limit=50):
        sql, args = "SELECT * FROM questions WHERE 1=1", []
        if status == "unresolved":
            sql += " AND status IN ('open','answered')"
        elif status in STATUSES:
            sql += " AND status=?"
            args.append(status)
        elif status not in (None, "", "all"):
            raise ValueError("status must be open, answered, resolved, on_hold, unresolved or all")
        if to:
            sql += " AND to_whom IN (?, 'anyone')"
            args.append(clean_name(to))
        sql += " ORDER BY updated DESC LIMIT ?"
        args.append(max(1, min(int(limit or 50), 500)))
        with self.lock:
            return [self._question(r) for r in self.db.execute(sql, args).fetchall()]

    # ---------------------------------------------------------------- users (the team list)
    def _seed_users(self):
        """First start with a users table: take the names from people.txt if there is one, and always
        include dev-claude (the Claude Code on the server)."""
        if self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        names = ["dev-claude"]
        roster = os.path.join(os.path.dirname(self.docdir), "people.txt")
        if os.path.isfile(roster):
            with open(roster, encoding="utf-8", errors="replace") as f:
                names += [clean_name(l.split("#", 1)[0]) for l in f if l.split("#", 1)[0].strip()]
        now = time.time()
        for n in dict.fromkeys(names):
            if n not in ("anyone", "unknown"):
                self.db.execute("INSERT OR IGNORE INTO users(name, kind, created) VALUES(?,?,?)",
                                (n, "claude" if n.endswith("claude") else "person", now))
        self.db.commit()

    def users(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT name, kind FROM users ORDER BY name")]

    def add_user(self, name, kind):
        name = clean_name(name)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", name) or name in ("anyone", "unknown"):
            raise ValueError("use a short name with letters, digits, - or _ (not 'anyone')")
        if kind not in ("person", "claude"):
            raise ValueError("kind must be person or claude")
        with self.lock:
            self.db.execute("INSERT INTO users(name, kind, created) VALUES(?,?,?) "
                            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind", (name, kind, time.time()))
            self.db.commit()
        return {"name": name, "kind": kind}

    def remove_user(self, name):
        with self.lock:
            cur = self.db.execute("DELETE FROM users WHERE name=?", (clean_name(name),))
            self.db.commit()
        if not cur.rowcount:
            raise KeyError("no user %s" % name)

    def people(self):
        users = self.users()
        if users:
            return sorted({u["name"] for u in users} | {"anyone"})
        # Without a users list: people.txt, or every name the board has seen.
        roster = os.path.join(os.path.dirname(self.docdir), "people.txt")
        if os.path.isfile(roster):
            names = set()
            with open(roster, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        names.add(clean_name(line))
            return sorted(names | {"anyone"})
        with self.lock:
            rows = self.db.execute(
                "SELECT author AS n FROM questions UNION SELECT to_whom FROM questions "
                "UNION SELECT author FROM answers UNION SELECT author FROM document_versions"
            ).fetchall()
        return sorted({r["n"] for r in rows} | {"anyone"})

    def digest(self, who):
        who = clean_name(who)
        with self.lock:
            waiting = self.db.execute(
                "SELECT id, title, author FROM questions WHERE status='open' "
                "AND to_whom IN (?, 'anyone') AND author<>? ORDER BY id",
                (who, who),
            ).fetchall()
            answered = self.db.execute(
                "SELECT id, title FROM questions WHERE status='answered' AND author=? ORDER BY id",
                (who,),
            ).fetchall()
            recent_docs = self.db.execute(
                "SELECT d.id, d.title, v.author FROM documents d JOIN document_versions v "
                "ON v.doc_id=d.id AND v.version=(SELECT MAX(version) FROM document_versions WHERE doc_id=d.id) "
                "WHERE d.archived=0 AND d.updated>? AND v.author<>? ORDER BY d.updated DESC LIMIT 5",
                (time.time() - 86400, who),
            ).fetchall()
        lines = []
        if waiting:
            lines.append("[QA board] %d open question(s) waiting for %s:" % (len(waiting), who))
            lines += ["  #%d from %s: %s" % (r["id"], r["author"], r["title"]) for r in waiting]
        if answered:
            lines.append("[QA board] %d of %s's questions have answers to review:" % (len(answered), who))
            lines += ["  #%d %s" % (r["id"], r["title"]) for r in answered]
        if lines and recent_docs:
            lines.append("[QA board] documents added or updated in the last day:")
            lines += ["  doc %d by %s: %s" % (r["id"], r["author"], r["title"]) for r in recent_docs]
        if lines:
            lines.append("(Use the qaboard tools: get_question / answer_question / resolve_question / get_document.)")
        return "\n".join(lines)

    # ---------------------------------------------------------------- documents
    def spool(self, chunks):
        """Write an upload to a temp file. Returns (tmp_path, size, sha256, first_bytes)."""
        fd, tmp = tempfile.mkstemp(dir=self.tmpdir)
        h, size, head = hashlib.sha256(), 0, b""
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_doc_bytes:
                        raise ValueError("file is larger than the %d MB limit" % (self.max_doc_bytes // 1048576))
                    if len(head) < 4096:
                        head += chunk[:4096 - len(head)]
                    h.update(chunk)
                    f.write(chunk)
            if size == 0:
                raise ValueError("the file is empty")
            return tmp, size, h.hexdigest(), head
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def add_document(self, spooled, filename, author, title=None, description=None,
                     question_id=None, replace_id=None, note=""):
        tmp, size, sha, head = spooled
        try:
            filename = safe_filename(filename)
            mime = guess_mime(filename, head)
            author = clean_name(author)
            now = time.time()
            with self.lock:
                if question_id not in (None, ""):
                    question_id = int(question_id)
                    if not self.db.execute("SELECT 1 FROM questions WHERE id=?", (question_id,)).fetchone():
                        raise KeyError("no question #%s" % question_id)
                else:
                    question_id = None
                if replace_id not in (None, ""):
                    doc_id = int(replace_id)
                    d = self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
                    if not d:
                        raise KeyError("no document %s" % doc_id)
                    version = self.db.execute(
                        "SELECT COALESCE(MAX(version),0)+1 FROM document_versions WHERE doc_id=?", (doc_id,)
                    ).fetchone()[0]
                    self.db.execute(
                        "UPDATE documents SET title=?, description=?, question_id=COALESCE(?, question_id), "
                        "archived=0, updated=? WHERE id=?",
                        ((title or "").strip() or d["title"],
                         d["description"] if description is None else description.strip(),
                         question_id, now, doc_id))
                else:
                    cur = self.db.execute(
                        "INSERT INTO documents(title, description, question_id, created, updated) VALUES(?,?,?,?,?)",
                        ((title or "").strip() or filename, (description or "").strip(), question_id, now, now))
                    doc_id, version = cur.lastrowid, 1
                rel = os.path.join(str(doc_id), "v%d-%s" % (version, filename))
                os.makedirs(os.path.join(self.docdir, str(doc_id)), exist_ok=True)
                os.replace(tmp, os.path.join(self.docdir, rel))
                tmp = None
                self.db.execute(
                    "INSERT INTO document_versions(doc_id, version, filename, mime, size, sha256, author, note, path, created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, version, filename, mime, size, sha, author, (note or "").strip(), rel, now))
                self.db.commit()
                return self._document(doc_id)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _document(self, doc_id, with_versions=True):
        d = self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not d:
            raise KeyError("no document %s" % doc_id)
        doc = dict(d)
        doc["archived"] = bool(doc["archived"])
        versions = [dict(r) for r in self.db.execute(
            "SELECT version, filename, mime, size, sha256, author, note, created FROM document_versions "
            "WHERE doc_id=? ORDER BY version DESC", (doc_id,))]
        cur = versions[0] if versions else {}
        doc.update({k: cur.get(k) for k in ("version", "filename", "mime", "size", "author")})
        doc["download"] = "/api/docs/%d/download" % doc_id
        doc["view"] = "/api/docs/%d/view" % doc_id
        if with_versions:
            doc["versions"] = versions
        else:
            doc["version_count"] = len(versions)
        return doc

    def get_document(self, doc_id):
        with self.lock:
            return self._document(int(doc_id))

    def list_documents(self, query=None, question_id=None, include_archived=False, limit=100):
        sql, args = "SELECT d.id FROM documents d WHERE 1=1", []
        if not include_archived:
            sql += " AND d.archived=0"
        if question_id not in (None, ""):
            sql += " AND d.question_id=?"
            args.append(int(question_id))
        if query:
            like = "%" + query.strip() + "%"
            sql += (" AND (d.title LIKE ? OR d.description LIKE ? OR EXISTS (SELECT 1 FROM document_versions v "
                    "WHERE v.doc_id=d.id AND (v.filename LIKE ? OR v.author LIKE ?)))")
            args += [like, like, like, like]
        sql += " ORDER BY d.updated DESC LIMIT ?"
        args.append(max(1, min(int(limit or 100), 500)))
        with self.lock:
            return [self._document(r["id"], with_versions=False) for r in self.db.execute(sql, args).fetchall()]

    def doc_file(self, doc_id, version=None):
        with self.lock:
            if version in (None, ""):
                r = self.db.execute(
                    "SELECT * FROM document_versions WHERE doc_id=? ORDER BY version DESC LIMIT 1", (int(doc_id),)
                ).fetchone()
            else:
                r = self.db.execute(
                    "SELECT * FROM document_versions WHERE doc_id=? AND version=?", (int(doc_id), int(version))
                ).fetchone()
        if not r:
            raise KeyError("no document %s%s" % (doc_id, "" if version in (None, "") else " version %s" % version))
        path = os.path.join(self.docdir, r["path"])
        if not os.path.isfile(path):
            raise KeyError("the file for document %s is missing on the server" % doc_id)
        return path, dict(r)

    def set_archived(self, doc_id, archived):
        with self.lock:
            cur = self.db.execute("UPDATE documents SET archived=?, updated=? WHERE id=?",
                                  (1 if archived else 0, time.time(), int(doc_id)))
            self.db.commit()
            if cur.rowcount == 0:
                raise KeyError("no document %s" % doc_id)
            return self._document(int(doc_id))


DEP_STATUSES = ("running", "waiting", "done", "failed")



# ============================================================== Gala data folder
# The portal's data folder (--gala-data). Each entry: key, folder under the data folder, title, hint, accepted types.
GALA_FOLDERS = [
    ("scanned", "pdf/scanned", "Scanned paper",
     "The scans: Lane Sheets.pdf, DQs.pdf, ETD.pdf", ".pdf"),
    ("original", "pdf/original", "Meet Manager and Gen7 PDFs",
     "The Meet Manager print and the Colorado Gen7 session report (*Session*.pdf)", ".pdf"),
    ("meet", "meet", "Meet files",
     "Entries and results from Meet Manager (.hy3, or the .zip)", ".hy3,.zip,.cl2,.ev3"),
    ("ocr", "ocr", "OCR cache",
     "JSON written by the gala-ocr skill (one file per lane sheet, DQ slip or ETD page)", ".json"),
    ("reports", "reports", "Reports",
     "Audit reports (HTML), merged .hy3 files, CSV and JSON written by Claude", ""),
]
GALA_KEYS = {f[0]: f for f in GALA_FOLDERS}
_GALA_CACHE = {}
_OCR_PAGE = re.compile(r"-p(\d{3,4})\.json$", re.I)


def _cached(kind, path, fn):
    try:
        st = os.stat(path)
    except OSError:
        return None
    k = (kind, path)
    hit = _GALA_CACHE.get(k)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        return hit[1]
    val = fn(path)
    _GALA_CACHE[k] = ((st.st_mtime, st.st_size), val)
    return val


def _pdf_pages(path):
    def run(p):
        try:
            out = subprocess.run(["pdfinfo", p], capture_output=True, text=True, timeout=30).stdout
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            return int(m.group(1)) if m else None
        except (OSError, subprocess.SubprocessError):
            return None
    return _cached("pages", path, run)


def _ocr_info(path):
    """(page, back page or None, has 'needsReview'/'unsure' flags) for one OCR JSON file."""
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
    return _cached("ocr", path, run)


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


def gala_overview(root):
    folders = []
    for key, rel, title, hint, types in GALA_FOLDERS:
        d = os.path.join(root, rel)
        files = _ls(d)
        entry = {"key": key, "path": rel, "title": title, "hint": hint, "types": types,
                 "count": len([f for f in files if not f["dir"]])}
        if key == "ocr":   # can be thousands: send the newest few only
            entry["files"] = sorted(files, key=lambda f: -f["mtime"])[:12]
            entry["latest"] = max([f["mtime"] for f in files] or [0])
        else:
            entry["files"] = files
        folders.append(entry)

    # OCR progress per scan: pages in the PDF vs pages with a JSON in ocr/ (a DQ slip's back page counts too)
    ocr_dir = os.path.join(root, "ocr")
    ocr_files = [f["name"] for f in _ls(ocr_dir) if f["name"].lower().endswith(".json")]
    scans = []
    for f in _ls(os.path.join(root, "pdf", "scanned")):
        if f["dir"] or not f["name"].lower().endswith(".pdf"):
            continue
        stem = f["name"][:-4]
        pages = _pdf_pages(os.path.join(root, "pdf", "scanned", f["name"]))
        done, review, bad, nfiles = set(), 0, 0, 0
        for n in ocr_files:
            if not n.startswith(stem + "-"):
                continue
            m = _OCR_PAGE.search(n)
            if not m:
                continue
            nfiles += 1
            done.add(int(m.group(1)))
            info = _ocr_info(os.path.join(ocr_dir, n)) or {}
            if info.get("bad"):
                bad += 1
            if info.get("back"):
                done.add(info["back"])
            if info.get("review"):
                review += 1
        kind = ("dq" if re.search(r"dq", stem, re.I) else "etd" if re.search(r"etd|programme|program", stem, re.I)
                else "lanes")
        missing = [p for p in range(1, (pages or 0) + 1) if p not in done]
        scans.append({"name": f["name"], "kind": kind, "pages": pages, "done": len([p for p in done if not pages or p <= pages]),
                      "files": nfiles, "review": review, "bad": bad, "missing": missing[:60],
                      "missingCount": len(missing)})
    tools = {t: bool(shutil.which(t)) for t in ("pdfinfo", "pdftoppm", "pdftotext")}
    return {"root": root, "folders": folders, "scans": scans, "tools": tools}


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


def clean_deployment(dep_id, state):
    """Check a deployment progress report: a JSON object (the whole state), up to 256 KB."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", dep_id or ""):
        raise ValueError("deployment id: letters, digits and . _ - only (up to 80)")
    if not isinstance(state, dict):
        raise ValueError("send the deployment state as a JSON object")
    state = dict(state)
    state["deployment"] = dep_id
    if state.get("status") not in DEP_STATUSES:
        raise ValueError("status must be one of " + ", ".join(DEP_STATUSES))
    if "progress" in state:
        try:
            state["progress"] = max(0.0, min(1.0, float(state["progress"])))
        except (TypeError, ValueError):
            state.pop("progress")
    if not isinstance(state.get("steps", []), list):
        raise ValueError("steps must be a list")
    if len(json.dumps(state)) > 256 * 1024:
        raise ValueError("deployment state is larger than 256 KB")
    return state


def clean_attention(reason, url=None, code=None, by="someone"):
    """Validate a needs-attention signal: one-line reason, an optional http(s) link, an optional short code."""
    reason = " ".join(str(reason or "").split())[:200]
    if not reason:
        raise ValueError("give a short reason, e.g. 'OK to deploy to www-dev?'")
    url = str(url or "").strip()
    if url and not re.fullmatch(r"https?://[^\s<>\"']{3,1000}", url):
        raise ValueError("url must be a plain http(s) link")
    code = str(code or "").strip()
    if code and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{1,39}", code):
        raise ValueError("code must be short letters/digits, e.g. ABCD-EFGH")
    att = {"reason": reason, "by": clean_name(by), "ts": time.time()}
    if url:
        att["url"] = url
    if code:
        att["code"] = code
    return att


def clean_name(name):
    name = (name or "").strip().lower().replace(" ", "-")
    return name[:40] or "unknown"


# --------------------------------------------------------------------------- MCP

TOOLS = [
    {
        "name": "check_inbox",
        "description": "Show open questions addressed to you (or to anyone), and your own questions that "
                       "have received answers. Call this at the start of a task and whenever asked "
                       "about the team board.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "post_question",
        "description": "Post a question to the shared team Q&A board. `to` is required: the person or Claude "
                       "who should answer (e.g. 'alice', 'bob', 'dev-claude'). Only use 'anyone' when the user "
                       "has said the question is for anyone; if they haven't said who it's for, ask them before "
                       "posting. Include enough context that the reader can answer without asking back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "One-line question"},
                "body": {"type": "string", "description": "Details, context, code, file paths"},
                "to": {"type": "string", "description": "Who should answer, e.g. 'dev-claude'. 'anyone' only if the user said so"},
            },
            "required": ["title", "to"],
        },
    },
    {
        "name": "list_questions",
        "description": "List questions on the board, newest activity first, with their answers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": ["unresolved", "open", "answered", "resolved", "on_hold", "all"],
                           "description": "Default 'unresolved' (open + answered; on-hold questions are left out)"},
                "to": {"type": "string", "description": "Only questions addressed to this name (or anyone)"},
                "limit": {"type": "integer", "description": "Default 20"},
            },
        },
    },
    {
        "name": "get_question",
        "description": "Read one question, all of its answers and the documents attached to it.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "answer_question",
        "description": "Post an answer to a question. Be specific; include commands, file paths and "
                       "code where useful.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "answer": {"type": "string"}},
            "required": ["id", "answer"],
        },
    },
    {
        "name": "resolve_question",
        "description": "Mark a question resolved (usually done by whoever asked it, once satisfied). "
                       "Use reopen=true to reopen it (also takes it off hold).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "reopen": {"type": "boolean"}},
            "required": ["id"],
        },
    },
    {
        "name": "hold_question",
        "description": "Put a question on hold: it stays on the board but drops out of everyone's inbox, and "
                       "nobody (you included) should answer, chase or ask about it until it is taken off hold. "
                       "Only do this when a person asks you to. release=true takes it off hold.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "release": {"type": "boolean"}},
            "required": ["id"],
        },
    },
    {
        "name": "deployment_progress",
        "description": "Show the progress of a deployment (or any long multi-step job) on the board: a progress "
                       "bar at the top of every page and the full step list on the Deploys tab. Send the WHOLE "
                       "state every time (a report replaces the previous one for that deployment id), when a step "
                       "starts or finishes and when you start waiting for someone. status: running, waiting, done "
                       "or failed. waitingFor ({reason, url, code}) raises the board's Needs-you banner and is cleared "
                       "when you send it as null. error: {step, message, log} with the full log uploaded as a "
                       "document ('doc 812').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string", "description": "Id, e.g. dep-20261104-0933"},
                "target": {"type": "string", "description": "Where to, e.g. www-dev"},
                "status": {"type": "string", "enum": list(DEP_STATUSES)},
                "step": {"type": "string", "description": "Id of the current step"},
                "progress": {"type": "number", "description": "0..1, optional (else counted from the steps)"},
                "questions": {"type": "array", "items": {"type": "integer"}, "description": "Board questions it ships"},
                "branches": {"type": "array", "items": {"type": "string"}},
                "commit": {"type": "string"},
                "started": {"type": "string", "description": "ISO 8601 time"},
                "updated": {"type": "string", "description": "ISO 8601 time"},
                "steps": {"type": "array", "description": "In order: {id, label, status, finished?, detail?, "
                          "stages?: [{name, status}]}", "items": {"type": "object"}},
                "waitingFor": {"type": ["object", "null"], "description": "{reason, url?, code?} or null"},
                "error": {"type": ["object", "null"], "description": "{step, message, log?} or null"},
            },
            "required": ["deployment", "status"],
        },
    },
    {
        "name": "upload_document",
        "description": "Store a document in the team's shared document store, where everyone on the board "
                       "can open it. Pass text files (markdown, code, CSV, logs, JSON) as `content`; small binary "
                       "files as `content_base64`. For large or binary files that exist on disk, uploading with "
                       "curl is cheaper (see the server instructions). Give replace_id to upload a new version of "
                       "an existing document (old versions are kept). Optionally attach it to a question.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "File name with extension, e.g. 'api-notes.md'"},
                "content": {"type": "string", "description": "Text content (UTF-8)"},
                "content_base64": {"type": "string", "description": "Binary content, base64-encoded"},
                "title": {"type": "string", "description": "Human-friendly title. Default: the file name"},
                "description": {"type": "string", "description": "One or two lines on what it is"},
                "question_id": {"type": "integer", "description": "Attach to this question"},
                "replace_id": {"type": "integer", "description": "Upload as a new version of this document"},
                "note": {"type": "string", "description": "What changed in this version"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "list_documents",
        "description": "List documents in the shared store, most recently updated first. Search by words in "
                       "the title, description, file name or author.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "question_id": {"type": "integer", "description": "Only documents attached to this question"},
                "include_archived": {"type": "boolean"},
                "limit": {"type": "integer", "description": "Default 30"},
            },
        },
    },
    {
        "name": "get_document",
        "description": "Read a document: its details and version history, plus the content of text files "
                       "(up to max_chars) or the picture for PNG/JPEG/GIF/WebP images. Other binary files "
                       "(PDF, Office, zip) come back as details and a download path unless as_base64=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "version": {"type": "integer", "description": "Default: latest"},
                "max_chars": {"type": "integer", "description": "Text limit, default 100000"},
                "as_base64": {"type": "boolean", "description": "Return binary content as base64 (max 5 MB)"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "archive_document",
        "description": "Hide a document from the normal list (nothing is deleted). restore=true brings it back.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "restore": {"type": "boolean"}},
            "required": ["id"],
        },
    },
    {
        "name": "needs_attention",
        "description": "Signal that you are blocked until a person does something in your session: e.g. give the "
                       "go-ahead to merge/deploy, sign in (aws sso login, gh auth login: include the sign-in url and code), "
                       "approve a risky step, "
                       "or answer a question you've asked in the terminal. The board shows it to the team (the "
                       "console preview's border pulses red, with your reason). Call it right after you ask, and "
                       "call attention_cleared once you have what you need.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "One short line: what you need, e.g. 'OK to deploy to www-dev?'"},
                "url": {"type": "string",
                        "description": "A link the person should open, e.g. the AWS SSO or GitHub device sign-in page"},
                "code": {"type": "string",
                         "description": "A code they must enter or check there, e.g. the AWS SSO / GitHub one-time code"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "attention_cleared",
        "description": "Clear the needs_attention signal once the person has responded and you can carry on.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

INSTRUCTIONS = (
    "Shared Q&A board and document store for the team (people and their Claude assistants). "
    "Your identity on the board is set by the connection. Use check_inbox at the start of work. "
    "When you need a decision, fact or context only a teammate has, post_question instead of guessing. "
    "Answer questions addressed to you with concrete, verified information. "
    "Put documents the team should be able to read (specs, reports, notes, screenshots) in the document store "
    "with upload_document, and link them from questions or answers as 'doc <id>'. "
    "When you run a deployment or another long multi-step job, report it with deployment_progress (the whole "
    "state each time a step starts or finishes) so the team can follow it on the board. "
    "A question with status on_hold has been put on hold by the team: don't answer, chase, follow up on or ask "
    "about it (not even your own) until it is taken off hold; it is left out of your inbox until then. "
    "Posts are Markdown; to show an image (PNG, JPEG, GIF, WebP or SVG) from the document store in a post, "
    "write ![caption](/api/docs/<id>/view) (add ?v=<n> for an older version). "
    "Whenever you stop to wait for a person in your session (a go-ahead to merge or deploy, a sign-in such as "
    "aws sso login, an approval, or an answer to a question you asked in the terminal), call needs_attention "
    "with a one-line reason so the team sees it on the board - for a sign-in, include its url and code - and "
    "call attention_cleared when you can carry on."
)


def _text(s):
    return {"type": "text", "text": s}


def call_tool(store, who, name, args, base_url=""):
    """Returns a list of MCP content blocks."""
    args = args or {}
    if name == "check_inbox":
        text = store.digest(who)
        return [_text(text or "Nothing waiting for %s on the QA board." % who)]
    if name == "post_question":
        to = (args.get("to") or "").strip()
        if not to:
            raise ValueError("not posted: no recipient. Ask the user who this question is for (a person, "
                             "a Claude such as dev-claude, or 'anyone'), then call post_question again with `to`.")
        q = store.post(args.get("title"), args.get("body"), who, to)
        note = ""
        if q["to"] == "anyone":
            note = (" Note: it is addressed to ANYONE, so no one in particular is asked to answer it. If the user "
                    "didn't mean that, tell them; they can ask for it to be re-posted to the right person.")
        return [_text("Posted question #%d to %s.%s\n%s" % (q["id"], q["to"], note, json.dumps(q, indent=1)))]
    if name == "list_questions":
        qs = store.list(args.get("status") or "unresolved", args.get("to"), args.get("limit") or 20)
        return [_text(json.dumps(qs, indent=1) if qs else "No matching questions.")]
    if name == "get_question":
        q = store.get(int(args["id"]))
        hold = ("This question is ON HOLD: don't answer, follow up on or ask about it until it is taken off hold.\n"
                if q["status"] == "on_hold" else "")
        return [_text(hold + json.dumps(q, indent=1))]
    if name == "answer_question":
        q = store.answer(int(args["id"]), args.get("answer"), who)
        return [_text("Answer added to #%d.\n%s" % (q["id"], json.dumps(q, indent=1)))]
    if name == "resolve_question":
        q = store.set_status(int(args["id"]), "reopen" if args.get("reopen") else "resolved")
        return [_text("Question #%d is now %s." % (q["id"], q["status"]))]
    if name == "hold_question":
        q = store.set_status(int(args["id"]), "reopen" if args.get("release") else "on_hold")
        return [_text("Question #%d is now %s." % (q["id"], q["status"].replace("_", " ")) +
                      ("" if args.get("release") else " Leave it alone until someone takes it off hold."))]

    if name == "upload_document":
        if args.get("content_base64"):
            try:
                data = base64.b64decode(args["content_base64"], validate=False)
            except (ValueError, TypeError):
                raise ValueError("content_base64 isn't valid base64")
        elif args.get("content") is not None:
            data = str(args["content"]).encode("utf-8")
        else:
            raise ValueError("give the file as content (text) or content_base64 (binary)")
        spooled = store.spool([data])
        d = store.add_document(spooled, args.get("filename"), who, args.get("title"), args.get("description"),
                               args.get("question_id"), args.get("replace_id"), args.get("note") or "")
        return [_text("Stored doc %d version %d (%s, %d bytes). Everyone on the board can open it at %s%s\n%s" % (
            d["id"], d["version"], d["filename"], d["size"], base_url, d["view"], json.dumps(d, indent=1)))]
    if name == "list_documents":
        docs = store.list_documents(args.get("query"), args.get("question_id"),
                                    bool(args.get("include_archived")), args.get("limit") or 30)
        return [_text(json.dumps(docs, indent=1) if docs else "No matching documents.")]
    if name == "get_document":
        doc = store.get_document(int(args["id"]))
        path, ver = store.doc_file(doc["id"], args.get("version"))
        meta = dict(doc)
        meta["shown_version"] = ver["version"]
        out = [_text(json.dumps(meta, indent=1))]
        mime = ver["mime"]
        if is_text_mime(mime):
            limit = max(1000, min(int(args.get("max_chars") or 100000), 1000000))
            with open(path, "rb") as f:
                raw = f.read(limit * 4 + 4)
            text = raw.decode("utf-8", errors="replace")
            cut = len(text) > limit or ver["size"] > len(raw)
            out.append(_text(text[:limit] + ("\n\n[... truncated; raise max_chars or download %s%s]"
                                             % (base_url, doc["download"]) if cut else "")))
        elif mime in IMAGE_MIMES and ver["size"] <= 5 * 1024 * 1024 and not args.get("as_base64"):
            with open(path, "rb") as f:
                out.append({"type": "image", "mimeType": mime, "data": base64.b64encode(f.read()).decode()})
        elif args.get("as_base64"):
            if ver["size"] > 5 * 1024 * 1024:
                raise ValueError("file is over 5 MB; download it from %s%s" % (base_url, doc["download"]))
            with open(path, "rb") as f:
                out.append(_text(base64.b64encode(f.read()).decode()))
        else:
            out.append(_text("Binary file (%s, %d bytes). Download: %s%s  (or call again with as_base64=true)"
                             % (mime, ver["size"], base_url, doc["download"])))
        return out
    if name == "archive_document":
        d = store.set_archived(int(args["id"]), not args.get("restore"))
        return [_text("Document %d is now %s." % (d["id"], "archived" if d["archived"] else "active"))]
    if name == "deployment_progress":
        d = store.put_deployment(str(args.get("deployment") or ""), args)
        cur = next((x for x in d.get("steps") or [] if isinstance(x, dict) and x.get("id") == d.get("step")), {})
        return [_text("Deployment %s: %s%s. Shown on the board's Deploys tab." % (
            d["deployment"], d["status"], (" - " + str(cur.get("label") or d.get("step"))) if d.get("step") else ""))]
    if name == "needs_attention":
        att = clean_attention(args.get("reason"), args.get("url"), args.get("code"), who)
        store.attention = att
        extra = (" with the link" if att.get("url") else "") + (" and code" if att.get("code") else "")
        return [_text("The board now shows the team that you need someone%s: %s. Call attention_cleared once "
                      "they've responded." % (extra, att["reason"]))]
    if name == "attention_cleared":
        had = store.attention
        store.attention = None
        return [_text("Cleared." if had else "Nothing was set.")]
    raise LookupError("unknown tool: %s" % name)


def handle_rpc(store, who, msg, base_url="", upload_hint=""):
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification (e.g. notifications/initialized) or a response: nothing to send
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "qaboard", "version": VERSION},
            "instructions": INSTRUCTIONS + " You are connected as '%s'." % who + upload_hint,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        try:
            content = call_tool(store, who, params.get("name"), params.get("arguments"), base_url)
            return ok({"content": content, "isError": False})
        except LookupError as e:
            if "unknown tool" in str(e):
                return err(-32602, str(e))
            msg = e.args[0] if isinstance(e, KeyError) and e.args else e
            return ok({"content": [_text("Error: %s" % msg)], "isError": True})
        except (ValueError, TypeError, OSError) as e:
            return ok({"content": [_text("Error: %s" % e)], "isError": True})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, "Method not found: %s" % method)


# --------------------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "qaboard/" + VERSION
    protocol_version = "HTTP/1.1"

    # -- helpers
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "text" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _length(self):
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1

    def _body(self, limit):
        n = self._length()
        if n < 0 or n > limit:
            self.close_connection = True
            raise OverflowError("request body too large")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}

    def _chunks(self, n):
        left = n
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                raise ValueError("upload was cut off")
            left -= len(chunk)
            yield chunk

    def _parsed(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        return u.path.rstrip("/") or "/", q

    def _authorized(self, q):
        key = self.server.key
        return not key or q.get("key") == key or self.headers.get("X-QA-Key") == key

    def _who(self, q):
        return clean_name(q.get("who") or self.headers.get("X-QA-Who") or "claude")

    def _base_url(self):
        host = self.headers.get("Host") or "localhost"
        return "http://%s" % host

    def _favicon(self):
        base = os.path.dirname(self.server.store.docdir)
        for name, mime in FAVICON_FILES:
            f = os.path.join(base, name)
            if os.path.isfile(f) and os.path.getsize(f) < 512 * 1024:
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), mime, {"Cache-Control": "public, max-age=3600"})
        return self._send(200, FAVICON_SVG.replace("{ACCENT}", self.server.accent), "image/svg+xml",
                          {"Cache-Control": "public, max-age=3600"})

    def _brand_dir(self):
        return os.path.join(os.path.dirname(self.server.store.docdir), "brand")

    def _brand_file(self, rel):
        base = os.path.realpath(self._brand_dir())
        f = os.path.realpath(os.path.join(base, urllib.parse.unquote(rel)))
        ctype = BRAND_TYPES.get(os.path.splitext(f)[1].lower())
        if not ctype or not f.startswith(base + os.sep) or not os.path.isfile(f):
            return self._json(404, {"error": "not found"})
        with open(f, "rb") as fh:
            return self._send(200, fh.read(), ctype, {"Cache-Control": "public, max-age=3600",
                                                      "X-Content-Type-Options": "nosniff"})

    def _page(self):
        page = self.server.page
        if os.path.isfile(os.path.join(self._brand_dir(), "brand.css")):
            v = int(os.path.getmtime(os.path.join(self._brand_dir(), "brand.css")))
            page = page.replace("</style>", '</style>\n<link rel="stylesheet" href="/brand/brand.css?v=%d">' % v, 1)
        return page

    def _send_file(self, path, ver, inline, q):
        mime = ver["mime"]
        headers = {"X-Content-Type-Options": "nosniff"}
        fname = ver["filename"]
        quoted = urllib.parse.quote(fname)
        if inline and mime in ("text/html", "application/xhtml+xml"):
            # An HTML page is shown as a page, in a sandbox: no scripts, no forms, no access to the board
            # (it gets its own empty origin), and it can't send anything anywhere. Styles, images and fonts
            # (inline, data: or https) load, so reports look as written; no Referer, so the key in this
            # address isn't passed on to sites the page loads images from. Download it to run its scripts.
            ctype = "text/html; charset=utf-8"
            headers["Content-Disposition"] = "inline; filename*=UTF-8''%s" % quoted
            headers["Content-Security-Policy"] = ("sandbox; default-src 'none'; style-src 'unsafe-inline' https:; "
                                                  "img-src 'self' data: blob: https:; font-src data: https:; "
                                                  "media-src 'self' data: https:; form-action 'none'")
            headers["Referrer-Policy"] = "no-referrer"
        elif inline and (mime in INLINE_SAFE or is_text_mime(mime)):
            ctype = mime if mime in INLINE_SAFE else "text/plain; charset=utf-8"
            headers["Content-Disposition"] = "inline; filename*=UTF-8''%s" % quoted
            headers["Referrer-Policy"] = "no-referrer"
            if mime != "application/pdf":   # Chrome's PDF viewer doesn't run in a sandboxed response
                headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
        else:
            ctype = mime if not is_text_mime(mime) else "application/octet-stream"
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quoted
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=0")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- verbs
    def _gala_root(self):
        return getattr(self.server, "gala_root", None)

    def _gala_file(self, q):
        root = self._gala_root()
        p = gala_path(root, q.get("path")) if root else None
        if not p:
            return self._json(404, {"error": "no such file in the data folder"})
        name = os.path.basename(p)
        with open(p, "rb") as f:
            head = f.read(2048)
        mime = guess_mime(name, head)
        low = name.lower()
        if low.endswith((".hy3", ".cl2", ".ev3", ".csv", ".log", ".md", ".txt", ".json")):
            mime = "text/plain" if not low.endswith(".json") else "application/json"
        quoted = urllib.parse.quote(name)
        headers = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
        if q.get("dl"):
            ctype = "application/octet-stream"
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quoted
        elif mime in ("text/html", "application/xhtml+xml"):
            # Audit reports are HTML with their own scripts (filters, sorting). They run in a sandbox with
            # their own empty origin: no access to the portal, no network except images/styles.
            ctype = "text/html; charset=utf-8"
            headers["Content-Disposition"] = "inline; filename*=UTF-8''%s" % quoted
            headers["Content-Security-Policy"] = ("sandbox allow-scripts allow-popups allow-modals allow-downloads; "
                                                  "default-src 'none'; script-src 'unsafe-inline'; "
                                                  "style-src 'unsafe-inline' https:; img-src data: blob: https:; "
                                                  "font-src data: https:; connect-src 'none'; form-action 'none'")
        elif mime in INLINE_SAFE or is_text_mime(mime):
            ctype = mime if mime in INLINE_SAFE else ("application/json" if mime == "application/json"
                                                      else "text/plain; charset=utf-8")
            headers["Content-Disposition"] = "inline; filename*=UTF-8''%s" % quoted
            if mime != "application/pdf":
                headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
        else:
            ctype = "application/octet-stream"
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quoted
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 65536)

    def _gala_upload(self, q):
        root = self._gala_root()
        if not root:
            return self._json(400, {"error": "this portal has no data folder (--gala-data)"})
        folder = GALA_KEYS.get(q.get("folder", ""))
        if not folder:
            return self._json(400, {"error": "unknown folder"})
        name = safe_filename(q.get("filename") or "")
        if not name or name.startswith("."):
            return self._json(400, {"error": "filename is required"})
        n = self._length()
        if n <= 0:
            return self._json(400, {"error": "empty upload"})
        if n > self.server.store.max_doc_bytes:
            self.close_connection = True
            return self._json(413, {"error": "file too large (max %d MB)" % (self.server.store.max_doc_bytes // 1048576)})
        d = os.path.join(root, folder[1])
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, name)
        if os.path.exists(dest) and not q.get("replace"):
            for _ in self._chunks(n):
                pass
            return self._json(409, {"error": "exists", "name": name})
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in self._chunks(n):
                    f.write(chunk)
            if os.path.exists(dest):   # keep the previous copy next to it
                os.replace(dest, os.path.join(d, ".%s.previous" % name))
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self._json(200, {"ok": True, "folder": folder[1], "name": name, "size": n})

    def do_GET(self):
        path, q = self._parsed()
        if path in ("/favicon.ico", "/favicon.svg", "/favicon.png"):   # browsers ask without the key
            return self._favicon()
        if path.startswith("/brand/"):   # the board's look: stylesheet, fonts, logo (no key needed)
            return self._brand_file(path[len("/brand/"):])
        if not self._authorized(q):
            return self._send(401, "Missing or wrong key. Add ?key=... to the URL.", "text/plain")
        store = self.server.store
        try:
            if path == "/":
                return self._send(200, self._page(), "text/html")
            if path == "/health":
                return self._json(200, {"ok": True, "version": VERSION, "gala": bool(self._gala_root())})
            if path == "/api/gala/overview":
                root = self._gala_root()
                if not root:
                    return self._json(200, {"root": None})
                return self._json(200, gala_overview(root))
            if path == "/api/gala/file":
                return self._gala_file(q)
            if path == "/api/questions":
                return self._json(200, {"questions": store.list(q.get("status", "unresolved"), q.get("to"), q.get("limit", 100))})
            if path.startswith("/api/questions/"):
                return self._json(200, store.get(int(path.split("/")[3])))
            if path == "/api/people":
                return self._json(200, {"people": store.people()})
            if path == "/api/users":
                return self._json(200, {"users": store.users()})
            if path == "/api/live":
                live = dict(getattr(self.server, "live", None) or {"ts": 0})
                live["attention"] = store.attention
                if live.get("text") and q.get("lines", "").isdigit():
                    n = int(q["lines"])
                    live["text"] = "\n".join(live["text"].split("\n")[-n:]) if n else ""
                return self._json(200, live)
            if path == "/api/deployments":
                return self._json(200, {"deployments": store.deployments(q.get("limit", 20))})
            if path.startswith("/api/deployments/"):
                found = store.deployments(dep_id=path[len("/api/deployments/"):])
                return self._json(200, found[0]) if found else self._json(404, {"error": "no such deployment"})
            if path == "/api/digest":
                return self._send(200, store.digest(q.get("to") or self._who(q)), "text/plain")
            if path == "/api/docs":
                return self._json(200, {
                    "documents": store.list_documents(q.get("q"), q.get("question"), q.get("archived") == "1",
                                                      q.get("limit", 200)),
                    "max_mb": store.max_doc_bytes // 1048576})
            parts = path.split("/")
            if len(parts) >= 4 and parts[1:3] == ["api", "docs"]:
                doc_id = int(parts[3])
                if len(parts) == 4:
                    return self._json(200, store.get_document(doc_id))
                if len(parts) == 5 and parts[4] in ("download", "view"):
                    fpath, ver = store.doc_file(doc_id, q.get("v"))
                    return self._send_file(fpath, ver, parts[4] == "view", q)
            if path == "/mcp":
                return self._send(405, "", extra={"Allow": "POST"})
            return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            return self._json(400, {"error": str(e)})

    def do_POST(self):
        path, q = self._parsed()
        if not self._authorized(q):
            self.close_connection = True
            return self._json(401, {"error": "missing or wrong key"})
        store = self.server.store
        try:
            if path == "/api/gala/upload":
                return self._gala_upload(q)
            if path == "/api/live":   # console snapshot from the board watcher
                d = self._body(2 * 1024 * 1024)
                text = str(d.get("text") or "")[-400000:]
                prev = getattr(self.server, "live", None) or {}
                now = time.time()
                self.server.live = {"ts": now, "changed": prev.get("changed", now) if prev.get("text") == text else now,
                                    "text": text, "busy": bool(d.get("busy")), "prompt": bool(d.get("prompt")),
                                    "title": str(d.get("title") or "")[:80]}
                return self._json(200, {"ok": True})
            if path.startswith("/api/deployments/"):   # PUT (or POST) the whole state of a deployment
                return self._json(200, store.put_deployment(path[len("/api/deployments/"):], self._body(300 * 1024)))
            if path == "/api/attention":   # set or clear "the session needs someone" (page, watcher, scripts)
                d = self._body(64 * 1024)
                if d.get("clear"):
                    store.attention = None
                else:
                    store.attention = clean_attention(d.get("reason"), d.get("url"), d.get("code"),
                                                      d.get("by") or "someone")
                return self._json(200, {"attention": store.attention})
            if path == "/api/docs":   # raw upload: body = file bytes, details in the query string
                n = self._length()
                if n <= 0:
                    return self._json(400, {"error": "send the file as the request body"})
                if n > store.max_doc_bytes:
                    self.close_connection = True
                    return self._json(413, {"error": "file is larger than the %d MB limit" % (store.max_doc_bytes // 1048576)})
                spooled = store.spool(self._chunks(n))
                author = q.get("author") or q.get("who") or self.headers.get("X-QA-Who") or "unknown"
                d = store.add_document(spooled, q.get("filename") or "upload", author, q.get("title"),
                                       q.get("description"), q.get("question"), q.get("replace"), q.get("note", ""))
                return self._json(201, d)
            data = self._body(store.max_doc_bytes * 4 // 3 + 1024 * 1024 if path == "/mcp" else 1024 * 1024)
        except OverflowError as e:
            return self._json(413, {"error": str(e)})
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'\"")})
        except (ValueError, UnicodeDecodeError) as e:
            if path == "/mcp":
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return self._json(400, {"error": str(e)})
        try:
            if path == "/mcp":
                who = self._who(q)
                base = self._base_url()
                keyq = ("?key=" + urllib.parse.quote(q["key"])) if q.get("key") else ""
                hint = (" This is the %s board." % self.server.brand +
                        " To upload a file from disk without pasting it, run: curl --data-binary @FILE "
                        "'%s/api/docs%s%sfilename=NAME&author=%s' (add &title=..., &question=ID or &replace=DOC_ID)."
                        % (base, keyq, "&" if keyq else "?", who))
                if isinstance(data, list):
                    out = [r for r in (handle_rpc(store, who, m, base, hint) for m in data) if r]
                else:
                    out = handle_rpc(store, who, data, base, hint)
                if not out:
                    return self._send(202)
                return self._json(200, out)
            if path == "/api/questions":
                return self._json(201, store.post(data.get("title"), data.get("body"), data.get("author"), data.get("to")))
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "questions"]:
                qid = int(parts[3])
                if parts[4] == "answers":
                    return self._json(201, store.answer(qid, data.get("body"), data.get("author")))
                if parts[4] == "status":
                    return self._json(200, store.set_status(qid, data.get("status")))
            if path == "/api/users":
                return self._json(201, store.add_user(data.get("name"), data.get("kind") or "person"))
            if len(parts) == 5 and parts[1:3] == ["api", "users"] and parts[4] == "delete":
                store.remove_user(urllib.parse.unquote(parts[3]))
                return self._json(200, {"ok": True})
            if len(parts) == 5 and parts[1:3] == ["api", "docs"] and parts[4] == "archive":
                return self._json(200, store.set_archived(int(parts[3]), data.get("archived", True)))
            return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            return self._json(400, {"error": str(e)})

    def do_PUT(self):
        if self._parsed()[0].startswith("/api/deployments/"):
            return self.do_POST()
        self._send(405, "", extra={"Allow": "GET, POST"})

    def do_DELETE(self):
        self._send(405, "", extra={"Allow": "GET, POST, PUT"})


def lighten(hex_colour, amount=0.45):
    """A lighter shade of the accent for dark mode."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b = (round(c + (255 - c) * amount) for c in (r, g, b))
    return "#%02x%02x%02x" % (r, g, b)


def render_page(brand, accent, terminal_url=""):
    import html
    if terminal_url and not re.fullmatch(r"https?://[^\s<>\"']+", terminal_url):
        terminal_url = ""
    return (PAGE.replace("{VERSION}", VERSION).replace("{BRAND_HTML}", html.escape(brand))
                .replace("{TERM_JSON}", json.dumps(terminal_url).replace("<", "\\u003c"))
                .replace("{BRAND_JSON}", json.dumps(brand))
                .replace("{ACCENT_DARK}", lighten(accent))
                .replace("{ACCENT}", accent))


def serve(args):
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    srv.store = Store(args.db, int(args.max_doc_mb * 1024 * 1024))
    srv.key = args.key
    srv.brand = args.name
    srv.accent = args.accent if re.fullmatch(r"#[0-9a-fA-F]{6}", args.accent or "") else "#3949ab"
    srv.gala_root = os.path.abspath(args.gala_data) if getattr(args, 'gala_data', None) else None
    if srv.gala_root:
        for f in GALA_FOLDERS:
            os.makedirs(os.path.join(srv.gala_root, f[1]), exist_ok=True)
    srv.page = render_page(srv.brand, srv.accent, args.terminal_url)
    sys.stderr.write("QA board v%s on http://%s:%d  (db: %s, docs: %s, max %s MB%s)\n" % (
        VERSION, args.host, args.port, args.db, srv.store.docdir, args.max_doc_mb,
        ", key required" if args.key else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------- stdio bridge

def bridge(args):
    """Relay newline-delimited JSON-RPC between stdin/stdout (Claude Desktop) and the board's /mcp."""
    url = args.url
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # LAN: never use a proxy
    session = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(url, data=line.encode("utf-8"), headers=headers, method="POST")
        try:
            with opener.open(req, timeout=120) as r:
                session = r.headers.get("Mcp-Session-Id") or session
                body = r.read().decode("utf-8").strip()
            if body:
                sys.stdout.write(json.dumps(json.loads(body), separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as e:  # board down / unreachable: answer requests so the client doesn't hang
            sys.stderr.write("qaboard bridge: %s\n" % e)
            if isinstance(msg, dict) and msg.get("id") is not None:
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32000, "message": "QA board unreachable at %s: %s" % (url, e)},
                }) + "\n")
                sys.stdout.flush()


# --------------------------------------------------------------------------- web page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{BRAND_HTML} · Gala Audit Portal</title>
<link rel="icon" href="/favicon.svg">
<style>
:root{--bg:#f6f6f3;--card:#fff;--fg:#1c1c1a;--muted:#6b6b66;--line:#e2e2dc;--accent:{ACCENT};
--open:#c2410c;--ans:#15803d;--res:#6b7280;--field:#fff;--drop:#eef0fb}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--card:#1f1f1d;--fg:#ececE8;--muted:#9a9a93;
--line:#34342f;--accent:{ACCENT_DARK};--open:#fb923c;--ans:#4ade80;--res:#9ca3af;--field:#191917;--drop:#23253a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:18px/1.5 system-ui,-apple-system,Segoe UI,Ubuntu,sans-serif}
main{max-width:1320px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:12px}
h1{font-size:24px;margin:0;line-height:1.15}
h1 span{display:block;font-size:15.6px;font-weight:500;color:var(--muted);letter-spacing:.02em}
.brand{display:flex;gap:10px;align-items:center}
.brand img{border-radius:8px;flex:0 0 auto}
h1 .ver{font-size:13.2px;font-weight:500;letter-spacing:0;text-transform:none;opacity:.75;margin-left:4px}
header{border-top:4px solid var(--accent);padding-top:12px}
.me{display:flex;gap:6px;align-items:center;color:var(--muted);font-size:16.8px}
input,textarea,select{font:inherit;color:var(--fg);background:var(--field);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;width:100%}
textarea{min-height:80px;resize:vertical}
.me input,.me select{width:160px;padding:5px 8px}
.forwhom{align-items:center}.forlabel{flex:0 0 auto!important}
.topills{display:flex;flex-wrap:wrap;gap:6px;flex:1 1 300px}
.topill{border-radius:99px;padding:4px 12px;font-size:16.8px}
.topill.on{background:var(--accent);border-color:var(--accent);color:#fff}
.topill .cl{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;background:var(--drop);color:var(--accent);border-radius:99px;padding:1px 6px;margin-right:6px;vertical-align:1px}
.topill.on .cl{background:rgba(255,255,255,.25);color:#fff}
.topill.any{border-style:dashed}
.urow{display:flex;align-items:center;gap:12px}.urow .main{flex:1;min-width:0}
.av{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-weight:700;background:var(--drop);color:var(--accent);flex:0 0 auto}
.av.cl{background:var(--accent);color:#fff;font-size:21.6px}
.ubadge{font-size:13.2px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--muted);margin-left:4px}
.ubadge.cl{border-color:var(--accent);color:var(--accent)}
.rmuser.danger{border-color:#e5484d;color:#e5484d}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;
padding:7px 14px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){button.primary{color:#111}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
details.new summary{cursor:pointer;font-weight:600}
.row{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}.row>*{flex:1 1 200px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 10px}
.tabs button{padding:4px 12px;font-size:16.8px}.tabs button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.views{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:14px}
.views button{border:0;border-bottom:3px solid transparent;border-radius:0;background:none;padding:8px 14px;font-weight:600;color:var(--muted)}
.views button.on{color:var(--fg);border-bottom-color:var(--accent)}
.q h3{margin:0;font-size:19.2px;cursor:pointer}
.meta{color:var(--muted);font-size:15.6px;margin-top:2px}
.pill{display:inline-block;font-size:14.4px;font-weight:600;border-radius:99px;padding:0 8px;margin-right:6px;border:1px solid}
.open{color:var(--open)}.answered{color:var(--ans)}.resolved{color:var(--res)}.on_hold{color:var(--muted);border-style:dashed!important}
.card.q.held{opacity:.8}
.card.q.dropover{outline:2px dashed var(--accent);outline-offset:2px;background:var(--drop)}
.attmsg{margin-right:auto;align-self:center}
.drafted{color:var(--accent);font-weight:600}
.body{white-space:pre-wrap;margin:10px 0 0;overflow-wrap:anywhere}
.md{white-space:normal;line-height:1.55}
.md p{margin:0 0 8px}
.md ul,.md ol{margin:2px 0 8px;padding-left:22px}
.md li{margin:2px 0}
.md li.sub{margin-left:20px;list-style-type:circle}
.md h4{margin:12px 0 4px;font-size:18px}
.md h4.h5,.md h4.h6{font-size:16.8px;color:var(--muted)}
.md blockquote{margin:4px 0 10px;padding:2px 0 2px 12px;border-left:3px solid var(--accent);color:var(--muted)}
.md blockquote p:last-child{margin-bottom:0}
.md hr{border:0;border-top:1px solid var(--line);margin:12px 0}
.md del{opacity:.7}
.md li.task{list-style:none;margin-left:-18px}
.md li.task input{width:auto;height:auto;min-height:0;padding:0;border:0;display:inline-block;margin:0 6px 0 0;vertical-align:-1px;accent-color:var(--accent)}
.md h4.h1{font-size:20.4px}
.md code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em;background:var(--drop);padding:1px 5px;border-radius:5px;overflow-wrap:anywhere}
.md pre{background:var(--drop);padding:8px 10px;border-radius:8px;overflow-x:auto;margin:4px 0 8px}
.md pre code{background:none;padding:0;white-space:pre}
.md .mdtable{overflow-x:auto;margin:4px 0 10px;max-width:100%}
.md table{border-collapse:collapse;font-size:.95em;font-variant-numeric:tabular-nums}
.md th,.md td{border:1px solid var(--line);padding:5px 10px;text-align:left;vertical-align:top}
.md th{background:var(--drop);font-weight:600}
.md tbody tr:nth-child(even) td{background:color-mix(in srgb,var(--drop) 40%,transparent)}
.md a.mdimg{display:block;margin:6px 0 8px;width:max-content;max-width:100%}
.md a.mdimg img{display:block;max-width:100%;max-height:480px;border:1px solid var(--line);border-radius:8px;background:#fff}
.md>:last-child{margin-bottom:0}
.md a{overflow-wrap:anywhere}
.ans{border-left:3px solid var(--line);padding:4px 0 4px 12px;margin-top:12px}
.ans.folded{cursor:pointer;margin-top:6px;padding:2px 0 2px 12px}
.ans.folded .meta{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ans.folded:hover{border-left-color:var(--accent)}
.ans.folded .peek{color:var(--fg);opacity:.75;margin-left:4px}
.ans .chev{display:inline-block;width:12px;text-decoration:none;color:var(--muted)}
.foldbar{font-size:15.6px;color:var(--muted);margin-top:10px}
.empty{color:var(--muted);text-align:center;padding:30px}
.err{color:var(--open);font-size:16.8px}
.ok{color:var(--ans);font-size:16.8px}
.forme{outline:2px solid var(--open);outline-offset:-1px}
.qfiles{align-items:center;gap:10px;border:1px dashed var(--line);border-radius:8px;padding:6px 10px}
.qfiles>*{flex:0 1 auto}.qfiles.over{background:var(--drop);border-color:var(--accent)}
.attachbtn{cursor:pointer;border:1px solid var(--line);border-radius:8px;padding:4px 12px;background:var(--card);font-size:16.8px;white-space:nowrap}
.livebox{border-radius:10px;overflow:hidden;border:1px solid var(--line)}
#livepre{margin:0;max-height:70vh;overflow:auto;background:#16141a;color:#e8e6ea;padding:12px 14px;
 font:15px/1.32 ui-monospace,SFMono-Regular,Menlo,"DejaVu Sans Mono",monospace;white-space:pre}
.topbar{position:fixed;top:0;left:0;right:0;margin:0 auto;max-width:1320px;z-index:20;background:var(--bg);padding:12px 16px 0}
/* the header and tabs are pinned (position:fixed, so nothing on the page can push them off the top);
   the page starts below them: --toph is the pinned bar's measured height */
main{padding-top:calc(var(--toph,160px) + 8px)}
html{scroll-padding-top:calc(var(--toph,160px) + 8px)}
.views{margin-bottom:0;background:var(--bg)}
main>section{padding-top:14px}
.brand{margin-right:auto}
.qdropzone{margin-top:8px;padding:12px}
.led{display:inline-block;width:10px;height:10px;border-radius:50%;background:#0e0e0e;border:1px solid rgba(160,160,160,.4)}
.led.flash{animation:ledflash .25s ease-out}
@keyframes ledflash{from{background:#39d96b;box-shadow:0 0 9px 2px rgba(57,217,107,.9)}to{background:#0e0e0e;box-shadow:none}}
.mini.alert{animation:attnpulse 1.2s ease-in-out infinite;border-color:#e5484d}
@keyframes attnpulse{0%,100%{box-shadow:0 0 0 1px #e5484d,0 0 4px 1px rgba(229,72,77,.35)}50%{box-shadow:0 0 0 3px #e5484d,0 0 14px 4px rgba(229,72,77,.75)}}
@media (prefers-reduced-motion:reduce){.mini.alert{animation:none;box-shadow:0 0 0 3px #e5484d}.led.flash{animation-duration:.01s}}
.livehead{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:0 0 14px}
#liveattn{margin-bottom:14px}
.livehead #livestate{margin:0;line-height:28px}
.livehead #openterm{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 12px;
 font-size:15.6px;font-weight:600;line-height:1;letter-spacing:0}
@media (prefers-color-scheme:dark){.livehead #openterm{color:#16121b}}
.deps{display:flex;flex-direction:column;gap:6px;padding:8px 0 2px}
.deprow{display:flex;align-items:center;gap:12px;width:100%;text-align:left;background:var(--card);border:1px solid var(--line);
 border-radius:10px;padding:7px 12px;cursor:pointer;font:inherit;font-size:15px;color:var(--fg)}
.deprow:hover{border-color:var(--accent)}
.depname{font-weight:600;white-space:nowrap}
.depbar{flex:1;min-width:80px;height:10px;border-radius:99px;background:var(--drop);overflow:hidden;position:relative}
.depbar i{position:absolute;inset:0 auto 0 0;border-radius:99px;background:var(--accent);transition:width .6s}
.s-running .depbar i{background-image:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);background-size:200% 100%;
 background-color:var(--accent);animation:depshine 1.6s linear infinite}
@keyframes depshine{from{background-position:200% 0}to{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){.s-running .depbar i{animation:none}}
.s-done .depbar i{background:var(--ans)}.s-failed .depbar i{background:#e5484d}.s-waiting .depbar i,.deprow.needs .depbar i{background:#d4a017}
.depstep{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:42%}
.deppct{font-variant-numeric:tabular-nums;color:var(--muted);min-width:3.2em;text-align:right}
.dstat{font-size:13px;font-weight:700;border-radius:99px;padding:0 8px;border:1px solid;white-space:nowrap}
.dstat.running{color:var(--accent)}.dstat.waiting,.dstat.needs{color:#b8860b}.dstat.done{color:var(--ans)}.dstat.failed{color:#e5484d}
.depcard.focus{outline:2px solid var(--accent);outline-offset:2px}
.dstat.hide{cursor:pointer}.dstat.hide:hover{background:var(--drop)}
.depcard h3{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 4px}
.depcard .depbar{height:12px;margin:10px 0 12px}
.depsteps{list-style:none;padding:0;margin:0;display:grid;gap:6px}
.depsteps li{display:grid;grid-template-columns:22px 1fr auto;gap:8px;align-items:baseline}
.depsteps .ic{text-align:center;font-weight:700}.depsteps .done .ic{color:var(--ans)}.depsteps .failed .ic{color:#e5484d}
.depsteps .running .ic{color:var(--accent)}.depsteps .waiting{color:var(--muted)}
.depsteps .when{color:var(--muted);font-size:14px;font-variant-numeric:tabular-nums}
.stages{grid-column:2/4;display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.stage{font-size:13px;border:1px solid var(--line);border-radius:6px;padding:1px 8px;background:var(--bg)}
.stage.done{color:var(--ans);border-color:var(--ans)}.stage.running{color:var(--accent);border-color:var(--accent);font-weight:600}
.stage.failed{color:#e5484d;border-color:#e5484d}.stage.waiting{color:var(--muted)}
.depmeta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:14px;color:var(--muted)}
.depmeta code{font-size:13px}
.deperr{margin-top:10px;border-left:3px solid #e5484d;padding:6px 10px;background:color-mix(in srgb,#e5484d 10%,transparent);border-radius:0 8px 8px 0}
.depwait{margin-top:10px;border-left:3px solid #d4a017;padding:6px 10px;background:color-mix(in srgb,#d4a017 12%,transparent);border-radius:0 8px 8px 0}
.mdshade{position:fixed;inset:0;z-index:109;background:rgba(20,20,24,.55)}
.mdwin{position:fixed;z-index:110;top:4vh;left:50%;transform:translateX(-50%);width:min(1000px,94vw);max-height:92vh;
 display:flex;flex-direction:column;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:12px;
 box-shadow:0 24px 70px rgba(0,0,0,.35);overflow:hidden}
.mdhead{display:flex;gap:14px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--bg)}
.mdtitles{flex:1;min-width:0}.mdtitles h3{margin:0;font-size:19px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mdhead a{color:var(--accent);font-size:15px;white-space:nowrap}
.mdhead button{flex:0 0 auto;border:0;background:none;font-size:20px;line-height:1;padding:6px 8px;cursor:pointer;color:var(--muted);border-radius:6px}
.mdhead button:hover{background:var(--drop);color:var(--fg)}
.mdbody{overflow:auto;padding:22px 34px 34px;line-height:1.65}
.mddoc h4{margin:1.2em 0 .5em;line-height:1.25;font-size:1.1em}
.mddoc h4.h1{font-size:1.9em;margin-top:.3em;padding-bottom:.25em;border-bottom:1px solid var(--line)}
.mddoc h4.h2{font-size:1.5em;padding-bottom:.2em;border-bottom:1px solid var(--line)}
.mddoc h4.h3{font-size:1.25em}
.mddoc p{margin:0 0 .9em}.mddoc ul,.mddoc ol{margin:0 0 .9em}.mddoc li{margin:3px 0}
.mddoc pre{margin:0 0 1em}.mddoc .mdtable{margin:0 0 1em}
body.mdopen{overflow:hidden}
@media (max-width:640px){.mdwin{top:0;width:100vw;max-height:100vh;height:100vh;border-radius:0}.mdbody{padding:16px}}
.termshade{position:fixed;inset:0;z-index:99;background:rgba(20,20,24,.55);backdrop-filter:blur(1px)}
body.termopen{overflow:hidden}
.termwin{position:fixed;z-index:100;display:flex;flex-direction:column;background:#202124;border-radius:10px;overflow:hidden;
 box-shadow:0 18px 50px rgba(0,0,0,.45),0 0 0 1px rgba(255,255,255,.08);min-width:360px;min-height:220px}
.termwin.max{left:8px!important;top:8px!important;width:calc(100vw - 16px)!important;height:calc(100vh - 16px)!important}
.termbar{display:flex;align-items:center;gap:8px;height:38px;padding:0 6px 0 10px;background:#2b2c30;cursor:move;user-select:none;flex:0 0 auto}
.termtab{display:flex;align-items:center;gap:7px;background:#3c3d42;color:#e8eaed;font:15.6px/1 system-ui,sans-serif;
 padding:7px 12px;border-radius:8px 8px 0 0;margin-top:8px;align-self:stretch;max-width:46%;overflow:hidden;white-space:nowrap}
.termtab span{overflow:hidden;text-overflow:ellipsis}
.termurl{flex:1;min-width:60px;background:#18191b;color:#bdc1c6;border:0;border-radius:99px;padding:5px 12px;font:14.4px/1 system-ui,sans-serif;
 text-align:left;cursor:text;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.termbtns{display:flex;gap:2px;flex:0 0 auto}
.termbtns a,.termbtns button{width:32px;height:28px;display:grid;place-items:center;border:0;border-radius:6px;background:transparent;
 color:#e8eaed;font:18px/1 system-ui,sans-serif;text-decoration:none;cursor:pointer;padding:0}
.termbtns a:hover,.termbtns button:hover{background:#3c3d42}
.termbtns .x:hover{background:#e5484d;color:#fff}
#termframe{flex:1;border:0;width:100%;background:#16141a}
.termwin.dragging #termframe{pointer-events:none}
.termresize{position:absolute;right:0;bottom:0;width:18px;height:18px;cursor:nwse-resize;
 background:linear-gradient(135deg,transparent 50%,rgba(255,255,255,.35) 50%,rgba(255,255,255,.35) 60%,transparent 60%,transparent 70%,rgba(255,255,255,.35) 70%,rgba(255,255,255,.35) 80%,transparent 80%)}
.termwin.max .termresize{display:none}
.attnacts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.attnlink{background:#e5484d;color:#fff!important;text-decoration:none;font-weight:600;border-radius:8px;padding:6px 12px}
.attncode b{font-family:ui-monospace,Menlo,monospace;letter-spacing:.06em;font-size:18px;user-select:all}
.attn{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:10px;padding:10px 12px;border-radius:8px;border:1px solid #e5484d;background:color-mix(in srgb,#e5484d 12%,var(--card));font-size:16.8px}
.mini{position:relative;width:210px;height:62px;padding:0;overflow:hidden;background:#16141a;border:1px solid var(--line);border-radius:8px;cursor:pointer;flex:0 0 auto}
.mini:hover{border-color:var(--accent)}
.mini pre{position:absolute;left:6px;right:6px;bottom:4px;margin:0;font:5.5px/1.25 ui-monospace,Menlo,monospace;color:#e8e6ea;white-space:pre;overflow:hidden;text-align:left;pointer-events:none}
.mini .led{position:absolute;top:5px;right:5px}
@media (max-width:640px){.mini{display:none}}
.warnbox{margin-top:10px;border-left:3px solid var(--open);background:var(--drop);padding:10px 12px;border-radius:0 8px 8px 0;font-size:16.8px}
.att{margin-top:8px;font-size:16.8px}.att a{margin-right:10px}
a{color:var(--accent)}
/* ---- Gala tab */
.galahead h2{margin:0 0 2px;font-size:1.3em}
.gh-top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start}
.steps{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.steps button{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:10px;text-align:left}
.steps button b{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--accent);color:#fff;font-size:.8em;flex:0 0 auto}
.steps button small{display:block;color:var(--muted);font-size:.8em}
.gfolders{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:10px}
.gfolder{margin:0}
.gfolder.over{outline:3px dashed var(--accent);outline-offset:-3px}
.gfolder h3{margin:0;font-size:1.05em;display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.gfolder h3 code{font-size:.75em;color:var(--muted);font-weight:400}
.gfiles{list-style:none;margin:8px 0 0;padding:0;max-height:260px;overflow:auto}
.gfiles li{display:flex;gap:8px;align-items:baseline;padding:3px 0;border-top:1px solid var(--line)}
.gfiles li a{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gfiles li .meta{flex:0 0 auto}
.gdrop{margin-top:8px;padding:10px;font-size:.92em}
.gbar{height:10px;border-radius:6px;background:var(--line);overflow:hidden;flex:1;min-width:120px}
.gbar i{display:block;height:100%;background:var(--accent)}
.gscan{display:flex;gap:12px;align-items:center;padding:6px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.gscan:first-of-type{border-top:0}
.gscan .nm{flex:0 0 280px;font-weight:600}
.gscan .ct{flex:0 0 auto;min-width:150px;text-align:right}
.gscan .miss{flex-basis:100%;font-size:.85em}
.gwarn{color:#b45309}

.drop{border:2px dashed var(--line);border-radius:12px;padding:16px;text-align:center;color:var(--muted)}
.drop.over{background:var(--drop);border-color:var(--accent)}
.doc{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.doc .main{flex:1 1 300px;min-width:0}
.doc h3{margin:0;font-size:19.2px;overflow-wrap:anywhere}
.doc .acts{display:flex;gap:6px;flex-wrap:wrap}
.doc .acts a,.doc .acts button{font-size:15.6px;padding:4px 10px;border:1px solid var(--line);border-radius:8px;text-decoration:none;background:var(--card);color:var(--fg)}
.badge{font-size:14.4px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--muted);margin-left:6px}
.vers{font-size:15.6px;color:var(--muted);margin-top:6px}
[hidden]{display:none!important}
</style></head>
<body><main>
<div class="topbar">
<header><div class="brand"><img src="/favicon.svg" alt="" width="34" height="34"><h1>{BRAND_HTML}<span>Gala Audit Portal <small class="ver">v{VERSION}</small></span></h1></div>
<button class="mini" id="mini" hidden title="Claude's console - open the Live view" aria-label="Open the Live view"><pre id="minipre"></pre><span class="led"></span></button>
<label class="me">I am <select id="me"><option value="">choose…</option></select></label></header>
<nav class="views" id="views"><button data-v="gala">Gala</button><button data-v="questions">Questions</button><button data-v="docs">Documents</button><button data-v="live">Live</button><button data-v="deploys">Deploys</button><button data-v="users">Users</button></nav>
<div class="deps" id="deps" hidden></div>
</div>

<section id="v-gala" hidden>
<div class="card galahead">
 <div class="gh-top"><div><h2>Gala audit</h2><div class="meta" id="g-root"></div></div>
 <div class="meta" id="g-tools"></div></div>
 <div class="steps" id="g-steps"></div>
 <div class="meta" style="margin-top:8px">Each button opens the Ask form with a request for <b>gala-claude</b>; change it as you like, then Post.
 Claude picks it up straight away and answers on the Questions tab. Watch it work on the Live tab.</div>
</div>
<div class="card" id="g-progress"></div>
<div class="gfolders" id="g-folders"></div>
</section>
<section id="v-questions">
<details class="card new" id="newbox"><summary>Ask a question</summary>
<div class="row"><input id="title" placeholder="Question (one line)"></div>
<div class="row"><textarea id="qbody" placeholder="Details, context, links, code"></textarea></div>
<div class="drop qdropzone" id="qdrop">Drop files here, or <label style="color:var(--accent);cursor:pointer;text-decoration:underline">choose files<input type="file" id="qfile" multiple hidden></label>
<div class="meta" id="qfilelist">They're attached to the question when you post it. You can also paste a screenshot (Ctrl+V).</div></div>
<div class="row forwhom"><span class="meta forlabel">For whom?</span><div class="topills" id="topills"></div><input type="hidden" id="to">
<button class="primary" id="post" style="flex:0 0 auto">Post</button></div>
<div class="warnbox" id="anywarn" hidden>
  <b>This question is for anyone.</b> Nobody in particular will be asked to answer it, and Claudes only pick it up if they choose to. Is that right?
  <div class="row" style="justify-content:flex-start"><button class="primary" id="postany" style="flex:0 0 auto">Post to anyone</button>
  <button id="chooseto" style="flex:0 0 auto">Choose someone</button></div>
</div>
<div class="err" id="perr"></div></details>
<div class="tabs" id="tabs"></div>
<div id="list"></div>
</section>

<section id="v-deploys" hidden>
<div class="meta" style="margin-bottom:10px">Deployments reported by Claude (deployment_progress) or a script (PUT /api/deployments/ID). The latest 20 are kept here.</div>
<div id="deplist"><div class="empty">No deployments yet.</div></div>
</section>
<section id="v-users" hidden>
<div class="card"><div class="row" style="margin-top:0"><input id="newuser" placeholder="Name, e.g. alice or bob-claude" autocomplete="off">
<select id="newkind" style="flex:0 0 130px"><option value="person">Person</option><option value="claude">Claude</option></select>
<button class="primary" id="adduser" style="flex:0 0 auto">Add user</button></div>
<div class="meta" id="usermsg" style="margin-top:6px">People choose themselves next to "I am". Claudes are listed first under "For whom?".</div></div>
<div id="userlist"></div>
</section>

<section id="v-live" hidden>
<div class="attn" id="liveattn" hidden><span id="liveattntext"></span>
  <span class="attnacts"><a class="attnlink" id="liveopen" target="_blank" rel="noopener noreferrer" hidden>Open link &#8599;</a>
  <span class="attncode" id="livecode" hidden>Code <b id="livecodetext"></b> <button id="livecopy">Copy</button></span>
  <button id="liveclear" hidden>Mark as handled</button></span></div>
<div class="livehead"><div class="meta" id="livestate">Loading…</div>
<button id="openterm" class="primary" title="Open the Claude Code web terminal in a window over the board">Open terminal</button></div>
<div class="livebox" id="livebox" hidden><pre id="livepre"></pre></div>
<div class="meta" style="margin-top:8px">Read-only copy of the Claude Code console on the server, refreshed every 2 seconds. To type to Claude, use the web terminal.</div>
</section>

<section id="v-docs" hidden>
<div class="card">
  <div class="drop" id="drop">Drop files here, or <label style="color:var(--accent);cursor:pointer;text-decoration:underline">choose files<input type="file" id="file" multiple hidden></label>
    <div class="meta" id="maxmb"></div></div>
  <div class="row"><input id="dtitle" placeholder="Title (optional; default: file name)">
  <input id="dq" placeholder="Attach to question # (optional)" inputmode="numeric" style="flex:0 1 220px"></div>
  <div class="row"><input id="ddesc" placeholder="Description (optional)"></div>
  <div id="uploadmsg" class="meta"></div>
</div>
<div class="row" style="margin-bottom:10px"><input id="dsearch" placeholder="Search documents" type="search">
<label class="meta" style="flex:0 0 auto;display:flex;gap:6px;align-items:center"><input type="checkbox" id="darch" style="width:auto"> show archived</label></div>
<div id="docs"></div>
<input type="file" id="verfile" hidden><input type="file" id="attfile" multiple hidden>
</section>
<datalist id="people"></datalist>
<div class="mdshade" id="mdshade" hidden></div>
<div class="mdwin" id="mdwin" hidden role="dialog" aria-modal="true" aria-labelledby="mdtitle">
 <div class="mdhead"><div class="mdtitles"><h3 id="mdtitle">Document</h3><div class="meta" id="mdmeta"></div></div>
  <a id="mdtext" target="_blank" rel="noopener">Open as text</a><a id="mddl">Download</a>
  <button id="mdclose" title="Close (Esc)" aria-label="Close">&#10005;</button></div>
 <div class="mdbody md mddoc" id="mdbody"></div>
</div>
<div class="termshade" id="termshade" hidden title="Close the terminal window to use the board"></div>
<div class="termwin" id="termwin" hidden role="dialog" aria-label="Claude Code terminal">
 <div class="termbar" id="termbar">
  <div class="termtab"><img src="/favicon.svg" alt="" width="14" height="14"><span>{BRAND_HTML} &middot; Claude Code</span></div>
  <button class="termurl" id="termurl" title="Change the terminal's address"></button>
  <span class="termbtns"><a id="termnew" target="_blank" rel="noopener" title="Open in its own browser tab">&#8599;</a>
  <button id="termmax" title="Maximise">&#9634;</button><button id="termclose" class="x" title="Close (Claude keeps running)">&#10005;</button></span>
 </div>
 <iframe id="termframe" title="Claude Code terminal" src="about:blank" allow="clipboard-read; clipboard-write"></iframe>
 <div class="termresize" id="termresize" title="Drag to resize"></div>
</div>
</main>
<script>
// keep the page clear of the pinned header, whatever its height (brand, mini console, narrow windows)
(()=>{const t=document.querySelector('.topbar'),set=()=>document.documentElement.style.setProperty('--toph',t.offsetHeight+'px');set();if(window.ResizeObserver)new ResizeObserver(set).observe(t);addEventListener('resize',set)})();
const KEY=new URLSearchParams(location.search).get('key');
const BRAND={BRAND_JSON};
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function store(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}
function url(path){const u=new URL(path,location.origin);if(KEY)u.searchParams.set('key',KEY);return u}
async function api(path,body){
 const r=await fetch(url(path),body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j}
function ago(t){const s=Date.now()/1000-t;if(s<60)return'just now';if(s<3600)return Math.floor(s/60)+'m ago';
 if(s<86400)return Math.floor(s/3600)+'h ago';return new Date(t*1000).toLocaleDateString()}
function size(n){return n<1024?n+' B':n<1048576?(n/1024).toFixed(0)+' KB':(n/1048576).toFixed(1)+' MB'}
const me=()=>($('#me').value.trim().toLowerCase().replace(/\s+/g,'-')||'');
$('#me').onchange=()=>{store('qa-me',$('#me').value);refresh()};

// ---- Gala tab: the data folder, OCR progress and one-click requests for gala-claude
const GALA_STEPS=[
 ['Check the files','what is here, what is missing',
  'Check the gala files',
  'Look through the data folder (data/) and tell me what is there for this gala: which scans, Meet Manager and Gen7 PDFs and meet files you found, how many pages each scan has, how much of the OCR cache is already done (run validate_cache.py), and what is missing before an audit can be run. Do not start transcribing yet.'],
 ['Read the lane sheets','gala-ocr skill',
  'Transcribe the lane sheets',
  'Use the gala-ocr skill to transcribe the lane sheet scan(s) in data/pdf/scanned into data/ocr. Skip pages that are already done. Report progress with deployment_progress (one step per event or per 10 pages), validate the cache when finished, and ask me on the board about anything you cannot read.'],
 ['Read the DQ slips','gala-ocr skill',
  'Read the DQ slips',
  'Use the gala-ocr skill to read the DQ slips scan in data/pdf/scanned into data/ocr (pair fronts and backs, one JSON per slip). Skip slips already done. Report progress with deployment_progress and ask me about anything unclear.'],
 ['Read the ETD programme','gala-ocr skill',
  'Read the ETD programme',
  'Use the gala-ocr skill to read the ETD programme scan in data/pdf/scanned into data/ocr. Skip pages already done.'],
 ['Gen7 session report','gen7-report skill',
  'Extract the Gen7 session report',
  'Use the gen7-report skill to turn the Colorado Gen7 session report PDF in data/pdf/original into JSON/CSV in data/reports, and summarise what it contains (sessions, events, heats).'],
 ['Assign DQ codes','gala-audit skill',
  'Assign the DQ codes',
  'Use the gala-audit skill to assign FINA DQ codes to the DQ slips (write data/ocr/dq-codes.json). Ask me on the board about any slip where the code is not clear.'],
 ['Build the audit report','gala-audit skill',
  'Build the audit report',
  'Use the gala-audit skill to build the HTML audit report from data/ocr and the Gen7 session report, into data/reports/ (name it after the gala and today\'s date). Include the DQ codes and the scans. Then summarise the findings in your answer and link the report.'],
 ['Write the merged results','gala-audit skill',
  'Write the merged results .hy3',
  'Use the gala-audit skill (merge_results.py) to write a merged results .hy3 from the OCR, the Gen7 report and the entries/results .hy3 in data/meet, into data/reports/. Tell me exactly what was changed and anything you were unsure of.'],
 ['Final audit','gala-audit skill',
  'Run the final audit',
  'Use the gala-audit skill (final_audit.py) to run the final audit of the merged results against the scans and Gen7, write the report to data/reports/ and summarise anything that still needs a person.'],
];
function galaAsk(i){
 const s=GALA_STEPS[i];showView('questions');const nb=$('#newbox');nb.open=true;
 $('#title').value=s[2];$('#qbody').value=s[3];draftSet('ask-title',s[2]);draftSet('ask-body',s[3]);
 const c=USERS.find(u=>u.name==='gala-claude');if(c){$('#to').value=c.name;toTouched=true;renderToPills()}
 nb.scrollIntoView({behavior:'smooth',block:'start'});$('#title').focus()}
function galaHref(rel,dl){const u=url('/api/gala/file');u.searchParams.set('path',rel);if(dl)u.searchParams.set('dl','1');return u.toString()}
let galaSig='';
async function loadGala(){
 if(!$('#g-steps').childElementCount)$('#g-steps').innerHTML=GALA_STEPS.map((s,i)=>`<button type="button" data-step="${i}"><b>${i+1}</b><span>${esc(s[0])}<small>${esc(s[1])}</small></span></button>`).join('');
 let j;try{j=await api('/api/gala/overview')}catch(e){$('#g-root').textContent='Could not load the data folder: '+e.message;return}
 const sig=JSON.stringify(j);if(sig===galaSig)return;galaSig=sig;
 if(!j.root){$('#g-root').textContent='This portal was started without a data folder (--gala-data).';return}
 $('#g-root').innerHTML='Data folder: <code>'+esc(j.root)+'</code>';
 const miss=Object.entries(j.tools||{}).filter(([k,v])=>!v).map(([k])=>k);
 $('#g-tools').innerHTML=miss.length?'<span class="gwarn">Missing: '+esc(miss.join(', '))+' (install poppler-utils)</span>':'poppler ✓';
 // progress
 const sc=j.scans||[];
 $('#g-progress').innerHTML='<h3 style="margin:0 0 6px">OCR progress</h3>'+(sc.length?sc.map(x=>{
  const pct=x.pages?Math.round(100*x.done/x.pages):0;
  return `<div class="gscan"><span class="nm">${esc(x.name)} <span class="meta">${x.kind==='dq'?'DQ slips':x.kind==='etd'?'ETD':'lane sheets'}</span></span>
   <span class="gbar" title="${pct}%"><i style="width:${pct}%"></i></span>
   <span class="ct">${x.pages==null?'? pages':`${x.done} / ${x.pages} pages`} · ${x.files} JSON${x.review?` · <span class="gwarn">${x.review} to check</span>`:''}${x.bad?` · <span class="gwarn">${x.bad} unreadable</span>`:''}</span>
   ${x.missingCount&&x.done?`<span class="miss meta">Not done yet: pages ${esc(x.missing.join(', '))}${x.missingCount>x.missing.length?' …':''}</span>`:''}</div>`}).join('')
  :'<div class="meta">Drop the scanned PDFs (Lane Sheets.pdf, DQs.pdf, ETD.pdf) on <b>Scanned paper</b> below.</div>');
 // folders
 $('#g-folders').innerHTML=j.folders.map(f=>{
  const files=f.files.filter(x=>!x.dir);
  const list=files.length?'<ul class="gfiles">'+files.map(x=>{const rel=f.path+'/'+x.name;
   return `<li><a href="${esc(galaHref(rel))}" target="_blank" rel="noopener" title="${esc(x.name)}">${esc(x.name)}</a><span class="meta">${size(x.size)} · ${agoShort(x.mtime)}</span><a class="meta" style="flex:0 0 auto" href="${esc(galaHref(rel,1))}" title="Download">⤓</a></li>`}).join('')+'</ul>'
   :'<div class="meta" style="margin-top:8px">Empty.</div>';
  const more=f.key==='ocr'&&f.count>files.length?`<div class="meta">Newest ${files.length} of ${f.count} files.</div>`:'';
  const drop=f.key==='ocr'?'':`<div class="drop gdrop">Drop files here, or <label style="color:var(--accent);cursor:pointer;text-decoration:underline">choose files<input type="file" multiple hidden data-gup="${esc(f.key)}"${f.types?` accept="${esc(f.types)}"`:''}></label><div class="meta" data-gmsg></div></div>`;
  return `<div class="card gfolder" data-gfolder="${esc(f.key)}"><h3><span>${esc(f.title)} <span class="meta">(${f.count})</span></span><code>data/${esc(f.path)}</code></h3>
   <div class="meta">${esc(f.hint)}</div>${list}${more}${drop}</div>`}).join('')}
async function galaUpload(folder,files,card){
 const msg=card&&card.querySelector('[data-gmsg]');const say=t=>{if(msg)msg.textContent=t};
 for(const f of files){
  say('Uploading '+f.name+'…');
  const send=async rep=>{const u=url('/api/gala/upload');u.searchParams.set('folder',folder);u.searchParams.set('filename',f.name);if(rep)u.searchParams.set('replace','1');
   return fetch(u,{method:'POST',body:f})};
  let r=await send(false);
  if(r.status===409){if(!confirm(f.name+' is already there. Replace it? (The old copy is kept as a hidden .previous file.)')){say('Skipped '+f.name);continue}r=await send(true)}
  const jj=await r.json().catch(()=>({}));
  if(!r.ok){say('Could not upload '+f.name+': '+(jj.error||r.status));return}
 }
 say('Uploaded '+files.length+' file'+(files.length>1?'s':'')+'.');galaSig='';loadGala()}
$('#g-steps').onclick=e=>{const b=e.target.closest('[data-step]');if(b)galaAsk(+b.dataset.step)};
$('#g-folders').addEventListener('change',e=>{const i=e.target.closest('[data-gup]');if(i&&i.files.length)galaUpload(i.dataset.gup,[...i.files],i.closest('.gfolder'))});
$('#g-folders').addEventListener('dragover',e=>{const c=e.target.closest('.gfolder');if(!c||c.dataset.gfolder==='ocr')return;e.preventDefault();c.classList.add('over')});
$('#g-folders').addEventListener('dragleave',e=>{const c=e.target.closest('.gfolder');if(c&&!c.contains(e.relatedTarget))c.classList.remove('over')});
$('#g-folders').addEventListener('drop',e=>{const c=e.target.closest('.gfolder');if(!c||c.dataset.gfolder==='ocr')return;e.preventDefault();c.classList.remove('over');
 const fs=[...(e.dataTransfer.files||[])];if(fs.length)galaUpload(c.dataset.gfolder,fs,c)});

// ---- views
let view=['#gala','#questions','#docs','#live','#users','#deploys'].includes(location.hash)?location.hash.slice(1):(store('qa-view')||'gala');
// ?me=NAME (from start-portal.sh) picks "I am" the first time
{const m=new URLSearchParams(location.search).get('me');if(m&&!store('qa-me'))store('qa-me',m)}
function showView(v){view=v;store('qa-view',v);if(v!=='deploys')focusDep=null;
 document.querySelectorAll('#views button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
 $('#v-gala').hidden=v!=='gala';$('#v-questions').hidden=v!=='questions';$('#v-docs').hidden=v!=='docs';$('#v-live').hidden=v!=='live';$('#v-users').hidden=v!=='users';$('#v-deploys').hidden=v!=='deploys';refresh()}
$('#views').onclick=e=>{const b=e.target.closest('button');if(b&&b.dataset.v)showView(b.dataset.v)};
$('#mini').onclick=()=>{showView('live');window.scrollTo(0,0)};
function refresh(){loadUsers();view==='gala'?loadGala():view==='docs'?loadDocs():view==='live'?loadLive():view==='users'?null:view==='deploys'?loadDeps():load()}
setInterval(()=>{if(!document.hidden)loadLive()},2000);

// ---- live console (sent by the board watcher on the server, when it runs with --live)
const ANSI16=['#1e1e1e','#e5534b','#57ab5a','#c69026','#539bf5','#b083f0','#39c5cf','#d0d0d0',
 '#636e7b','#ff938a','#6bc46d','#daaa3f','#6cb6ff','#dcbdfb','#56d4dd','#ffffff'];
function ansi256(n){if(n<16)return ANSI16[n];if(n>=232){const v=8+(n-232)*10;return `rgb(${v},${v},${v})`}
 n-=16;const c=[0,95,135,175,215,255];return `rgb(${c[Math.floor(n/36)]},${c[Math.floor(n/6)%6]},${c[n%6]})`}
function ansiToHtml(text){
 let st={},out='',open=false;const rx=/\x1b\[([0-9;]*)m/g;let last=0,m;
 const style=()=>{const s=[];if(st.fg)s.push('color:'+st.fg);if(st.bg)s.push('background:'+st.bg+';padding:.08em 0');if(st.b)s.push('font-weight:700');
  if(st.d)s.push('opacity:.6');if(st.i)s.push('font-style:italic');if(st.u)s.push('text-decoration:underline');if(st.inv)s.push('filter:invert(1)');return s.join(';')};
 const emit=t=>{if(!t)return;t=esc(t.replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b./g,''));const s=style();out+=s?`<span style="${s}">${t}</span>`:t};
 while((m=rx.exec(text))){emit(text.slice(last,m.index));last=rx.lastIndex;
  const p=m[1]===''?[0]:m[1].split(';').map(Number);
  for(let i=0;i<p.length;i++){const c=p[i];
   if(c===0)st={};else if(c===1)st.b=1;else if(c===2)st.d=1;else if(c===3)st.i=1;else if(c===4)st.u=1;else if(c===7)st.inv=1;
   else if(c===22){st.b=0;st.d=0}else if(c===23)st.i=0;else if(c===24)st.u=0;else if(c===27)st.inv=0;
   else if(c>=30&&c<=37)st.fg=ANSI16[c-30];else if(c>=90&&c<=97)st.fg=ANSI16[c-82];else if(c===39)st.fg=null;
   else if(c>=40&&c<=47)st.bg=ANSI16[c-40];else if(c>=100&&c<=107)st.bg=ANSI16[c-92];else if(c===49)st.bg=null;
   else if((c===38||c===48)&&p[i+1]===5){st[c===38?'fg':'bg']=ansi256(p[i+2]);i+=2}
   else if((c===38||c===48)&&p[i+1]===2){st[c===38?'fg':'bg']=`rgb(${p[i+2]},${p[i+3]},${p[i+4]})`;i+=4}}}
 emit(text.slice(last));return out}
let liveTs=0,liveChanged=0,lastFlash=0;
function agoShort(t){const a=Date.now()/1000-t;return a<5?'just now':a<90?Math.round(a)+'s ago':ago(t)}
// The mini box's light flashes green whenever the screen changes and fades to black in 1/4 s,
// so steady activity shows as a pulse. Its border pulses red when the session needs someone.
function flashLed(){const el=$('#mini .led');if(!el)return;el.classList.remove('flash');void el.offsetWidth;el.classList.add('flash')}
function attentionText(j){
 if(j.attention)return esc(j.attention.reason)+' <span class="meta">('+esc(j.attention.by)+', '+agoShort(j.attention.ts)+')</span>';
 if(j.prompt)return 'Claude is waiting at a question or permission prompt on its screen';
 return ''}
// Polled on every tab: feeds the header's mini screen; the Live tab gets the full text.
async function loadLive(){
 const full=view==='live';
 try{const j=await api('/api/live'+(full?'':'?lines=40'));
  const fresh=j.ts&&Date.now()/1000-j.ts<90;
  const changed=j.ts?(j.changed||j.ts):0;
  if(changed&&changed!==lastFlash){if(lastFlash)flashLed();lastFlash=changed}
  liveChanged=changed;
  const mini=$('#mini');mini.hidden=!j.ts;
  const need=!!(j.attention||(fresh&&j.prompt));
  mini.classList.toggle('alert',need);
  mini.title=need?'Needs you: '+(j.attention?j.attention.reason+(j.attention.url?' (link on the Live view)':''):'Claude is waiting at a prompt on its screen')+' - click to open the Live view':'Claude\'s console (click to open the Live view)';
  if(j.ts)$('#minipre').innerHTML=ansiToHtml((j.text||'').split('\n').slice(-16).join('\n'));
  if(!full)return;
  const box=$('#livebox'),st=$('#livestate'),att=$('#liveattn');
  att.hidden=!need;
  if(need){$('#liveattntext').innerHTML='<b>Needs you:</b> '+attentionText(j);$('#liveclear').hidden=!j.attention;
   const a=j.attention||{},lk=$('#liveopen'),cd=$('#livecode');
   lk.hidden=!a.url;if(a.url){lk.href=a.url;lk.title=a.url}
   cd.hidden=!a.code;if(a.code)$('#livecodetext').textContent=a.code}
  if(!j.ts){st.textContent='Nothing yet. The live view appears when the board watcher on the server runs with --live.';box.hidden=true;return}
  st.innerHTML=(!fresh?'<b class="err">Not updating</b> (last update '+ago(j.ts)+'). The watcher may be stopped.'
   :(j.busy?'<b>Claude is working</b>':'Claude is idle')+' · screen last changed '+agoShort(changed))+(j.title?' · '+esc(j.title):'');
  if(changed!==liveTs){liveTs=changed;const pre=$('#livepre');const atBottom=box.hidden||pre.scrollTop+pre.clientHeight>=pre.scrollHeight-30;
   box.hidden=false;pre.innerHTML=ansiToHtml(j.text||'');if(atBottom)pre.scrollTop=pre.scrollHeight}
 }catch(e){if(full)$('#livestate').textContent='Could not reach the board: '+e.message}}
$('#livecopy').onclick=()=>{const t=$('#livecodetext').textContent;const b=$('#livecopy');
 const done=m=>{b.textContent=m;setTimeout(()=>b.textContent='Copy',1500)};
 try{navigator.clipboard.writeText(t).then(()=>done('Copied'),()=>done('Select it'))}catch(e){done('Select it')}};
$('#liveclear').onclick=async()=>{try{await api('/api/attention',{clear:true,by:me()||'someone'});loadLive()}catch(e){$('#livestate').textContent=e.message}};


// ---- users: the board's team list. People pick themselves under "I am"; everyone appears in "For whom?"
let USERS=[],usersSig='';
const isClaude=u=>u.kind==='claude';
function sortUsers(list){return [...list].sort((a,b)=>(a.name==='dev-claude'?-1:b.name==='dev-claude'?1:0)||(isClaude(b)-isClaude(a))||a.name.localeCompare(b.name))}
async function loadUsers(){
 try{const j=await api('/api/users');const sig=JSON.stringify(j.users);if(sig===usersSig&&view!=='users')return;usersSig=sig;
  USERS=sortUsers(j.users);renderMe();renderToPills();if(view==='users')renderUsers()}catch(e){}}
function renderMe(){
 const sel=$('#me'),cur=sel.value||store('qa-me')||'';const people=USERS.filter(u=>!isClaude(u));
 sel.innerHTML='<option value="">choose…</option>'+people.map(u=>`<option value="${esc(u.name)}">${esc(u.name)}</option>`).join('');
 sel.value=people.some(u=>u.name===cur)?cur:''}
// New questions are for the board's Claude by default (dev-claude, else the first Claude in the list),
// until you pick someone else or clear the choice.
let toTouched=false;
function defaultTo(){const c=USERS.find(u=>u.name==='gala-claude')||USERS.find(u=>u.name==='dev-claude')||USERS.find(isClaude);return c?c.name:''}
function renderToPills(){
 if(!$('#to').value&&!toTouched)$('#to').value=defaultTo();
 const cur=$('#to').value,box=$('#topills');
 box.innerHTML=USERS.map(u=>`<button type="button" class="topill${u.name===cur?' on':''}" data-to="${esc(u.name)}" aria-pressed="${u.name===cur}">${isClaude(u)?'<span class="cl">Claude</span>':''}${esc(u.name)}</button>`).join('')
  +`<button type="button" class="topill any${cur==='anyone'?' on':''}" data-to="anyone" aria-pressed="${cur==='anyone'}">anyone</button>`
  +(USERS.length?'':'<span class="meta">Add people in the Users tab.</span>')}
$('#topills').onclick=e=>{const b=e.target.closest('button');if(!b)return;toTouched=true;
 $('#to').value=b.dataset.to===$('#to').value?'':b.dataset.to;$('#anywarn').hidden=true;renderToPills()};
function renderUsers(){
 const list=$('#userlist');
 if(!USERS.length){list.innerHTML='<div class="empty">No users yet. Add the team above.</div>';return}
 list.innerHTML=USERS.map(u=>{const cl=isClaude(u),dev=u.name==='dev-claude';
  return `<div class="card urow"><div class="av${cl?' cl':''}" aria-hidden="true">${cl?'&#10035;':esc(u.name[0].toUpperCase())}</div>
  <div class="main"><b>${esc(u.name)}</b> <span class="ubadge${cl?' cl':''}">${cl?'Claude':'Person'}</span>
  <div class="meta">${dev?'Claude Code on the server. It answers questions on this board and is listed first.':cl?'A Claude assistant (e.g. in someone\'s Claude Desktop).':'Picks themselves next to "I am".'}</div></div>
  <button class="rmuser" data-rm="${esc(u.name)}">Remove</button></div>`}).join('')}
$('#userlist').onclick=async e=>{const b=e.target.closest('button[data-rm]');if(!b)return;
 if(!b.dataset.confirm){b.dataset.confirm='1';b.textContent='Remove '+b.dataset.rm+'?';b.classList.add('danger');
  setTimeout(()=>{if(b.isConnected){delete b.dataset.confirm;b.textContent='Remove';b.classList.remove('danger')}},4000);return}
 try{await api('/api/users/'+encodeURIComponent(b.dataset.rm)+'/delete',{});usermsg('Removed '+b.dataset.rm+'. Their questions and answers stay on the board.');usersSig='';loadUsers()}
 catch(err){usermsg(err.message,true)}};
function usermsg(t,bad){const m=$('#usermsg');m.className=bad?'err':'ok';m.textContent=t}
$('#adduser').onclick=async()=>{const n=$('#newuser').value.trim();if(!n)return usermsg('Type a name first.',true);
 try{const u=await api('/api/users',{name:n,kind:$('#newkind').value});$('#newuser').value='';usermsg('Added '+u.name+'.');usersSig='';loadUsers()}
 catch(err){usermsg(err.message,true)}};
$('#newuser').onkeydown=e=>{if(e.key==='Enter')$('#adduser').click()};

// ---- floating terminal window: the Claude Code web terminal (ttyd) in an iframe
const TERM_DEFAULT={TERM_JSON};
const termUrl=()=>store('qa-term')||TERM_DEFAULT;
function termGeom(){try{return JSON.parse(store('qa-termbox')||'null')}catch(e){return null}}
function saveGeom(){const w=$('#termwin');if(w.classList.contains('max'))return;
 store('qa-termbox',JSON.stringify({l:w.offsetLeft,t:w.offsetTop,w:w.offsetWidth,h:w.offsetHeight}))}
function placeTerm(){const w=$('#termwin'),g=termGeom();
 const W=Math.min(innerWidth-24,g?g.w:Math.round(innerWidth*.8)),H=Math.min(innerHeight-24,g?g.h:Math.round(innerHeight*.72));
 w.style.width=W+'px';w.style.height=H+'px';
 w.style.left=Math.max(8,Math.min(innerWidth-W-8,g?g.l:Math.round((innerWidth-W)/2)))+'px';
 w.style.top=Math.max(8,Math.min(innerHeight-60,g?g.t:Math.round((innerHeight-H)/2)))+'px'}
function openTerm(){const w=$('#termwin');
 if(!termUrl()){changeTermUrl();if(!termUrl())return}
 if(w.hidden){placeTerm();$('#termframe').src=termUrl();$('#termurl').textContent=termUrl().replace(/^https?:\/\//,'');
  $('#termnew').href=termUrl();w.hidden=false;$('#termshade').hidden=false;document.body.classList.add('termopen')}
 $('#termframe').focus()}
function closeTerm(){$('#termframe').src='about:blank';$('#termwin').hidden=true;$('#termshade').hidden=true;document.body.classList.remove('termopen')}
function changeTermUrl(){const v=prompt('Address of this board\'s Claude Code terminal (as your browser reaches it, e.g. through the SSH tunnel):',termUrl());
 if(v===null)return;const t=v.trim();
 if(t&&!/^https?:\/\/[^\s]+$/.test(t)){alert('Use an address like http://127.0.0.1:17681');return}
 store('qa-term',t&&t!==TERM_DEFAULT?t:'');if(!$('#termwin').hidden){$('#termframe').src=termUrl();$('#termurl').textContent=termUrl().replace(/^https?:\/\//,'');$('#termnew').href=termUrl()}}
$('#openterm').onclick=openTerm;
$('#termclose').onclick=closeTerm;
$('#termmax').onclick=()=>{const w=$('#termwin');w.classList.toggle('max');$('#termmax').title=w.classList.contains('max')?'Restore':'Maximise';if(!w.classList.contains('max'))placeTerm()};
$('#termurl').onclick=changeTermUrl;
$('#termbar').ondblclick=e=>{if(!e.target.closest('button,a,#termurl'))$('#termmax').click()};
// drag by the title bar, resize from the corner (the iframe ignores the pointer meanwhile)
let tdrag=null;
function startDrag(e,kind){const w=$('#termwin');if(w.classList.contains('max'))return;
 tdrag={kind,x:e.clientX,y:e.clientY,l:w.offsetLeft,t:w.offsetTop,w:w.offsetWidth,h:w.offsetHeight};
 e.currentTarget.setPointerCapture(e.pointerId);w.classList.add('dragging');e.preventDefault()}
function moveDrag(e){if(!tdrag)return;const w=$('#termwin'),dx=e.clientX-tdrag.x,dy=e.clientY-tdrag.y;
 if(tdrag.kind==='move'){w.style.left=Math.max(-tdrag.w+120,Math.min(innerWidth-120,tdrag.l+dx))+'px';w.style.top=Math.max(0,Math.min(innerHeight-40,tdrag.t+dy))+'px'}
 else{w.style.width=Math.max(360,tdrag.w+dx)+'px';w.style.height=Math.max(220,tdrag.h+dy)+'px'}}
function endDrag(){if(!tdrag)return;tdrag=null;$('#termwin').classList.remove('dragging');saveGeom()}
$('#termbar').addEventListener('pointerdown',e=>{if(!e.target.closest('button,a,#termurl'))startDrag(e,'move')});
$('#termresize').addEventListener('pointerdown',e=>startDrag(e,'size'));
for(const el of [$('#termbar'),$('#termresize')]){el.addEventListener('pointermove',moveDrag);el.addEventListener('pointerup',endDrag);el.addEventListener('pointercancel',endDrag)}
addEventListener('resize',()=>{if(!$('#termwin').hidden&&!$('#termwin').classList.contains('max'))placeTerm()});

// ---- questions
let filter=store('qa-filter')||'unresolved';const expanded=new Set();
// Long threads: only the latest SHOW_LAST answers are open; earlier ones fold to a one-line preview.
const SHOW_LAST=2,openAns=new Set(),closedAns=new Set();
function peek(t){t=String(t||'').replace(/!\[([^\]]*)\]\([^)]*\)/g,'[image: $1]').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1')
 .replace(/```[\s\S]*?(```|$)/g,' [code] ').replace(/^\s*#+\s+/gm,'').replace(/[`*]/g,'').replace(/^\s*&gt;\s?|^\s*>\s?/gm,'').replace(/\s+/g,' ').trim();return t.length>140?t.slice(0,137)+'…':t}
const FILTERS=[['unresolved','Unresolved'],['open','Open'],['answered','Answered'],['on_hold','On hold'],['resolved','Resolved'],['all','All']];
function tabs(){$('#tabs').innerHTML=FILTERS.map(([k,l])=>`<button data-f="${k}" class="${k===filter?'on':''}">${l}</button>`).join('');}
$('#tabs').onclick=e=>{const f=e.target.dataset.f;if(f){filter=f;store('qa-filter',f);tabs();load()}};
function card(q){const open=expanded.has(q.id);const forMe=me()&&q.status==='open'&&(q.to===me()||q.to==='anyone')&&q.author!==me();
 const docs=q.documents||[];
 let h=`<div class="card q ${forMe?'forme':''}${q.status==='on_hold'?' held':''}" data-id="${q.id}"><h3 data-toggle="${q.id}"><span class="pill ${q.status}">${q.status.replace('_',' ')}</span>#${q.id} ${esc(q.title)}</h3>
 <div class="meta">${esc(q.author)} → ${esc(q.to)} · ${ago(q.created)} · ${q.answers.length} answer${q.answers.length===1?'':'s'}${docs.length?` · ${docs.length} document${docs.length===1?'':'s'}`:''}${DRAFTS['a'+q.id]?' · <span class="drafted" title="Your unsent answer is kept in this browser">✎ your draft</span>':''}</div>`;
 if(open){if(q.body)h+=`<div class="body md">${md(q.body)}</div>`;
  if(docs.length)h+=`<div class="att">Attached: ${docs.map(d=>`<a href="${url('/api/docs/'+d.id+'/view')}" target="_blank" rel="noopener">${esc(d.title)}</a>`).join('')}</div>`;
  const n=q.answers.length,early=a=>q.answers.indexOf(a)<n-SHOW_LAST;
  const folded=q.answers.filter(a=>early(a)?!openAns.has(a.id):closedAns.has(a.id));
  const bar=[];
  if(folded.length>1)bar.push(`${folded.length} replies folded · <a href="#" data-unfoldall="${q.id}">show all</a>`);
  if(q.answers.some(a=>early(a)&&!folded.includes(a)))bar.push(`<a href="#" data-foldall="${q.id}">fold earlier replies</a>`);
  if(bar.length)h+=`<div class="foldbar">${bar.join(' · ')}</div>`;
  for(const a of q.answers){
   if(folded.includes(a))h+=`<div class="ans folded" data-unfold="${a.id}" title="Show this reply"><div class="meta"><span class="chev">▸</span> ${esc(a.author)} · ${ago(a.created)} <span class="peek">${esc(peek(a.body))}</span></div></div>`;
   else h+=`<div class="ans"><div class="meta"><a href="#" class="chev" data-fold="${a.id}" title="Fold this reply">▾</a> ${esc(a.author)} · ${ago(a.created)}</div><div class="body md" style="margin-top:4px">${md(a.body)}</div></div>`}
  h+=`<div class="row"><textarea data-draft="${q.id}" placeholder="Write an answer. Drop files on the question, use Attach files, or paste a screenshot (Ctrl+V) to attach them and show them here."></textarea></div>
  <div class="row" style="justify-content:flex-end"><span class="meta attmsg" data-attmsg="${q.id}"></span><button data-attach="${q.id}" style="flex:0 0 auto" title="Attach one or more files (or drop them on the question, or paste a screenshot)">Attach files</button><button class="primary" data-answer="${q.id}" style="flex:0 0 auto">Answer</button>
  ${q.status==='on_hold'?`<button data-status="${q.id}:reopen" style="flex:0 0 auto" title="Back in everyone's inbox; Claude picks it up again">Take off hold</button>`
    :q.status!=='resolved'?`<button data-status="${q.id}:on_hold" style="flex:0 0 auto" title="Out of everyone's inbox; Claude leaves it alone until it's taken off hold">Put on hold</button>`:''}
  ${q.status==='resolved'?`<button data-status="${q.id}:reopen" style="flex:0 0 auto">Reopen</button>`:`<button data-status="${q.id}:resolved" style="flex:0 0 auto">Mark resolved</button>`}</div>`}
 return h+'</div>'}
function linkDocs(html){return html.replace(/\bdoc (\d+)\b/gi,(m,id)=>`<a href="${url('/api/docs/'+id+'/view')}" target="_blank" rel="noopener">${m}</a>`)}
// Markdown for posts (what Claude writes): **bold**, *italic*, `code`, ``` blocks, lists, # headings, links,
// and images from the board's own document store: ![caption](/api/docs/12/view).
// The text is HTML-escaped first, so a post can never add its own HTML.
function docUrl(u,kind){ // a link to one of this board's documents (relative, or written for any host/port)
 const g=String(u).replace(/&amp;/g,'&').match(/^(?:https?:\/\/[^\/\s]+)?\/api\/gala\/file\?(\S*)$/);   // a file in data/
 if(g){const p=new URLSearchParams(g[1]).get('path');if(p)return galaHref(p,kind==='download')}
 const m=String(u).replace(/&amp;/g,'&').match(/^(?:https?:\/\/[^\/\s]+)?\/api\/docs\/(\d+)\/(view|download)(?:\?(\S*))?$/);
 if(!m)return null;
 const v=new URLSearchParams(m[3]||'').get('v');
 return url('/api/docs/'+m[1]+'/'+(kind||m[2])+(v&&/^\d+$/.test(v)?'?v='+v:'')).href;   // with this viewer's key
}
function mdInline(t){
 const codes=[],keep=[],hold=h=>{keep.push(h);return '\u0001'+(keep.length-1)+'\u0001'};
 t=t.replace(/`([^`\n]+)`/g,(m,c)=>{codes.push(c);return '\u0000'+(codes.length-1)+'\u0000'});
 t=t.replace(/\\([\\`*_~#|[\]()!-])/g,(m,c)=>hold(c));   // \* \_ \~ ... show the character itself
 t=t.replace(/!\[([^\]\n]*)\]\(([^\s)]+)\)/g,(m,alt,src)=>{const d=docUrl(src,'view');
  if(d)return hold(`<a class="mdimg" href="${esc(d)}" target="_blank" rel="noopener"><img src="${esc(d)}" alt="${alt}" title="${alt}" loading="lazy"></a>`);
  return /^https?:\/\//.test(src)?hold(`<a href="${src}" target="_blank" rel="noopener">${alt||src}</a>`):m});
 t=t.replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|\/api\/docs\/|\/api\/gala\/)[^\s)]+)\)/g,(m,txt,href)=>{const d=docUrl(href);
  return hold(`<a href="${d?esc(d):href}" target="_blank" rel="noopener">${txt}</a>`)});
 t=t.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g,(m,pre,href)=>{const d=docUrl(href);
  return pre+hold(`<a href="${d?esc(d):href}" target="_blank" rel="noopener">${href}</a>`)});
 t=t.replace(/\*\*(?=\S)([^\n]*?\S)\*\*/g,'<strong>$1</strong>');
 t=t.replace(/(^|[^*\w])\*(?=\S)([^*\n]*?\S)\*(?![*\w])/g,'$1<em>$2</em>');
 t=t.replace(/(^|[^_\w])__(?=\S)([^\n]*?\S)__(?![_\w])/g,'$1<strong>$2</strong>');
 t=t.replace(/(^|[^_\w])_(?=\S)([^_\n]*?\S)_(?![_\w])/g,'$1<em>$2</em>');   // not inside snake_case words
 t=t.replace(/~~(?=\S)([^\n]*?\S)~~/g,'<del>$1</del>');
 t=linkDocs(t);
 t=t.replace(/\u0001(\d+)\u0001/g,(m,i)=>keep[+i]);
 return t.replace(/\u0000(\d+)\u0000/g,(m,i)=>'<code>'+codes[+i]+'</code>');
}
const TABLE_SEP=/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/;
function cells(row){ // split a table row on | (not inside `code`, not \|), without the outer pipes
 const out=[];let cur='',tick=false;row=row.trim();
 for(let i=0;i<row.length;i++){const ch=row[i];
  if(ch==='\\'&&row[i+1]==='|'){cur+='|';i++;continue}
  if(ch==='`')tick=!tick;
  if(ch==='|'&&!tick){out.push(cur.trim());cur='';continue}
  cur+=ch}
 out.push(cur.trim());
 if(row.startsWith('|'))out.shift();
 if(row.endsWith('|')&&!row.endsWith('\\|'))out.pop();
 return out}
function md(src,opt){return mdLines(esc(src||'').split('\n'),opt||{})}
function mdLines(lines,opt){ // lines are already HTML-escaped; opt.doc: a Markdown file (wrapped lines join up)
 opt=opt||{};
 const out=[];let para=[],list=null,code=null;
 const flushP=()=>{if(para.length){out.push('<p>'+para.map(mdInline).join(opt.doc?' ':'<br>')+'</p>');para=[]}};
 const flushL=()=>{if(list){out.push((list.t==='ol'&&list.start!=='1'?`<ol start="${list.start}">`:`<${list.t}>`)+
   list.items.map(i=>{const t=i.text.match(/^\[([ xX])\]\s+(.*)$/);   // task list: - [ ] todo / - [x] done
    return t?`<li class="task${i.sub?' sub':''}"><input type="checkbox" disabled${t[1]===' '?'':' checked'}> ${mdInline(t[2])}</li>`
     :`<li${i.sub?' class="sub"':''}>${mdInline(i.text)}</li>`}).join('')+`</${list.t}>`);list=null}};
 for(let li=0;li<lines.length;li++){const line=lines[li];
  if(code!==null){if(/^\s*```/.test(line)){out.push('<pre><code>'+code.join('\n')+'</code></pre>');code=null}else code.push(line);continue}
  let m;
  if(line.includes('|')&&li+1<lines.length&&TABLE_SEP.test(lines[li+1])&&cells(line).length>1){
   // a table: header row, separator row (|---|:--:|), then rows until a line without a |
   flushP();flushL();const head=cells(line),al=cells(lines[li+1]).map(c=>/^:-+:$/.test(c)?'center':/^-+:$/.test(c)?'right':'');
   const rows=[];li+=2;while(li<lines.length&&lines[li].includes('|')&&!/^\s*$/.test(lines[li]))rows.push(cells(lines[li++]));li--;
   const td=(t,c,i)=>`<${t}${al[i]?` style="text-align:${al[i]}"`:''}>${mdInline(c||'')}</${t}>`;
   out.push('<div class="mdtable"><table><thead><tr>'+head.map((c,i)=>td('th',c,i)).join('')+'</tr></thead><tbody>'+
    rows.map(r=>'<tr>'+head.map((_,i)=>td('td',r[i],i)).join('')+'</tr>').join('')+'</tbody></table></div>');
  }
  else if(/^\s*```/.test(line)){flushP();flushL();code=[]}
  else if(/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)){flushP();flushL();out.push('<hr>')}   // --- *** ___
  else if(/^\s{0,3}&gt;/.test(line)){   // > block quote (consecutive lines; nests: >>)
   flushP();flushL();const q=[];
   while(li<lines.length&&/^\s{0,3}&gt;/.test(lines[li]))q.push(lines[li++].replace(/^\s{0,3}&gt;\s?/,''));li--;
   out.push('<blockquote>'+mdLines(q,opt)+'</blockquote>')}
  else if((m=line.match(/^\s{0,3}(#{1,6})\s+(.*)$/))){flushP();flushL();out.push('<h4 class="h'+m[1].length+'">'+mdInline(m[2])+'</h4>')}
  else if((m=line.match(/^(\s*)[-*•]\s+(.*)$/))){flushP();if(!list||list.t!=='ul'){flushL();list={t:'ul',items:[]}}list.items.push({text:m[2],sub:m[1].length>=2})}
  else if((m=line.match(/^(\s*)(\d+)[.)]\s+(.*)$/))){flushP();if(!list||list.t!=='ol'){flushL();list={t:'ol',items:[],start:m[2]}}list.items.push({text:m[3],sub:m[1].length>=2})}
  else if(/^\s*$/.test(line)){flushP();flushL()}
  else if(list&&/^\s{2,}\S/.test(line)){list.items[list.items.length-1].text+=' '+line.trim()}
  else{flushL();para.push(line)}
 }
 if(code!==null)out.push('<pre><code>'+code.join('\n')+'</code></pre>');
 flushP();flushL();return out.join('');
}
async function load(){
 // While someone is in an answer box, hold the refresh: always if they've typed something, and for
 // 20s of no activity if it's empty. After that the list refreshes and the box gets its focus back.
 const a=document.activeElement;let refocus=null;
 if(a&&a.closest&&a.closest('#list')&&/^(TEXTAREA|INPUT)$/.test(a.tagName)){
  if(a.value||Date.now()-lastActivity<20000)return;
  refocus=a.dataset.draft;
 }
 const drafts={};document.querySelectorAll('[data-draft]').forEach(t=>{if(t.value)drafts[t.dataset.draft]=t.value});
 try{const [{questions},{people}]=await Promise.all([api('/api/questions?status='+filter),api('/api/people')]);
  $('#people').innerHTML=people.map(p=>`<option value="${esc(p)}">`).join('');
  QUESTIONS=questions;questions.forEach(q=>noteDocs(q.documents||[]));
  $('#list').innerHTML=questions.length?questions.map(card).join(''):'<div class="empty">No questions here.</div>';
  for(const[id,v]of Object.entries(drafts)){const t=document.querySelector(`[data-draft="${id}"]`);if(t)t.value=v}
  document.querySelectorAll('[data-draft]').forEach(t=>{const d=DRAFTS['a'+t.dataset.draft];if(!t.value&&d)t.value=d.v});
  if(refocus){const t=document.querySelector(`[data-draft="${refocus}"]`);if(t)t.focus()}
  const n=questions.filter(q=>q.status==='open').length;document.title=(n?'('+n+') ':'')+BRAND+' · Gala Audit';
 }catch(e){$('#list').innerHTML=`<div class="empty err">Could not reach the board: ${esc(e.message)}</div>`}}
let attachTo=null,lastActivity=0,QUESTIONS=[],DRAFTS={};
// ---- drafts: what you've typed in an answer box or the Ask form is kept in this browser until you post it
// (or for 14 days), so a reload, a crash or a closed tab doesn't lose it
function draftsGet(){try{const d=JSON.parse(store('qa-drafts')||'{}')||{},old=Date.now()-14*864e5;
 for(const k in d)if(!d[k]||!d[k].ts||d[k].ts<old)delete d[k];return d}catch(e){return {}}}
function draftSet(k,v){const d=draftsGet();if(v&&v.trim())d[k]={v,ts:Date.now()};else delete d[k];
 try{store('qa-drafts',JSON.stringify(d))}catch(e){}DRAFTS=d}
DRAFTS=draftsGet();
['focusin','input','keydown'].forEach(ev=>$('#list').addEventListener(ev,()=>{lastActivity=Date.now()}));
$('#list').addEventListener('input',e=>{const t=e.target;if(t.dataset&&t.dataset.draft)draftSet('a'+t.dataset.draft,t.value)});
$('#list').onclick=async e=>{const t=e.target;
 if(t.dataset.toggle){const id=+t.dataset.toggle;expanded.has(id)?expanded.delete(id):expanded.add(id);load();return}
 const uf=t.closest&&t.closest('[data-unfold]');
 if(uf&&!t.closest('a[href]:not([data-unfold])')){const id=+uf.dataset.unfold;openAns.add(id);closedAns.delete(id);load();return}
 if(t.dataset.fold){e.preventDefault();const id=+t.dataset.fold;openAns.delete(id);closedAns.add(id);load();return}
 if(t.dataset.unfoldall||t.dataset.foldall){e.preventDefault();const q=QUESTIONS.find(x=>x.id===+(t.dataset.unfoldall||t.dataset.foldall));
  if(q)for(const a of q.answers){if(t.dataset.unfoldall){openAns.add(a.id);closedAns.delete(a.id)}else{openAns.delete(a.id);if(q.answers.indexOf(a)>=q.answers.length-SHOW_LAST)closedAns.delete(a.id)}}
  load();return}
 if(!me()){alertBox('Choose who you are next to "I am" first.');return}
 if(t.dataset.attach){attachTo=+t.dataset.attach;$('#attfile').click();return}
 if(t.dataset.answer){const ta=document.querySelector(`[data-draft="${t.dataset.answer}"]`);if(!ta.value.trim())return;
  try{await api(`/api/questions/${t.dataset.answer}/answers`,{body:ta.value,author:me()});ta.value='';draftSet('a'+t.dataset.answer,'');ta.blur();load()}catch(err){alertBox(err.message)}}
 if(t.dataset.status){const[id,s]=t.dataset.status.split(':');try{await api(`/api/questions/${id}/status`,{status:s});load()}catch(err){alertBox(err.message)}}};
function alertBox(m){$('#perr').textContent=m;$('#newbox').open=true;setTimeout(()=>$('#perr').textContent='',4000)}
let qFiles=[];
function showQFiles(){$('#qfilelist').innerHTML=qFiles.length?qFiles.map((f,i)=>`${esc(f.name)} (${size(f.size)}) <a href="#" data-rmq="${i}">remove</a>`).join(' · '):'They\'re attached to the question when you post it. You can also paste a screenshot (Ctrl+V).'}
function addQFiles(list){for(const f of list){if(f.size>maxMB*1048576){alertBox(`${f.name} is larger than the ${maxMB} MB limit.`);continue}qFiles.push(f)}showQFiles()}
$('#qfile').onchange=e=>{addQFiles([...e.target.files]);e.target.value=''};
$('#qfilelist').onclick=e=>{if(e.target.dataset.rmq){e.preventDefault();qFiles.splice(+e.target.dataset.rmq,1);showQFiles()}};
const qdrop=$('#newbox');
['dragenter','dragover'].forEach(ev=>qdrop.addEventListener(ev,e=>{if(e.dataTransfer&&[...e.dataTransfer.types].includes('Files')){e.preventDefault();$('#qdrop').classList.add('over')}}));
['dragleave','drop'].forEach(ev=>qdrop.addEventListener(ev,()=>$('#qdrop').classList.remove('over')));
qdrop.addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files.length){e.preventDefault();qdrop.open=true;addQFiles([...e.dataTransfer.files])}});
async function postQuestion(to){
 const btns=[$('#post'),$('#postany')];btns.forEach(b=>b.disabled=true);
 try{const q=await api('/api/questions',{title:$('#title').value,body:$('#qbody').value,to,author:me()});
  const failed=[];
  for(const [i,f] of qFiles.entries()){$('#qfilelist').textContent=`Attaching ${f.name} (${i+1} of ${qFiles.length})…`;
   try{await upload(f,{question:q.id})}catch(err){failed.push(f.name+': '+err.message)}}
  qFiles=[];showQFiles();
  $('#title').value=$('#qbody').value=$('#to').value='';toTouched=false;draftSet('ask-title','');draftSet('ask-body','');$('#anywarn').hidden=true;renderToPills();
  if(failed.length)alertBox(`Question #${q.id} was posted, but these files weren't attached: ${failed.join('; ')}`);else $('#newbox').open=false;
  expanded.add(q.id);load()}catch(e){alertBox(e.message)}
 finally{btns.forEach(b=>b.disabled=false)}}
// the Ask form's draft
['title','qbody'].forEach(id=>{const k=id==='title'?'ask-title':'ask-body',d=DRAFTS[k];
 if(d&&!$('#'+id).value){$('#'+id).value=d.v;$('#newbox').open=true}
 $('#'+id).addEventListener('input',e=>draftSet(k,e.target.value))});
$('#post').onclick=()=>{if(!me())return alertBox('Choose who you are next to "I am" first.');
 if(!$('#title').value.trim())return alertBox('Write the question first.');
 const to=$('#to').value.trim().toLowerCase();
 if(!to||to==='anyone'){$('#anywarn').hidden=false;$('#postany').focus();return}
 postQuestion(to)};
$('#postany').onclick=()=>postQuestion('anyone');
$('#chooseto').onclick=()=>{toTouched=true;$('#anywarn').hidden=true;$('#to').value='';renderToPills();const b=$('#topills button');if(b)b.focus()};

// ---- documents
let maxMB=20;
async function upload(file,opts={}){
 const p=new URLSearchParams({filename:file.name,author:me()||'unknown'});
 if(opts.title)p.set('title',opts.title);if(opts.description)p.set('description',opts.description);
 if(opts.question)p.set('question',opts.question);if(opts.replace)p.set('replace',opts.replace);
 const r=await fetch(url('/api/docs?'+p),{method:'POST',body:file});
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j}
async function uploadMany(files){
 if(!me()){$('#uploadmsg').className='err';$('#uploadmsg').textContent='Choose who you are next to "I am" first.';return}
 const many=files.length>1;
 for(const f of files){
  if(f.size>maxMB*1048576){msg(`${f.name}: larger than the ${maxMB} MB limit.`,true);continue}
  msg(`Uploading ${f.name}…`);
  try{const d=await upload(f,{title:many?'':$('#dtitle').value.trim(),description:$('#ddesc').value.trim(),question:$('#dq').value.trim()});
   msg(`Stored ${d.filename} as doc ${d.id}.`)}catch(e){msg(`${f.name}: ${e.message}`,true)}}
 $('#dtitle').value=$('#ddesc').value='';loadDocs()}
function msg(t,bad){const m=$('#uploadmsg');m.className=bad?'err':'ok';m.textContent=t}
$('#file').onchange=e=>{uploadMany([...e.target.files]);e.target.value=''};
const drop=$('#drop');
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',e=>uploadMany([...e.dataTransfer.files]));
let verFor=null;
// ---- deployments: a progress bar under the tabs while one is running (or just finished), full detail on Deploys
let DEPS=[],depTimer=null,focusDep=null;
// Finished deployments hidden from the bar under the tabs (per browser): {id: the report time that was hidden}.
// A newer report for the same deployment shows it again.
let HIDDEN={};try{HIDDEN=JSON.parse(store('qa-dephidden')||'{}')||{}}catch(e){HIDDEN={}}
function hideDep(d){HIDDEN[d.deployment]=d.received;const k=Object.keys(HIDDEN);
 if(k.length>50)for(const x of k.slice(0,k.length-50))delete HIDDEN[x];store('qa-dephidden',JSON.stringify(HIDDEN))}
function depTime(t){const d=t?new Date(t):null;return d&&!isNaN(d)?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):''}
function depPct(d){
 if(typeof d.progress==='number')return Math.round(d.progress*100);
 const st=(d.steps||[]).filter(x=>x&&typeof x==='object');if(!st.length)return d.status==='done'?100:0;
 let n=0;for(const x of st){if(x.status==='done')n+=1;
  else if(x.status==='running'){const sg=(x.stages||[]).filter(g=>g&&typeof g==='object');n+=sg.length?sg.filter(g=>g.status==='done').length/sg.length:0.5}}
 return Math.round(n/st.length*100)}
function depCur(d){const st=(d.steps||[]).filter(x=>x&&typeof x==='object');
 const s=st.find(x=>x.id===d.step)||st.find(x=>x.status==='running')||st.find(x=>x.status==='failed');
 if(!s)return '';const g=(s.stages||[]).find(x=>x&&x.status==='running');return (s.label||s.id||'')+(g?' · '+g.name:'')}
function depState(d){return d.waitingFor?'needs':d.status}
function depLabel(d){const s=depState(d),stale=d.status==='running'&&Date.now()/1000-d.received>900;
 return s==='needs'?'needs you':stale?'no update '+ago(d.received).replace(' ago',''):s}
function depActive(d){return d.status==='running'||d.status==='waiting'||d.waitingFor||Date.now()/1000-d.received<900}
function renderDepStrip(){const act=DEPS.filter(d=>depActive(d)&&HIDDEN[d.deployment]!==d.received).slice(0,3),box=$('#deps');
 box.hidden=!act.length;
 box.innerHTML=act.map(d=>{const p=depPct(d);return `<button class="deprow s-${esc(d.status)}${d.waitingFor?' needs':''}" data-dep="${esc(d.deployment)}" title="Open on the Deploys tab">
  <span class="depname">${esc(d.target||d.deployment)}</span><span class="depbar"><i style="width:${p}%"></i></span>
  <span class="deppct">${p}%</span><span class="depstep">${esc(depCur(d)||(d.status==='done'?'finished '+depTime(d.updated):''))}</span>
  ${d.status==='done'||d.status==='failed'?`<span class="dstat ${depState(d)} hide" data-hide="${esc(d.deployment)}" title="Hide from the bar (still on the Deploys tab)">${esc(depLabel(d))} ✕</span>`
    :`<span class="dstat ${depState(d)}">${esc(depLabel(d))}</span>`}</button>`}).join('')}
const ICON={done:'✓',running:'●',failed:'✕',waiting:'○'};
function depCard(d){const p=depPct(d),st=(d.steps||[]).filter(x=>x&&typeof x==='object'),wf=d.waitingFor,er=d.error;
 const qs=(d.questions||[]).map(n=>`<a href="#" data-goq="${+n}">#${+n}</a>`).join(' ');
 return `<div class="card depcard s-${esc(d.status)}${wf?' needs':''}${d.deployment===focusDep?' focus':''}" id="dep-${esc(d.deployment)}">
 <h3>${esc(d.target||'Deployment')} <span class="dstat ${depState(d)}">${esc(depLabel(d))}</span><span class="meta" style="font-weight:400">${esc(d.deployment)}</span></h3>
 <div class="depmeta">${d.started?`<span>started ${esc(depTime(d.started))}</span>`:''}<span>updated ${ago(d.received)}</span>
  ${d.commit?`<span>commit <code>${esc(d.commit)}</code></span>`:''}${(d.branches||[]).length?`<span>${(d.branches||[]).map(b=>`<code>${esc(b)}</code>`).join(' ')}</span>`:''}
  ${qs?`<span>questions ${qs}</span>`:''}</div>
 <div class="depbar"><i style="width:${p}%"></i></div>
 <ol class="depsteps">${st.map(x=>{const s=['done','running','failed','waiting'].includes(x.status)?x.status:'waiting';
  const sg=(x.stages||[]).filter(g=>g&&typeof g==='object');
  return `<li class="${s}"><span class="ic">${ICON[s]}</span><span>${esc(x.label||x.id||'')}${x.detail?` <span class="meta">· ${linkDocs(esc(String(x.detail)))}</span>`:''}</span>
   <span class="when">${x.finished?esc(depTime(x.finished)):''}</span>
   ${sg.length?`<div class="stages">${sg.map(g=>`<span class="stage ${['done','running','failed'].includes(g.status)?g.status:'waiting'}">${esc(g.name||'')}</span>`).join('')}</div>`:''}</li>`}).join('')}</ol>
 ${wf?`<div class="depwait"><b>Needs you:</b> ${esc(wf.reason||'')}${wf.url&&/^https?:\/\//.test(wf.url)?` · <a href="${esc(wf.url)}" target="_blank" rel="noopener noreferrer">Open link</a>`:''}${wf.code?` · code <code>${esc(wf.code)}</code>`:''}</div>`:''}
 ${er?`<div class="deperr"><b>Failed${er.step?' at '+esc(er.step):''}:</b> ${esc(er.message||'')}${er.log?` · log: ${linkDocs(esc(String(er.log)))}`:''}</div>`:''}
 </div>`}
function renderDeps(){$('#deplist').innerHTML=DEPS.length?DEPS.map(depCard).join(''):'<div class="empty">No deployments yet.</div>'}
async function loadDeps(){
 clearTimeout(depTimer);
 try{DEPS=(await api('/api/deployments?limit=20')).deployments||[];renderDepStrip();if(view==='deploys')renderDeps()}catch(e){}
 depTimer=setTimeout(()=>{if(!document.hidden)loadDeps();else depTimer=setTimeout(loadDeps,5000)},DEPS.some(depActive)||view==='deploys'?3000:15000)}
$('#deps').onclick=e=>{
 const h=e.target.closest('[data-hide]');
 if(h){e.stopPropagation();const d=DEPS.find(x=>x.deployment===h.dataset.hide);if(d){hideDep(d);renderDepStrip()}return}
 const b=e.target.closest('[data-dep]');if(b)goToDep(b.dataset.dep)};
function goToDep(id){   // the Deploys tab, scrolled to that deployment's card, which is outlined
 focusDep=id;showView('deploys');renderDeps();
 const c=document.getElementById('dep-'+id);if(c)c.scrollIntoView({block:'start'})}
$('#deplist').onclick=e=>{const a=e.target.closest('[data-goq]');if(!a)return;e.preventDefault();
 expanded.add(+a.dataset.goq);filter='all';store('qa-filter','all');tabs();showView('questions')};
loadDeps();
// Markdown documents (.md) open in a pop-up, laid out like a page, instead of as plain text.
const DOCMETA={};
function noteDocs(list){for(const d of list||[])DOCMETA[d.id]=Object.assign(DOCMETA[d.id]||{},d)}
function isMd(d){return !!d&&(/\.(md|markdown|mdown|mkd)$/i.test(d.filename||'')||d.mime==='text/markdown')}
async function docMeta(id){if(!DOCMETA[id]||!DOCMETA[id].filename){try{noteDocs([await api('/api/docs/'+id)])}catch(e){}}return DOCMETA[id]}
async function openMd(id,v){
 const d=DOCMETA[id]||{},q=v?'?v='+encodeURIComponent(v):'';
 $('#mdtitle').textContent=d.title||('doc '+id);$('#mdmeta').textContent=`doc ${id}${d.filename?' · '+d.filename:''}${v?' · version '+v:''}`;
 $('#mdtext').href=url('/api/docs/'+id+'/view'+q);$('#mddl').href=url('/api/docs/'+id+'/download'+q);
 $('#mdbody').innerHTML='<div class="meta">Loading…</div>';
 $('#mdshade').hidden=$('#mdwin').hidden=false;document.body.classList.add('mdopen');
 try{const r=await fetch(url('/api/docs/'+id+'/view'+q));if(!r.ok)throw new Error(r.status===404?'not found':'error '+r.status);
  $('#mdbody').innerHTML=md(await r.text(),{doc:true});$('#mdbody').scrollTop=0}
 catch(e){$('#mdbody').innerHTML=`<div class="empty err">Couldn't open doc ${id}: ${esc(e.message)}</div>`}}
function closeMd(){$('#mdshade').hidden=$('#mdwin').hidden=true;$('#mdbody').innerHTML='';document.body.classList.remove('mdopen')}
$('#mdclose').onclick=closeMd;$('#mdshade').onclick=closeMd;
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#mdwin').hidden){e.preventDefault();closeMd()}});
document.addEventListener('click',async e=>{   // any link to a document's view: .md files open in the pop-up
 if(e.defaultPrevented||e.button!==0||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;
 const a=e.target.closest&&e.target.closest('a[href]');if(!a||a.classList.contains('mdimg')||a.id==='mdtext')return;
 const m=a.href.match(/\/api\/docs\/(\d+)\/view(?:\?(.*))?$/);if(!m)return;
 const id=+m[1],v=new URLSearchParams(m[2]||'').get('v'),known=DOCMETA[id]&&DOCMETA[id].filename;
 if(known&&!isMd(DOCMETA[id]))return;           // not Markdown: open as usual
 e.preventDefault();
 if(isMd(await docMeta(id)))openMd(id,v);else window.open(a.href,'_blank','noopener')});
// Paste an image (e.g. a screenshot) from the clipboard:
//  - in the Ask form: it's attached to the question when you post it
//  - in an open question (its answer box): it's uploaded and attached to that question straight away,
//    and ![…](/api/docs/ID/view) is put in the answer box so it shows in your answer
//  - on the Documents tab: it's stored as a new document
// Pasting text works as usual.
function pastedFiles(e){
 const cd=e.clipboardData;if(!cd)return [];
 let files=[...(cd.files||[])];
 if(!files.length&&cd.items)files=[...cd.items].filter(i=>i.kind==='file').map(i=>i.getAsFile()).filter(Boolean);
 const stamp=new Date().toISOString().replace(/\..*/,'').replace(/[-:]/g,'').replace('T','-');
 return files.map((f,i)=>{const ext=(f.type.split('/')[1]||'png').replace('jpeg','jpg').replace('svg+xml','svg');
  const generic=!f.name||/^image\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name);
  return generic?new File([f],`pasted-${stamp}${files.length>1?'-'+(i+1):''}.${ext}`,{type:f.type}):f})}
document.addEventListener('paste',async e=>{
 const t=e.target instanceof Element?e.target:document.activeElement;
 const inAsk=t&&t.closest&&t.closest('#newbox'),card=t&&t.closest&&t.closest('.card.q[data-id]');
 if(!inAsk&&!card&&view!=='docs')return;
 const files=pastedFiles(e);if(!files.length)return;    // plain text: let the browser paste it
 e.preventDefault();
 if(inAsk){$('#newbox').open=true;addQFiles(files);return}
 if(card){attachFiles(+card.dataset.id,files);return}
 uploadMany(files)});
// Attach files to a question (Attach files button, drop on the question, paste): each is uploaded and attached,
// and a line for it goes into the answer box - an image for pictures, a link for other files - so your
// answer can show or point to it. Delete the line if you don't want it in the answer.
async function attachFiles(qid,files){
 if(!me()){alertBox('Choose who you are next to "I am" first.');return}
 const card=()=>document.querySelector(`.card.q[data-id="${qid}"]`),say=t=>{const m=document.querySelector(`[data-attmsg="${qid}"]`);if(m)m.textContent=t};
 const failed=[];let done=0;lastActivity=Date.now();
 for(const [i,f] of files.entries()){
  if(f.size>maxMB*1048576){failed.push(`${f.name}: larger than the ${maxMB} MB limit`);continue}
  say(files.length>1?`Attaching ${i+1} of ${files.length}: ${f.name}…`:`Attaching ${f.name}…`);
  try{const d=await upload(f,{question:qid});done++;noteDocs([{id:d.id,title:d.title,filename:d.filename||f.name,mime:d.mime||f.type}]);
   const c=card(),ta=c&&c.querySelector('[data-draft]');
   if(ta){const name=f.name.replace(/[\[\]]/g,''),ins=(/^image\//.test(f.type)?`![${name}](/api/docs/${d.id}/view)`:`[${name}](/api/docs/${d.id}/view)`)+'\n';
    const a=ta.selectionStart??ta.value.length,b=ta.selectionEnd??a;
    ta.setRangeText((a>0&&ta.value[a-1]!=='\n'?'\n':'')+ins,a,b,'end');lastActivity=Date.now();draftSet('a'+qid,ta.value)}
  }catch(err){failed.push(`${f.name}: ${err.message}`)}}
 say(done?`${done} file${done===1?'':'s'} attached.`:'');setTimeout(()=>say(''),4000);
 if(failed.length)alertBox('Not attached: '+failed.join('; '));
 const ta=card()&&card().querySelector('[data-draft]');if(ta)ta.focus();
 if(!(ta&&ta.value.trim()))load()}
$('#attfile').onchange=e=>{const files=[...e.target.files];e.target.value='';if(files.length&&attachTo)attachFiles(attachTo,files)};
// Drop files on a question: attached to it (the whole card is the drop target)
$('#list').addEventListener('dragover',e=>{const c=e.target.closest&&e.target.closest('.card.q[data-id]');
 if(c&&e.dataTransfer&&[...e.dataTransfer.types].includes('Files')){e.preventDefault();
  document.querySelectorAll('.card.q.dropover').forEach(x=>x!==c&&x.classList.remove('dropover'));c.classList.add('dropover')}});
$('#list').addEventListener('dragleave',e=>{const c=e.target.closest&&e.target.closest('.card.q');if(c&&!c.contains(e.relatedTarget))c.classList.remove('dropover')});
$('#list').addEventListener('drop',e=>{const c=e.target.closest&&e.target.closest('.card.q[data-id]');
 document.querySelectorAll('.card.q.dropover').forEach(x=>x.classList.remove('dropover'));
 if(c&&e.dataTransfer&&e.dataTransfer.files.length){e.preventDefault();
  if(!expanded.has(+c.dataset.id)){expanded.add(+c.dataset.id);load()}
  attachFiles(+c.dataset.id,[...e.dataTransfer.files])}});
$('#verfile').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;
 try{
  await upload(f,{replace:verFor});msg(`New version of doc ${verFor} stored.`);loadDocs()
 }catch(err){view==='docs'?msg(err.message,true):alertBox(err.message)}};
function docRow(d){
 const v=url('/api/docs/'+d.id+'/view'),dl=url('/api/docs/'+d.id+'/download');
 return `<div class="card doc"><div class="main"><h3>${esc(d.title)}${d.archived?'<span class="badge">archived</span>':''}</h3>
 <div class="meta">doc ${d.id} · ${esc(d.filename)} · ${size(d.size)} · v${d.version}${d.version_count>1?` (${d.version_count} versions)`:''} · ${esc(d.author)} · ${ago(d.updated)}${d.question_id?` · <a href="#" data-goq="${d.question_id}">question #${d.question_id}</a>`:''}</div>
 ${d.description?`<div class="body md" style="margin-top:4px">${md(d.description)}</div>`:''}
 <div class="vers" data-vers="${d.id}"></div></div>
 <div class="acts"><a href="${v}" target="_blank" rel="noopener">Open</a><a href="${dl}">Download</a>
 <button data-newver="${d.id}">New version</button>${d.version_count>1?`<button data-showvers="${d.id}">History</button>`:''}
 <button data-arch="${d.id}" data-on="${d.archived?0:1}">${d.archived?'Restore':'Archive'}</button></div></div>`}
let dsearchT=null;
$('#dsearch').oninput=()=>{clearTimeout(dsearchT);dsearchT=setTimeout(loadDocs,250)};
$('#darch').onchange=loadDocs;
async function loadDocs(){
 const a=document.activeElement;if(a&&a.closest&&a.closest('#v-docs')&&a.id!=='dsearch'&&a.value&&a.type!=='checkbox')return;
 try{const p=new URLSearchParams();const s=$('#dsearch').value.trim();if(s)p.set('q',s);if($('#darch').checked)p.set('archived','1');
  const j=await api('/api/docs?'+p);maxMB=j.max_mb||maxMB;$('#maxmb').textContent=`Up to ${maxMB} MB per file. Everyone with access to the board can open these.`;
  noteDocs(j.documents);
  $('#docs').innerHTML=j.documents.length?j.documents.map(docRow).join(''):'<div class="empty">No documents yet. Drop a file above to add one.</div>';
 }catch(e){$('#docs').innerHTML=`<div class="empty err">Could not reach the board: ${esc(e.message)}</div>`}}
$('#docs').onclick=async e=>{const t=e.target;
 if(t.dataset.goq){e.preventDefault();expanded.add(+t.dataset.goq);filter='all';store('qa-filter','all');tabs();showView('questions');return}
 if(t.dataset.newver){if(!me()){msg('Choose who you are next to "I am" first.',true);return}verFor=+t.dataset.newver;$('#verfile').dataset.mode='version';$('#verfile').click()}
 if(t.dataset.showvers){const box=document.querySelector(`[data-vers="${t.dataset.showvers}"]`);
  if(box.innerHTML){box.innerHTML='';return}
  const d=await api('/api/docs/'+t.dataset.showvers);
  box.innerHTML=d.versions.map(v=>`v${v.version} · <a href="${url('/api/docs/'+d.id+'/download')}&v=${v.version}">${esc(v.filename)}</a> · ${size(v.size)} · ${esc(v.author)} · ${ago(v.created)}${v.note?' · '+esc(v.note):''}`).join('<br>')}
 if(t.dataset.arch){try{await api(`/api/docs/${t.dataset.arch}/archive`,{archived:t.dataset.on==='1'});loadDocs()}catch(err){msg(err.message,true)}}};

api('/api/docs?limit=1').then(j=>{maxMB=j.max_mb||maxMB}).catch(()=>{});
tabs();showView(view);setInterval(refresh,8000);if(view!=='live')loadLive();
</script></body></html>
"""


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Tiny shared Q&A board and document store with an MCP endpoint for Claude.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the board")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--db", default="qaboard.db")
    s.add_argument("--key", default=None, help="optional shared secret; clients add ?key=... or X-QA-Key")
    s.add_argument("--max-doc-mb", type=float, default=20, help="largest document accepted (default 20)")
    s.add_argument("--name", default="CGA Gala Audit", help='board name shown on the page, e.g. "Acme Web App"')
    s.add_argument("--accent", default="#0b7a75", help="accent colour, e.g. #7a4cab")
    s.add_argument("--gala-data", default=None, help="the Gala data folder shown on the Gala tab")
    s.add_argument("--terminal-url", default="", help="where browsers reach this board's Claude Code web terminal, "
                                                      "e.g. http://127.0.0.1:17681 (the SSH tunnel)")
    b = sub.add_parser("bridge", help="stdio<->HTTP MCP bridge for Claude Desktop")
    b.add_argument("url", help='e.g. "http://server.example:8787/mcp?who=alice-claude&key=KEY"')
    args = p.parse_args()
    serve(args) if args.cmd == "serve" else bridge(args)


if __name__ == "__main__":
    main()
