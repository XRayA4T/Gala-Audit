#!/usr/bin/env python3
"""
board-watch.py - wake a Claude Code session when the QA board has something for it.

Runs on the server next to the claude-box web terminal. Every POLL seconds it asks the board
(no tokens used) for:
  - open questions addressed to WHO (or to anyone) that WHO didn't write
  - new replies from others on threads WHO is part of (addressed to it, started by it, or answered in)
When there is something new and the Claude session in tmux has been idle for IDLE seconds
(its input box unchanged, Claude not working, and - unless prompt_check is off - no permission or
question prompt on screen), it types one short prompt into the session.
Each question/answer is announced once; the list of what was announced is kept in state.json.

Config comes from config.json next to this file:
  {"board": "http://127.0.0.1:8787", "key": "...", "who": "dev-claude",
   "tmux_socket": "claudebox", "tmux_session": "claude-box", "poll": 15, "idle": 20}

  board-watch.py            run forever
  board-watch.py --check    show what would be announced now, change nothing
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {"board": "http://127.0.0.1:8787", "key": "", "who": "dev-claude",
       "tmux_socket": "claudebox", "tmux_session": "claude-box", "poll": 15, "idle": 20,
       "prompt_check": True}
try:
    with open(os.path.join(HERE, "config.json")) as f:
        CFG.update(json.load(f))
except FileNotFoundError:
    pass
STATE_FILE = os.path.join(HERE, "state.json")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def board(path, **params):
    if CFG["key"]:
        params["key"] = CFG["key"]
    url = CFG["board"].rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    with OPENER.open(url, timeout=10) as r:
        return json.loads(r.read().decode())


# Sign-in prompts the watcher recognises on Claude's screen, and the lines that mean they were completed.
# Device sign-in pages (they work from any browser): AWS SSO device code flow and GitHub's device login.
SIGNIN_URL = re.compile(r"https://(?:device\.sso\.[a-z0-9-]+\.amazonaws\.com|oidc\.[a-z0-9-]+\.amazonaws\.com/device"
                        r"|github\.com/login/device)[^\s'\"<>)]*", re.I)
SIGNIN_CODE = re.compile(r"(?:code|user_code=)[:=\s]*\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
SIGNIN_DONE = ("successfully logged into start url", "logged in as", "authentication complete",
               "successfully signed in", "login succeeded")


def attention_from_screen(vis, att, st):
    """Raise, keep or clear the needs-attention signal from what's on screen. Returns the signal still active."""
    low = vis.lower()
    working = "esc to interrupt" in low
    done_at = max((low.rfind(m) for m in SIGNIN_DONE), default=-1)
    def on_done_line(m):
        line = vis[vis.rfind("\n", 0, m.start()) + 1:vis.find("\n", m.end()) if vis.find("\n", m.end()) >= 0 else len(vis)]
        return any(d in line.lower() for d in SIGNIN_DONE)
    found = [m for m in SIGNIN_URL.finditer(vis) if not on_done_line(m)]
    url_m = found[-1] if found else None
    # Only a real sign-in prompt counts: the link AND a one-time code printed next to it (as gh and
    # aws sso print them). The link alone (e.g. Claude quoting its instructions) is not a sign-in.
    key = code_m = None
    if url_m:
        code_m = SIGNIN_CODE.search(vis[max(0, url_m.start() - 400):url_m.end() + 400])
        if code_m:
            key = url_m.group(0).rstrip(".,;:") + "|" + code_m.group(1)
    # Someone dismissed the alert we raised (Mark as handled) while it's still on screen: leave it dismissed.
    if st.get("raised") and not att:
        st["done_urls"].add(st["raised"])
        st["raised"] = None
    # 1. A sign-in completed after the last sign-in link on screen: clear any sign-in alert.
    if att and done_at >= 0 and (url_m is None or done_at > url_m.start()) and (att.get("url") or att.get("by") == "watcher"
                                                                                 or "sign" in att.get("reason", "").lower()
                                                                                 or "login" in att.get("reason", "").lower()):
        board_post("/api/attention", {"clear": True, "by": "watcher"})
        log("cleared the needs-attention signal (sign-in completed): %s" % att.get("reason"))
        for k in (st.get("raised"), key):
            if k:
                st["done_urls"].add(k)
        st["raised"] = None
        return None
    # 2. A sign-in prompt waiting on screen (and not completed after it): raise it with the link and code.
    if key and not (done_at > url_m.start()) and key not in st["done_urls"]:
        url = key.split("|")[0]
        if not att or (not att.get("url") and att.get("by") != "watcher") or att.get("url") != url:
            what = "GitHub" if "github.com" in url else "AWS SSO"
            payload = {"reason": "%s sign-in waiting: open the link and approve with the code" % what,
                       "url": url, "code": code_m.group(1), "by": "watcher"}
            if att and att.get("reason") and att.get("by") != "watcher":
                payload["reason"] = att["reason"]          # keep Claude's wording, add the link
            try:
                board_post("/api/attention", payload)
                log("needs-attention from the screen: %s %s" % (payload["reason"], url))
                att = dict(payload, ts=time.time())
                st["raised"] = key
            except Exception as e:
                log("couldn't raise the sign-in alert: %s" % e)
    if not att:
        st.update(ts=None, idle_seen=False)
        return None
    # 3. A go-ahead or answer: once Claude has been seen waiting (not working) and then works again, the
    #    person has responded. (Claude often raises the alert mid-reply, so working alone isn't enough.)
    if st["ts"] != att.get("ts"):
        st.update(ts=att.get("ts"), idle_seen=False)
    if not working:
        st["idle_seen"] = True
    elif st["idle_seen"] and not url_m:
        board_post("/api/attention", {"clear": True, "by": "watcher"})
        log("cleared the needs-attention signal (Claude is working again): %s" % att.get("reason"))
        st.update(ts=None, idle_seen=False)
        return None
    return att


