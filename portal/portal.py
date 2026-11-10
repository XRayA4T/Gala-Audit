#!/usr/bin/env python3
"""
Gala Audit Portal - the QA Board with Gala and Checklist tabs, for auditing a swimming gala while it runs
with a local Claude Code and the gala-ocr plugin. Files arrive in an inbox folder and are sorted by session;
each event is handed to Claude as soon as its lane sheets and Gen7 races are in. Based on the QA Board:

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
import hmac
import json
import mimetypes
import os
import re
import select
import shutil
import socket
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

VERSION = "0.15.0"
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
        self.presence = {}      # open board pages: {page id: {who, view, term, hidden, ts}}
        self.terminals = None   # tmux clients attached to Claude's session, from the watcher: {count, ts}
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
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated REAL NOT NULL);
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

    def post(self, title, body, author, to, documents=None):
        """documents: ids of documents uploaded while writing the question (pasted screenshots, files); those not
        attached to a question yet are attached to this one."""
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        now = time.time()
        docs = []
        for d in documents or []:
            try:
                docs.append(int(d))
            except (TypeError, ValueError):
                pass
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO questions(title, body, author, to_whom, status, created, updated) "
                "VALUES(?,?,?,?, 'open', ?, ?)",
                (title, (body or "").strip(), clean_name(author), clean_name(to or "anyone"), now, now),
            )
            for d in docs[:50]:
                self.db.execute("UPDATE documents SET question_id=? WHERE id=? AND question_id IS NULL",
                                (cur.lastrowid, d))
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

    # -- settings made on the board's Settings tab (branding); they win over the command line's
    SETTING_KEYS = ("name", "accent", "css", "logo")

    def settings(self):
        with self.lock:
            return {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM settings")}

    def set_setting(self, key, value):
        if key not in self.SETTING_KEYS:
            raise ValueError("unknown setting %r" % key)
        with self.lock:
            if value is None or value == "":
                self.db.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO settings(key, value, updated) VALUES(?,?,?)",
                                (key, value, time.time()))
            self.db.commit()

    # -- deployments: progress reports (the whole state each time; a report replaces the earlier one)
    def put_deployment(self, dep_id, state):
        state = clean_deployment(dep_id, state)
        prev = self.deployments(dep_id=dep_id)
        deployment_times(state, prev[0] if prev else {})
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
        w = self.watching_line()
        if w:
            lines.append("[QA board] " + w)
        return "\n".join(lines)

    # -- who is looking: each open board page checks in every 15 s (POST /api/presence) with the name chosen
    # next to "I am", its tab and whether the terminal window is open; silent for 75 s = gone.
    PRESENCE_TTL = 75

    def touch_presence(self, pid, who, view, term, hidden, gone=False):
        pid = re.sub(r"[^A-Za-z0-9_-]", "", str(pid or ""))[:32]
        if not pid:
            raise ValueError("id required")
        with self.lock:
            if gone:
                self.presence.pop(pid, None)
            else:
                self.presence[pid] = {"who": clean_name(who) if who else "", "view": str(view or "")[:20],
                                      "term": bool(term), "hidden": bool(hidden), "ts": time.time()}
                if len(self.presence) > 500:   # a runaway client: keep the newest
                    for k in sorted(self.presence, key=lambda k: self.presence[k]["ts"])[:-500]:
                        del self.presence[k]

    def watchers(self):
        """One entry per person (pages merged): {who, term, live, board, background}."""
        now, people = time.time(), {}
        with self.lock:
            for k in [k for k, v in self.presence.items() if now - v["ts"] > self.PRESENCE_TTL]:
                del self.presence[k]
            for v in self.presence.values():
                p = people.setdefault(v["who"] or "someone", {"who": v["who"] or "someone", "term": False,
                                                              "live": False, "board": False, "background": True})
                p["term"] = p["term"] or v["term"]
                p["live"] = p["live"] or (v["view"] == "live" and not v["hidden"])
                p["board"] = True
                p["background"] = p["background"] and v["hidden"] and not v["term"]
            term_pages = sum(1 for v in self.presence.values() if v["term"])
            t = self.terminals
        others = 0
        if t and now - t["ts"] < 60:   # terminals attached to the session that aren't a board page's window
            others = max(0, t["count"] - term_pages)
        return sorted(people.values(), key=lambda p: (not p["term"], not p["live"], p["background"], p["who"])), others

    def watching_line(self):
        """"Watching your session now: raymond (terminal open), lorretta (Live tab); on the board: bob", or ""."""
        people, others = self.watchers()
        watching = [p for p in people if p["term"] or p["live"]]
        rest = [p for p in people if not (p["term"] or p["live"])]
        if not watching and not rest and not others:
            return ""
        out = []
        if watching or others:
            names = ["%s (%s)" % (p["who"], "terminal open" if p["term"] else "Live tab") for p in watching]
            if others:
                names.append("%d other terminal%s attached (name unknown)" % (others, "" if others == 1 else "s"))
            out.append("Watching your session now: " + ", ".join(names))
        if rest:
            out.append(("on the board" if out else "On the board now (not watching your session)") + ": "
                       + ", ".join(p["who"] + (" (in the background)" if p["background"] else "") for p in rest))
        return "; ".join(out) + "."

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
        self.jobs = {}

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

    def meet_wide(self, n, kind):
        """The newest file of a kind in session n's meet folder, else in any session's: (session, file) or
        (None, None). The empty merge results export (and a meet-wide entries export) is needed once per gala."""
        own = self.meet_files(n)[kind]
        if own and kind != "results":   # the empty results export: always the gala's newest, wherever it is
            return n, own[-1]
        best = (None, None)
        for m in self.sessions():
            f = self.meet_files(m)[kind] if m != n or kind == "results" else []
            if f and (best[1] is None or f[-1]["mtime"] > best[1]["mtime"]):
                best = (m, f[-1])
        return best

    def meet_path(self, n, kind):
        m, f = self.meet_wide(n, kind)
        return os.path.join(self.sdir(m, "meet"), f["name"]) if f else None

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
        if kind == "results":   # one per gala: next to the one there is, else the first session
            there = [n for n in have if self.meet_files(n)["results"]]
            return there[0] if there else (min(have) if have else 1)
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

    def entry_events(self):
        """Every event in the meet's entries exports (all sessions); empty until an entries export is in."""
        out = set()
        for n in self.sessions():
            f = self.meet_files(n)["entries"]
            if f:
                out |= set(self.hy3(os.path.join(self.sdir(n, "meet"), f[-1]["name"])))
        return out

    def stray_gen7(self, path):
        """The events of a Gen7 PDF whose races are all for events not in the entries (a test or warm-up race,
        e.g. Gen7's own "Event 1 100 Meter"), or None if it belongs to the meet (or there are no entries yet)."""
        races = self.gen7(path)["races"]
        known = self.entry_events()
        evs = sorted({r["event"] for r in races})
        return evs if races and known and not set(evs) & known else None

    def ignore(self, path, evs, copy=False):
        d = os.path.join(self.root, "ignored")
        os.makedirs(d, exist_ok=True)
        name = os.path.basename(path)
        dest = os.path.join(d, name)
        if os.path.exists(dest) and _same_file(path, dest):
            if not copy:
                os.unlink(path)
            return "ignored"
        dest = os.path.join(d, _unique(d, name))
        (shutil.copy2 if copy else shutil.move)(path, dest)
        self.log("%s ignored: event %s isn't in the meet entries (a test race?). It's under Ignored on the Gala tab."
                 % (name, ", ".join(map(str, evs))), name=name, to="ignored", kind="gen7")
        return "ignored"

    def ignore_strays(self):
        """Gen7 races already filed for events not in the entries (e.g. filed before the entries came in) are moved to
        ignored/, unless someone moved them back into a session by hand."""
        kept = self.state.get("keptGen7", [])
        for n in self.sessions():
            d = self.sdir(n, "gen7")
            for f in _ls(d):
                p = os.path.join(d, f["name"])
                if f["dir"] or not f["name"].lower().endswith(".pdf") or "S%d/%s" % (n, f["name"]) in kept:
                    continue
                evs = self.stray_gen7(p)
                if evs:
                    self.ignore(p, evs)

    def file_one(self, path, force_session=None, force_folder=None, copy=False):
        name = os.path.basename(path)
        kind, folder, sess, event = self.classify(path)
        if kind == "gen7" and not force_session and not force_folder:
            evs = self.stray_gen7(path)
            if evs:
                return self.ignore(path, evs, copy)
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
        if folder == "gen7":
            with self.lock:
                kept = self.state.setdefault("keptGen7", [])
                kept.append("S%d/%s" % (int(session), os.path.basename(dest)))
                self.save()
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
            for kind_ in ("entries", "results"):   # a pre-gala entries export works too; the empty results export is per gala
                f_ = self.meet_wide(n, kind_)[1]
                if f_:
                    merge_inputs.append((f_["name"], f_["mtime"]))
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
            ns = self.state.get("notSwum", {}).get("S%d:e%d" % (n, e)) or {}
            not_swum = [h for h in ns.get("heats", []) if h not in have]
            if not_swum:   # confirmed on the checklist: part of what the event is audited from
                inputs = inputs + [("not swum %s" % ",".join(map(str, not_swum)), ns.get("at", 0))]
            sig = hashlib.sha1(json.dumps(sorted(inputs)).encode()).hexdigest()[:12]
            newest = max([t for _, t in inputs] or [0])
            ready = bool(pk)   # Claude can read the pack as soon as it is in; the rest waits for the portal
            read = bool(pk) and all(p["pages"] and p["done"] >= p["pages"] and not p["bad"] for p in pk)
            pack_sig = hashlib.sha1(json.dumps(sorted([p["name"], p["mtime"]] for p in pk)).encode()).hexdigest()[:12]
            a = asked.get("S%d:e%d" % (n, e))
            job = self.jobs.get("S%d:e%d:run" % (n, e)) or {}
            if job.get("state") == "running":
                status = "computing"
            elif pk and not read:   # the pack (or a rescan of it) still being read
                status = "asked" if a and a.get("sig") == pack_sig else "ready"
            elif report and report["mtime"] >= newest:
                status = "audited"
            elif report:
                status = "changed"
            elif read:
                status = "transcribed"
            else:
                status = "waiting"
            events.append({"event": e, "desc": desc.get(e, ""), "heats": heats, "gen7": have,
                           "gen7Missing": [h for h in range(1, heats + 1) if h not in have and h not in not_swum],
                           "notSwum": not_swum, "notSwumBy": ns.get("by") if not_swum else None,
                           "expected": bool(x), "lanes": len(x.get("lanes", [])) or len(print_lanes.get(e, ())) or None,
                           "packs": [p["name"] for p in pk], "printed": printed.get(e, []), "ocrLanes": len(lanes_done.get(e, ())),
                           "report": report["name"] if report else None, "ready": ready, "sig": sig,
                           "status": status, "asked": a, "read": read, "packSig": pack_sig, "run": job or None,
                           "computed": self.state.get("computed", {}).get("S%d:e%d:run" % (n, e)), "merge": merge_file, "mergeAt": merge_f["mtime"] if merge_f else None,
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
        tm, tf = self.meet_wide(n, "results")
        em, ef = self.meet_wide(n, "entries")
        return {"n": n, "meet": {k: [f["name"] for f in v] for k, v in meet.items()}, "lanePrints": prints,
                "template": "sessions/S%d/meet/%s" % (tm, tf["name"]) if tf else None,
                "templateName": tf["name"] if tf else None, "templateSession": tm,
                "entriesFile": "sessions/S%d/meet/%s" % (em, ef["name"]) if ef else None,
                "jobs": {k: self.jobs.get("S%d:%s" % (n, k)) for k in ("merge", "final", "etd") if self.jobs.get("S%d:%s" % (n, k))},
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
                    "ignored": _ls(os.path.join(self.root, "ignored")),
                    "sessions": [self.session(n) for n in self.sessions()],
                    "ticks": self.state["ticks"],
                    "tools": {t: bool(shutil.which(t)) for t in ("pdfinfo", "pdftoppm", "pdftotext")},
                    "summary": self.summary(), "archives": self.archives()}

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
        kind = key.split(":")[1]
        if kind.startswith("e") and kind[1:].isdigit():
            e = int(kind[1:])
            ev = next((x for x in s["events"] if x["event"] == e), None)
            if not ev:
                return None
            packs = ", ".join("`%s`" % self.P(n, "scans", p) for p in ev["packs"]) or "(no lane sheet pack yet)"
            redo = bool(ev["report"]) or bool(ev["asked"] and ev["asked"].get("sig") != ev["packSig"])
            title = "Session %d · Event %d%s: %s" % (n, e, (" " + ev["desc"]) if ev["desc"] else "",
                                                     "rescanned, read it again" if redo else "transcribe the lane sheets")
            if ev["printed"]:
                orig = ("Take the printed fields (names, ages, teams, seed times) from Meet Manager's printed lane sheets "
                        "%s with the skill's `original_sheets.py` (pass the PDF and the event)."
                        % ", ".join("`data/%s`" % f for f in ev["printed"]))
            else:
                orig = ("There are no printed lane sheets for this event yet, so read the printed fields from the scan "
                        "(and say so in your answer).")
            missing = ""
            if ev.get("notSwum"):
                missing += ("\n\nHeat%s %s %s **not swum** (confirmed on the checklist by %s): there is no Gen7 race "
                            "for %s. Don't treat %s as missing; if the lane sheets have swims in %s, say so."
                            % ("s" if len(ev["notSwum"]) > 1 else "", ", ".join(map(str, ev["notSwum"])),
                               "were" if len(ev["notSwum"]) > 1 else "was", ev.get("notSwumBy") or "an official",
                               "them" if len(ev["notSwum"]) > 1 else "it", "them" if len(ev["notSwum"]) > 1 else "it",
                               "them" if len(ev["notSwum"]) > 1 else "it"))
            body = (("Event %d's lane sheets are in: %s.%s\n\n"
                     "1. **Transcribe** them with the **gala-ocr** skill into `%s/` (skip pages already done%s). %s\n"
                     "2. **Answer** briefly: the pages read, and anything you couldn't read or are unsure of (which sheet "
                     "and page), sheets not signed, names written in.\n\n"
                     "**That's all.** Don't run merge_results.py or audit_report.py: as soon as every page is "
                     "transcribed (and the Gen7 races are in) the portal writes the event's merge results, audit, "
                     "quick fixes and the session report itself, and checks your files with validate_cache.py.")
                    % (e, packs, missing, ocr,
                       "; a rescanned pack replaced the old one and its old OCR was set aside, so read its pages again"
                       if redo and any(x["name"] in ev["packs"] and x["pages"] and x["done"] < x["pages"] for x in s["scans"])
                       else "", orig))
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
                        "about any code you're unsure of.\n"
                        "3. Answer with the DQs and their codes, and any slip you couldn't read.\n\n"
                        "Don't run the audit scripts: the portal rewrites the events' merge results, audits and the session "
                        "report itself once the slips are read and coded."
                        % (self.P(n, "scans", name), ocr, ocr))
            if sc["kind"] == "etd":
                return ("Session %d: read the ETD programme %s" % (n, name),
                        "The ETD programme is in: `%s`.\n\n1. Read the race numbers with the **gala-ocr** skill into `%s/`.\n"
                        "2. Answer briefly: the pages read and anything you couldn't read.\n\n"
                        "Don't run check_etd.py or the audit: the portal checks the race numbers against Gen7 and updates "
                        "the session report itself once every page is read."
                        % (self.P(n, "scans", name), ocr))
            return None
        if kind == "merge":
            if not s["template"] or not s["entriesFile"]:
                return None
            ent, res = "data/" + s["entriesFile"], "data/" + s["template"]
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
            ent, tpl = "data/" + s["entriesFile"], "data/" + s["template"]
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
        if kind == "codes":
            e = int(key.split(":")[2])
            inp = self.event_inputs(n, e, s) or {}
            slips = inp.get("uncoded") or []
            if not slips:
                return None
            return ("Session %d · Event %d: DQ codes for %d slip%s" % (n, e, len(slips), "" if len(slips) == 1 else "s"),
                    "These DQ slips of event %d are transcribed but have no code in `%s/dq-codes.json`:\n\n%s\n\n"
                    "Give each its code from the transcribed infraction and the DQ code list below (**gala-audit** "
                    "skill); don't change codes already given, and ask the referee here about any you're unsure of. "
                    "**Don't read any scans and don't run the scripts**: the portal regenerates the event's merge results "
                    "as soon as the codes are in. Answer with the codes given."
                    % (e, ocr, "\n".join("- `%s/%s`" % (ocr, x) for x in slips)))
        if kind == "fixocr":
            e = int(key.split(":")[2])
            probs = (getattr(self, "_ocr_problems", {}) or {}).get((n, e)) or []
            if not probs:
                return None
            return ("Session %d · Event %d: fix the transcription files" % (n, e),
                    "validate_cache.py (gala-ocr skill) found problems in event %d's transcription files in `%s/`:\n\n"
                    "```\n%s\n```\n\nFix the files (re-read only the pages concerned if you need to), then answer "
                    "briefly. The portal carries on with the event once they pass." % (e, ocr, "\n".join(probs[:40])))
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
                sig = ev and ev["packSig"]
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

    # ---------------------------------------------------------------- work the portal does itself (no Claude)
    # Claude is only asked to READ scans (lane sheet handwriting, DQ slips, the ETD programme) and to code DQ slips.
    # Everything computed from what it has read - the merge results, the audits, the quick fixes, the session report,
    # the session merge results, the final audit, the ETD check - the portal runs with the plugin's scripts, in the
    # background, as soon as its inputs are complete (and again when they change). Jobs: self.jobs[key];
    # the inputs each was last run on: state["computed"][key]["sig"].
    STABLE = 15   # seconds the newest transcription file must be old before a pack counts as fully read

    @staticmethod
    def find_script(name):
        """The newest installed copy of one of the gala-ocr plugin's scripts (any of its skills), or None."""
        import glob
        found = glob.glob(os.path.expanduser("~/.claude/plugins/**/skills/*/scripts/%s" % name), recursive=True)
        return max(found, key=os.path.getmtime) if found else None

    def _script_opts(self, n, codes=True, fixes=True):
        out = []
        if fixes and os.path.exists(self.fixes_file(n)) and self.fixes(n):
            out += ["--fixes", self.fixes_file(n)]
        own = self.meet_files(n)["codes"]
        if codes and own:
            out += ["--codes", os.path.join(self.sdir(n, "meet"), own[-1]["name"])]
        return out

    @staticmethod
    def _run_script(cmd, timeout=600):
        try:
            r = subprocess.run([sys.executable, "-I"] + cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode == 0, (r.stdout + "\n" + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as ex:
            return False, str(ex)

    def _entries_hy3(self, n, tmp):
        """The session's entries export as a .hy3 file (taken out of the zip into tmp if need be)."""
        p = self.meet_path(n, "entries")
        if not p:
            return None
        if not p.lower().endswith(".zip"):
            return p
        import zipfile
        with zipfile.ZipFile(p) as z:
            names = sorted((x for x in z.namelist() if x.lower().endswith(".hy3")),
                           key=lambda x: (not os.path.basename(x).lower().startswith("hfile"), x))
            if not names:
                return None
            out = os.path.join(tmp, "entries.hy3")
            with open(out, "wb") as fh:
                fh.write(z.read(names[0]))
            return out

    def _job(self, key):
        return self.jobs.get(key) or {}

    def _start(self, key, fn, args, who, why, sig):
        """Run fn(*args) -> (ok, message) in the background. sig (the inputs) is recorded whatever the outcome, so a
        failed job isn't retried until its inputs change (or someone runs it again by hand)."""
        if self._job(key).get("state") == "running":
            return False
        self.jobs[key] = {"state": "running", "at": time.time(), "by": who, "why": why}
        threading.Thread(target=self._job_thread, args=(key, fn, args, who, sig), daemon=True).start()
        return True

    def _job_thread(self, key, fn, args, who, sig):
        try:
            ok, msg = fn(*args)
        except Exception as ex:   # never leave a job "running"
            ok, msg = False, "%s: %s" % (type(ex).__name__, ex)
        self.jobs[key] = {"state": "done" if ok else "failed", "at": time.time(), "by": who, "msg": msg[-400:]}
        with self.lock:
            done = self.state.setdefault("computed", {})
            done[key] = {"sig": sig, "at": time.time(), "ok": ok, "msg": msg[-300:]}
            self.save()
        self.log(msg if ok else "Not finished: %s" % msg[-300:])

    def event_inputs(self, n, e, s=None):
        """What the portal needs to compute event e: {complete, missing, sig, slips_uncoded, ocr}."""
        s = s or self.session(n)
        ev = next((x for x in s["events"] if x["event"] == e), None)
        ocr_dir = self.sdir(n, "ocr")
        if not ev:
            return None
        packs = [x for x in s["scans"] if x["name"] in ev["packs"]]
        now = time.time()
        lanes = [f for f in _ls(ocr_dir) if OCR_LANE.search(f["name"]) and not OCR_SLIP.search(f["name"])
                 and int(OCR_LANE.search(f["name"]).group(1)) == e]
        slips = [f for f in _ls(ocr_dir) if OCR_SLIP.search(f["name"]) and int(OCR_SLIP.search(f["name"]).group(1)) == e]
        newest = max([f["mtime"] for f in lanes] or [0])
        read = bool(packs) and all(p["pages"] and p["done"] >= p["pages"] and not p["bad"] for p in packs) \
            and now - newest >= self.STABLE
        missing = []
        if not packs:
            missing.append("its lane sheet pack")
        elif not read:
            missing.append("the transcription of its lane sheets")
        if ev["gen7Missing"] or not ev["heats"]:
            missing.append("Gen7 heat%s %s" % ("s" if len(ev["gen7Missing"]) != 1 else "",
                                                ", ".join(map(str, ev["gen7Missing"])) or "?"))
        codes = {}
        cf = os.path.join(ocr_dir, "dq-codes.json")
        if slips and os.path.exists(cf):
            try:
                with open(cf, encoding="utf-8") as fh:
                    codes = json.load(fh)
            except (OSError, ValueError):
                codes = {}
        uncoded = [f["name"] for f in slips if not (codes.get(f["name"]) or {}).get("code")]
        races = [r for r in self.gen7_races(n) if r["event"] == e]
        meet = self.meet_files(n)
        parts = [[f["name"], f["mtime"]] for f in lanes] + [[r["file"], r["mtime"]] for r in races] \
            + [[f["name"], f["mtime"]] for f in slips] + [["codes", sorted((k, str(v)) for k, v in codes.items()
                                                           if k in {f["name"] for f in slips})]] \
            + [[k, [[f["name"], f["mtime"]] for f in [self.meet_wide(n, k)[1]] if f]] for k in ("entries", "results")] \
            + [["codes", [[f["name"], f["mtime"]] for f in meet["codes"][-1:]]]] \
            + [["notSwum", ev.get("notSwum") or []]]
        return {"complete": not missing, "missing": missing, "read": read, "uncoded": uncoded, "ev": ev,
                "sig": hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]}

    def compute_event(self, n, e, who="portal", why=""):
        """Write event e's merge results, audit, quick fixes and times, and the session report, from what is
        transcribed (no scans are read). Returns a message, or raises ValueError if it can't be done yet."""
        key = "S%d:e%d:run" % (n, e)
        inp = self.event_inputs(n, e)
        if not inp:
            raise ValueError("no event %d in session %d" % (e, n))
        if not inp["read"]:
            raise ValueError("event %d isn't fully transcribed yet" % e)
        if not self.find_script("audit_report.py"):
            raise ValueError("the gala-ocr plugin isn't installed for this user")
        if self._job(key).get("state") == "running":
            return "already running"
        self._start(key, self._compute_event, (n, e, inp), who, why, inp["sig"])
        return "started"

    def _compute_event(self, n, e, inp):
        ev = inp["ev"]
        rep, ocr, gen7, scans = self.sdir(n, "reports"), self.sdir(n, "ocr"), self.sdir(n, "gen7"), self.sdir(n, "scans")
        audit, merge = self.find_script("audit_report.py"), self.find_script("merge_results.py")
        tmp = tempfile.mkdtemp(prefix="gala-run-")
        notes = []
        try:
            vc = self.find_script("validate_cache.py")
            if vc:
                files = [os.path.join(ocr, f["name"]) for f in _ls(ocr)
                         if (OCR_LANE.search(f["name"]) or OCR_SLIP.search(f["name"]))
                         and int((OCR_SLIP.search(f["name"]) or OCR_LANE.search(f["name"])).group(1)) == e]
                ok, out = self._run_script([vc] + files, 120)
                if not ok:
                    bad = [ln for ln in out.splitlines() if ln.strip() and "files checked" not in ln]
                    self.ask_fix_transcription(n, e, bad)
                    return False, "Event %d: %d problem%s in the transcription files; Claude was asked to fix them" % (
                        e, len(bad), "" if len(bad) == 1 else "s")
            mfile = None
            if ev["canMerge"] and merge:
                ent = self._entries_hy3(n, tmp)
                tpl = self.meet_path(n, "results")
                mfile = os.path.join(rep, "Event %02d merge results %s.hy3" % (e, time.strftime("%Y-%m-%d_%H%M")))
                ok, out = self._run_script([merge, ocr, gen7, ent, tpl, mfile, "--events", str(e)] + self._script_opts(n))
                if not ok:
                    return False, "event %d merge results: %s" % (e, out.splitlines()[-1] if out else "failed")
                notes.append("merge results %s" % os.path.basename(mfile))
            else:
                notes.append("no merge results yet (needs the entries export and the empty merge results export)")
            cmd = [audit, ocr, gen7, os.path.join(rep, "Event %02d audit.html" % e), "--events", str(e), "--scans", scans,
                   "--quick-fixes", os.path.join(rep, "Event %02d quick fixes.html" % e),
                   "--times", os.path.join(rep, "Event %02d times.json" % e)] + (["--merge", mfile] if mfile else [])
            ok, out = self._run_script(cmd + self._script_opts(n))
            if not ok:
                return False, "event %d audit: %s" % (e, out.splitlines()[-1] if out else "failed")
            m = re.search(r"(\d+) swims? in Quick Fix", out)
            qf = int(m.group(1)) if m else None
            notes.append("%s quick fix%s" % (qf, "" if qf == 1 else "es") if qf is not None else "quick fixes written")
            self.rechecks.pop((n, e), None)
            self.compute_session_report(n)
            if inp["uncoded"]:
                notes.append("%d DQ slip%s without a code (Claude was asked)" % (len(inp["uncoded"]), "" if len(inp["uncoded"]) == 1 else "s"))
                self.ask_codes(n, e, inp["uncoded"])
            return True, "Event %d done by the portal: %s" % (e, "; ".join(notes))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def compute_session_report(self, n):
        """The session audit report, for every event transcribed so far (synchronous; called from jobs)."""
        audit = self.find_script("audit_report.py")
        if not audit:
            return False
        s = self.session(n)
        done = ",".join(str(x["event"]) for x in s["events"] if x["ocrLanes"])
        if not done:
            return False
        out = os.path.join(self.sdir(n, "reports"), "Session %d audit.html" % n)
        ok, msg = self._run_script([audit, self.sdir(n, "ocr"), self.sdir(n, "gen7"), out, "--events", done,
                                    "--scans", self.sdir(n, "scans")] + self._script_opts(n))
        if not ok:
            self.log("Session %d report: %s" % (n, msg.splitlines()[-1] if msg else "failed"))
        return ok

    def compute_session(self, n, who="portal", why=""):
        """The session's merge results (every event) and the session audit with its Quick Fix list."""
        s = self.session(n)
        if not s["template"] or not s["entriesFile"]:
            raise ValueError("needs the entries export and Meet Manager's empty merge results export")
        self._start("S%d:merge" % n, self._compute_session, (n,), who, why, self._session_sig(n, s))

    def _session_sig(self, n, s):
        parts = [[k, v.get("sig"), v.get("at")] for k, v in sorted(self.state.get("computed", {}).items())
                 if k.startswith("S%d:e" % n)]   # any event worked out again (a regenerate too)
        parts += [[f["name"], f["mtime"]] for k in ("results", "entries") for f in [self.meet_wide(n, k)[1]] if f]
        return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]

    def _compute_session(self, n):
        rep, ocr, gen7 = self.sdir(n, "reports"), self.sdir(n, "ocr"), self.sdir(n, "gen7")
        tmp = tempfile.mkdtemp(prefix="gala-run-")
        try:
            ent = self._entries_hy3(n, tmp)
            tpl = self.meet_path(n, "results")
            out = os.path.join(rep, "Merge Meet Results-S%d-audit %s.hy3" % (n, time.strftime("%Y-%m-%d_%H%M")))
            ok, msg = self._run_script([self.find_script("merge_results.py"), ocr, gen7, ent, tpl, out] + self._script_opts(n))
            if not ok:
                return False, "session %d merge results: %s" % (n, msg.splitlines()[-1] if msg else "failed")
            s = self.session(n)
            done = ",".join(str(x["event"]) for x in s["events"] if x["ocrLanes"])
            ok, msg = self._run_script([self.find_script("audit_report.py"), ocr, gen7, os.path.join(rep, "Session %d audit.html" % n),
                                        "--events", done, "--scans", self.sdir(n, "scans"), "--merge", out,
                                        "--quick-fixes", os.path.join(rep, "Session %d quick fixes.html" % n)] + self._script_opts(n))
            m = re.search(r"(\d+) swims? in Quick Fix", msg)
            return ok, ("Session %d merge results written by the portal: %s%s" % (
                n, os.path.basename(out), "; %s quick fixes for the session" % m.group(1) if m else "")
                if ok else "session %d audit: %s" % (n, msg.splitlines()[-1] if msg else "failed"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def compute_final(self, n, who="portal", why=""):
        s = self.session(n)
        if not s["meet"]["final"] or not s["merged"]:
            raise ValueError("needs Meet Manager's final export and the session's merge results")
        sig = hashlib.sha1(json.dumps([s["meet"]["final"][-1], s["merged"][-1]]).encode()).hexdigest()[:16]
        self._start("S%d:final" % n, self._compute_final, (n,), who, why, sig)

    def _compute_final(self, n):
        s = self.session(n)
        rep = self.sdir(n, "reports")
        fin = os.path.join(self.sdir(n, "meet"), s["meet"]["final"][-1])
        cmd = [self.find_script("final_audit.py"), os.path.join(rep, s["merged"][-1]), fin,
               os.path.join(rep, "Session %d final audit.html" % n)]
        if os.path.exists(os.path.join(rep, "Session %d quick fixes.json" % n)):
            cmd += ["--quick-fixes", os.path.join(rep, "Session %d quick fixes.json" % n)]
        ok, msg = self._run_script(cmd + self._script_opts(n, fixes=False))
        last = next((ln for ln in reversed(msg.splitlines()) if ln.strip() and not ln.startswith("Wrote")), "") if msg else ""
        return ok, ("Session %d final audit written by the portal (Session %d final audit.html)%s" % (n, n, ": " + last if last else "")
                    if ok else "session %d final audit: %s" % (n, last or "failed"))

    def compute_etd(self, n, sig, who="portal"):
        self._start("S%d:etd" % n, self._compute_etd, (n,), who, "ETD programme read", sig)

    def _compute_etd(self, n):
        script = self.find_script("check_etd.py")
        if not script:
            return False, "no check_etd.py in the plugin"
        ok, msg = self._run_script([script, self.sdir(n, "ocr"), self.sdir(n, "gen7")], 300)
        out = os.path.join(self.sdir(n, "reports"), "Session %d ETD check.txt" % n)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(msg + "\n")
        self.compute_session_report(n)
        problems = [ln for ln in msg.splitlines() if ln.strip()]
        return True, ("Session %d ETD programme checked by the portal: %s (Session %d ETD check.txt)" % (
            n, "no problems" if ok else "%d line%s to look at" % (len(problems), "" if len(problems) == 1 else "s"), n))

    def scan_read(self, n, sc):
        """A scan whose every page is transcribed, the newest transcription a little while ago."""
        if not sc["pages"] or sc["done"] < sc["pages"] or sc["bad"]:
            return False
        stem = sc["name"][:-4] + "-"
        newest = max([f["mtime"] for f in _ls(self.sdir(n, "ocr")) if f["name"].startswith(stem)] or [0])
        return time.time() - newest >= self.STABLE

    def ask_codes(self, n, e, slips):
        key = "S%d:codes:%d" % (n, e)
        sig = hashlib.sha1(json.dumps(sorted(slips)).encode()).hexdigest()[:12]
        prev = self.state["asked"].get(key)
        if prev and prev.get("sig") == sig:
            return
        if self.ask(key, force=True):
            self.state["asked"][key]["sig"] = sig
            self.save()

    def ask_fix_transcription(self, n, e, problems):
        key = "S%d:fixocr:%d" % (n, e)
        sig = hashlib.sha1(json.dumps(sorted(problems)).encode()).hexdigest()[:12]
        prev = self.state["asked"].get(key)
        if prev and prev.get("sig") == sig:
            return
        self._ocr_problems = getattr(self, "_ocr_problems", {})
        self._ocr_problems[(n, e)] = problems
        if self.ask(key, force=True):
            self.state["asked"][key]["sig"] = sig
            self.save()

    def check_report(self, n):
        """What is in and what is missing for the session, per event (what Claude was asked to check by hand)."""
        s = self.session(n)
        lines = []
        if not s["entriesFile"]:
            lines.append("No entries export yet.")
        if not s["template"]:
            lines.append("No empty merge results export yet (one for the whole gala; needed for the merge results).")
        if not s["lanePrints"]:
            lines.append("No printed lane sheets yet.")
        for ev in s["events"]:
            inp = self.event_inputs(n, ev["event"], s) or {}
            st = {"audited": "done", "changed": "done, inputs changed since"}.get(ev["status"])
            if st:
                lines.append("Event %d: %s." % (ev["event"], st))
            else:
                lines.append("Event %d: waiting for %s." % (ev["event"], ", ".join(inp.get("missing") or ["nothing"])))
        dq = [x for x in s["scans"] if x["kind"] == "dq"]
        unread = [x["name"] for x in dq if x["pages"] and x["done"] < x["pages"]]
        lines.append("DQ slips: %d batch%s%s." % (len(dq), "" if len(dq) == 1 else "es",
                                                 ", not fully read: " + ", ".join(unread) if unread else ""))
        if not s["meet"]["final"]:
            lines.append("No final export yet.")
        return "\n".join(lines)

    def auto_compute(self):
        """Run what the portal can work out itself, when its inputs are complete and have changed."""
        if not self.state["auto"]:
            return
        done = self.state.setdefault("computed", {})
        for n in self.sessions():
            s = self.session(n)
            for ev in s["events"]:
                key = "S%d:e%d:run" % (n, ev["event"])
                inp = self.event_inputs(n, ev["event"], s)
                if inp and inp["complete"] and (done.get(key) or {}).get("sig") != inp["sig"] \
                        and self._job(key).get("state") != "running":
                    try:
                        self.compute_event(n, ev["event"], "portal", "inputs complete")
                    except ValueError:
                        pass
            for sc in s["scans"]:
                if sc["kind"] == "etd" and self.scan_read(n, sc):
                    sig = "%s:%s:%s" % (sc["name"], sc["mtime"], sc["done"])
                    if (done.get("S%d:etd" % n) or {}).get("sig") != sig and self._job("S%d:etd" % n).get("state") != "running":
                        self.compute_etd(n, sig)
                if sc["kind"] == "dq" and self.scan_read(n, sc):   # the events with slips rerun via their own inputs
                    sig = "%s:%s:%s" % (sc["name"], sc["mtime"], sc["done"])
                    k = "S%d:dq:%s" % (n, sc["name"])
                    if (done.get(k) or {}).get("sig") != sig:
                        done[k] = {"sig": sig, "at": time.time(), "ok": True}
                        self.save()
                        threading.Thread(target=self.compute_session_report, args=(n,), daemon=True).start()
            all_done = s["events"] and all(e["status"] == "audited" and (done.get("S%d:e%d:run" % (n, e["event"])) or {}).get("ok")
                                           for e in s["events"])
            if all_done and s["template"] and s["entriesFile"]:
                k = "S%d:merge" % n
                if (done.get(k) or {}).get("sig") != self._session_sig(n, s) and self._job(k).get("state") != "running":
                    self.compute_session(n, "portal", "every event done")
            if s["meet"]["final"] and s["merged"]:
                k = "S%d:final" % n
                sig = hashlib.sha1(json.dumps([s["meet"]["final"][-1], s["merged"][-1]]).encode()).hexdigest()[:16]
                if (done.get(k) or {}).get("sig") != sig and self._job(k).get("state") != "running":
                    self.compute_final(n, "portal", "final export in")

    def auto_ask(self):
        """Ask Claude to READ what has arrived: an event's lane sheet pack (until it is fully transcribed), DQ slips
        and the ETD programme. Everything worked out from what it read, the portal does itself (auto_compute)."""
        if not self.state["auto"]:
            return
        for n in self.sessions():
            s = self.session(n)
            for ev in s["events"]:
                key = "S%d:e%d" % (n, ev["event"])
                prev = self.state["asked"].get(key)
                if ev["packs"] and not ev["read"] and (not prev or prev.get("sig") != ev["packSig"]):
                    self.ask(key)
            for sc in s["scans"]:
                if sc["kind"] in ("dq", "etd") and not (sc["pages"] and sc["done"] >= sc["pages"]):
                    key = "S%d:scan:%s" % (n, sc["name"])
                    if key not in self.state["asked"]:
                        self.ask(key)

    def act(self, key, who):
        """A button on the checklist: work the portal does itself, or a question for Claude."""
        m = re.fullmatch(r"S(\d{1,2}):(remerge|merge|final|check|etd)(?::(\d{1,4}))?", key or "")
        if m:
            n, kind = int(m.group(1)), m.group(2)
            if kind == "remerge":
                return {"job": self.compute_event(n, int(m.group(3) or 0), who, "regenerate")}
            if kind == "merge":
                self.compute_session(n, who, "asked")
                return {"job": "started"}
            if kind == "final":
                self.compute_final(n, who, "asked")
                return {"job": "started"}
            if kind == "etd":
                self.compute_etd(n, "manual %s" % time.time(), who)
                return {"job": "started"}
            return {"report": self.check_report(n)}
        row = self.ask(key, who, force=True)
        if not row:
            raise ValueError("nothing to ask yet for that (its files aren't in)")
        return {"question": row["id"]}

    def run(self, every=4):
        while True:
            try:
                self.sort_inbox()
                if not getattr(self, "_refiled", False):
                    self.refile()
                    self._refiled = True
                self.ignore_strays()
                self.auto_ask()
                self.auto_compute()
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
                # The preferred name, when the entries have one, is the name used (a nickname rather than the legal first name).
                out.append((score, dict(s, entered=entered,
                                        name="%s, %s" % (s["surname"], s.get("preferred") or s["first"]))))
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

    # ---------------------------------------------------------------- restarting: clear the gala out
    def _size(self, path):
        if os.path.islink(path):
            return 0, 0
        if os.path.isfile(path):
            return 1, os.path.getsize(path)
        files = size = 0
        for dp, dirs, fs in os.walk(path):
            for f in fs:
                try:
                    size += os.path.getsize(os.path.join(dp, f))
                    files += 1
                except OSError:
                    pass
        return files, size

    def _inbox_own(self):
        """What is in the inbox itself: files and folders, but not linked folders (those belong to other computers)."""
        return [os.path.join(self.inbox, f["name"]) for f in _ls(self.inbox)
                if not self._linked(os.path.join(self.inbox, f["name"]))]

    @staticmethod
    def _linked(path):
        """A folder that belongs to another computer: a symbolic link, or a share mounted straight into the inbox."""
        return os.path.islink(path) or os.path.ismount(path)

    def summary(self):
        files, size = 0, 0
        for p in [os.path.join(self.root, d) for d in ("sessions", "unsorted", "ignored")] + self._inbox_own():
            if os.path.exists(p):
                f, b = self._size(p)
                files, size = files + f, size + b
        return {"sessions": len(self.sessions()), "files": files, "bytes": size,
                "questions": self.store.db.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
                "documents": self.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                "linked": [f["name"] for f in _ls(self.inbox) if self._linked(os.path.join(self.inbox, f["name"]))]}

    def archives(self):
        out = []
        for f in _ls(os.path.join(self.root, "archive")):
            if f["dir"]:
                a = os.path.join(self.root, "archive", f["name"])
                n, b = self._size(a)
                ss = os.path.join(a, "sessions")
                out.append({"name": f["name"], "files": n, "bytes": b, "mtime": f["mtime"],
                            "sessions": len([x for x in os.listdir(ss) if re.fullmatch(r"S\d+", x)]) if os.path.isdir(ss) else 0,
                            "questions": os.path.exists(os.path.join(a, "portal-questions.db"))})
        return sorted(out, key=lambda a: a["name"], reverse=True)

    def restart(self, mode, questions, reimport, label, who):
        """Clear the gala out: its sessions, unsorted files, what is waiting in the inbox (not linked folders) and the
        portal's state (ticks, history, log). mode "archive" moves it all to data/archive/<date time label>/;
        "delete" deletes it for good. questions: also clear the Questions, answers, documents and deployments.
        reimport: forget which files were copied from linked inbox folders, so they are copied in again."""
        if mode not in ("archive", "delete"):
            raise ValueError("mode must be archive or delete")
        with self.lock:
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            name = stamp + ("_" + re.sub(r"[^\w -]", "", label or "").strip()[:40] if (label or "").strip() else "")
            dest = os.path.join(self.root, "archive", name)
            moving = [os.path.join(self.root, "sessions"), os.path.join(self.root, "unsorted"), os.path.join(self.root, "ignored"),
                      self.state_file] + self._inbox_own()
            for p in moving:
                if not os.path.exists(p):
                    continue
                if mode == "archive":
                    rel = "inbox/" + os.path.basename(p) if os.path.dirname(p) == self.inbox else os.path.relpath(p, self.root)
                    os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
                    shutil.move(p, os.path.join(dest, rel))
                elif os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            if questions:
                st = self.store
                with st.lock:
                    if mode == "archive":
                        os.makedirs(dest, exist_ok=True)
                        st.db.commit()
                        bk = sqlite3.connect(os.path.join(dest, "portal-questions.db"))
                        st.db.backup(bk)
                        bk.close()
                        if os.path.isdir(st.docdir):
                            shutil.copytree(st.docdir, os.path.join(dest, "documents"), dirs_exist_ok=True)
                    for t in ("answers", "questions", "document_versions", "documents", "deployments"):
                        st.db.execute("DELETE FROM %s" % t)
                    st.db.commit()
                    st.attention = None
                    if os.path.isdir(st.docdir):
                        for f in os.listdir(st.docdir):
                            if f == ".tmp":   # the store's upload area: keep it
                                continue
                            p = os.path.join(st.docdir, f)
                            shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
            copied = {} if reimport else self.state.get("copied", {})
            self.state = {"auto": self.state.get("auto", True), "log": [], "asked": {}, "ticks": {}, "qf": {},
                          "copied": copied}
            self.rechecks, self.cache, self.seen = {}, {}, {}
            for d in (self.inbox, os.path.join(self.root, "sessions"), os.path.join(self.root, "unsorted")):
                os.makedirs(d, exist_ok=True)
            self.save()
        what = ("archived to data/archive/%s" % name) if mode == "archive" else "deleted"
        self.log("Restarted by %s: the gala's files and the portal's state %s%s" % (
            who or "someone", what, "; questions and documents cleared" if questions else ""))
        return {"archive": name if mode == "archive" else None}

    def restore_archive(self, name, questions, who):
        """Bring an archived gala back: its sessions, unsorted and inbox files and the portal's state (ticks, quick-fix
        history, log), and with questions its Questions, answers, documents and deployments. Whatever is in the portal
        now is archived first (nothing is deleted), so a restore can itself be undone the same way."""
        src = os.path.join(self.root, "archive", name or "")
        if not re.fullmatch(r"[\w -]{1,80}", name or "") or not os.path.isdir(src):
            raise ValueError("no such archive")
        qdb = os.path.join(src, "portal-questions.db")
        questions = bool(questions) and os.path.exists(qdb)
        with self.lock:
            # 1. set aside what is here now
            now = self.summary()
            saved = None
            if now["sessions"] or now["files"] or (questions and (now["questions"] or now["documents"])):
                saved = self.restart("archive", questions, False, "before a restore", who)["archive"]
            elif questions:
                self.restart("delete", True, False, "", who)   # nothing here but empty tables: just clear them
            copied = self.state.get("copied", {})
            # 2. the files
            for d in ("sessions", "unsorted", "ignored"):
                dst = os.path.join(self.root, d)
                if os.path.isdir(os.path.join(src, d)):
                    if os.path.isdir(dst) and not os.listdir(dst):
                        os.rmdir(dst)
                    shutil.move(os.path.join(src, d), dst)
            inbox_src = os.path.join(src, "inbox")
            if os.path.isdir(inbox_src):
                for f in os.listdir(inbox_src):
                    dst = os.path.join(self.inbox, f)
                    if os.path.lexists(dst):   # e.g. a linked folder of the same name: keep it, park the file
                        dst = os.path.join(self.root, "unsorted", f)
                    shutil.move(os.path.join(inbox_src, f), dst)
                shutil.rmtree(inbox_src, ignore_errors=True)
            # 3. the state
            try:
                with open(os.path.join(src, "state.json"), encoding="utf-8") as f:
                    st = json.load(f)
            except (OSError, ValueError):
                st = {}
            for k, v in (("auto", True), ("log", []), ("asked", {}), ("ticks", {}), ("qf", {}), ("copied", {})):
                st.setdefault(k, v)
            st["copied"] = dict(st["copied"], **copied)   # files already copied from linked folders stay copied
            self.state = st
            self.rechecks, self.cache, self.seen = {}, {}, {}
            for d in (self.inbox, os.path.join(self.root, "sessions"), os.path.join(self.root, "unsorted")):
                os.makedirs(d, exist_ok=True)
            self.save()
            # 4. the questions, answers and documents
            if questions:
                stq = self.store
                with stq.lock:
                    stq.db.execute("ATTACH DATABASE ? AS arc", (qdb,))
                    try:
                        for t in ("questions", "answers", "documents", "document_versions", "deployments"):
                            have = [r[1] for r in stq.db.execute("PRAGMA arc.table_info(%s)" % t)]
                            cols = [c for c in (r[1] for r in stq.db.execute("PRAGMA main.table_info(%s)" % t)) if c in have]
                            if cols:
                                stq.db.execute("INSERT OR REPLACE INTO main.%s(%s) SELECT %s FROM arc.%s"
                                               % (t, ",".join(cols), ",".join(cols), t))
                        stq.db.commit()
                    finally:
                        stq.db.execute("DETACH DATABASE arc")
                    docs = os.path.join(src, "documents")
                    if os.path.isdir(docs):
                        shutil.copytree(docs, stq.docdir, dirs_exist_ok=True)
            shutil.rmtree(src)   # it is back in the portal now
        self.log("Archive %s restored by %s%s%s" % (name, who or "someone", " with its questions and documents" if questions else "",
                                                     "; what was here is in data/archive/%s" % saved if saved else ""))
        return {"restored": name, "archive": saved}

    def delete_archive(self, name):
        p = os.path.join(self.root, "archive", name)
        if not re.fullmatch(r"[\w -]{1,80}", name or "") or not os.path.isdir(p):
            raise ValueError("no such archive")
        shutil.rmtree(p)
        self.log("Archive %s deleted for good" % name)

    # ---- updates from GitHub (the portal folder is a git clone; update.sh does the work)
    def project_dir(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # portal/portal.py -> the project

    def update_status(self, fetch=False):
        d = self.project_dir()
        out = {"git": os.path.isdir(os.path.join(d, ".git")) and bool(shutil.which("git")), "version": VERSION}
        if not out["git"]:
            return out

        def git(*a, timeout=10):
            r = subprocess.run(["git", "-C", d] + list(a), capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() if r.returncode == 0 else None
        try:
            branch = git("rev-parse", "--abbrev-ref", "HEAD") or "main"
            branch = "main" if branch == "HEAD" else branch
            cached = getattr(self, "_upd", None)
            if fetch or not cached or time.time() - cached > 600:
                out["fetched"] = git("fetch", "-q", "origin", branch, timeout=30) is not None
                self._upd = time.time() if out["fetched"] else None
            out["checkedAt"] = getattr(self, "_upd", None)
            out["current"] = git("log", "-1", "--format=%h|%cd|%s", "--date=format:%Y-%m-%d %H:%M")
            log = git("log", "--format=%h|%cd|%s", "--date=format:%Y-%m-%d", "HEAD..origin/" + branch) or ""
            out["behind"] = [dict(zip(("sha", "date", "subject"), x.split("|", 2))) for x in log.splitlines() if x]
            out["ahead"] = int(git("rev-list", "--count", "origin/%s..HEAD" % branch) or 0)
            out["changed"] = [x[3:] for x in (git("status", "--porcelain", "--untracked-files=no") or "").splitlines()
                              if x[3:] != ".claude/settings.json"]   # start-portal.sh rewrites it; update.sh puts it back
        except (OSError, subprocess.SubprocessError) as e:
            out["error"] = str(e)
        lf = os.path.join(d, ".portal", "update.log")
        if os.path.exists(lf):
            with open(lf, encoding="utf-8", errors="replace") as f:
                out["log"] = f.read()[-3000:]
        return out

    def start_update(self, who, claude=False):
        """Runs update.sh --restart on its own (it stops this portal, updates and starts it again)."""
        d = self.project_dir()
        st = self.update_status()
        if not st.get("git"):
            raise ValueError("this copy isn't a git clone, so it can't update itself")
        if st.get("changed"):
            raise ValueError("files were changed on this computer: %s" % ", ".join(st["changed"][:6]))
        if st.get("ahead"):
            raise ValueError("this copy has changes of its own that aren't on GitHub")
        os.makedirs(os.path.join(d, ".portal"), exist_ok=True)
        log = open(os.path.join(d, ".portal", "update.log"), "a", encoding="utf-8")
        log.write("\n---- update asked for by %s\n" % who)
        log.flush()
        subprocess.Popen(["bash", os.path.join(d, "update.sh"), "--restart"] + (["--restart-claude"] if claude else []), cwd=d, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        self.log("Update started by %s; the portal restarts in a minute" % who)

    def tick(self, key, checked, who, not_swum=None):
        """Tick a checklist item. For an event (S1:e105) ticked complete while heats have no Gen7 race, not_swum is the
        list of those heats, confirmed as not swum: the event then counts as having all its races."""
        if not re.fullmatch(r"S\d{1,2}:[\w.:=() \-]{1,200}", key or ""):
            raise ValueError("bad checklist key")
        with self.lock:
            ns = self.state.setdefault("notSwum", {})
            if checked:
                self.state["ticks"][key] = {"by": str(who or "someone")[:60], "at": time.time()}
                heats = sorted({int(h) for h in (not_swum or []) if str(h).isdigit() and 0 < int(h) < 100})
                if heats and re.fullmatch(r"S\d{1,2}:e\d{1,4}", key):
                    ns[key] = {"heats": heats, "by": str(who or "someone")[:60], "at": time.time()}
            else:
                self.state["ticks"].pop(key, None)
                ns.pop(key, None)
            self.save()
        if checked and not_swum and key in self.state.get("notSwum", {}):
            self.log("Event %s ticked complete by %s: heat%s %s confirmed not swum" % (
                key.split(":e")[1], who or "someone", "s" if len(ns[key]["heats"]) > 1 else "",
                ", ".join(map(str, ns[key]["heats"]))))

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


def iso_now():
    """The time now in ISO 8601 with this computer's offset, e.g. 2026-11-09T14:32:05+02:00."""
    z = time.strftime("%z")
    return time.strftime("%Y-%m-%dT%H:%M:%S") + (z[:3] + ":" + z[3:] if len(z) == 5 else "")


def deployment_times(state, prev):
    """Fill in the times a report leaves out, from the previous report or the time it arrives, so the board can
    show when the deployment and each step started and finished, how long they took and an ETA:
    the deployment's started (kept from the first report), updated and finished (once done or failed), and each
    step's started (when first seen running) and finished (when first seen done or failed)."""
    now = iso_now()
    state["started"] = state.get("started") or prev.get("started") or now
    state["updated"] = state.get("updated") or now
    if state.get("status") in ("done", "failed"):
        state["finished"] = state.get("finished") or prev.get("finished") or now
    else:
        state.pop("finished", None)
    old = {x.get("id"): x for x in prev.get("steps") or [] if isinstance(x, dict) and x.get("id")}
    last_end = state["started"]
    for x in state.get("steps") or []:
        if not isinstance(x, dict):
            continue
        p = old.get(x.get("id")) or {}
        st = x.get("status")
        if st in ("running", "done", "failed") and not x.get("started"):
            # first seen running now; reported straight as done: it started when the step before it ended
            x["started"] = p.get("started") or (now if st == "running" else last_end)
        if st in ("done", "failed") and not x.get("finished"):
            x["finished"] = p.get("finished") or now
        if "estimate" in x:   # seconds the step is expected to take
            try:
                x["estimate"] = max(0.0, float(x["estimate"]))
            except (TypeError, ValueError):
                x.pop("estimate")
        if x.get("finished"):
            last_end = x["finished"]
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
        "name": "who_is_watching",
        "description": "Who is watching your session from the board right now: people with the board's terminal "
                       "window open (who can type to you), on its Live tab, or just on the board, by the name they "
                       "chose there, and how many other terminals are attached. Check before asking someone to do "
                       "something at the keyboard (a sign-in, an approval) or when deciding whether to wait for an answer.",
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
                       "or failed. Times (ISO 8601 with timezone, e.g. from `date -Iseconds`): the deployment's started "
                       "(once, kept), each step's started and finished, kept in every later report; give eta and a "
                       "step's estimate (seconds) when you can tell, and the board shows the time left (otherwise it "
                       "guesses from the steps done; times you leave out are filled in when the reports arrive). "
                       "waitingFor ({reason, url, code}) raises the board's Needs-you banner and is cleared "
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
                "started": {"type": "string", "description": "ISO 8601 time the deployment began (keep it the same in every report)"},
                "updated": {"type": "string", "description": "ISO 8601 time of this report"},
                "finished": {"type": "string", "description": "ISO 8601 time it ended (done or failed)"},
                "eta": {"type": "string", "description": "ISO 8601 time you expect it to finish, if you can tell"},
                "steps": {"type": "array", "description": "In order: {id, label, status, started?, finished?, "
                          "estimate? (seconds the step usually takes), detail?, stages?: [{name, status}]}. "
                          "started/finished are ISO 8601 times", "items": {"type": "object"}},
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
    "state each time a step starts or finishes, with each step's started and finished times and an eta or step "
    "estimates when you can tell) so the team can follow it on the board. "
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
    if name == "who_is_watching":
        return [_text(store.watching_line() or "Nobody is on the board right now (no board page open).")]
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
        if len(args) > 1 and str(args[1]).startswith(("2", "3")) and str(args[0]).startswith("GET "):
            return
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
        return getattr(self.server, "brand_dir", None) or os.path.join(os.path.dirname(self.server.store.docdir), "brand")

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
        extra = ""
        if os.path.isfile(os.path.join(self._brand_dir(), "brand.css")):
            v = int(os.path.getmtime(os.path.join(self._brand_dir(), "brand.css")))
            extra += '\n<link rel="stylesheet" href="/brand/brand.css?v=%d">' % v
        st = self.server.store.settings()
        if st.get("css"):   # the Settings tab's own CSS, after brand.css so it wins
            extra += '\n<style id="settings-css">%s</style>' % re.sub(r"(?i)</style", "<\\/style", st["css"])
        if extra:
            page = page.replace("</style>", "</style>" + extra, 1)
        if st.get("logo"):
            page = page.replace('<div class="brand"><img src="/favicon.svg" alt="" width="34" height="34">',
                                '<div class="brand"><img src="/brand-logo?v=%d" alt="" height="34" class="setlogo">'
                                % (zlib_crc(st["logo"]) & 0xffff), 1)
        return page

    def _logo(self):
        st = self.server.store.settings()
        try:
            d = json.loads(st.get("logo") or "{}")
            data = base64.b64decode(d.get("data") or "")
        except (ValueError, TypeError):
            data = b""
        if not data:
            return self._favicon()
        return self._send(200, data, d.get("mime") or "image/png", {"Cache-Control": "public, max-age=3600",
                                                                     "X-Content-Type-Options": "nosniff"})

    def _settings_out(self):
        st, srv = self.server.store.settings(), self.server
        return {"name": st.get("name") or "", "accent": st.get("accent") or "", "css": st.get("css") or "",
                "logo": bool(st.get("logo")), "defaults": {"name": srv.cli_brand, "accent": srv.cli_accent},
                "effective": {"name": srv.brand, "accent": srv.accent},
                "brandCss": os.path.isfile(os.path.join(self._brand_dir(), "brand.css")), "version": VERSION}

    def _apply_settings(self):
        st, srv = self.server.store.settings(), self.server
        srv.brand = st.get("name") or srv.cli_brand
        srv.accent = st.get("accent") or srv.cli_accent
        srv.page = render_page(srv.brand, srv.accent, srv.terminal_url)

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

    def _gala(self):
        return getattr(self.server, "gala", None)

    # ---- the web terminal, served through the portal: /terminal/ro/ (watch) and /terminal/rw/ (type)
    # Two ttyd's on 127.0.0.1 attach to Claude's tmux session; the portal passes the page and its WebSocket through,
    # so the browser only talks to the portal, with its key. Watching needs the portal's key; typing needs the
    # admin key too (or a browser on this computer).
    def _term_token(self, mode):
        secret = (self.server.admin_key if mode == "rw" and getattr(self.server, "admin_key", "") else self.server.key) or ""
        return hmac.new(secret.encode(), ("gala-term-" + mode).encode(), hashlib.sha256).hexdigest()[:32]

    def _term_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _terminal(self):
        raw = urllib.parse.urlsplit(self.path)
        parts = raw.path.split("/")
        mode = parts[2] if len(parts) > 2 else ""
        port = (getattr(self.server, "term_ports", None) or {}).get(mode)
        if not port:
            return self._send(404, "No web terminal here (install ttyd and start the portal again).", "text/plain")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(raw.query).items()}
        cookie = "gala_term_" + mode
        jar = {}
        for c in (self.headers.get("Cookie") or "").split(";"):
            if "=" in c:
                k, v = c.strip().split("=", 1)
                jar[k] = v
        token = self._term_token(mode)
        by_cookie = hmac.compare_digest(jar.get(cookie, ""), token)
        admin_ok = bool(self.server.admin_key) and hmac.compare_digest(q.get("admin", ""), self.server.admin_key)
        # typing: the admin key if one is given (a wrong one is refused, even here), else only on this computer
        by_query = self._authorized(q) and (mode == "ro" or (admin_ok if "admin" in q else self._term_local()))
        if not (by_cookie or by_query):
            if mode == "rw" and self._authorized(q):
                return self._send(403, "Typing in the terminal needs the admin key (see start-portal.sh on the portal computer).", "text/plain")
            return self._send(401, "Missing or wrong key.", "text/plain")
        # the request for ttyd, without the keys
        rest = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(raw.query) if k not in ("key", "admin")])
        upgrade = (self.headers.get("Upgrade") or "").lower() == "websocket"
        lines = ["GET %s%s HTTP/1.1" % (raw.path, "?" + rest if rest else ""), "Host: 127.0.0.1:%d" % port]
        for k, v in self.headers.items():
            if k.lower() in ("host", "cookie", "connection", "keep-alive", "x-qa-key") or (not upgrade and k.lower() == "upgrade"):
                continue
            lines.append("%s: %s" % (k, v))
        lines.append("Connection: Upgrade" if upgrade else "Connection: close")
        try:
            up = socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError:
            return self._send(502, "The web terminal isn't running (start-portal.sh starts it).", "text/plain")
        self.close_connection = True
        try:
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = up.recv(65536)
                if not chunk:
                    break
                head += chunk
                if len(head) > 65536:
                    break
            if by_query and not by_cookie and b"\r\n" in head:   # remember the browser for the page's own requests
                first, rest_b = head.split(b"\r\n", 1)
                head = first + ("\r\nSet-Cookie: %s=%s; Path=/terminal/%s/; HttpOnly; SameSite=Strict"
                                % (cookie, token, mode)).encode() + b"\r\n" + rest_b
            self.connection.sendall(head)
            up.settimeout(None)
            self.connection.settimeout(None)
            socks = [self.connection, up]
            while True:
                r, _, _ = select.select(socks, [], [], 3600)
                if not r:
                    break
                for s_ in r:
                    data = s_.recv(65536)
                    if not data:
                        return
                    (up if s_ is self.connection else self.connection).sendall(data)
        except OSError:
            return
        finally:
            try:
                up.close()
            except OSError:
                pass

    def _gala_file(self, q):
        g = self._gala()
        p = gala_path(g.root, q.get("path")) if g else None
        if not p:
            return self._json(404, {"error": "no such file in the data folder"})
        name = os.path.basename(p)
        with open(p, "rb") as f:
            head = f.read(2048)
        mime = guess_mime(name, head)
        low = name.lower()
        if low.endswith((".hy3", ".cl2", ".ev3", ".csv", ".log", ".md", ".txt")):
            mime = "text/plain"
        elif low.endswith(".json"):
            mime = "application/json"
        quoted = urllib.parse.quote(name)
        headers = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
        if q.get("dl"):
            ctype = "application/octet-stream"
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quoted
        elif mime in ("text/html", "application/xhtml+xml"):
            # Audit reports are HTML with their own scripts (filters, expand/collapse). They run in a sandbox with
            # their own empty origin: no access to the portal, and no network apart from images and styles.
            ctype = "text/html; charset=utf-8"
            headers["Content-Disposition"] = "inline; filename*=UTF-8''%s" % quoted
            headers["Content-Security-Policy"] = ("sandbox allow-scripts allow-popups allow-modals allow-downloads; "
                                                  "default-src 'none'; script-src 'unsafe-inline'; "
                                                  "style-src 'unsafe-inline' https:; img-src data: blob: https:; "
                                                  "font-src data: https:; connect-src 'none'; form-action 'none'")
        elif mime in INLINE_SAFE or is_text_mime(mime) or mime == "application/json":
            ctype = mime if mime in INLINE_SAFE or mime == "application/json" else "text/plain; charset=utf-8"
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
        """POST /api/gala/upload?folder=inbox|S<n>/<folder>&filename=NAME, the file as the body."""
        g = self._gala()
        if not g:
            return self._json(400, {"error": "this portal has no data folder (--gala-data)"})
        folder = q.get("folder") or "inbox"
        m = re.fullmatch(r"S(\d{1,2})/(\w+)", folder)
        if folder == "inbox":
            d = g.inbox
        elif m and m.group(2) in SESSION_KEYS:
            d = g.sdir(int(m.group(1)), m.group(2))
        else:
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
        os.makedirs(d, exist_ok=True)
        # Written under a dot name, so the inbox sorter doesn't pick it up half written.
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in self._chunks(n):
                    f.write(chunk)
            dest = os.path.join(d, _unique(d, name))
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        if folder != "inbox":
            g.log("%s uploaded to %s" % (os.path.basename(dest), folder), name=os.path.basename(dest), to=folder)
        return self._json(200, {"ok": True, "folder": folder, "name": os.path.basename(dest), "size": n})

    def _gala_post(self, path, q):
        g = self._gala()
        if not g:
            return self._json(400, {"error": "no data folder"})
        d = self._body(64 * 1024)
        who = d.get("by") or q.get("who") or "someone"
        try:
            if path == "/api/gala/tick":
                g.tick(str(d.get("key") or ""), bool(d.get("checked")), who, d.get("notSwum"))
            elif path == "/api/gala/auto":
                g.set_auto(bool(d.get("auto")))
            elif path == "/api/gala/ask":
                return self._json(200, dict(g.act(str(d.get("key") or ""), who), ok=True))
            elif path == "/api/gala/move":
                g.move(str(d.get("path") or ""), int(d.get("session") or 0), str(d.get("folder") or ""))
            elif path == "/api/gala/restart":
                mode = str(d.get("mode") or "")
                if str(d.get("confirm") or "").strip().upper() != ("DELETE" if mode == "delete" else "RESTART"):
                    return self._json(400, {"error": "type %s to confirm" % ("DELETE" if mode == "delete" else "RESTART")})
                return self._json(200, dict(g.restart(mode, bool(d.get("questions")), bool(d.get("reimport")),
                                                      str(d.get("label") or ""), who), ok=True))
            elif path == "/api/gala/update":
                g.start_update(who, bool(d.get("claude")))
            elif path == "/api/gala/archive-restore":
                return self._json(200, dict(g.restore_archive(str(d.get("name") or ""), bool(d.get("questions")), who), ok=True))
            elif path == "/api/gala/archive-delete":
                g.delete_archive(str(d.get("name") or ""))
            elif path == "/api/gala/recheck":
                g.recheck(int(d.get("session") or 0), int(d.get("event") or 0), who)
            elif path == "/api/gala/fix":
                g.set_fix(int(d.get("session") or 0), d.get("event"), d.get("heat"), d.get("lane"), d, who)
            elif path == "/api/gala/session":
                n = max(g.sessions() or [0]) + 1
                g.sdir(n)
                g.log("Session %d added by %s" % (n, who))
            else:
                return self._json(404, {"error": "not found"})
        except (ValueError, TypeError, OSError) as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"ok": True})


    def do_GET(self):
        path, q = self._parsed()
        if path in ("/favicon.ico", "/favicon.svg", "/favicon.png"):   # browsers ask without the key
            return self._favicon()
        if path.startswith("/brand/"):   # the board's look: stylesheet, fonts, logo (no key needed)
            return self._brand_file(path[len("/brand/"):])
        if path == "/brand-logo":   # the logo set on the Settings tab (no key needed, like the favicon)
            return self._logo()
        if path.startswith("/terminal/"):
            return self._terminal()
        if not self._authorized(q):
            return self._send(401, "Missing or wrong key. Add ?key=... to the URL.", "text/plain")
        store = self.server.store
        try:
            if path == "/":
                return self._send(200, self._page(), "text/html")
            if path == "/health":
                return self._json(200, {"ok": True, "version": VERSION, "gala": bool(self._gala())})
            if path == "/api/gala/overview":
                g = self._gala()
                return self._json(200, g.overview() if g else {"root": None})
            if path == "/api/gala/swimmers":
                g = self._gala()
                return self._json(200, {"swimmers": g.find_swimmers(int(q.get("session") or 1), q.get("q", ""),
                                                                    q.get("event")) if g else []})
            if path == "/api/gala/codes":
                g = self._gala()
                return self._json(200, g.code_list(int(q.get("session") or 1)) if g else {"codes": []})
            if path == "/api/gala/update-status":
                g = self._gala()
                return self._json(200, g.update_status(q.get("fetch") == "1") if g else {"git": False})
            if path == "/api/gala/file":
                return self._gala_file(q)
            if path == "/api/board/update":
                return self._json(200, self.server.updater.status(q.get("check") == "1"))
            if path == "/api/settings":
                return self._json(200, self._settings_out())
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
            if path == "/api/presence":
                people, others = store.watchers()
                return self._json(200, {"people": people, "otherTerminals": others, "line": store.watching_line()})
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
            if path == "/api/board/update":   # the board's own updater is off in the portal (it updates with git)
                return self._json(200, {"ok": True, "update": self.server.updater.start(self._who(q))})
            if path == "/api/gala/upload":
                return self._gala_upload(q)
            if path in ("/api/gala/tick", "/api/gala/auto", "/api/gala/ask", "/api/gala/move", "/api/gala/session", "/api/gala/fix", "/api/gala/recheck",
                        "/api/gala/restart", "/api/gala/archive-delete", "/api/gala/archive-restore", "/api/gala/update"):
                return self._gala_post(path, q)
            if path == "/api/settings/logo":   # the image itself as the body; empty body removes it
                n = int(self.headers.get("Content-Length") or 0)
                if n > 512 * 1024:
                    return self._json(413, {"error": "the logo must be 512 KB or less"})
                data = self.rfile.read(n) if n else b""
                mime = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if data and mime not in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"):
                    return self._json(400, {"error": "the logo must be a PNG, JPEG, GIF, WebP or SVG image"})
                store.set_setting("logo", json.dumps({"mime": mime, "data": base64.b64encode(data).decode()}) if data else None)
                self._apply_settings()
                return self._json(200, self._settings_out())
            if path == "/api/settings":
                d = self._body(256 * 1024)
                for k in d.get("reset") or []:
                    if k in Store.SETTING_KEYS:
                        store.set_setting(k, None)
                if "name" in d:
                    name = re.sub(r"\s+", " ", str(d.get("name") or "")).strip()
                    if len(name) > 60:
                        raise ValueError("the name can be at most 60 characters")
                    store.set_setting("name", name or None)
                if "accent" in d:
                    acc = str(d.get("accent") or "").strip()
                    if acc and not re.fullmatch(r"#[0-9a-fA-F]{6}", acc):
                        raise ValueError("the accent colour must look like #cd2529")
                    store.set_setting("accent", acc.lower() or None)
                if "css" in d:
                    css = str(d.get("css") or "")
                    if len(css) > 64 * 1024:
                        raise ValueError("the CSS can be at most 64 KB")
                    store.set_setting("css", css.strip() or None)
                self._apply_settings()
                return self._json(200, self._settings_out())
            if path == "/api/presence":   # a board page checking in (or saying goodbye, gone: true)
                d = self._body(8 * 1024)
                store.touch_presence(d.get("id"), d.get("who"), d.get("view"), d.get("term"), d.get("hidden"), d.get("gone"))
                people, others = store.watchers()
                return self._json(200, {"people": people, "otherTerminals": others})
            if path == "/api/live":   # console snapshot from the board watcher
                d = self._body(2 * 1024 * 1024)
                if isinstance(d.get("clients"), int):   # terminals attached to Claude's tmux session
                    store.terminals = {"count": max(0, d["clients"]), "ts": time.time()}
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
                return self._json(201, store.post(data.get("title"), data.get("body"), data.get("author"), data.get("to"),
                                                  data.get("documents") if isinstance(data.get("documents"), list) else None))
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


def zlib_crc(text):
    import zlib
    return zlib.crc32(text.encode("utf-8"))


def render_page(brand, accent, terminal_url=""):
    import html
    if terminal_url and terminal_url != "/terminal/" and not re.fullmatch(r"https?://[^\s<>\"']+", terminal_url):
        terminal_url = ""
    return (PAGE.replace("{VERSION}", VERSION).replace("{BRAND_HTML}", html.escape(brand))
                .replace("{TERM_JSON}", json.dumps(terminal_url).replace("<", "\\u003c"))
                .replace("{BRAND_JSON}", json.dumps(brand))
                .replace("{ACCENT_DARK}", lighten(accent))
                .replace("{ACCENT}", accent))


# --------------------------------------------------------------------------- updating itself from GitHub

UPDATE_URL = "https://raw.githubusercontent.com/XRayA4T/Claude-QA-Board/main/server/qaboard.py"
UPDATE_REPO = "XRayA4T/Claude-QA-Board"


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


class Updater:
    """The board updates itself with --self-update: it checks GitHub for a newer qaboard.py (at most hourly,
    or when asked), and on request downloads it, checks it compiles and serves /health on a spare port, keeps
    the old file as qaboard.py.prev, puts the new one in place and exits, so the container's restart policy
    (or a service manager) starts the new version. The file must be writable (not a read-only mount)."""

    def __init__(self, path, url=UPDATE_URL, repo=UPDATE_REPO, enabled=False):
        self.path, self.url, self.repo, self.enabled = path, url, repo, enabled
        self.cache, self.checked, self.state = None, 0, {"state": "idle"}
        self.lock = threading.Lock()

    def _get(self, url, limit=4 * 1024 * 1024):
        req = urllib.request.Request(url, headers={"User-Agent": "qaboard/%s" % VERSION})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(limit + 1)[:limit + 1]

    def status(self, force=False):
        out = {"enabled": self.enabled, "version": VERSION, "update": self.state}
        if not self.enabled:
            return out
        out["writable"] = os.access(self.path, os.W_OK) and os.access(os.path.dirname(self.path), os.W_OK)
        out["previous"] = os.path.exists(self.path + ".prev")
        if force or not self.cache or time.time() - self.checked > 3600:
            info = {"checked": time.time()}
            try:
                src = self._get(self.url).decode("utf-8", "replace")
                m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
                info["latest"] = m.group(1) if m else None
            except Exception as e:   # offline, rate limited: say so, keep running
                info["error"] = str(e)[:200]
            if info.get("latest") and version_tuple(info["latest"]) > version_tuple(VERSION):
                try:   # what's new: the commit messages of the versions since this one ("Board 1.18.0: ...")
                    api = "https://api.github.com/repos/%s/commits?path=server/qaboard.py&per_page=20" % self.repo
                    notes = []
                    for c in json.loads(self._get(api, 1024 * 1024).decode("utf-8")):
                        msg = (c.get("commit") or {}).get("message", "").split("\n")[0]
                        m = re.match(r"Board (\d+(?:\.\d+)+)", msg)
                        if m and version_tuple(m.group(1)) <= version_tuple(VERSION):
                            break
                        notes.append(msg[:200])
                    info["notes"] = notes[:15]
                except Exception:
                    pass
            self.cache, self.checked = info, time.time()
        out.update(self.cache)
        out["available"] = bool(out.get("latest")) and version_tuple(out["latest"]) > version_tuple(VERSION)
        return out

    def start(self, who):
        if not self.enabled:
            raise ValueError("this board doesn't update itself (start it with --self-update)")
        with self.lock:
            if self.state.get("state") == "running":
                return self.state
            self.state = {"state": "running", "by": who, "at": time.time(), "msg": "downloading"}
        threading.Thread(target=self._update, args=(who,), daemon=True).start()
        return self.state

    def _update(self, who):
        def fail(msg):
            self.state = {"state": "failed", "by": who, "at": time.time(), "msg": msg}
            sys.stderr.write("update failed: %s\n" % msg)
        try:
            if not (os.access(self.path, os.W_OK) and os.access(os.path.dirname(self.path), os.W_OK)):
                return fail("qaboard.py can't be replaced here (a read-only mount?): redeploy the board instead")
            src = self._get(self.url)
            if len(src) > 4 * 1024 * 1024:
                return fail("the download is too big")
            m = re.search(rb'^VERSION = "([^"]+)"', src, re.M)
            new_v = m.group(1).decode() if m else ""
            if not new_v or version_tuple(new_v) <= version_tuple(VERSION):
                return fail("GitHub has %s, not newer than %s" % (new_v or "no version", VERSION))
            new = self.path + ".new"
            with open(new, "wb") as f:
                f.write(src)
            self.state["msg"] = "testing %s" % new_v
            import py_compile
            import shutil
            try:
                py_compile.compile(new, cfile=os.path.join(tempfile.gettempdir(), "qaboard-new.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                os.unlink(new)
                return fail("the new version doesn't compile: %s" % e)
            # does it start and answer /health? (a spare port, an empty board in a temp folder)
            tmp = tempfile.mkdtemp(prefix="qaboard-test-")
            import socket
            sk = socket.socket()
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
            sk.close()
            import subprocess
            p = subprocess.Popen([sys.executable, new, "serve", "--host", "127.0.0.1", "--port", str(port),
                                  "--db", os.path.join(tmp, "test.db")], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
            ok, err = False, ""
            try:
                for _ in range(40):
                    time.sleep(0.25)
                    if p.poll() is not None:
                        err = (p.stderr.read() or b"").decode("utf-8", "replace")[-300:]
                        break
                    try:
                        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                                "http://127.0.0.1:%d/health" % port, timeout=2) as r:
                            ok = json.loads(r.read().decode()).get("version") == new_v
                            break
                    except Exception:
                        continue
            finally:
                if p.poll() is None:
                    p.kill()
                shutil.rmtree(tmp, ignore_errors=True)
            if not ok:
                os.unlink(new)
                return fail("the new version didn't start: %s" % (err.strip().splitlines()[-1] if err.strip() else "no answer"))
            shutil.copy2(self.path, self.path + ".prev")
            os.replace(new, self.path)
            self.state = {"state": "restarting", "by": who, "at": time.time(), "msg": "updated to %s" % new_v}
            sys.stderr.write("updated to %s by %s; restarting\n" % (new_v, who))
            time.sleep(1.5)   # let the answer reach the browser
            os._exit(0)       # the container's restart policy starts the new version
        except Exception as e:
            fail("%s: %s" % (type(e).__name__, e))


def serve(args):
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    srv.store = Store(args.db, int(args.max_doc_mb * 1024 * 1024))
    srv.key = args.key
    srv.brand = args.name
    srv.accent = args.accent if re.fullmatch(r"#[0-9a-fA-F]{6}", args.accent or "") else "#3949ab"
    srv.updater = Updater(os.path.abspath(__file__), args.update_url or UPDATE_URL, UPDATE_REPO, args.self_update)
    srv.cli_brand, srv.cli_accent, srv.terminal_url = srv.brand, srv.accent, args.terminal_url
    st = srv.store.settings()   # name and colour changed on the Settings tab win over --name and --accent
    srv.brand, srv.accent = st.get("name") or srv.brand, st.get("accent") or srv.accent
    srv.brand_dir = os.path.abspath(args.brand_dir) if args.brand_dir else None
    srv.term_ports = {k: v for k, v in (("rw", args.term_rw_port), ("ro", args.term_ro_port)) if v}
    srv.admin_key = args.admin_key or ""
    if srv.term_ports:
        args.terminal_url = srv.terminal_url = "/terminal/"
    srv.gala = Gala(args.gala_data, args.inbox, srv.store) if args.gala_data else None
    if srv.gala:
        threading.Thread(target=srv.gala.run, daemon=True).start()
        sys.stderr.write('Gala data: %s, inbox: %s\n' % (srv.gala.root, srv.gala.inbox))
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
.setcard h3{margin:0 0 6px}.sethead{margin:18px 0 8px}
.setgrid{display:grid;grid-template-columns:150px 1fr;gap:10px 14px;align-items:center;margin-top:12px}
.setgrid>label{color:var(--muted)}.setgrid textarea{min-height:120px;font:13px/1.4 ui-monospace,Menlo,monospace}
.setacc{display:flex;gap:8px;align-items:center}.setacc input[type=color]{width:44px;height:34px;padding:2px;flex:0 0 auto}.setacc #set-accent{max-width:140px}
.setlogo-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}#set-logoimg{height:40px;max-width:160px;object-fit:contain;border:1px solid var(--line);border-radius:6px;padding:2px;background:var(--card)}
.setbtns{justify-content:flex-end;align-items:center;gap:8px}.setbtns button{flex:0 0 auto}.setbtns .meta{margin-right:auto}
.brand img.setlogo{width:auto;max-width:140px;object-fit:contain}
@media (max-width:640px){.setgrid{grid-template-columns:1fr}}
.watchers{flex:1 1 auto;text-align:right;margin-right:8px}.watchers b{color:var(--fg)}
.updbtn{font:600 12px/1 system-ui,sans-serif;letter-spacing:0;text-transform:none;margin-left:6px;padding:4px 9px;border-radius:99px;
 border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;vertical-align:2px}
.updlg{position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.45);display:grid;place-items:center;padding:16px}
.updlg[hidden]{display:none}
.updbox{background:var(--card);color:var(--fg,inherit);border-radius:12px;max-width:520px;width:100%;padding:18px 20px;box-shadow:0 10px 40px rgba(0,0,0,.3)}
.updbox h3{margin:0 0 8px}.updbox ul{margin:6px 0 10px;padding-left:20px;max-height:220px;overflow:auto}
.updbox .btns{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}.updbox .bad{color:#e5484d}
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
.depsteps .when{color:var(--muted);font-size:14px;font-variant-numeric:tabular-nums;white-space:nowrap}
.depeta{font-variant-numeric:tabular-nums}.depeta.late{color:#e5484d}
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
/* ---- Gala and Checklist tabs */
.galahead h2,.clhead h2{margin:0 0 2px;font-size:1.3em}
.gh-top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start}
.gh-right{text-align:right}
.gauto{font-weight:600;display:inline-flex;gap:8px;align-items:center;cursor:pointer}
.gauto input{width:20px;height:20px;accent-color:var(--accent)}
.lnk{color:var(--accent);cursor:pointer;text-decoration:underline}
.gdrop{margin-top:10px;padding:12px;font-size:.95em}
.ginbox{padding:18px}
.gdrop.over,.gfolder.over,.cl-row.over{outline:3px dashed var(--accent);outline-offset:-3px}
.spills{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 10px;align-items:center}
.spills button{border-radius:999px;padding:4px 14px;font-weight:600}
.spills button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.spills .meta{margin-left:6px}
.gfolders{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:10px}
.gfolder{margin:0}
.gfolder h3{margin:0;font-size:1.05em;display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.gfolder h3 code{font-size:.75em;color:var(--muted);font-weight:400}
.gfiles{list-style:none;margin:8px 0 0;padding:0;max-height:260px;overflow:auto}
.gfiles li{display:flex;gap:8px;align-items:baseline;padding:3px 0;border-top:1px solid var(--line)}
.gfiles li a:first-child{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gfiles li .meta{flex:0 0 auto}
.gbar{height:10px;border-radius:6px;background:var(--line);overflow:hidden;flex:1;min-width:120px}
.gbar i{display:block;height:100%;background:var(--accent)}
.gscan{display:flex;gap:12px;align-items:center;padding:6px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.gscan:first-of-type{border-top:0}
.gscan .nm{flex:0 0 300px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gscan .ct{flex:0 0 auto;min-width:150px;text-align:right}
.gscan .miss{flex-basis:100%;font-size:.85em}
.gwarn{color:#b45309}
.gstats{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.gstat{border:1px solid var(--line);border-radius:8px;padding:6px 12px;min-width:90px}
.gstat b{display:block;font-size:1.4em}
.gask{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.glog{list-style:none;margin:6px 0 0;padding:0;max-height:240px;overflow:auto;font-size:.92em}
.glog li{padding:3px 0;border-top:1px solid var(--line);display:flex;gap:10px}
.glog li .meta{flex:0 0 70px}
.st{display:inline-block;font-size:.78em;font-weight:700;border-radius:999px;padding:1px 9px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.st.ready{border-color:#b45309;color:#b45309}
.st.asked{border-color:var(--accent);color:var(--accent)}
.st.audited{border-color:var(--ans);color:var(--ans)}
.st.changed{border-color:#b45309;background:#b45309;color:#fff}
/* checklist */
.clhead .gbar{margin-top:10px;height:8px}
.cl-sec{margin:14px 0 4px;font-size:.85em;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:700}
.cl-row{display:grid;grid-template-columns:30px 1fr auto;gap:2px 12px;align-items:start;padding:9px 0;border-top:1px solid var(--line)}
.cl-sec+.cl-row,.cl-sec+table{border-top:0}
.cl-row input[type=checkbox],.cl-ev input[type=checkbox]{width:22px;height:22px;margin:2px 0 0;accent-color:var(--accent);cursor:pointer}
.cl-row.done .cl-t,.cl-ev tr.done .evname{text-decoration:line-through;text-decoration-color:var(--muted);color:var(--muted)}
.cl-t{font-weight:600}
.cl-why{font-size:.9em;color:var(--muted)}
.cl-f{font-size:.9em;margin-top:3px}
.ok{color:var(--ans);font-weight:600}
.no{color:var(--muted)}
.cl-by{font-size:.8em;color:var(--muted)}
.cl-act{display:flex;gap:8px;align-items:center;font-size:.88em}
.cl-ev{width:100%;border-collapse:collapse;font-size:.93em}
.cl-ev th{text-align:left;font-size:.8em;color:var(--muted);font-weight:600;padding:4px 6px;border-bottom:1px solid var(--line)}
.cl-ev td{padding:6px;border-top:1px solid var(--line);vertical-align:top}
.cl-ev td.c{text-align:center}
.cl-ev .evname{font-weight:600}
.cl-ev .sm{font-size:.85em;color:var(--muted)}
.cl-ev button{padding:2px 8px;font-size:.85em}
.tblwrap{overflow-x:auto}
.spills button.on .meta{color:#fff;opacity:.85}
.gfiles li select{flex:0 0 auto;width:auto;padding:2px 6px}
.gfiles li button{flex:0 0 auto;padding:2px 10px}
button:disabled{opacity:.45;cursor:not-allowed}
.cl-ev tr.qfrow td{border-top:0;padding-top:0}
.cl-ev details summary{cursor:pointer;font-size:.9em;font-weight:600;color:var(--muted)}
ul.qf{list-style:none;margin:6px 0 4px;padding:0}
ul.qf>li{display:grid;grid-template-columns:28px 1fr;gap:8px;padding:6px 0;border-top:1px dashed var(--line)}
ul.qf>li.done>div{opacity:.6}
ul.qf ul{margin:2px 0 2px 18px;padding:0}
ul.qf input[type=checkbox]{width:20px;height:20px;accent-color:var(--accent)}
form.qffix{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;margin:6px 0 2px;font-size:.88em}
form.qffix label{display:inline-flex;gap:4px;align-items:center;color:var(--muted)}
form.qffix input,form.qffix select{padding:2px 6px;font-size:1em}
form.qffix .qfnote input{width:200px}
form.qffix button{padding:2px 10px}
.qfdone{margin:6px 0;padding:8px 10px;border:1px solid var(--ans);border-radius:8px;color:var(--ans);font-weight:600;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.qfws,.qfsw{flex-basis:100%;display:flex;flex-direction:column;gap:4px;padding:6px 8px;border-left:3px solid var(--accent);background:var(--drop,rgba(0,0,0,.03));border-radius:4px;color:var(--fg)}
.qfw{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center}
.qfw span:first-child{min-width:340px}
.swlist{display:flex;flex-direction:column;gap:2px}
.swopt{display:flex!important;gap:6px;align-items:center;color:var(--fg)!important}
.qfws input[type=radio],.qfsw input[type=radio]{width:auto!important;flex:0 0 auto;margin:0}
.qfws input[type=text],.qfws input:not([type]),.qfsw input:not([type]){width:auto!important;flex:0 0 auto}
.qfw label,.swopt{display:inline-flex!important;align-items:center;gap:6px;white-space:nowrap;justify-content:flex-start!important;text-align:left}
.qfsw .swlist{align-items:flex-start}
.qfsw label.sm{display:inline-flex;gap:6px;align-items:center}
.qft{flex-basis:100%;padding:6px 8px;border-left:3px solid var(--accent);background:var(--drop,rgba(0,0,0,.03));border-radius:4px}
.qft table{border-collapse:collapse;margin-bottom:4px}
.qft td{padding:2px 12px 2px 0;vertical-align:top;color:var(--fg)}
.qft .tl{font-size:.8em;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.qft td b{font-size:1.05em;font-variant-numeric:tabular-nums}
.qft input[data-swcol]{width:6.5em!important;font-variant-numeric:tabular-nums;font-weight:600}
.qft input.bad{outline:2px solid #b45309}
.qft td.unsure input[data-swcol]{border-color:#b45309}
.qft td.unread input[data-swcol]{border-style:dashed}
.qft td.fin b{color:var(--accent)}
.qft label.sm{display:flex!important;gap:4px;align-items:center;white-space:nowrap}
.qft input[type=checkbox]{width:auto!important}
.qft [data-swdiff] span,.qft [data-meddiff] span{font-size:.82rem;font-weight:600}
ul.qf>li.cleared>div>ul{opacity:.75}
.gdlg{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:1000;padding:16px}
.gdlg[hidden]{display:none}
.gdlg-box{background:var(--card);color:var(--fg);border-radius:10px;padding:18px 20px;max-width:520px;width:100%;box-shadow:0 10px 40px rgba(0,0,0,.3)}
.gdlg-box h3{margin:0 0 8px}
.gdlg-box p{margin:0 0 12px;color:var(--muted)}
.gdlg-in{width:100%;margin:4px 0}
.gdlg-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}
#gnotes{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:1001;max-width:min(440px,calc(100vw - 32px))}
.gnote{display:flex;gap:10px;align-items:flex-start;background:var(--card);color:var(--fg);border-left:4px solid var(--ans);border-radius:8px;padding:10px 12px;box-shadow:0 4px 18px rgba(0,0,0,.2)}
.gnote.err{border-left-color:var(--accent)}
.gnote span{flex:1}
.gnote button{padding:0 6px;border:0;background:none;cursor:pointer;color:var(--muted)}
.cl-ev button.primary.regen{padding:3px 10px;font-size:.85em;color:#fff}
.rs-opts{display:flex;flex-direction:column;gap:8px;margin:12px 0}
.rs-opts label{display:block}
.rs-opts input[type=radio],.rs-opts input[type=checkbox]{width:auto!important;margin:0 8px 0 0;vertical-align:-2px;accent-color:var(--accent)}
.rs-opts .rs-label{margin-left:26px;color:var(--muted)}
.rs-opts .rs-label input,.rs-go input{width:auto!important;display:inline-block}
.rs-go{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding-top:10px;border-top:1px solid var(--line)}
.rs-go button{flex:0 0 auto}
#rs-arch table{border-collapse:collapse;width:100%}
#rs-arch td{padding:4px 8px;border-top:1px solid var(--line)}
.up-list{margin:4px 0 0 18px;padding:0}
.up-log{max-height:260px;overflow:auto;font-size:12px;background:var(--bg);padding:8px;border-radius:6px;white-space:pre-wrap}
#up-card .bad{color:#cd2529}
.gdlg-ck{display:flex;gap:8px;align-items:flex-start;margin:4px 0 10px}
.gdlg-ck input{width:auto!important;margin-top:3px;accent-color:var(--accent)}
.gign{margin:0 0 6px}.gign summary{cursor:pointer}
.st.transcribed{border-color:#0e7490;color:#0e7490}
.st.computing{border-color:var(--accent);background:var(--accent);color:#fff}
.gdlg-box p{white-space:pre-line}
/* the portal updates itself from its Restart tab (git), not the QA board's own updater */
#set-update{display:none}

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
<nav class="views" id="views"><button data-v="gala">Gala</button><button data-v="checklist">Checklist</button><button data-v="questions">Questions</button><button data-v="docs">Documents</button><button data-v="live">Live</button><button data-v="deploys">Deploys</button><button data-v="users">Settings</button><button data-v="restart">Restart</button></nav>
<div class="deps" id="deps" hidden></div>
</div>

<section id="v-gala" hidden>
<div class="card galahead">
 <div class="gh-top"><div><h2>Gala audit</h2><div class="meta" id="g-root"></div></div>
  <div class="gh-right"><label class="gauto" title="Ask Claude to read each pack, DQ batch and ETD programme as it arrives, and work out the merge results, audits and quick fixes as soon as they are read"><input type="checkbox" id="g-auto"> Work automatically</label><div class="meta" id="g-tools"></div></div></div>
 <div class="drop gdrop ginbox" id="g-inbox"><b>Drop the gala's files here</b> (lane sheet packs, DQ slips, Gen7 race PDFs, Meet Manager exports)
  or <label class="lnk">choose files<input type="file" multiple hidden id="g-inboxfile"></label>.
  <div class="meta">They go into the inbox and are sorted into their session within a few seconds. The scanner and the Gen7 computer can save straight into the inbox folder too.</div>
  <div class="meta" data-gmsg></div></div>
</div>
<div class="spills" id="g-sess"></div>
<div id="g-session"></div>
<div class="card" id="g-activity"></div>
</section>
<section id="v-checklist" hidden>
<div class="spills" id="cl-sess"></div>
<div id="cl-body"></div>
</section>
<section id="v-restart" hidden>
<div class="card"><h2 style="margin:0 0 4px;font-size:1.3em">Restart</h2>
<div class="meta">Clear this gala out of the portal to start the next one: every session's files (scans, Gen7 PDFs, Meet Manager
files, transcriptions, reports), files not sorted or still in the inbox, the checklist ticks, the quick-fix history, the fixes
made here and the activity log. Linked inbox folders (the Gen7 or scanner shares) are left alone.</div>
<div class="gstats" id="rs-sum"></div>
<div class="rs-opts">
 <label><input type="radio" name="rsmode" value="archive" checked> <b>Archive it</b> <span class="meta">moved to <code>data/archive/&lt;date time&gt;</code>; it can be looked at or deleted later</span></label>
 <label class="rs-label">Archive name <input id="rs-label" placeholder="e.g. CGA Summer 3" size="28"></label>
 <label><input type="radio" name="rsmode" value="delete"> <b>Delete it for good</b> <span class="meta">nothing is kept (the gala's files hold children's details)</span></label>
 <label><input type="checkbox" id="rs-q"> Also clear the <b>Questions</b>, answers, <b>Documents</b> and deployments <span class="meta">(archived with the rest when archiving)</span></label>
 <label><input type="checkbox" id="rs-re"> Copy in again the files still in linked inbox folders <span class="meta">(normally off: they belong to the gala being cleared)</span></label>
</div>
<div class="rs-go">Type <b id="rs-word">RESTART</b> to confirm: <input id="rs-confirm" size="10" autocomplete="off"> <button type="button" class="primary" id="rs-btn" disabled>Restart the portal</button> <span class="meta" id="rs-msg"></span></div>
</div>
<div class="card"><h3 style="margin:0 0 6px">Archives</h3><div id="rs-arch" class="meta">None yet.</div></div>
<div class="card" id="up-card"><h3 style="margin:0 0 6px">Updates</h3>
<div id="up-info" class="meta">Checking…</div>
<div class="rs-go"><button type="button" id="up-check">Check now</button> <button type="button" class="primary" id="up-go" hidden>Update now</button>
 <label id="up-claude-l" hidden><input type="checkbox" id="up-claude"> Restart Claude too <span class="meta">(picks up new instructions and plugin; only between events)</span></label></div>
<details id="up-logd" hidden><summary class="meta">Update log</summary><pre id="up-log" class="up-log"></pre></details>
</div>
</section>
<section id="v-questions">
<details class="card new" id="newbox"><summary>Ask a question</summary>
<div class="row"><input id="title" placeholder="Question (one line)"></div>
<div class="row"><textarea id="qbody" placeholder="Details, context, links, code. Paste a screenshot (Ctrl+V) or drop files to show them here."></textarea></div>
<div class="drop qdropzone" id="qdrop">Drop files here, or <label style="color:var(--accent);cursor:pointer;text-decoration:underline">choose files<input type="file" id="qfile" multiple hidden></label>
<div class="meta" id="qfilelist">They're uploaded straight away and shown in the question where your cursor is; you can also paste a screenshot (Ctrl+V).</div></div>
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
<div class="card setcard" id="set-brand"><h3>Branding</h3>
<div class="meta">How this board looks to everyone. Saved on the board, so redeploying keeps it; empty fields fall back to how the board was started.</div>
<div class="setgrid">
 <label for="set-name">Board name</label><input id="set-name" maxlength="60" autocomplete="off">
 <label for="set-accent">Accent colour</label><span class="setacc"><input type="color" id="set-accentpick"><input id="set-accent" placeholder="#3949ab" maxlength="7" autocomplete="off"></span>
 <label>Logo</label><span class="setlogo-row"><img id="set-logoimg" alt="" hidden><label class="lnk">upload an image<input type="file" id="set-logofile" accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml" hidden></label><a href="#" id="set-logorm" hidden>remove</a><span class="meta">PNG, JPEG, GIF, WebP or SVG, up to 512 KB; shown in the header</span></span>
 <label for="set-css">Your own CSS</label><textarea id="set-css" spellcheck="false" placeholder=":root{--accent:#cd2529}  /* the board's variables: --bg --card --fg --muted --line --accent --open --ans --res --field --drop */"></textarea>
</div>
<div class="row setbtns"><span class="meta" id="set-msg"></span><button type="button" id="set-reset">Reset to how it was started</button><button type="button" class="primary" id="set-save">Save</button></div>
</div>
<h3 class="sethead">Users</h3>
<div class="card"><div class="row" style="margin-top:0"><input id="newuser" placeholder="Name, e.g. alice or bob-claude" autocomplete="off">
<select id="newkind" style="flex:0 0 130px"><option value="person">Person</option><option value="claude">Claude</option></select>
<button class="primary" id="adduser" style="flex:0 0 auto">Add user</button></div>
<div class="meta" id="usermsg" style="margin-top:6px">People choose themselves next to "I am". Claudes are listed first under "For whom?".</div></div>
<div id="userlist"></div>
<div class="card setcard" id="set-update"><h3>Updates</h3><div id="set-upinfo" class="meta">Checking…</div>
<div class="row setbtns"><button type="button" id="set-check">Check for updates</button><button type="button" class="primary" id="set-upgo" hidden>Update now</button></div></div>
</section>

<section id="v-live" hidden>
<div class="attn" id="liveattn" hidden><span id="liveattntext"></span>
  <span class="attnacts"><a class="attnlink" id="liveopen" target="_blank" rel="noopener noreferrer" hidden>Open link &#8599;</a>
  <span class="attncode" id="livecode" hidden>Code <b id="livecodetext"></b> <button id="livecopy">Copy</button></span>
  <button id="liveclear" hidden>Mark as handled</button></span></div>
<div class="livehead"><div class="meta" id="livestate">Loading…</div><div class="meta watchers" id="watchers"></div>
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

// ---- Gala and Checklist tabs: the gala's files by session, sorted from the inbox; per-event progress; ticks
let GALA=null,galaSig='',GSESS=+(store('qa-gsess')||0);
function galaHref(rel,dl){const u=url('/api/gala/file');u.searchParams.set('path',rel);if(dl)u.searchParams.set('dl','1');return u.toString()}
async function galaPost(path,body){return api(path,Object.assign({by:me()||'someone'},body||{}))}
const ST_TXT={waiting:'waiting',ready:'ready',asked:'with Claude',transcribed:'transcribed',computing:'working…',audited:'audited',changed:'new files'};
const FOLDER_TXT={meet:['Meet Manager files','Entries, merge results and final exports (.zip / .hy3)'],gen7:['Gen7 race PDFs','One PDF per race'],
 scans:['Scans','Lane sheet packs (one per event), DQ slips, ETD programme'],ocr:['OCR (Claude)','JSON per lane sheet, DQ slip and ETD page; dq-codes.json'],
 reports:['Reports (Claude)','Event and session audits, merge results .hy3, final audit']};
function curSession(){if(!GALA||!GALA.sessions.length)return null;
 return GALA.sessions.find(s=>s.n===GSESS)||GALA.sessions[GALA.sessions.length-1]}
function galaEditing(){const a=document.activeElement;return !!(a&&a.closest&&a.closest('form.qffix'))||!!document.querySelector('form.qffix[data-dirty]')}
let CODES={},CODES_FOR=null;
async function loadCodes(n){if(CODES_FOR===n)return;CODES_FOR=n;
 try{const j=await api('/api/gala/codes?session='+n);CODES=j;let dl=$('#dqcodes');if(!dl){dl=document.createElement('datalist');dl.id='dqcodes';document.body.appendChild(dl)}
  dl.innerHTML=(j.codes||[]).map(c=>`<option value="${esc(c[0])}">${esc(c[0]+' – '+c[1]+(c[2]?' ('+c[2]+')':''))}</option>`).join('')}catch(e){CODES_FOR=null}}
async function loadGala(){
 if(galaEditing())return;
 let j;try{j=await api('/api/gala/overview')}catch(e){$('#g-root').textContent='Could not load the data folder: '+e.message;return}
 const sig=JSON.stringify(j);if(sig===galaSig)return;galaSig=sig;GALA=j;
 if(!j.root){$('#g-root').textContent='This portal was started without a data folder (--gala-data).';return}
 const cs=curSession();if(cs)GSESS=cs.n;
 renderGalaHead();renderRestart();renderPills('#g-sess');renderPills('#cl-sess');renderSession();renderActivity();renderChecklist()}
function renderGalaHead(){
 $('#g-root').innerHTML='Data folder: <code>'+esc(GALA.root)+'</code><br>Inbox: <code>'+esc(GALA.inbox)+'</code>'
  +(GALA.waiting.length?` · <span class="gwarn">${GALA.waiting.length} file${GALA.waiting.length>1?'s':''} arriving</span>`:'');
 $('#g-auto').checked=!!GALA.auto;
 const miss=Object.entries(GALA.tools||{}).filter(([k,v])=>!v).map(([k])=>k);
 $('#g-tools').innerHTML=miss.length?'<span class="gwarn">Missing: '+esc(miss.join(', '))+' (install poppler-utils)</span>':'poppler ✓'}
function renderPills(sel){
 const box=$(sel);if(!GALA.sessions.length){box.innerHTML='<span class="meta">No sessions yet: drop the first session\'s entries export or any gala file above.</span> <button type="button" data-newsess>+ Session</button>';return}
 box.innerHTML=GALA.sessions.map(s=>{const a=s.events.filter(e=>e.status==='audited').length;
  return `<button type="button" data-sess="${s.n}" class="${s.n===GSESS?'on':''}">Session ${s.n} <span class="meta">${a}/${s.events.length}</span></button>`}).join('')
  +'<button type="button" data-newsess title="Add an empty session">+</button>'}
function fileList(s,key,files,limit){
 if(!files.length)return '<div class="meta" style="margin-top:8px">Empty.</div>';
 return '<ul class="gfiles">'+files.filter(f=>!f.dir).slice(0,limit||500).map(f=>{const rel='sessions/S'+s.n+'/'+key+'/'+f.name;
  return `<li><a href="${esc(galaHref(rel))}" target="_blank" rel="noopener" title="${esc(f.name)}">${esc(f.name)}</a><span class="meta">${size(f.size)} · ${agoShort(f.mtime)}</span><a class="meta" href="${esc(galaHref(rel,1))}" title="Download">⤓</a></li>`}).join('')+'</ul>'}
function renderSession(){
 const s=curSession(),box=$('#g-session');if(!s){box.innerHTML='';return}
 const c=k=>s.events.filter(e=>e.status===k).length;
 const stat=(n,t)=>`<div class="gstat"><b>${n}</b><span class="meta">${t}</span></div>`;
 const scans=s.scans.length?s.scans.map(x=>{const pct=x.pages?Math.round(100*x.done/x.pages):0;
  const what=x.kind==='dq'?'DQ slips':x.kind==='etd'?'ETD':'lane sheets'+(x.event!=null?' · event '+x.event:'');
  return `<div class="gscan"><span class="nm" title="${esc(x.name)}">${esc(x.name)} <span class="meta">${esc(what)}</span></span>
   <span class="gbar" title="${pct}%"><i style="width:${pct}%"></i></span>
   <span class="ct">${x.pages==null?'? pages':`${x.done} / ${x.pages} pages`}${x.review?` · <span class="gwarn">${x.review} to check</span>`:''}${x.bad?` · <span class="gwarn">${x.bad} unreadable</span>`:''}</span>
   ${x.missingCount&&x.done?`<span class="miss meta">Not done yet: pages ${esc(x.missing.join(', '))}${x.missingCount>x.missing.length?' …':''}</span>`:''}</div>`}).join('')
  :'<div class="meta">No scans in this session yet.</div>';
 const ent=s.meet.entries.length?`${s.expected.events} events, ${s.expected.heats} heats expected (from ${esc(s.meet.entries[s.meet.entries.length-1])})`:'<span class="gwarn">No entries export yet</span>: events are only known as their files arrive.';
 box.innerHTML=`<div class="card"><div class="gh-top"><div><h3 style="margin:0">Session ${s.n}</h3><div class="meta">${ent} · ${s.races} Gen7 race${s.races===1?'':'s'}</div></div>
   <a href="#checklist" data-goto="checklist">Checklist →</a></div>
  <div class="gstats">${stat(c('audited'),'audited')}${stat(c('asked'),'with Claude')}${c('transcribed')?stat(c('transcribed'),'transcribed'):''}${c('computing')?stat(c('computing'),'working'):''}${stat(c('ready'),'ready')}${stat(c('waiting'),'waiting')}${c('changed')?stat(c('changed'),'new files'):''}</div>
  <h4 style="margin:10px 0 2px">OCR progress</h4>${scans}
  <div class="gask"><span class="meta" style="align-self:center" title="Done by the portal itself, from what Claude has read (no scans are read)">Run now:</span>
   <button type="button" data-ask="S${s.n}:check">Check the files</button>
   <button type="button" data-ask="S${s.n}:merge"${s.template&&s.entriesFile?'':' disabled title="Needs the entries export and the empty merge results export"'}>Merge results</button>
   <button type="button" data-ask="S${s.n}:final"${s.meet.final.length&&s.merged.length?'':' disabled title="Needs our merge results and Meet Manager\'s final export"'}>Final audit</button>
   <span class="meta" data-askmsg>${['merge','final','etd'].map(k=>{const j=(s.jobs||{})[k];if(!j)return '';
     const t={merge:'Merge results',final:'Final audit',etd:'ETD check'}[k];
     return j.state==='running'?`${t}: working…`:j.state==='failed'?`<span class="gwarn">${t} failed: ${esc(j.msg||'')}</span>`:`${t}: done ${esc(agoShort(j.at))}`}).filter(Boolean).join(' · ')}</span></div></div>
  <div class="gfolders">${Object.keys(FOLDER_TXT).map(k=>{const f=s.folders[k]||[];const mine=k==='ocr'||k==='reports';
   return `<div class="card gfolder" data-gfolder="S${s.n}/${k}"><h3><span>${esc(FOLDER_TXT[k][0])} <span class="meta">(${k==='ocr'?s.ocrCount:f.filter(x=>!x.dir).length})</span></span><code>sessions/S${s.n}/${k}</code></h3>
    <div class="meta">${esc(FOLDER_TXT[k][1])}</div>${fileList(s,k,f)}${k==='ocr'&&s.ocrCount>f.length?`<div class="meta">Newest ${f.length} of ${s.ocrCount}.</div>`:''}
    ${mine?'':`<div class="drop gdrop">Drop files here, or <label class="lnk">choose files<input type="file" multiple hidden data-gup="S${s.n}/${k}"></label><div class="meta" data-gmsg></div></div>`}</div>`}).join('')}</div>`}
function renderActivity(){
 const un=GALA.unsorted.filter(f=>!f.dir);
 const opts=GALA.sessions.map(s=>`<option value="${s.n}">Session ${s.n}</option>`).join('');
 const unsorted=un.length?`<h3 style="margin:0 0 4px" class="gwarn">Not sorted (${un.length})</h3><div class="meta">The portal couldn't tell what these are. Choose where each one goes.</div><ul class="gfiles">`
  +un.map(f=>`<li><a href="${esc(galaHref('unsorted/'+f.name))}" target="_blank" rel="noopener">${esc(f.name)}</a>
   <select data-mvs>${opts||'<option value="1">Session 1</option>'}</select><select data-mvf>${['scans','gen7','meet'].map(k=>`<option value="${k}">${k}</option>`).join('')}</select>
   <button type="button" data-move="${esc('unsorted/'+f.name)}">Move</button></li>`).join('')+'</ul>':'';
 const ig=(GALA.ignored||[]).filter(f=>!f.dir);
 const ignored=ig.length?`<details class="gign"><summary class="meta">Ignored (${ig.length}): Gen7 races for events not in the meet entries, e.g. a test race. Move one into a session if it does belong.</summary><ul class="gfiles">`
  +ig.map(f=>`<li><a href="${esc(galaHref('ignored/'+f.name))}" target="_blank" rel="noopener">${esc(f.name)}</a>
   <select data-mvs>${opts||'<option value="1">Session 1</option>'}</select><select data-mvf><option value="gen7">gen7</option><option value="meet">meet</option><option value="scans">scans</option></select>
   <button type="button" data-move="${esc('ignored/'+f.name)}">Move</button></li>`).join('')+'</ul></details>':'';
 const log=GALA.log.length?'<ul class="glog">'+GALA.log.slice(0,25).map(l=>`<li><span class="meta">${esc(agoShort(l.ts))}</span><span>${esc(l.text)}</span></li>`).join('')+'</ul>':'<div class="meta">Nothing yet.</div>';
 $('#g-activity').innerHTML=unsorted+ignored+'<h3 style="margin:'+(un.length||ig.length?'14px':'0')+' 0 4px">Activity</h3>'+log}
function tickBox(key,label){const t=GALA.ticks[key];return `<input type="checkbox" data-tick="${esc(key)}" ${t?'checked':''} aria-label="Tick off: ${esc(label)}" title="${t?esc('Ticked by '+t.by+' · '+agoShort(t.at)):'Tick off when checked'}">`}
function clRow(s,key,title,why,found,folder,claude){
 const t=GALA.ticks[key];
 const f=found&&found.length?'<span class="ok">✓</span> '+found.join(', '):`<span class="no">${claude?'Not made yet':'Not in yet'}</span>`;
 return `<div class="cl-row${t?' done':''}"${folder?` data-cldrop="${esc(folder)}"`:''}>${tickBox(key,title)}
  <div><div class="cl-t">${esc(title)}</div><div class="cl-why">${why}</div><div class="cl-f">${f}</div>
  ${t?`<div class="cl-by">Ticked by ${esc(t.by)} · ${esc(agoShort(t.at))}</div>`:''}<div class="meta" data-gmsg></div></div>
  <div class="cl-act">${folder?`<label class="lnk">add file<input type="file" multiple hidden data-gup="${esc(folder)}"></label>`:''}</div></div>`}
function evRange(evs){const out=[];for(let i=0;i<evs.length;i++){let j=i;while(j+1<evs.length&&evs[j+1]===evs[j]+1)j++;out.push(j>i?evs[i]+'–'+evs[j]:''+evs[i]);i=j}return out.join(', ')}
function link(s,sub,name){return `<a href="${esc(galaHref('sessions/S'+s.n+'/'+sub+'/'+name))}" target="_blank" rel="noopener">${esc(name)}</a>`}
function renderChecklist(){setTimeout(swAll,0);
 const s=curSession(),box=$('#cl-body');if(s)loadCodes(s.n);
 if(!s){box.innerHTML='<div class="card meta">No sessions yet. Drop the session\'s Meet Manager entries export on the Gala tab (or in the inbox folder) to start.</div>';return}
 const qfKey=(e,f)=>'S'+s.n+':e'+e.event+':qf:'+f.heat+'.'+f.lane;
 const keys=['entries','lanesprint','mmresults',...s.events.map(e=>'e'+e.event),'dq','etd','codes','merged','mmfinal','final'].map(k=>'S'+s.n+':'+k)
  .concat(s.events.flatMap(e=>(e.fixes||[]).map(f=>qfKey(e,f))));
 const ticked=keys.filter(k=>GALA.ticks[k]).length,pct=Math.round(100*ticked/keys.length);
 const dq=s.scans.filter(x=>x.kind==='dq'),etd=s.scans.filter(x=>x.kind==='etd');
 const ev=s.events.length?`<div class="tblwrap"><table class="cl-ev"><tr><th></th><th>Event</th><th class="c">Printed</th><th>Lane sheets</th><th class="c">Gen7 heats</th><th class="c">OCR lanes</th><th>Audit</th><th>Merge</th><th class="c">Quick fixes</th><th>Status</th><th></th></tr>`
  +s.events.map(e=>{const key='S'+s.n+':e'+e.event,t=GALA.ticks[key];
   const g7=e.heats?`${e.gen7.length}/${e.heats}`:(e.gen7.length||'–');
   const fx=e.fixes||null,fxDone=fx?fx.filter(f=>GALA.ticks[qfKey(e,f)]).length:0;
   const fxCell=fx==null?'<span class="no">–</span>':fx.length?(fxDone===fx.length?`<span class="ok" title="All ${fx.length} ticked">done ✓ <span class="sm">(${fx.length})</span></span>`:`<span class="gwarn">${fxDone}/${fx.length}</span>`):'<span class="ok" title="No quick fixes">none ✓</span>';
   const lastFix=Math.max(0,...Object.values(e.handFixes||{}).map(h=>h.at||0)),needRegen=fx&&fx.length&&fxDone===fx.length&&lastFix>(e.mergeAt||0);
   const regen=`<div><button type="button" class="primary regen" data-ask="S${s.n}:remerge:${e.event}" ${e.canMerge&&e.ocrLanes?'':'disabled'} title="${esc(e.canMerge?(e.ocrLanes?'The portal writes the merge results, audit and quick fixes again from what is already transcribed, with the fixes made here and every DQ slip read and coded so far (no scans are read, no Claude)':'The lane sheets aren\'t transcribed yet'):'Needs the entries export and the empty merge results export')}">${e.merge?'Regenerate':'Generate'}</button>${e.dqSlips?`<span class="sm"> ${e.dqSlips} DQ</span>`:''}</div>`;
   const mergeCell=(e.merge?link(s,'reports',e.merge):e.canMerge?'<span class="no">–</span>':`<span class="no" title="${esc(!s.entriesFile?'No entries export':'No empty merge results export (one for the whole gala)')}">waiting</span>`)+regen;
   const fxRows=fx&&fx.length?`<tr class="qfrow"><td></td><td colspan="10">${needRegen?`<div class="qfdone">All quick fixes for event ${e.event} are done, and fixes were made since its merge results were written. <button type="button" class="primary" data-ask="S${s.n}:remerge:${e.event}" ${e.canMerge?'':'disabled title="Needs the entries and the empty merge results export"'}>Regenerate the merge results</button> <span class="sm">with the fixes made here and every DQ slip read so far</span></div>`:''}<details data-qfd="S${s.n}:e${e.event}"${(QF_OPEN['S'+s.n+':e'+e.event]??(fxDone<fx.length))?' open':''}><summary>Quick fixes for event ${e.event} (${fxDone} of ${fx.length} ticked${fx.some(x=>x.cleared)?', '+fx.filter(x=>x.cleared).length+' settled by the re-check':''})${e.recheck?` · <span class="${e.recheck.state==='failed'?'gwarn':''}">${e.recheck.state==='running'?'re-checking…':e.recheck.state==='failed'?'re-check failed: '+esc(e.recheck.msg||''):'re-checked '+esc(agoShort(e.recheck.at))+(e.recheck.msg?' ('+esc(e.recheck.msg)+')':'')}</span>`:''} · <button type="button" class="sm" data-recheck="${s.n}|${e.event}">Check again</button>${Object.keys(e.handFixes||{}).length?` · ${Object.keys(e.handFixes).length} fixed in the portal`:''}${e.fixesFile?` · <a href="${esc(galaHref('sessions/S'+s.n+'/reports/'+e.fixesFile))}" target="_blank" rel="noopener">printable list</a>`:''}</summary>
     <ul class="qf">${fx.map(f=>{const k=qfKey(e,f),t=GALA.ticks[k],hf=(e.handFixes||{})[f.heat+'.'+f.lane]||{};
      return `<li class="${t?'done':''}${f.cleared?' cleared':''}">${tickBox(k,'Lane '+f.lane+' heat '+f.heat)}<div><b>Lane ${esc(f.lane)} · Heat ${esc(f.heat)}</b>${f.cleared?` <span class="st audited" title="${esc('No longer flagged since '+agoShort(f.clearedAt))}">settled by the re-check</span>`:''} <span class="meta">in the merge file: ${esc(f.inFile||'–')}</span>
       <ul>${f.checks.map(c=>`<li>${esc(c)}</li>`).join('')}</ul>
       ${f.scans.length?`<div class="sm">${f.scans.map(sc=>`<a href="${esc(galaHref('sessions/S'+s.n+'/scans/'+sc.scan))}#page=${esc(sc.page)}" target="_blank" rel="noopener">${esc(sc.label)}, page ${esc(sc.page)}</a>`).join(' · ')}</div>`:''}
       <form class="qffix" data-fix="${s.n}|${e.event}|${esc(f.heat)}|${esc(f.lane)}" autocomplete="off">
        ${(()=>{const w=qfTimes(f,hf),sw=qfSwimmer(s,e,f,hf);f._block=!!(w||sw);f._watch=!!w;return w+sw})()}
        <label>Result <select name="status"><option value="">as audited</option><option value="OK"${hf.status==='OK'?' selected':''}>Swam</option><option value="NS"${hf.status==='NS'?' selected':''}>No-show</option><option value="DQ"${hf.status==='DQ'?' selected':''}>DQ</option></select></label>
        ${f._block&&!hf.time?'':`<label>Time <input name="time" value="${esc(hf.time||'')}" placeholder="m:ss.hh" size="8"></label>`}
        <label class="qfcode"${hf.status==='DQ'?'':' hidden'}>Code <input name="code" value="${esc(hf.code||'')}" list="dqcodes" size="6"></label>
        <label class="qfnote">Note <input name="note" value="${esc(hf.note||'')}" placeholder="what was fixed"></label>
        ${(()=>{const mi=/not in the merge file/i.test(f.checks.join(' ')),wr=!!(f.row&&f.row.writtenIn)||/written in/i.test(f.checks.join(' '));
          return `<button type="submit"${mi||wr||f._watch?' class="primary"':''}>${mi||wr?(hf.add?'Confirmed · update':mi?'Confirm swim':'Confirm swimmer'):f._watch?'Save &amp; recalculate':hf.by?'Update':'Save fix'}</button>`})()}${hf.by?'<button type="button" data-clearfix>Clear</button>':''}
        <span class="sm" data-fixmsg>${hf.by?esc('Fixed by '+hf.by+' · '+agoShort(hf.at)):'Or just tick it if nothing needs changing.'}</span></form>
       ${t?`<div class="sm">done · ticked by ${esc(t.by)} · ${esc(agoShort(t.at))}</div>`:''}</div></li>`}).join('')}</ul></details></td></tr>`:'';
   return `<tr class="${t?'done':''}" data-cldrop="S${s.n}/scans">
    <td>${tickBox(key,'Event '+e.event)}</td>
    <td><span class="evname">Event ${e.event}</span>${e.desc?`<div class="sm">${esc(e.desc)}</div>`:''}${!e.expected&&s.meet.entries.length?'<div class="sm gwarn">not in the entries</div>':''}${t?`<div class="sm">ticked by ${esc(t.by)}</div>`:''}</td>
    <td class="c">${e.printed.length?'<span class="ok" title="'+esc(e.printed.map(x=>x.split('/').pop()).join(', '))+'">✓</span>':'<span class="no" title="No printed lane sheets for this event">–</span>'}</td>
    <td>${e.packs.length?e.packs.map(p=>link(s,'scans',p)).join('<br>'):'<span class="no">–</span>'}</td>
    <td class="c">${g7}${e.gen7Missing.length&&e.gen7.length?`<div class="sm">missing ${esc(e.gen7Missing.join(', '))}</div>`:''}${(e.notSwum||[]).length?`<div class="sm" title="${esc('Confirmed by '+(e.notSwumBy||'someone'))}">not swum ${esc(e.notSwum.join(', '))}</div>`:''}</td>
    <td class="c">${e.ocrLanes?e.ocrLanes+(e.lanes?'/'+e.lanes:''):'–'}</td>
    <td>${e.report?link(s,'reports',e.report):'–'}</td>
    <td>${mergeCell}</td><td class="c">${fxCell}</td>
    <td><span class="st ${e.status}">${esc(ST_TXT[e.status]||e.status)}</span>${e.computed&&!e.computed.ok&&e.status!=='computing'?`<div class="sm gwarn" title="${esc(e.computed.msg||'')}">portal: ${esc((e.computed.msg||'failed').slice(0,90))}</div>`:''}</td>
    <td><button type="button" data-ask="${key}" ${e.packs.length?'':'disabled title="No lane sheet pack yet"'}>${e.asked?'Ask again':'Ask Claude'}</button></td></tr>${fxRows}`}).join('')+'</table></div>'
  :'<div class="meta" style="padding:8px 0">No events yet. They come from the entries export, or appear as lane sheet packs and Gen7 races arrive.</div>';
 const scanFound=list=>list.map(x=>link(s,'scans',x.name)+` <span class="meta">(${x.done}/${x.pages??'?'} pages read)</span>`);
 box.innerHTML=`<div class="card clhead"><div class="gh-top"><div><h2>Session ${s.n} checklist</h2>
   <div class="meta">What the session's audit needs, in order. Files are found automatically when they arrive; tick each item once you've checked it. Ticks are shared.</div></div>
   <div style="font-weight:600;white-space:nowrap">${ticked} of ${keys.length} ticked</div></div><div class="gbar"><i style="width:${pct}%"></i></div></div>
  <div class="card">
  <div class="cl-sec">Start of the session</div>
  ${clRow(s,'S'+s.n+':entries','Meet entries export','From Meet Manager, for this session: the events, heats and lanes to expect (<code>Meet Entries-…zip</code> or its <code>Hfile…hy3</code>).',
    s.meet.entries.map(n=>link(s,'meet',n)+(s.expected.events?` <span class="meta">(${s.expected.events} events, ${s.expected.heats} heats)</span>`:'')
     +(s.events.some(e=>e.expected&&!e.seeded)?' <span class="meta">(not seeded: a pre-gala export; the merge results seed each swim from the lane sheets)</span>':'')),'S'+s.n+'/meet')}
  ${clRow(s,'S'+s.n+':lanesprint','Printed lane sheets (Meet Manager)','The lane sheet PDF Meet Manager printed for the session (not the scan). Claude copies the names, ages, teams and seed times from it, which is much more accurate than reading them off the scans. Bring it in before the first event.',
    s.lanePrints.map(p=>`<a href="${esc(galaHref(p.path))}" target="_blank" rel="noopener">${esc(p.name)}</a>`+` <span class="meta">(${p.sheets} sheet${p.sheets===1?'':'s'}, event${p.events.length===1?'':'s'} ${esc(evRange(p.events))})</span>`),'S'+s.n+'/meet')}
  ${clRow(s,'S'+s.n+':mmresults','Empty merge results export (Meet Manager)','<b>Once for the whole gala</b>: export merge results from Meet Manager before any results are in (name it with "Results"). Its header and layout are copied into every merge results file, in every session.',
    s.template?[`<a href="${esc(galaHref(s.template))}" target="_blank" rel="noopener">${esc(s.templateName)}</a>`+(s.templateSession!==s.n?` <span class="meta">(in session ${s.templateSession}; used for the whole gala)</span>`:'')]:[],'S'+s.n+'/meet')}
  <div class="cl-sec">Each event, as it is swum</div>
  <div class="meta" style="margin-bottom:4px">An event is <b>ready</b> when its lane sheet pack and every heat's Gen7 race PDF are in${GALA.auto?'; Claude is then asked to transcribe and audit it':''}. Claude then writes the event's <b>merge results</b> (<code>.hy3</code>, to import into Meet Manager) and its <b>quick fixes</b>: the swims to check by hand, listed under the event to tick off as they're fixed. Drop a lane sheet pack on an event to add it.</div>
  ${ev}
  <div class="cl-sec">During and after the session</div>
  ${clRow(s,'S'+s.n+':dq','DQ slips','The referees\' DQ slips, scanned (name the file with "DQ"). Claude reads each batch as it arrives.',scanFound(dq),'S'+s.n+'/scans')}
  ${clRow(s,'S'+s.n+':etd','ETD programme','The programme with the Gen7 race numbers written by each heat (name the file with "ETD"). Needed for combined events and re-started heats.',scanFound(etd),'S'+s.n+'/scans')}
  ${clRow(s,'S'+s.n+':codes','DQ codes','Each slip\'s code, given by Claude from '+(s.meet.codes.length?'the meet\'s own list ('+link(s,'meet',s.meet.codes[s.meet.codes.length-1])+')':'the full FINA list in the plugin (<code>gala-audit/data/dqcodesFINA.txt</code>); drop a <code>.txt</code> with the meet\'s own list in the inbox to use that instead')+'. Codes it wasn\'t sure of need the referee\'s OK.',s.dqCodes?[link(s,'ocr','dq-codes.json')]:[],null,true)}
  ${clRow(s,'S'+s.n+':merged','Session merge results (all events)','Written by Claude once every event is audited: one file with the whole session, and the session report.',s.merged.map(n=>link(s,'reports',n)).concat(s.sessionReport?['session report: '+link(s,'reports',s.sessionReport)]:[]),null,true)}
  ${clRow(s,'S'+s.n+':mmfinal','Final merge results export','Exported from Meet Manager after the audited results were imported and finalised (name it with "final").',s.meet.final.map(n=>link(s,'meet',n)),'S'+s.n+'/meet')}
  ${clRow(s,'S'+s.n+':final','Final audit report','Written by the portal when the final export is in: every change made by hand in Meet Manager.',s.finalReports.map(n=>link(s,'reports',n)),null,true)}
  </div>`}
async function galaUpload(folder,files,card){
 const msg=card&&card.querySelector('[data-gmsg]');const say=t=>{if(msg)msg.textContent=t};
 for(const f of files){
  say('Uploading '+f.name+'…');
  const u=url('/api/gala/upload');u.searchParams.set('folder',folder);u.searchParams.set('filename',f.name);
  const r=await fetch(u,{method:'POST',body:f});const jj=await r.json().catch(()=>({}));
  if(!r.ok){say('Could not upload '+f.name+': '+(jj.error||r.status));return}}
 say(folder==='inbox'?`Uploaded ${files.length} file${files.length>1?'s':''}: sorting…`:`Uploaded ${files.length} file${files.length>1?'s':''}.`);
 galaSig='';loadGala();if(folder==='inbox')setTimeout(()=>{galaSig='';loadGala()},9000)}
function galaClicks(e){
 const sb=e.target.closest('[data-sess]');if(sb){GSESS=+sb.dataset.sess;store('qa-gsess',GSESS);galaSig='';loadGala();return}
 if(e.target.closest('[data-newsess]')){galaPost('/api/gala/session').then(()=>{GSESS=(GALA&&GALA.sessions.length?Math.max(...GALA.sessions.map(s=>s.n)):0)+1;store('qa-gsess',GSESS);galaSig='';loadGala()});return}
 const go=e.target.closest('[data-goto]');if(go){e.preventDefault();showView(go.dataset.goto);return}
 const ab=e.target.closest('[data-ask]');if(ab){ab.disabled=true;
  galaPost('/api/gala/ask',{key:ab.dataset.ask}).then(j=>{
    if(j.report){ab.disabled=false;galaDialog({title:'Session files',text:j.report,ok:'OK',cancel:false});return}
    if(j.job){ab.disabled=false;galaNotice(j.job==='already running'?'Already working on it.':'Working on it: the result shows here and in the activity log in a minute.');galaSig='';setTimeout(loadGala,1500);return}
    ab.textContent='Asked (#'+j.question+')';galaSig='';setTimeout(loadGala,1500)})
   .catch(err=>{ab.disabled=false;galaNotice(err.message,'error')});return}
 const mv=e.target.closest('[data-move]');if(mv){const li=mv.closest('li');
  galaPost('/api/gala/move',{path:mv.dataset.move,session:+li.querySelector('[data-mvs]').value,folder:li.querySelector('[data-mvf]').value})
   .then(()=>{galaSig='';loadGala()}).catch(err=>galaNotice(err.message,'error'))}}
function galaChange(e){
 const fm=e.target.closest('form.qffix');if(fm){fm.dataset.dirty='1';if(e.target.name==='status')fm.querySelector('.qfcode').hidden=e.target.value!=='DQ';return}
 const up=e.target.closest('[data-gup]');if(up&&up.files.length){galaUpload(up.dataset.gup,[...up.files],up.closest('[data-gfolder],.cl-row,tr,.drop')||up.parentElement);return}
 const tk=e.target.closest('[data-tick]');if(tk)galaTick(tk)}
// Ticking an event complete while heats have no Gen7 race: confirm those races weren't swum.
async function galaTick(tk){const key=tk.dataset.tick,body={key,checked:tk.checked};
 const m=/^S(\d+):e(\d+)$/.exec(key);
 if(m&&tk.checked){const s=GALA.sessions.find(x=>x.n===+m[1]),ev=s&&s.events.find(x=>x.event===+m[2]),miss=ev?ev.gen7Missing:[];
  if(miss.length){const many=miss.length>1;
   const ok=await galaDialog({title:'Event '+ev.event+' is short of results',
    text:'There is no Gen7 result for heat'+(many?'s ':' ')+miss.join(', ')+' (of '+ev.heats+'). Were '+(many?'these races':'this race')+' not swum? '
     +'Ticking the event complete records '+(many?'them':'it')+' as not swum, and Claude audits the event without '+(many?'them':'it')+'.',
    ok:'Not swum · tick complete',cancel:'Keep it open'});
   if(!ok){tk.checked=false;return}
   body.notSwum=miss}}
 galaPost('/api/gala/tick',body).then(()=>{if(tk.checked)offerRegen(key,key);galaSig='';loadGala()})
  .catch(err=>{tk.checked=!tk.checked;galaNotice('Could not save: '+err.message,'error')})}
for(const sec of ['#v-gala','#v-checklist']){const el=$(sec);el.addEventListener('click',galaClicks);el.addEventListener('change',galaChange);
 el.addEventListener('dragover',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(!c)return;e.preventDefault();c.classList.add('over')});
 el.addEventListener('dragleave',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(c&&!c.contains(e.relatedTarget))c.classList.remove('over')});
 el.addEventListener('drop',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(!c)return;e.preventDefault();c.classList.remove('over');
  const fs=[...(e.dataTransfer.files||[])];if(!fs.length)return;
  const folder=c.id==='g-inbox'?'inbox':(c.dataset.gfolder||c.dataset.cldrop);
  if(/\/(ocr|reports)$/.test(folder)){galaNotice('That folder is written by Claude. Drop the file on the inbox instead.','error');return}
  galaUpload(folder,fs,c)})}
$('#g-inboxfile').addEventListener('change',e=>{if(e.target.files.length)galaUpload('inbox',[...e.target.files],$('#g-inbox'))});
$('#g-auto').addEventListener('change',e=>galaPost('/api/gala/auto',{auto:e.target.checked}).then(()=>{galaSig='';loadGala()}));
setInterval(()=>{if(!document.hidden&&(view==='gala'||view==='checklist'||view==='restart'))loadGala()},4000);

// After the last quick fix of an event is done (ticked or fixed here), offer to write its merge results again.
function offerRegen(key,justDone){
 const m=/^S(\d+):e(\d+):qf:/.exec(key);if(!m)return;const n=+m[1],ev=+m[2];
 const s=GALA&&GALA.sessions.find(x=>x.n===n),e=s&&s.events.find(x=>x.event===ev);if(!e||!e.fixes||!e.canMerge)return;
 const left=e.fixes.filter(f=>{const k='S'+n+':e'+ev+':qf:'+f.heat+'.'+f.lane;return k!==justDone&&!GALA.ticks[k]});
 if(left.length)return;
 galaDialog({title:'All quick fixes for event '+ev+' are done',
  text:'Regenerate the event\'s merge results now, with the fixes made in the portal and every DQ slip read so far? No scans are read again.',
  ok:'Regenerate the merge results',cancel:'Not now'}).then(yes=>{if(!yes)return;
  galaPost('/api/gala/ask',{key:'S'+n+':remerge:'+ev}).then(j=>{galaNotice('Regenerating event '+ev+'\'s merge results (the portal does it; a minute).');galaSig='';setTimeout(loadGala,1500)})
   .catch(err=>galaNotice(err.message,'error'))})}
document.addEventListener('submit',async ev=>{const fm=ev.target.closest('form.qffix');if(!fm)return;ev.preventDefault();
 const [n,event,heat,lane]=fm.dataset.fix.split('|'),d=Object.fromEntries([...new FormData(fm).entries()].filter(([k])=>!/^(w\d+|wt\d+|swpick|swq)$/.test(k)));
 const watches={};let bad='';
 fm.querySelectorAll('input[data-swcol]').forEach(i=>{const c=i.dataset.swcol,tv=i.value.trim(),orig=i.dataset.orig||'',acc=fm.querySelector('input[data-swacc="'+c+'"]');
  if(tv&&tv!==orig){if(!/^(\d{1,2}:)?\d{1,2}\.\d{2}$/.test(tv))bad='SW'+c+': type the time off the sheet, like 1:05.32';else watches[c]={time:tv}}
  else if(acc&&acc.checked&&orig)watches[c]={accepted:true}});
 if(bad){fm.querySelector('[data-fixmsg]').textContent=bad;return}
 d.watches=watches;
 const pick=fm.querySelector('input[name=swpick]:checked');if(pick&&pick.value)d.swimmer={id:pick.value,name:pick.dataset.name};
 const swb=fm.querySelector('.qfsw[data-add]');if(swb){if(!d.swimmer){fm.querySelector('[data-fixmsg]').textContent='Pick the swimmer first';return}d.add=true}
 if(d.time&&!/^(\d{1,2}:)?\d{1,2}\.\d{2}$/.test(d.time)){fm.querySelector('[data-fixmsg]').textContent='Time like 1:05.32 or 32.10';return}
 if(d.status==='DQ'&&!d.code){fm.querySelector('[data-fixmsg]').textContent='A DQ needs its code';return}
 if(d.code&&CODES.codes&&CODES.codes.length&&!CODES.codes.some(c=>c[0]===d.code)&&
   !await galaDialog({title:'DQ code '+d.code+' is not in the list',text:d.code+' is not in '+(CODES.source||'the code list')+'. Save it anyway?',ok:'Save it',cancel:'Change it'}))return;
 galaPost('/api/gala/fix',Object.assign({session:+n,event:+event,heat:+heat,lane:+lane},d)).then(()=>{
  delete fm.dataset.dirty;document.activeElement&&document.activeElement.blur();
  if(Object.keys(d.watches||{}).length||d.swimmer||fm.querySelector('.qft'))galaPost('/api/gala/recheck',{session:+n,event:+event}).catch(err=>galaNotice('Could not re-check: '+err.message,'error'));
  offerRegen('S'+n+':e'+event+':qf:'+heat+'.'+lane,'S'+n+':e'+event+':qf:'+heat+'.'+lane);galaSig='';loadGala()})
  .catch(err=>{fm.querySelector('[data-fixmsg]').textContent='Could not save: '+err.message})});
document.addEventListener('click',ev=>{const b=ev.target.closest('[data-clearfix]');if(!b)return;const fm=b.closest('form.qffix');
 const [n,event,heat,lane]=fm.dataset.fix.split('|');
 galaPost('/api/gala/fix',{session:+n,event:+event,heat:+heat,lane:+lane,status:'',time:'',note:''}).then(()=>{delete fm.dataset.dirty;galaSig='';loadGala()})});

// ---- quick fixes: watches read with medium/low confidence (accept as read, or type the reading), and swimmers
// written in on the sheet (pick the entered swimmer: suggestions from the entries file, or search for another).
function qfWatches(f,hf){
 const row=f.row;if(!row)return '';
 const text=f.checks.join(' ');
 // The watches a check is about: "SW timer N" names one; "In doubt" / "Poor SW" / "SW median" are about them all.
 const named=new Set([...text.matchAll(/\bSW timer (\d+)/gi)].map(m=>+m[1]));
 const all=/in doubt|poor sw|sw median|sw times?\b/i.test(text);
 if(!named.size&&!all&&!/watch/i.test(text))return '';
 const ws=row.watches.filter(w=>all||named.has(w.column)||!w.normalized||w.confidence!=='HIGH'||w.corrected);
 if(!ws.length)return '';
 const fixed=hf.watches||{};
 return '<div class="qfws"><div class="sm">Check the watch times on the sheet; correct any that were misread, save, and the event is checked again.</div>'+ws.map(w=>{const c=w.column,fx=fixed[c]||{},readable=!!w.normalized;
  const sure=readable&&w.confidence==='HIGH'&&!w.corrected;
  const how=readable?`read as <b>${esc(w.normalized)}</b>${sure?'':` (${esc((w.confidence||'').toLowerCase())} confidence${w.corrected?', overwritten on the sheet':''})`}`:'<b class="gwarn">couldn\'t be read</b>';
  return `<div class="qfw" data-col="${esc(c)}"><span>Timer ${esc(c)}: written “${esc(w.written||'?')}”, ${how}</span>
   ${readable&&!sure?`<label><input type="radio" name="w${esc(c)}" value="accept"${fx.accepted?' checked':''}> accept ${esc(w.normalized)}</label>`:''}
   <label><input type="radio" name="w${esc(c)}" value="capture"${fx.time?' checked':''}> it reads</label>
   <input name="wt${esc(c)}" value="${esc(fx.time||'')}" placeholder="m:ss.hh" size="8">
   ${(fx.accepted||fx.time)?'':`<label class="sm"><input type="radio" name="w${esc(c)}" value="" checked> leave</label>`}</div>`}).join('')+'</div>'}
function qfSwimmer(s,e,f,hf){
 const row=f.row;
 const missing=/not in the merge file/i.test(f.checks.join(' ')),written=!!(row&&row.writtenIn)||/written in/i.test(f.checks.join(' '));
 if(!written&&!missing&&!/written in|swimmer is not the one|different swimmer|no seeded entry|swimmer/i.test(f.checks.join(' ')))return '';
 const name=(row&&row.name)||'',chosen=hf.swimmer;
 return `<div class="qfsw" data-q="${esc(name)}" data-event="${e.event}" data-session="${s.n}" data-chosen="${esc(chosen?chosen.id:'')}"${missing||written?' data-add="1"':''}>
  ${missing?`<div><b>Confirm the swim</b>: pick the swimmer (the best match from the entries is chosen) and press <b>Confirm swim</b>. It is put in the merge file for them, with an entry added if they weren't entered in this event.</div>`
   :`<div><b>Confirm the swimmer</b> written in on the sheet: pick them (the best match from the entries is chosen) and press <b>Confirm swimmer</b>. The swim is given to them in the merge file, with an entry added if they weren't entered in this event; the printed swimmer gets no result.</div>`}
  ${row&&row.notes?`<div class="sm">On the sheet: ${esc(row.notes)}</div>`:''}
  <div>Swimmer on the sheet: <b>${esc(name||'?')}</b>${chosen?` · <span class="ok">chosen: ${esc(chosen.name)}</span>`:''}</div>
  <div class="swlist sm">Looking for matches in the entries…</div>
  <label class="sm">Search for another swimmer <input name="swq" placeholder="surname or first name" size="22"></label></div>`}
const SWCACHE={};
async function swFill(box,q){
 const n=box.dataset.session,ev=box.dataset.event,key=n+'|'+ev+'|'+q,list=box.querySelector('.swlist');
 try{SWCACHE[key]=SWCACHE[key]||(await api('/api/gala/swimmers?session='+n+'&event='+ev+'&q='+encodeURIComponent(q))).swimmers}catch(err){list.textContent='Could not search the entries: '+err.message;return}
 const rows=SWCACHE[key],chosen=box.dataset.chosen,prev=(box.querySelector('input[name=swpick]:checked')||{}).value;
 list.innerHTML=rows.length?rows.map((w,i)=>`<label class="swopt"><input type="radio" name="swpick" value="${esc(w.id)}" data-name="${esc(w.name)}"${(prev||chosen)===w.id||(!prev&&!chosen&&i===0&&(w.entered||w.score>=0.8))?' checked':''}>
   ${esc(w.name)}${w.preferred&&w.preferred.toLowerCase()!==w.first.toLowerCase()?' <span class="meta">(first name '+esc(w.first)+')</span>':''} <span class="meta">${esc([w.sex==='F'?'girl':w.sex==='M'?'boy':'',w.age&&('age '+w.age),w.team].filter(Boolean).join(' · '))}</span>
   ${w.entered?'<span class="ok">entered in this event</span>':'<span class="gwarn">not entered in this event</span>'}${i===0&&q&&!prev&&!chosen?' <span class="meta">(best match)</span>':''}</label>`).join('')
  +'<label class="swopt"><input type="radio" name="swpick" value=""> none of these</label>'
  :'<span class="meta">No swimmer in the entries matches.</span>'}
function swAll(){document.querySelectorAll('.qfsw').forEach(b=>{if(!b.dataset.filled){b.dataset.filled='1';swFill(b,b.dataset.q)}})}
let swTimer=null;
document.addEventListener('input',ev=>{const i=ev.target.closest('.qfsw input[name=swq]');if(!i)return;const box=i.closest('.qfsw');
 clearTimeout(swTimer);swTimer=setTimeout(()=>swFill(box,i.value.trim()||box.dataset.q),300)});
document.addEventListener('focusin',ev=>{const i=ev.target.closest('.qfw input[name^=wt]');if(i){const r=i.closest('.qfw').querySelector('input[value=capture]');if(r)r.checked=true}});

document.addEventListener('click',ev=>{const b=ev.target.closest('[data-recheck]');if(!b)return;ev.preventDefault();const [n,e]=b.dataset.recheck.split('|');
 b.disabled=true;b.textContent='re-checking…';galaPost('/api/gala/recheck',{session:+n,event:+e}).then(()=>{galaSig='';setTimeout(loadGala,1500)}).catch(err=>{b.disabled=false;galaNotice(err.message,'error')})});

// ---- the times row at the top of every quick fix: TP, PB, each stopwatch (correctable), median and final.
// Typing a watch time recalculates the median and the differences here; "Save & recalculate" saves the corrected
// readings and re-runs the event's check against TP and PB.
function hund(t){const m=/^(?:(\d{1,2}):)?(\d{1,2})\.(\d{2,3})$/.exec((t||'').trim());return m?((+m[1]||0)*60+(+m[2]))*100+Math.floor(+(m[3]+'0').slice(0,3)/10):null}
function fmtH(h){if(h==null)return '';const neg=h<0;h=Math.abs(Math.round(h));const m=Math.floor(h/6000),s=Math.floor(h%6000/100),c=h%100;
 return (neg?'-':'')+(m?m+':'+String(s).padStart(2,'0'):s)+'.'+String(c).padStart(2,'0')}
function diffH(a,b){if(a==null||b==null)return '';const d=(a-b)/100;return (d>0?'+':'')+d.toFixed(2)+'s'}
function qfTimes(f,hf){
 const t=f.times,row=f.row;if(!t&&!row)return '';
 const fixed=hf.watches||{},tol=Math.round(((t&&t.tolerance)||0.30)*100);
 const byCol={};(row?row.watches:[]).forEach(w=>byCol[w.column]=w);(t?t.sw:[]).forEach(w=>{byCol[w.column]=Object.assign({normalized:w.time},byCol[w.column]||{},w)});
 const cols=[1,2,3];
 const tp=t?hund(t.tp):null,pb=t?hund(t.pb):null;
 const cell=(label,v,cls)=>`<td class="${cls||''}"><div class="tl">${label}</div><b>${esc(v||'–')}</b></td>`;
 const swCells=cols.map(c=>{const w=byCol[c]||{},orig=w.normalized||'',val=(fixed[c]&&fixed[c].time)||orig,sure=!w.confidence||(w.confidence==='HIGH'&&!w.corrected);
  return `<td class="sw${orig?'':' unread'}${sure?'':' unsure'}"><div class="tl">SW${c}${w.written&&w.written!==orig?` <span class="sm">“${esc(w.written)}”</span>`:''}</div>
   <input data-swcol="${c}" data-orig="${esc(orig)}" value="${esc(val)}" placeholder="${orig?'':'not read'}" size="8">
   ${orig&&!sure?`<label class="sm"><input type="checkbox" data-swacc="${c}"${fixed[c]&&fixed[c].accepted?' checked':''}> OK as read (${esc((w.confidence||'').toLowerCase())})</label>`:''}
   <div class="sm" data-swdiff="${c}"></div></td>`}).join('');
 return `<div class="qft" data-tp="${tp??''}" data-pb="${pb??''}" data-tol="${tol}"><table><tr>
   ${cell('TP',t&&t.tp)}${cell('PB',t&&t.pb)}${swCells}<td><div class="tl">SW median</div><b data-swmed>${esc(t&&t.median||'–')}</b><div class="sm" data-meddiff></div></td>${cell('Final',t&&t.final,'fin')}</tr></table>
  <div class="sm">Correct any stopwatch time that was misread on the sheet, then <b>Save &amp; recalculate</b>: the swim is checked again against TP and PB (tolerance ${(tol/100).toFixed(2)}s).</div></div>`}
function qftUpdate(box){
 const tp=box.dataset.tp?+box.dataset.tp:null,pb=box.dataset.pb?+box.dataset.pb:null,tol=+box.dataset.tol||30;
 const vals=[];box.querySelectorAll('input[data-swcol]').forEach(i=>{const v=hund(i.value);i.classList.toggle('bad',!!i.value.trim()&&v==null);
  const d=box.querySelector('[data-swdiff="'+i.dataset.swcol+'"]');
  if(v!=null){vals.push(v);const parts=[];if(tp!=null)parts.push(`<span class="${Math.abs(v-tp)>tol?'gwarn':'ok'}">TP ${diffH(v,tp)}</span>`);if(pb!=null)parts.push(`<span class="${Math.abs(v-pb)>tol?'gwarn':'ok'}">PB ${diffH(v,pb)}</span>`);d.innerHTML=parts.join(' ')}else d.innerHTML=''});
 vals.sort((a,b)=>a-b);const med=vals.length?(vals.length%2?vals[(vals.length-1)/2]:(vals[vals.length/2-1]+vals[vals.length/2])/2):null;
 const m=box.querySelector('[data-swmed]');if(m)m.textContent=med==null?'–':fmtH(med);
 const md=box.querySelector('[data-meddiff]');if(md)md.innerHTML=med==null?'':[tp!=null?`<span class="${Math.abs(med-tp)>tol?'gwarn':'ok'}">TP ${diffH(med,tp)}</span>`:'',pb!=null?`<span class="${Math.abs(med-pb)>tol?'gwarn':'ok'}">PB ${diffH(med,pb)}</span>`:''].join(' ')}
document.addEventListener('input',ev=>{const b=ev.target.closest('.qft');if(b)qftUpdate(b)});
const _qftObs=new MutationObserver(()=>document.querySelectorAll('.qft:not([data-init])').forEach(b=>{b.dataset.init='1';qftUpdate(b)}));
_qftObs.observe(document.body,{childList:true,subtree:true});

// Remember which quick-fix lists are open, so a refresh doesn't fold one you are looking at (or unfold one you closed).
const QF_OPEN={};
document.addEventListener('toggle',ev=>{const d=ev.target;if(d.matches&&d.matches('details[data-qfd]'))QF_OPEN[d.dataset.qfd]=d.open},true);

// ---- dialogs and notices that are part of the page (never the browser's alert/confirm/prompt boxes)
function galaDialog(o){
 return new Promise(res=>{
  let d=$('#gdlg');if(!d){d=document.createElement('div');d.id='gdlg';d.className='gdlg';d.setAttribute('role','dialog');d.setAttribute('aria-modal','true');document.body.appendChild(d)}
  d.innerHTML=`<div class="gdlg-box"><h3>${esc(o.title||'')}</h3>${o.text?`<p>${esc(o.text)}</p>`:''}
   ${o.check?`<label class="gdlg-ck"><input type="checkbox"${o.checked?' checked':''}> ${esc(o.check)}</label>`:''}
   ${o.input!==undefined?`<input class="gdlg-in" value="${esc(o.input)}" placeholder="${esc(o.placeholder||'')}"><div class="sm gwarn gdlg-err"></div>`:''}
   <div class="gdlg-btns">${o.cancel===false?'':`<button type="button" data-no>${esc(o.cancel||'Cancel')}</button>`}<button type="button" class="primary" data-yes>${esc(o.ok||'OK')}</button></div></div>`;
  d.hidden=false;const inp=d.querySelector('.gdlg-in'),tgt=inp||d.querySelector('[data-yes]');
  // the keyboard may still be in the terminal (an iframe): take it back, or typing goes to the terminal
  const af=document.activeElement;if(af&&af.tagName==='IFRAME')af.blur();window.focus();tgt.focus();setTimeout(()=>tgt.focus(),50);
  const done=v=>{if(o.input!==undefined&&v!==false&&o.validate){const err=o.validate(v);if(err){d.querySelector('.gdlg-err').textContent=err;return}}
   if(o.check){const c=d.querySelector('.gdlg-ck input');o.checked=!!(c&&c.checked)}   // read back by the caller
   d.hidden=true;d.innerHTML='';document.removeEventListener('keydown',key,true);res(v)};
  const key=ev=>{if(ev.key==='Escape'){ev.preventDefault();done(o.input!==undefined?null:false)}else if(ev.key==='Enter'&&(inp||document.activeElement.closest('#gdlg'))){ev.preventDefault();done(inp?inp.value:true)}};
  document.addEventListener('keydown',key,true);
  d.onclick=ev=>{if(ev.target===d)done(o.input!==undefined?null:false);else if(ev.target.closest('[data-yes]'))done(inp?inp.value:true);else if(ev.target.closest('[data-no]'))done(o.input!==undefined?null:false)}})}
function galaNotice(text,kind){
 let box=$('#gnotes');if(!box){box=document.createElement('div');box.id='gnotes';box.setAttribute('aria-live','polite');document.body.appendChild(box)}
 const n=document.createElement('div');n.className='gnote'+(kind==='error'?' err':'');n.innerHTML=`<span>${esc(text)}</span><button type="button" aria-label="Close">✕</button>`;
 n.querySelector('button').onclick=()=>n.remove();box.appendChild(n);setTimeout(()=>n.remove(),kind==='error'?12000:6000)}

// ---- Restart tab: archive or delete the gala and start again
function renderRestart(){
 if(!GALA||!GALA.summary)return;const s=GALA.summary;
 const stat=(n,t)=>`<div class="gstat"><b>${n}</b><span class="meta">${t}</span></div>`;
 $('#rs-sum').innerHTML=stat(s.sessions,'sessions')+stat(s.files,'files')+stat(size(s.bytes),'on disk')+stat(s.questions,'questions')+stat(s.documents,'documents')
  +(s.linked.length?`<div class="meta" style="align-self:center">Linked inbox folders, left alone: ${esc(s.linked.join(', '))}</div>`:'');
 const a=GALA.archives||[];
 $('#rs-arch').innerHTML=a.length?'<table>'+a.map(x=>`<tr><td><code>data/archive/${esc(x.name)}</code></td><td>${x.sessions} session${x.sessions===1?'':'s'} · ${x.files} files · ${size(x.bytes)}${x.questions?' · with questions':''}</td><td style="text-align:right;white-space:nowrap"><button type="button" data-rsres="${esc(x.name)}" data-q="${x.questions?1:''}">Restore</button> <button type="button" data-rsdel="${esc(x.name)}">Delete for good</button></td></tr>`).join('')+'</table>':'None yet.'}
function rsCheck(){const mode=(document.querySelector('input[name=rsmode]:checked')||{}).value,w=mode==='delete'?'DELETE':'RESTART';
 $('#rs-word').textContent=w;$('#rs-btn').disabled=$('#rs-confirm').value.trim().toUpperCase()!==w;$('#rs-btn').textContent=mode==='delete'?'Delete and restart':'Archive and restart'}
$('#v-restart').addEventListener('input',rsCheck);$('#v-restart').addEventListener('change',rsCheck);
$('#rs-btn').addEventListener('click',async()=>{
 const mode=document.querySelector('input[name=rsmode]:checked').value,q=$('#rs-q').checked;
 const ok=await galaDialog({title:mode==='delete'?'Delete this gala for good?':'Archive this gala and start again?',
  text:(mode==='delete'?'Every session\'s files, the ticks, fixes and history are deleted and can\'t be brought back.':'Everything is moved to data/archive and the portal starts empty.')+(q?' The questions, answers and documents are cleared too.':'')+' Claude carries on, but its earlier work will be gone from the portal.',
  ok:mode==='delete'?'Delete for good':'Archive and restart',cancel:'Cancel'});
 if(!ok)return;$('#rs-btn').disabled=true;$('#rs-msg').textContent='Working…';
 try{const j=await galaPost('/api/gala/restart',{mode,questions:q,reimport:$('#rs-re').checked,label:$('#rs-label').value,confirm:$('#rs-confirm').value});
  $('#rs-confirm').value='';rsCheck();$('#rs-msg').textContent='';galaNotice(j.archive?'Archived to data/archive/'+j.archive+'. The portal is empty and ready for the next gala.':'Deleted. The portal is empty and ready for the next gala.');
  try{localStorage.removeItem('qa-drafts')}catch(e){}GSESS=0;galaSig='';loadGala()}
 catch(err){$('#rs-msg').textContent='';galaNotice(err.message,'error');rsCheck()}});
$('#rs-arch').addEventListener('click',async ev=>{const r=ev.target.closest('[data-rsres]');if(r){rsRestore(r.dataset.rsres,!!r.dataset.q);return}
 const b=ev.target.closest('[data-rsdel]');if(!b)return;
 if(!await galaDialog({title:'Delete archive '+b.dataset.rsdel+' for good?',text:'Its files are deleted and can\'t be brought back.',ok:'Delete for good',cancel:'Keep it'}))return;
 galaPost('/api/gala/archive-delete',{name:b.dataset.rsdel}).then(()=>{galaNotice('Archive deleted.');galaSig='';loadGala()}).catch(err=>galaNotice(err.message,'error'))});

// ---- Restart tab: updates from GitHub (update.sh)
let UPD=null,updAt=0;
async function loadUpdate(fetch){
 try{UPD=await api('/api/gala/update-status'+(fetch?'?fetch=1':''));updAt=Date.now()}catch(e){$('#up-info').textContent='Could not check: '+e.message;return}
 renderUpdate()}
function renderUpdate(){const u=UPD;if(!u)return;const go=$('#up-go'),cl=$('#up-claude-l');go.hidden=cl.hidden=true;
 if(!u.git){$('#up-info').innerHTML='This copy of the portal (v'+esc(u.version)+') isn\'t a git clone, so it can\'t update itself. Install it with <code>install.sh</code> to get updates.';return}
 const cur=(u.current||'').split('|');let h='Installed: <b>v'+esc(u.version)+'</b> · <code>'+esc(cur[0]||'?')+'</code> '+esc(cur[1]||'')+(cur[2]?' – '+esc(cur[2]):'');
 if(u.fetched===false)h+='<br><span class="bad">Couldn\'t reach GitHub just now (no internet?).</span>';
 if(u.checkedAt)h+='<br>Checked '+new Date(u.checkedAt*1000).toLocaleTimeString()+'.';
 const b=u.behind||[];
 if(u.changed&&u.changed.length)h+='<br><span class="bad">Portal files were changed on this computer, so it won\'t update: '+esc(u.changed.join(', '))+'</span>';
 else if(u.ahead)h+='<br><span class="bad">This copy has '+u.ahead+' change(s) of its own that aren\'t on GitHub (the development copy?), so it won\'t update.</span>';
 if(b.length){h+='<br><b>'+b.length+' update'+(b.length>1?'s':'')+' available:</b><ul class="up-list">'+b.slice(0,15).map(c=>'<li><code>'+esc(c.sha)+'</code> '+esc(c.date)+' – '+esc(c.subject)+'</li>').join('')+'</ul>';
  if(!(u.changed&&u.changed.length)&&!u.ahead)go.hidden=cl.hidden=false}
 else if(u.fetched!==false)h+='<br>Up to date.';
 $('#up-info').innerHTML=h;
 if(u.log){$('#up-logd').hidden=false;$('#up-log').textContent=u.log}}
$('#up-check').addEventListener('click',()=>{$('#up-info').textContent='Checking GitHub…';loadUpdate(true)});
$('#up-go').addEventListener('click',async()=>{const claude=$('#up-claude').checked;
 if(!await galaDialog({title:'Update the portal now?',text:'The portal stops, updates from GitHub (and the gala-ocr plugin), and starts again, which takes about a minute; this page reloads itself. The gala\'s files, ticks, fixes and questions are kept.'+(claude?' Claude Code is restarted too: do this between events, not while Claude is auditing one.':' Claude Code keeps running.'),ok:'Update now',cancel:'Not now'}))return;
 $('#up-go').disabled=true;
 try{await galaPost('/api/gala/update',{claude})}catch(err){$('#up-go').disabled=false;galaNotice(err.message,'error');return}
 galaNotice('Updating… the page reloads when the portal is back.');$('#up-info').textContent='Updating…';
 const was=UPD&&UPD.current;let down=false;const t0=Date.now();
 const poll=async()=>{if(Date.now()-t0>180000){galaNotice('The portal hasn\'t come back after 3 minutes. Look at .portal/update.log, or run bash start-portal.sh.','error');return}
  try{const r=await fetch(url('/api/gala/update-status'),{cache:'no-store'});if(r.ok){const j=await r.json();
   if(down||(j.current&&j.current!==was)){location.reload();return}}else down=true}catch(e){down=true}
  setTimeout(poll,3000)};setTimeout(poll,4000)});
// check when the Restart tab is shown (every 10 minutes at most)
setInterval(()=>{if(!document.hidden&&view==='restart'&&Date.now()-updAt>600000)loadUpdate(false)},5000);

async function rsRestore(name,hasQ){const s=(GALA&&GALA.summary)||{};
 const busy=s.sessions||s.files;
 const o={title:'Restore '+name+'?',text:'Its sessions, files, checklist ticks, quick fixes and activity log come back into the portal, and the archive is emptied.'
  +(busy?' What is in the portal now ('+s.sessions+' session'+(s.sessions===1?'':'s')+', '+s.files+' files) is archived first, so nothing is lost.':'')
  +' Claude isn\'t asked again about events it already did.',ok:'Restore',cancel:'Cancel'};
 if(hasQ){o.check='Also restore its Questions, answers and Documents (those in the portal now are archived with the rest)';o.checked=true}
 if(!await galaDialog(o))return;
 try{const j=await galaPost('/api/gala/archive-restore',{name,questions:!!o.checked});
  galaNotice('Restored '+name+'.'+(j.archive?' What was here is in data/archive/'+j.archive+'.':''));GSESS=0;galaSig='';loadGala()}
 catch(err){galaNotice(err.message,'error')}}

// ---- the web terminal through the portal: watch with the portal's key, type with the admin key
{const a=new URLSearchParams(location.search).get('admin');if(a)store('qa-admin',a)}
function galaAdmin(){return store('qa-admin')||''}
function galaTermUrl(){const a=galaAdmin();
 return location.origin+'/terminal/'+(a?'rw':'ro')+'/?key='+encodeURIComponent(KEY||'')+(a?'&admin='+encodeURIComponent(a):'')}
function galaTermBar(){const b=$('#termurl');
 if(TERM_DEFAULT!=='/terminal/'){b.textContent=termUrl().replace(/^https?:\/\//,'');return}
 b.textContent=galaAdmin()?'Typing to Claude · click to watch only':'Watching (read-only) · click to type';
 b.title=galaAdmin()?'This browser can type to Claude (it has the admin key). Click to go back to watching.':'Enter the admin key to type to Claude'}
async function galaTermClick(){if(TERM_DEFAULT!=='/terminal/')return changeTermUrl();
 if(galaAdmin()){if(!await galaDialog({title:'Watch only?',text:'This browser forgets the admin key and goes back to watching Claude without typing.',ok:'Watch only',cancel:'Keep typing'}))return;
  try{localStorage.removeItem('qa-admin')}catch(e){}}
 else{const v=await galaDialog({title:'Type to Claude',text:'Typing in the terminal needs the admin key. start-portal.sh prints it (the admin link) on the portal computer.',
   input:'',placeholder:'admin key',ok:'Type',validate:x=>x.trim()?'':'Enter the admin key'});
  if(v===null)return;
  const r=await fetch('/terminal/rw/?key='+encodeURIComponent(KEY||'')+'&admin='+encodeURIComponent(v.trim()),{cache:'no-store'}).catch(()=>null);
  if(!r||!r.ok){galaNotice(r&&r.status===403?'That isn\'t the admin key.':'The terminal didn\'t answer ('+(r?r.status:'no connection')+').','error');return}
  store('qa-admin',v.trim())}
 galaTermBar();$('#termnew').href=termUrl();if(!$('#termwin').hidden)$('#termframe').src=termUrl()}

// ---- views
if(location.hash==='#settings')location.hash='#users';
let view=['#gala','#checklist','#restart','#questions','#docs','#live','#users','#deploys'].includes(location.hash)?location.hash.slice(1):(store('qa-view')||'gala');
// ?me=NAME (from start-portal.sh) picks "I am" the first time
{const m=new URLSearchParams(location.search).get('me');if(m&&!store('qa-me'))store('qa-me',m)}
function showView(v){view=v;store('qa-view',v);if(v!=='deploys')focusDep=null;
 document.querySelectorAll('#views button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
 $('#v-gala').hidden=v!=='gala';$('#v-checklist').hidden=v!=='checklist';$('#v-restart').hidden=v!=='restart';$('#v-questions').hidden=v!=='questions';$('#v-docs').hidden=v!=='docs';$('#v-live').hidden=v!=='live';$('#v-users').hidden=v!=='users';$('#v-deploys').hidden=v!=='deploys';if(v==='users'){loadSettings();if(!UPD)checkUpdate(false)}if(typeof presence==='function')setTimeout(presence,50);refresh()}
$('#views').onclick=e=>{const b=e.target.closest('button');if(b&&b.dataset.v)showView(b.dataset.v)};
$('#mini').onclick=()=>{showView('live');window.scrollTo(0,0)};
function refresh(){loadUsers();(view==='gala'||view==='checklist'||view==='restart')?loadGala():view==='docs'?loadDocs():view==='live'?loadLive():view==='users'?null:view==='deploys'?loadDeps():load()}
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
  +(USERS.length?'':'<span class="meta">Add people on the Settings tab.</span>')}
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
const termUrl=()=>TERM_DEFAULT==='/terminal/'?galaTermUrl():(store('qa-term')||TERM_DEFAULT);
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
 if(w.hidden){placeTerm();$('#termframe').src=termUrl();galaTermBar();
  $('#termnew').href=termUrl();w.hidden=false;$('#termshade').hidden=false;document.body.classList.add('termopen')}
 $('#termframe').focus()}
function closeTerm(){$('#termframe').src='about:blank';$('#termwin').hidden=true;$('#termshade').hidden=true;document.body.classList.remove('termopen')}
async function changeTermUrl(){const v=await galaDialog({title:'Claude Code terminal address',text:'The address of this board\'s Claude Code terminal, as your browser reaches it (e.g. through the SSH tunnel).',
  input:termUrl(),placeholder:'http://127.0.0.1:17681',ok:'Save',validate:x=>x.trim()&&!/^https?:\/\/[^\s]+$/.test(x.trim())?'Use an address like http://127.0.0.1:17681':''});
 if(v===null)return;const t=v.trim();
 store('qa-term',t&&t!==TERM_DEFAULT?t:'');if(!$('#termwin').hidden){$('#termframe').src=termUrl();$('#termurl').textContent=termUrl().replace(/^https?:\/\//,'');$('#termnew').href=termUrl()}}
$('#openterm').onclick=openTerm;
$('#termclose').onclick=closeTerm;
$('#termmax').onclick=()=>{const w=$('#termwin');w.classList.toggle('max');$('#termmax').title=w.classList.contains('max')?'Restore':'Maximise';if(!w.classList.contains('max'))placeTerm()};
$('#termurl').onclick=galaTermClick;
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
// Files for a new question (paste, drop, choose files) are uploaded straight away, like an answer's, and an image
// (or a link for other files) goes into the question where the cursor is; posting attaches them to the question.
let qFiles=[];   // [{id, name, size, line}]
const QHINT='They\'re uploaded straight away and shown in the question where your cursor is; you can also paste a screenshot (Ctrl+V).';
function showQFiles(t){$('#qfilelist').innerHTML=t?esc(t):qFiles.length?qFiles.map((f,i)=>`${esc(f.name)} (${size(f.size)}) <a href="#" data-rmq="${i}">remove</a>`).join(' · '):QHINT}
async function addQFiles(list){
 if(!me()){alertBox('Choose who you are next to "I am" first.');return}
 const ta=$('#qbody'),failed=[];
 for(const [i,f] of list.entries()){
  if(f.size>maxMB*1048576){failed.push(`${f.name}: larger than the ${maxMB} MB limit`);continue}
  showQFiles(list.length>1?`Uploading ${i+1} of ${list.length}: ${f.name}…`:`Uploading ${f.name}…`);
  try{const d=await upload(f);noteDocs([{id:d.id,title:d.title,filename:d.filename||f.name,mime:d.mime||f.type}]);
   const name=f.name.replace(/[\[\]]/g,''),line=(/^image\//.test(f.type)?`![${name}](/api/docs/${d.id}/view)`:`[${name}](/api/docs/${d.id}/view)`);
   const a=ta.selectionStart??ta.value.length,b=ta.selectionEnd??a;
   ta.setRangeText((a>0&&ta.value[a-1]!=='\n'?'\n':'')+line+'\n',a,b,'end');draftSet('ask-body',ta.value);
   qFiles.push({id:d.id,name:f.name,size:f.size,line})}catch(err){failed.push(`${f.name}: ${err.message}`)}}
 showQFiles();ta.focus();
 if(failed.length)alertBox('Not uploaded: '+failed.join('; '))}
// the documents a new question shows or links to (also ones from a draft kept over a reload)
function qDocIds(){const ids=new Set(qFiles.map(f=>f.id));for(const m of $('#qbody').value.matchAll(/\/api\/docs\/(\d+)\/view/g))ids.add(+m[1]);return [...ids]}
$('#qfile').onchange=e=>{const fs=[...e.target.files];e.target.value='';if(fs.length)addQFiles(fs)};
$('#qfilelist').onclick=async e=>{if(!e.target.dataset.rmq)return;e.preventDefault();
 const f=qFiles.splice(+e.target.dataset.rmq,1)[0];if(!f)return;
 const ta=$('#qbody');ta.value=ta.value.replace(f.line+'\n','').replace(f.line,'');draftSet('ask-body',ta.value);showQFiles();
 api('/api/docs/'+f.id+'/archive',{archived:true}).catch(()=>{})};
const qdrop=$('#newbox');
['dragenter','dragover'].forEach(ev=>qdrop.addEventListener(ev,e=>{if(e.dataTransfer&&[...e.dataTransfer.types].includes('Files')){e.preventDefault();$('#qdrop').classList.add('over')}}));
['dragleave','drop'].forEach(ev=>qdrop.addEventListener(ev,()=>$('#qdrop').classList.remove('over')));
qdrop.addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files.length){e.preventDefault();qdrop.open=true;addQFiles([...e.dataTransfer.files])}});
async function postQuestion(to){
 const btns=[$('#post'),$('#postany')];btns.forEach(b=>b.disabled=true);
 try{const q=await api('/api/questions',{title:$('#title').value,body:$('#qbody').value,to,author:me(),documents:qDocIds()});
  const failed=[];
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
// ---- the board updating itself from GitHub (started with --self-update): an "update to x.y.z" button by the version
let UPD=null;
async function checkUpdate(force){try{UPD=await api('/api/board/update'+(force?'?check=1':''))}catch(e){return}renderUpd()}
function renderUpd(){if(typeof renderSetUpdate==='function')renderSetUpdate();const v=document.querySelector('header .ver');if(!v||!UPD||!UPD.enabled)return;let b=$('#updbtn');
 if(UPD.available){if(!b){b=document.createElement('button');b.id='updbtn';b.type='button';b.className='updbtn';v.after(b);b.onclick=openUpd}
  b.textContent='update to '+UPD.latest;b.title='A newer version of the board is on GitHub'}
 else if(b)b.remove()}
function updBox(html){let d=$('#updlg');if(!d){d=document.createElement('div');d.id='updlg';d.className='updlg';document.body.appendChild(d);
  d.onclick=e=>{if(e.target===d||e.target.closest('[data-updno]'))d.hidden=true}}
 d.innerHTML='<div class="updbox" role="dialog" aria-modal="true">'+html+'</div>';d.hidden=false;return d}
function openUpd(){const u=UPD||{};
 const notes=(u.notes||[]).length?'<ul>'+u.notes.map(n=>'<li>'+esc(n)+'</li>').join('')+'</ul>':'';
 const d=updBox(`<h3>Update the board to ${esc(u.latest||'')}?</h3><div>This board is ${esc(u.version||'')}. What's new:</div>${notes||'<div class="meta">(see the repository)</div>'}
  <div class="meta">The board downloads the new version from GitHub, checks it starts, and restarts with it, which takes a few seconds; the questions, documents and settings are kept, and the old version is kept as qaboard.py.prev.</div>
  ${u.writable===false?'<div class="bad">This board can\'t replace its own file (it is mounted read-only): redeploy it to update.</div>':''}
  <div class="btns"><button type="button" data-updno>Not now</button><button type="button" class="primary" id="updgo"${u.writable===false?' disabled':''}>Update now</button></div>`);
 $('#updgo').onclick=async()=>{$('#updgo').disabled=true;$('#updgo').textContent='Updating…';
  try{await api('/api/board/update'+(me()?'?who='+encodeURIComponent(me()):''),{})}catch(e){d.querySelector('.btns').insertAdjacentHTML('beforebegin','<div class="bad">'+esc(e.message)+'</div>');return}
  const from=u.version,t0=Date.now();
  const poll=async()=>{if(Date.now()-t0>120000){updBox('<h3>The update didn\'t finish</h3><div>The board hasn\'t come back with the new version after two minutes. Look at its log (docker logs).</div><div class="btns"><button type="button" data-updno>Close</button></div>');return}
   try{const r=await fetch(url('/health'),{cache:'no-store'});if(r.ok){const j=await r.json();if(j.version&&j.version!==from){location.reload();return}}
    const st=await api('/api/board/update');if(st.update&&st.update.state==='failed'){updBox('<h3>The board wasn\'t updated</h3><div class="bad">'+esc(st.update.msg||'')+'</div><div class="meta">It is still running '+esc(from)+'.</div><div class="btns"><button type="button" data-updno>Close</button></div>');return}}catch(e){}
   setTimeout(poll,2000)};setTimeout(poll,2000)}}
setTimeout(()=>checkUpdate(false),1500);setInterval(()=>{if(!document.hidden)checkUpdate(false)},3600*1000);

// ---- presence: this page checks in every 15 s with who you are, the tab and whether the terminal window is open,
// so the board (and Claude) can see who is watching the session
const PAGEID=(()=>{const r=()=>Math.random().toString(36).slice(2,12);try{let c=sessionStorage.getItem('qa-page');if(!c){c=r();sessionStorage.setItem('qa-page',c)}return c}catch(e){return r()}})();
let WATCH=null;
function presenceBody(gone){return {id:PAGEID,who:me()||'',view,term:!!$('#termwin')&&!$('#termwin').hidden,hidden:document.hidden,gone:!!gone}}
function presence(){api('/api/presence',presenceBody()).then(j=>{WATCH=j;renderWatch()}).catch(()=>{})}
function renderWatch(){const el=$('#watchers');if(!el||!WATCH)return;const ppl=(WATCH.people||[]).filter(p=>p.term||p.live);
 const bits=ppl.map(p=>'<b>'+esc(p.who)+'</b>'+(p.term?' (terminal)':''));
 if(WATCH.otherTerminals)bits.push(WATCH.otherTerminals+' other terminal'+(WATCH.otherTerminals===1?'':'s'));
 el.innerHTML=bits.length?'Watching: '+bits.join(', '):''}
setInterval(presence,15000);setTimeout(presence,800);
document.addEventListener('visibilitychange',presence);
addEventListener('pagehide',()=>{try{navigator.sendBeacon(url('/api/presence'),new Blob([JSON.stringify(presenceBody(true))],{type:'application/json'}))}catch(e){}});
$('#me').addEventListener('change',()=>setTimeout(presence,50));
for(const id of ['openterm','termclose'])$('#'+id).addEventListener('click',()=>setTimeout(presence,100));

// ---- Settings tab: branding (saved on the board) and updates
let SET=null;
async function loadSettings(){try{SET=await api('/api/settings')}catch(e){return}renderSettings();renderSetUpdate()}
function renderSettings(){if(!SET)return;const a=document.activeElement;
 const keep=a&&a.closest&&a.closest('#set-brand')&&$('#set-brand').dataset.dirty;   // don't overwrite what's being typed
 if(!keep){$('#set-name').value=SET.name;$('#set-name').placeholder=SET.defaults.name||'';
  $('#set-accent').value=SET.accent;$('#set-accent').placeholder=SET.defaults.accent||'';
  $('#set-accentpick').value=SET.accent||SET.defaults.accent||'#3949ab';$('#set-css').value=SET.css;delete $('#set-brand').dataset.dirty}
 $('#set-logoimg').hidden=!SET.logo;$('#set-logorm').hidden=!SET.logo;if(SET.logo)$('#set-logoimg').src='/brand-logo?t='+Date.now();
 if(SET.brandCss&&!$('#set-brand .brandnote'))$('#set-brand .meta').insertAdjacentHTML('afterend','<div class="meta brandnote">This board also has a brand folder (brand.css), which can change the look too.</div>')}
function setMsg(t,bad){const m=$('#set-msg');m.textContent=t;m.style.color=bad?'#e5484d':'';if(t&&!bad)setTimeout(()=>{if(m.textContent===t)m.textContent=''},4000)}
$('#set-brand').addEventListener('input',e=>{$('#set-brand').dataset.dirty='1';
 if(e.target.id==='set-accentpick')$('#set-accent').value=e.target.value;
 if(e.target.id==='set-accent'&&/^#[0-9a-fA-F]{6}$/.test(e.target.value))$('#set-accentpick').value=e.target.value;
 if(e.target.id==='set-accentpick'||e.target.id==='set-accent'){const v=$('#set-accent').value;if(/^#[0-9a-fA-F]{6}$/.test(v))document.documentElement.style.setProperty('--accent',v)}});
function afterBrand(){setTimeout(()=>location.reload(),600)}
$('#set-save').onclick=async()=>{const acc=$('#set-accent').value.trim();
 if(acc&&!/^#[0-9a-fA-F]{6}$/.test(acc))return setMsg('The accent colour must look like #cd2529.',true);
 try{SET=await api('/api/settings',{name:$('#set-name').value,accent:acc,css:$('#set-css').value});delete $('#set-brand').dataset.dirty;setMsg('Saved. Reloading to show it…');afterBrand()}
 catch(e){setMsg(e.message,true)}};
$('#set-reset').onclick=()=>{updBox(`<h3>Reset the branding?</h3><div>The name, colour, logo and CSS set here are cleared, and the board looks the way it was started (${esc(SET&&SET.defaults.name||'')}, ${esc(SET&&SET.defaults.accent||'')}).</div>
  <div class="btns"><button type="button" data-updno>Cancel</button><button type="button" class="primary" id="set-resetgo">Reset</button></div>`);
 $('#set-resetgo').onclick=async()=>{$('#updlg').hidden=true;
  try{SET=await api('/api/settings',{reset:['name','accent','css','logo']});setMsg('Reset. Reloading…');afterBrand()}catch(e){setMsg(e.message,true)}}};
$('#set-logofile').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;
 if(f.size>512*1024)return setMsg('The logo must be 512 KB or less.',true);
 try{const r=await fetch(url('/api/settings/logo'),{method:'POST',headers:{'Content-Type':f.type},body:f});const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.error||r.status);SET=j;setMsg('Logo saved. Reloading…');afterBrand()}catch(err){setMsg(err.message,true)}};
$('#set-logorm').onclick=async e=>{e.preventDefault();
 try{const r=await fetch(url('/api/settings/logo'),{method:'POST',headers:{'Content-Type':'image/png'},body:''});SET=await r.json();setMsg('Logo removed. Reloading…');afterBrand()}catch(err){setMsg(err.message,true)}};
function renderSetUpdate(){const u=UPD,box=$('#set-upinfo');if(!box)return;
 if(!u){box.textContent='Checking…';return}
 let h='This board is <b>v'+esc(u.version||'')+'</b>.';
 if(!u.enabled)h+=' It doesn\'t update itself (started without <code>--self-update</code>): update it the way it was installed, e.g. by deploying it again.';
 else{if(u.error)h+=' <span style="color:#e5484d">Couldn\'t reach GitHub: '+esc(u.error)+'</span>';
  else if(u.available)h+=' <b>Version '+esc(u.latest)+' is available.</b>'+((u.notes||[]).length?'<ul>'+u.notes.map(n=>'<li>'+esc(n)+'</li>').join('')+'</ul>':'');
  else if(u.latest)h+=' It is up to date (GitHub has v'+esc(u.latest)+').';
  if(u.checked)h+=' <span class="meta">Checked '+esc(ago(u.checked))+'.</span>';
  if(u.writable===false)h+='<div style="color:#e5484d">It can\'t replace its own file (mounted read-only): redeploy it to update.</div>'}
 box.innerHTML=h;$('#set-upgo').hidden=!(u.enabled&&u.available&&u.writable!==false);$('#set-check').hidden=!u.enabled}
$('#set-check').onclick=async()=>{$('#set-upinfo').textContent='Checking GitHub…';await checkUpdate(true);renderSetUpdate()};
$('#set-upgo').onclick=()=>openUpd();

// ---- deployments: a progress bar under the tabs while one is running (or just finished), full detail on Deploys
let DEPS=[],depTimer=null,focusDep=null;
// Finished deployments hidden from the bar under the tabs (per browser): {id: the report time that was hidden}.
// A newer report for the same deployment shows it again.
let HIDDEN={};try{HIDDEN=JSON.parse(store('qa-dephidden')||'{}')||{}}catch(e){HIDDEN={}}
function hideDep(d){HIDDEN[d.deployment]=d.received;const k=Object.keys(HIDDEN);
 if(k.length>50)for(const x of k.slice(0,k.length-50))delete HIDDEN[x];store('qa-dephidden',JSON.stringify(HIDDEN))}
function depTime(t){const d=t?new Date(t):null;return d&&!isNaN(d)?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):''}
// times: ISO strings (or epoch seconds) -> seconds; durations as 45s, 2m 05s, 1h 04m
function tsec(t){if(t==null||t==='')return null;const d=typeof t==='number'?new Date(t*1000):new Date(t);return isNaN(d)?null:d.getTime()/1000}
function fdur(s){s=Math.max(0,Math.round(s));if(s<60)return s+'s';const m=Math.floor(s/60);
 return m<60?m+'m '+String(s%60).padStart(2,'0')+'s':Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m'}
function stepDur(x,now){const a=tsec(x.started),b=tsec(x.finished);return a==null?null:(b!=null?b:(x.status==='running'?now:null))-a}
// elapsed time, and the time left: from the report's eta, else the steps' estimates, else (a guess) the
// average time of the steps done so far
function depTimes(d){const now=Date.now()/1000,st=(d.steps||[]).filter(x=>x&&typeof x==='object');
 const start=tsec(d.started),fin=d.status==='done'||d.status==='failed'?(tsec(d.finished)||d.received):null;
 const out={start,elapsed:start!=null?(fin||now)-start:null,finished:fin,left:null,eta:null,guess:false};
 if(fin)return out;
 const todo=st.filter(x=>x.status!=='done'&&x.status!=='failed');
 if(tsec(d.eta)!=null){out.eta=tsec(d.eta);out.left=out.eta-now;return out}
 if(todo.length&&todo.every(x=>typeof x.estimate==='number')){
  out.left=todo.reduce((n,x)=>n+Math.max(0,x.estimate-(x.status==='running'?(stepDur(x,now)||0):0)),0)}
 else{const durs=st.filter(x=>x.status==='done').map(x=>stepDur(x,now)).filter(v=>v!=null&&v>=0);
  if(durs.length&&todo.length){const avg=durs.reduce((a,b)=>a+b,0)/durs.length;out.guess=true;
   out.left=todo.reduce((n,x)=>n+(typeof x.estimate==='number'?x.estimate:avg)-(x.status==='running'?Math.min(stepDur(x,now)||0,typeof x.estimate==='number'?x.estimate:avg):0),0)}}
 if(out.left!=null)out.eta=now+out.left;
 return out}
function depLeft(t){if(t.left==null)return '';return t.left<0?'overdue by '+fdur(-t.left):(t.guess?'about ':'~')+fdur(t.left)+' left'}
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
  <span class="deppct">${p}%</span><span class="depstep">${esc((()=>{const t=depTimes(d),l=depLeft(t);return (depCur(d)||(d.status==='done'?'finished '+depTime(d.finished||d.updated)+(t.elapsed!=null?' · took '+fdur(t.elapsed):''):''))+(l?' · '+l:'')})())}</span>
  ${d.status==='done'||d.status==='failed'?`<span class="dstat ${depState(d)} hide" data-hide="${esc(d.deployment)}" title="Hide from the bar (still on the Deploys tab)">${esc(depLabel(d))} ✕</span>`
    :`<span class="dstat ${depState(d)}">${esc(depLabel(d))}</span>`}</button>`}).join('')}
const ICON={done:'✓',running:'●',failed:'✕',waiting:'○'};
function depCard(d){const p=depPct(d),st=(d.steps||[]).filter(x=>x&&typeof x==='object'),wf=d.waitingFor,er=d.error;
 const qs=(d.questions||[]).map(n=>`<a href="#" data-goq="${+n}">#${+n}</a>`).join(' ');
 return `<div class="card depcard s-${esc(d.status)}${wf?' needs':''}${d.deployment===focusDep?' focus':''}" id="dep-${esc(d.deployment)}">
 <h3>${esc(d.target||'Deployment')} <span class="dstat ${depState(d)}">${esc(depLabel(d))}</span><span class="meta" style="font-weight:400">${esc(d.deployment)}</span></h3>
 <div class="depmeta">${(()=>{const t=depTimes(d),l=depLeft(t);return (d.started?`<span>started ${esc(depTime(d.started))}</span>`:'')
   +(t.elapsed!=null?`<span>${t.finished?'took':'running for'} ${esc(fdur(t.elapsed))}</span>`:'')
   +(t.eta!=null?`<span class="depeta${t.left<0?' late':''}" title="${esc(t.guess?'A guess from the average time of the steps done so far':d.eta?'The ETA in the report':'From the steps\' estimates')}">ETA ${esc(depTime(t.eta*1000))} · ${esc(l)}</span>`:'')})()}<span>updated ${ago(d.received)}</span>
  ${d.commit?`<span>commit <code>${esc(d.commit)}</code></span>`:''}${(d.branches||[]).length?`<span>${(d.branches||[]).map(b=>`<code>${esc(b)}</code>`).join(' ')}</span>`:''}
  ${qs?`<span>questions ${qs}</span>`:''}</div>
 <div class="depbar"><i style="width:${p}%"></i></div>
 <ol class="depsteps">${st.map(x=>{const s=['done','running','failed','waiting'].includes(x.status)?x.status:'waiting';
  const sg=(x.stages||[]).filter(g=>g&&typeof g==='object');
  return `<li class="${s}"><span class="ic">${ICON[s]}</span><span>${esc(x.label||x.id||'')}${x.detail?` <span class="meta">· ${linkDocs(esc(String(x.detail)))}</span>`:''}</span>
   <span class="when">${esc((()=>{const now=Date.now()/1000,du=stepDur(x,now),est=typeof x.estimate==='number'?x.estimate:null;
     if(s==='done'||s==='failed')return (x.finished?depTime(x.finished):'')+(du!=null?' · '+fdur(du):'')+(est!=null&&du!=null?' (est. '+fdur(est)+')':'');
     if(s==='running')return (x.started?'since '+depTime(x.started):'')+(du!=null?' · '+fdur(du):'')+(est!=null?' of ~'+fdur(est):'');
     return est!=null?'~'+fdur(est):''})())}</span>
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
//  - in the Ask form: it's uploaded straight away and ![…](/api/docs/ID/view) is put in the question, so it
//    shows there; posting attaches it to the question
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
    s.add_argument("--self-update", action="store_true", help="let the board update itself from GitHub (an Update "
                   "button when there's a newer version; qaboard.py must be writable and something must restart "
                   "the board when it exits, e.g. docker --restart unless-stopped)")
    s.add_argument("--update-url", default=None, help="where the newest qaboard.py is (default: the GitHub repository)")
    s.add_argument("--db", default="qaboard.db")
    s.add_argument("--key", default=None, help="optional shared secret; clients add ?key=... or X-QA-Key")
    s.add_argument("--max-doc-mb", type=float, default=20, help="largest document accepted (default 20)")
    s.add_argument("--name", default="CGA Gala Audit", help='board name shown on the page, e.g. "Acme Web App"')
    s.add_argument("--accent", default="#cd2529", help="accent colour, e.g. #7a4cab")
    s.add_argument("--gala-data", default=None, help="the gala's data folder (sessions/, inbox/, state.json)")
    s.add_argument("--inbox", default=None, help="folder new gala files arrive in (default: DATA/inbox)")
    s.add_argument("--brand-dir", default=None, help="folder with brand.css, logo and fonts")
    s.add_argument("--term-rw-port", type=int, default=None, help="ttyd (on 127.0.0.1) to type to Claude: served at /terminal/rw/")
    s.add_argument("--term-ro-port", type=int, default=None, help="ttyd (on 127.0.0.1) to watch Claude: served at /terminal/ro/")
    s.add_argument("--admin-key", default="", help="key that lets a browser type in the web terminal")
    s.add_argument("--terminal-url", default="", help="where browsers reach this board's Claude Code web terminal, "
                                                      "e.g. http://127.0.0.1:17681 (the SSH tunnel)")
    b = sub.add_parser("bridge", help="stdio<->HTTP MCP bridge for Claude Desktop")
    b.add_argument("url", help='e.g. "http://server.example:8787/mcp?who=alice-claude&key=KEY"')
    args = p.parse_args()
    serve(args) if args.cmd == "serve" else bridge(args)


if __name__ == "__main__":
    main()
