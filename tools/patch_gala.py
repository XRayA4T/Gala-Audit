#!/usr/bin/env python3
"""Builds the Gala Audit Portal (portal/portal.py) from the QA Board (qaboard.py) and the pieces in src/.

    python3 patch_gala.py path/to/qaboard.py portal/portal.py
"""
import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
VERSION = "0.5.0"
s = open(sys.argv[1], encoding="utf-8").read()
piece = lambda name: open(os.path.join(SRC, name), encoding="utf-8").read()  # noqa: E731


def sub(old, new):
    global s
    if old not in s:
        sys.exit("patch anchor not found: %r" % old[:80])
    s = s.replace(old, new, 1)


# ---------------------------------------------------------------- header, version, imports
sub('"""\nQA Board - a tiny shared question & answer board',
    '"""\nGala Audit Portal - the QA Board with Gala and Checklist tabs, for auditing a swimming gala while it runs\n'
    'with a local Claude Code and the gala-ocr plugin. Files arrive in an inbox folder and are sorted by session;\n'
    'each event is handed to Claude as soon as its lane sheets and Gen7 races are in. Based on the QA Board:\n\n'
    'QA Board - a tiny shared question & answer board')
s = re.sub(r'^VERSION = "[^"]+"', 'VERSION = "%s"' % VERSION, s, count=1, flags=re.M)
sub("import sqlite3\n", "import shutil\nimport sqlite3\nimport subprocess\n")

# ---------------------------------------------------------------- back end
sub("\n\ndef clean_deployment(", piece("backend.py") + "\n\ndef clean_deployment(")
sub("\n    def do_GET(self):", "\n" + piece("handler.py").rstrip("\n").rsplit("\n    def do_GET(self):", 1)[0]
    + "\n\n    def do_GET(self):")
sub('''            if path == "/health":
                return self._json(200, {"ok": True, "version": VERSION})''',
    '''            if path == "/health":
                return self._json(200, {"ok": True, "version": VERSION, "gala": bool(self._gala())})
            if path == "/api/gala/overview":
                g = self._gala()
                return self._json(200, g.overview() if g else {"root": None})
            if path == "/api/gala/codes":
                g = self._gala()
                return self._json(200, g.code_list(int(q.get("session") or 1)) if g else {"codes": []})
            if path == "/api/gala/file":
                return self._gala_file(q)''')
sub('''        store = self.server.store
        try:
            if path == "/api/live":''', '''        store = self.server.store
        try:
            if path == "/api/gala/upload":
                return self._gala_upload(q)
            if path in ("/api/gala/tick", "/api/gala/auto", "/api/gala/ask", "/api/gala/move", "/api/gala/session", "/api/gala/fix"):
                return self._gala_post(path, q)
            if path == "/api/live":''')
# quieter log: the page and the watcher poll every few seconds
sub('''    def log_message(self, fmt, *args):
        sys.stderr.write''', '''    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith(("2", "3")) and str(args[0]).startswith("GET "):
            return
        sys.stderr.write''')
# brand folder
sub('''    def _brand_dir(self):
        return os.path.join(os.path.dirname(self.server.store.docdir), "brand")''',
    '''    def _brand_dir(self):
        return getattr(self.server, "brand_dir", None) or os.path.join(os.path.dirname(self.server.store.docdir), "brand")''')

# ---------------------------------------------------------------- serve() and the command line
sub("    srv.page = render_page(srv.brand, srv.accent, args.terminal_url)",
    "    srv.brand_dir = os.path.abspath(args.brand_dir) if args.brand_dir else None\n"
    "    srv.gala = Gala(args.gala_data, args.inbox, srv.store) if args.gala_data else None\n"
    "    if srv.gala:\n"
    "        threading.Thread(target=srv.gala.run, daemon=True).start()\n"
    "        sys.stderr.write('Gala data: %s, inbox: %s\\n' % (srv.gala.root, srv.gala.inbox))\n"
    "    srv.page = render_page(srv.brand, srv.accent, args.terminal_url)")
sub('''    s.add_argument("--terminal-url", default="",''',
    '''    s.add_argument("--gala-data", default=None, help="the gala's data folder (sessions/, inbox/, state.json)")
    s.add_argument("--inbox", default=None, help="folder new gala files arrive in (default: DATA/inbox)")
    s.add_argument("--brand-dir", default=None, help="folder with brand.css, logo and fonts")
    s.add_argument("--terminal-url", default="",''')
sub('s.add_argument("--name", default="Team"', 's.add_argument("--name", default="CGA Gala Audit"')
sub('s.add_argument("--accent", default="#3949ab"', 's.add_argument("--accent", default="#cd2529"')

# ---------------------------------------------------------------- page
sub("<title>{BRAND_HTML} · Q&amp;A Board</title>", "<title>{BRAND_HTML} · Gala Audit Portal</title>")
sub("BRAND+' · Q&A Board'", "BRAND+' · Gala Audit'")
sub('<span>Q&amp;A Board <small class="ver">', '<span>Gala Audit Portal <small class="ver">')
sub('<nav class="views" id="views"><button data-v="questions">Questions</button>',
    '<nav class="views" id="views"><button data-v="gala">Gala</button><button data-v="checklist">Checklist</button>'
    '<button data-v="questions">Questions</button>')
sub('<section id="v-questions">', piece("section.html") + '<section id="v-questions">')
sub("\n.drop{border:2px dashed", "\n" + piece("gala.css") + "\n.drop{border:2px dashed")
sub("let view=['#docs','#live','#users','#deploys'].includes(location.hash)?location.hash.slice(1):(store('qa-view')||'questions');",
    "let view=['#gala','#checklist','#questions','#docs','#live','#users','#deploys'].includes(location.hash)?location.hash.slice(1):(store('qa-view')||'gala');\n"
    "// ?me=NAME (from start-portal.sh) picks \"I am\" the first time\n"
    "{const m=new URLSearchParams(location.search).get('me');if(m&&!store('qa-me'))store('qa-me',m)}")
sub("$('#v-questions').hidden=v!=='questions';",
    "$('#v-gala').hidden=v!=='gala';$('#v-checklist').hidden=v!=='checklist';$('#v-questions').hidden=v!=='questions';")
sub("function refresh(){loadUsers();view==='docs'?",
    "function refresh(){loadUsers();(view==='gala'||view==='checklist')?loadGala():view==='docs'?")
sub("function defaultTo(){const c=USERS.find(u=>u.name==='dev-claude')||",
    "function defaultTo(){const c=USERS.find(u=>u.name==='gala-claude')||USERS.find(u=>u.name==='dev-claude')||")
sub("\n// ---- views\n", piece("gala.js") + "\n// ---- views\n")
# links to data files in posts open with the viewer's key
sub("""function docUrl(u,kind){ // a link to one of this board's documents (relative, or written for any host/port)
""", """function docUrl(u,kind){ // a link to one of this board's documents (relative, or written for any host/port)
 const g=String(u).replace(/&amp;/g,'&').match(/^(?:https?:\\/\\/[^\\/\\s]+)?\\/api\\/gala\\/file\\?(\\S*)$/);   // a file in data/
 if(g){const p=new URLSearchParams(g[1]).get('path');if(p)return galaHref(p,kind==='download')}
""")
sub("(?:https?:\\/\\/|\\/api\\/docs\\/)[^\\s)]+)", "(?:https?:\\/\\/|\\/api\\/docs\\/|\\/api\\/gala\\/)[^\\s)]+)")

open(sys.argv[2], "w", encoding="utf-8").write(s)
print("wrote %s (v%s, %d bytes)" % (sys.argv[2], VERSION, len(s.encode())))