def board_post(path, payload):
    params = {"key": CFG["key"]} if CFG["key"] else {}
    url = CFG["board"].rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with OPENER.open(req, timeout=10) as r:
        return r.status


def live_loop():
    """Send the console (screen plus recent scrollback, with colours) to the board's Live view."""
    interval = float(CFG.get("live_interval", 2))
    lines = int(CFG.get("live_lines", 300))
    last_hash, last_sent, failing = None, 0.0, False
    while True:
        try:
            r = tmux("capture-pane", "-p", "-e", "-J", "-S", "-%d" % lines, "-t", CFG["tmux_session"])
            if r.returncode == 0:
                text = r.stdout.rstrip()
                h = hashlib.sha1(text.encode()).hexdigest()
                if h != last_hash or time.time() - last_sent > 30:
                    visible = (screen() or "").lower()
                    cl = tmux("list-clients", "-t", CFG["tmux_session"])   # terminals attached (web or ssh)
                    clients = len([x for x in cl.stdout.splitlines() if x.strip()]) if cl.returncode == 0 else None
                    board_post("/api/live", {"text": text, "busy": "esc to interrupt" in visible,
                                             "prompt": any(m in visible for m in PROMPT_MARKERS),
                                             "title": CFG.get("title", ""), "clients": clients})
                    last_hash, last_sent = h, time.time()
                    if failing:
                        log("live view: sending again")
                        failing = False
        except Exception as e:
            if not failing:
                log("live view: waiting (%s)" % e)
                failing = True
        time.sleep(interval)


def pending():
    """{event_key: description} of everything WHO should hear about:
    - a new open question addressed to WHO (or anyone) that WHO didn't write
    - a new reply from someone else on a thread WHO is part of: addressed to WHO, started by WHO,
      or one WHO has answered in (e.g. a question to "anyone" that WHO took on). Follow-ups like
      "write the design" arrive as replies on the same question."""
    who = CFG["who"]
    out = {}
    for q in board("/api/questions", status="unresolved", limit=200)["questions"]:
        title = q["title"] if len(q["title"]) <= 70 else q["title"][:67] + "..."
        if q["status"] == "open" and not q["answers"] and q["to"] in (who, "anyone") and q["author"] != who:
            out["q%d" % q["id"]] = "question #%d from %s (\"%s\")" % (q["id"], q["author"], title)
            continue
        involved = q["to"] == who or q["author"] == who or any(a["author"] == who for a in q["answers"])
        if involved and q["answers"] and q["answers"][-1]["author"] != who:
            last = q["answers"][-1]
            body = " ".join((last.get("body") or "").split())
            excerpt = body if len(body) <= 160 else body[:157] + "..."
            out["a%d-%d" % (q["id"], last["id"])] = "%s replied on question #%d (\"%s\"): \"%s\"" % (
                last["author"], q["id"], title, excerpt)
    return out


