
    def _gala(self):
        return getattr(self.server, "gala", None)

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
                row = g.ask(str(d.get("key") or ""), who, force=True)
                if not row:
                    return self._json(409, {"error": "nothing to ask yet for that (its files aren't in)"})
                return self._json(200, {"ok": True, "question": row["id"]})
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