BORDER_CHARS = set("\u2500\u2501\u2550\u256d\u256e\u256f\u2570\u250c\u2510\u2514\u2518\u2502\u2503 -")
PROMPT_STARTS = (">", "\u276f")


def is_border(line):
    """A box edge or rule line, even with a label in it such as '──── fff-dev ─'."""
    t = line.strip()
    if len(t) < 10:
        return False
    rule = sum(1 for c in t if c in "\u2500\u2501\u2550")
    return t[0] in "\u2500\u2501\u2550\u256d\u2570\u250c\u2514" and rule >= 0.6 * len(t)


def prompt_area(text):
    """The input box at the bottom of Claude Code's screen: from the last line that starts with
    '>' (inside or outside a box frame) out to the box's borders. None if it can't be found."""
    lines = text.rstrip("\n").split("\n")
    for i in range(len(lines) - 1, max(-1, len(lines) - 25), -1):
        t = lines[i].strip().lstrip("\u2502\u2503|").strip()
        if t.startswith(PROMPT_STARTS):
            top = i
            while top > 0 and not is_border(lines[top - 1]) and i - top < 10:
                top -= 1
            bottom = i
            while bottom < len(lines) - 1 and not is_border(lines[bottom + 1]) and bottom - i < 10:
                bottom += 1
            return "\n".join(lines[top:bottom + 1])
    return None


SGR = re.compile(r"\x1b\[([0-9;]*)m")
ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _is_grey(params):
    """Does this colour code make text dim or grey (Claude Code's suggestion/placeholder style)?"""
    p = [int(x) if x.isdigit() else 0 for x in params.split(";")] if params else [0]
    state = None
    i = 0
    while i < len(p):
        c = p[i]
        if c in (0, 22, 39):
            state = False
        elif c in (2, 90):
            state = True
        elif c == 38 and i + 2 < len(p) and p[i + 1] == 5:
            n = p[i + 2]
            state = n == 8 or 237 <= n <= 250
            i += 2
        elif c == 38 and i + 4 < len(p) and p[i + 1] == 2:
            r, g, b = p[i + 2:i + 5]
            state = max(r, g, b) - min(r, g, b) <= 24 and 80 <= r <= 175
            i += 4
        elif 30 <= c <= 37 or 91 <= c <= 97:
            state = False
        i += 1
    return state


def typed_text(ansi_line):
    """The text on the prompt line that isn't dim/grey, i.e. what a person actually typed."""
    out, grey, pos = [], False, 0
    for m in SGR.finditer(ansi_line):
        if not grey:
            out.append(ansi_line[pos:m.start()])
        g = _is_grey(m.group(1))
        if g is not None:
            grey = g
        pos = m.end()
    if not grey:
        out.append(ansi_line[pos:])
    t = ESC.sub("", "".join(out)).strip().lstrip("\u2502\u2503|").strip()
    for mark in PROMPT_STARTS:
        if t.startswith(mark):
            t = t[len(mark):]
            break
    return t.strip().rstrip("\u2502\u2503|").strip()


def draft(plain):
    """Typed (not suggested) text waiting in the input box, '' if empty, None if it can't tell."""
    r = tmux("capture-pane", "-p", "-e", "-t", CFG["tmux_session"])
    if r.returncode != 0:
        return None
    plain_lines = plain.rstrip("\n").split("\n")
    ansi_lines = r.stdout.rstrip("\n").split("\n")
    for i in range(len(plain_lines) - 1, max(-1, len(plain_lines) - 25), -1):
        t = plain_lines[i].strip().lstrip("\u2502\u2503|").strip()
        if t.startswith(PROMPT_STARTS):
            return typed_text(ansi_lines[i]) if i < len(ansi_lines) else None
    return None


def tmux(*args):
    return subprocess.run(["tmux", "-L", CFG["tmux_socket"]] + list(args),
                          capture_output=True, text=True)


def screen():
    r = tmux("capture-pane", "-p", "-t", CFG["tmux_session"])
    return r.stdout if r.returncode == 0 else None


# Claude is working: never type then.
WORKING_MARKERS = ("esc to interrupt",)
# A question or permission prompt is on screen: Enter would answer it. Skipped when the config has
# "prompt_check": false.
PROMPT_MARKERS = ("do you want to", "\u276f 1.", "> 1. yes", "(y/n)", "[y/n]", "press enter")


# A permission prompt (approve a command / edit): only a person in the terminal should answer it.
PERMISSION_MARKERS = ("do you want to", "(y/n)", "[y/n]")
# The highlighted first option of a choice menu, e.g. Claude asking the user a question.
MENU_LINE = re.compile(r"^\s*[\u2502\u2503|]?\s*[\u276f>]\s*1\.\s", re.M)


def working(text):
    return any(m in text.lower() for m in WORKING_MARKERS)


def permission_prompt(text):
    """A permission or yes/no prompt at the bottom of the screen."""
    lines = text.rstrip("\n").split("\n")
    tail, last = "\n".join(lines[-30:]), "\n".join(lines[-5:]).lower()
    return (bool(MENU_LINE.search(tail)) and "do you want to" in tail.lower()) or "(y/n)" in last or "[y/n]" in last


def question_menu(text):
    """Claude is waiting on its own multiple-choice question (not a permission prompt). A board reply
    can answer it: the watcher closes the menu with Esc and sends the reply as the next prompt."""
    tail = "\n".join(text.rstrip("\n").split("\n")[-30:])
    return bool(MENU_LINE.search(tail)) and not any(m in tail.lower() for m in PERMISSION_MARKERS)


def busy(text):
    low = text.lower()
    markers = WORKING_MARKERS + (PROMPT_MARKERS if CFG.get("prompt_check", True) else ())
    return any(m in low for m in markers)


def zombie_count():
    """Dead (zombie) processes in Claude's container, None if it can't tell. The container is found by
    its name prefix (claude-box names it claude-<project folder>-<pid>)."""
    prefix = CFG.get("container_prefix")
    if not prefix:
        return None
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    names = [n for n in r.stdout.split() if n.startswith(prefix)] if r.returncode == 0 else []
    if not names:
        return None
    r = subprocess.run(["docker", "top", names[0], "-eo", "stat"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return sum(1 for line in r.stdout.splitlines()[1:] if line.strip().startswith("Z"))


def restart_claude():
    """Quit Claude Code with /exit. The web terminal's claude-loop starts it again a few seconds later in a
    fresh container, continuing the conversation and telling Claude to redo its start-up tasks."""
    target = CFG["tmux_session"]
    tmux("send-keys", "-t", target, "-l", "/exit")
    time.sleep(0.8)
    tmux("send-keys", "-t", target, "Enter")


def announce(items):
    n = len(items)
    text = ("[QA board] New for you: %s. Read %s with the qaboard tools (get_question) and act on %s. "
            "If you were waiting for someone (a deploy, a decision, feedback), this may be their answer "
            "or may change what should be done: follow it rather than what you were waiting for. "
            "Otherwise carry on with what you were doing." % (
                "; ".join(items), "it" if n == 1 else "them", "it" if n == 1 else "them"))
    target = CFG["tmux_session"]
    tmux("send-keys", "-t", target, "-l", text)
    time.sleep(0.3)
    tmux("send-keys", "-t", target, "Enter")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(seen):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(seen), f)
    os.replace(tmp, STATE_FILE)


def main():
    if "--check" in sys.argv:
        seen = load_state()
        p = pending()
        new = {k: v for k, v in p.items() if k not in seen}
        print("Would announce:" if new else "Nothing new to announce.")
        for v in new.values():
            print("  -", v)
        s = screen()
        print("Claude session:", "not found" if s is None else
              ("busy (working or waiting on a prompt)" if busy(s) else "found, idle"))
        if s is not None:
            area = prompt_area(s)
            print("Input box the idle check watches:" if area else
                  "Input box not found - the idle check uses the whole screen instead.")
            if area:
                print("\n".join("  | " + l for l in area.split("\n")))
            d = draft(s)
            print("Typed text waiting in the input box:",
                  "can't tell" if d is None else (repr(d) + " (the watcher waits)" if d else "none (grey suggestions ignored)"))
        return
    seen = load_state()
    if CFG.get("live"):
        import threading
        threading.Thread(target=live_loop, daemon=True).start()
        log("live view: on (every %ss)" % CFG.get("live_interval", 2))
    last_hash, last_change = None, time.time()
    waiting_note = None
    att_state = {"ts": None, "idle_seen": False, "done_urls": set(), "raised": None}
    zombie_checked, restarted_at = 0.0, 0.0
    if CFG.get("restart_zombies"):
        log("restart on zombies: when idle %d min with %d+ dead processes in containers named %s*" % (
            CFG.get("restart_idle", 600) // 60, CFG["restart_zombies"], CFG.get("container_prefix")))
    log("watching %s for %s (poll %ss, idle %ss, prompt check %s)" % (
        CFG["board"], CFG["who"], CFG["poll"], CFG["idle"], "on" if CFG.get("prompt_check", True) else "off"))
    while True:
        try:
            # Someone is needed in the session: Claude called needs_attention, a page set it, or a sign-in
            # prompt is on screen. Never type then - the session would take the line as the person's answer.
            att = None
            try:
                att = board("/api/live", lines=0).get("attention")
            except Exception:
                pass
            vis = screen()
            if vis is not None:
                att = attention_from_screen(vis, att, att_state)
            # A sign-in prompt is on screen: anything typed would go into it, so hold. Any other
            # "needs you" (Claude waiting for a deploy, a decision, feedback) doesn't hold board replies:
            # a reply on the board is often exactly the answer it's waiting for.
            if att and att.get("url") and vis is not None and att["url"].split("?")[0] in vis:
                if waiting_note != "attention":
                    log("holding announcements: sign-in prompt on screen (%s)" % att.get("reason"))
                    waiting_note = "attention"
                time.sleep(CFG["poll"])
                continue
            if waiting_note == "attention":
                waiting_note = None
            p = pending()
            seen &= set(p) | {k for k in seen if k.startswith("a")}   # forget closed questions
            new = {k: v for k, v in p.items() if k not in seen}
            s = screen()
            if s is not None:
                # Idle = the input box hasn't changed (falls back to the whole screen if not found)
                area = prompt_area(s)
                h = hashlib.sha1((area if area is not None else s).encode()).hexdigest()
                if h != last_hash:
                    last_hash, last_change = h, time.time()
            if new and s is not None and not working(s) and question_menu(s) \
                    and time.time() - last_change >= CFG["idle"]:
                # Claude asked a question in the terminal and is waiting on the menu. The board reply
                # may be the answer (or change the plan): close the menu (Esc) and send it as a prompt.
                tmux("send-keys", "-t", CFG["tmux_session"], "Escape")
                time.sleep(2)
                s = screen()
                if s is None or working(s) or question_menu(s) or prompt_area(s) is None:
                    if waiting_note != "menu":
                        log("holding: couldn't close the question menu on screen")
                        waiting_note = "menu"
                else:
                    log("closed Claude's question menu to deliver a board reply")
                    last_change = 0      # the input box is empty and Claude is waiting: deliver now
            if new and s is not None and permission_prompt(s):
                if waiting_note != "permission":
                    log("holding: a permission prompt is on screen (a person answers it in the terminal)")
                    waiting_note = "permission"
                new = {}
            if new and s is not None:
                idle_for = time.time() - last_change
                d = draft(s) if not busy(s) and idle_for >= CFG["idle"] else None
                if d:
                    if waiting_note != d:
                        log("holding: typed text waiting in the input box (%r)" % d[:60])
                        waiting_note = d
                elif not busy(s) and idle_for >= CFG["idle"]:
                    waiting_note = None
                    announce(list(new.values()))
                    seen |= set(new)
                    save_state(seen)
                    log("announced: " + " | ".join(new.values()))
                    last_change = time.time()
            # Dead processes piling up in Claude's container (e.g. Chrome runs without an init process):
            # once Claude has been idle long enough, quit it so claude-loop restarts it in a clean container.
            limit = CFG.get("restart_zombies") or 0
            if limit and s is not None and not new and not att and time.time() - zombie_checked >= 60 \
                    and time.time() - restarted_at >= 900:
                idle_for = time.time() - last_change
                if idle_for >= CFG.get("restart_idle", 600) and not busy(s) and not question_menu(s) \
                        and not permission_prompt(s) and draft(s) == "":
                    zombie_checked = time.time()
                    z = zombie_count()
                    if z is not None and z >= limit:
                        log("restarting Claude: idle %d min and %d dead processes in its container" % (idle_for // 60, z))
                        restart_claude()
                        restarted_at = time.time()
                        last_change = time.time()
        except Exception as e:  # board restarting, tmux gone, etc. - try again next round
            log("waiting: %s" % e)
        time.sleep(CFG["poll"])


if __name__ == "__main__":
    main()
