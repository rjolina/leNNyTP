#!/usr/bin/env python3
"""
lemmy_nntp.py – NNTP server bridging to Lemmy with local spool.

Architecture:
  - Background poller fetches communities/posts/comments -> disk spool
  - NNTP server reads exclusively from spool (zero API calls on read)
  - Only POST and AUTH hit the Lemmy API directly

Usage:
    python3 lemmy_nntp.py --instance https://lemmy.ml [options]

Options:
  --instance URL        Lemmy instance URL (required)
  --host ADDR           Bind address (default: 127.0.0.1)
  --port PORT           NNTP port (default: 1190)
  --spool DIR           Spool directory for local article storage (default: ./spool)
  --interval SECS       Background poll interval in seconds (default: 900)
  --listing-type TYPE   Community listing type: Local, All, or Subscribed (default: Local)
  --username USER       Lemmy username for authenticated polling (optional)
  --password PASS       Lemmy password for authenticated polling (optional)
  --no-auth             Allow reading articles without authentication (default: auth required)
  --ssl-cert FILE       Path to SSL certificate (PEM) for NNTPS (optional)
  --ssl-key FILE        Path to SSL private key for NNTPS (optional)
  --debug               Enable debug logging

Examples:
  # Plain NNTP on localhost (for use behind reverse proxy):
  python3 lemmy_nntp.py --instance https://lemmy.ml --port 1190

  # NNTPS directly exposed to the internet on port 563:
  python3 lemmy_nntp.py --instance https://lemmy.ml --host 0.0.0.0 --port 563 \\
      --ssl-cert /etc/ssl/certs/example.pem --ssl-key /etc/ssl/certs/example.key
"""

import argparse, asyncio, functools, json, logging, os, re, ssl, textwrap, threading, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lemmy-nntp")


# ===== Lemmy API client =====================================================

class LemmyAPI:
    def __init__(self, instance_url: str):
        self.base = instance_url.rstrip("/")
        self.domain = urllib.parse.urlparse(self.base).hostname

    def _req(self, method, path, data=None, token=None):
        url = f"{self.base}/api/v3{path}"
        headers = {"Content-Type": "application/json", "User-Agent": "lemmy-nntp/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if method == "GET":
            if data:
                clean = {}
                for k, v in data.items():
                    if v is None: continue
                    if isinstance(v, bool): v = str(v).lower()
                    clean[k] = v
                url += "?" + urllib.parse.urlencode(clean)
            body = None
        else:
            body = json.dumps(data or {}).encode()
        log.debug("API %s %s", method, url)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode() if exc.fp else ""
            log.error("API %s %s -> %s: %s", method, path, exc.code, err_body[:300])
            raise

    def get(self, path, params=None, token=None):
        return self._req("GET", path, params, token)

    def post(self, path, data=None, token=None):
        return self._req("POST", path, data, token)

    def put(self, path, data=None, token=None):
        return self._req("PUT", path, data, token)

    def login(self, username, password):
        resp = self.post("/user/login", {"username_or_email": username, "password": password})
        jwt = resp.get("jwt")
        if not jwt:
            raise RuntimeError("Login failed")
        return jwt

    def list_communities(self, page=1, limit=50, listing_type="Local", token=None):
        resp = self.get("/community/list", {"type_": listing_type, "sort": "TopAll", "page": page, "limit": limit}, token=token)
        return resp.get("communities", [])

    def list_all_communities(self, listing_type="Local", token=None):
        all_c, page = [], 1
        while True:
            batch = self.list_communities(page=page, limit=50, listing_type=listing_type, token=token)
            if not batch:
                break
            all_c.extend(batch)
            if len(batch) < 50 or page > 20:
                break
            page += 1
        return all_c

    def get_community(self, name, token=None):
        return self.get("/community", {"name": name}, token=token).get("community_view", {})

    def list_posts(self, community_name, page=1, limit=50, sort="New", token=None):
        return self.get("/post/list", {"community_name": community_name, "sort": sort, "page": page, "limit": limit}, token=token).get("posts", [])

    def create_post(self, community_id, title, body=None, url=None, token=""):
        payload = {"community_id": community_id, "name": title}
        if body: payload["body"] = body
        if url: payload["url"] = url
        return self.post("/post", payload, token=token)

    def list_comments(self, post_id, sort="New", limit=200, token=None):
        return self.get("/comment/list", {"post_id": post_id, "sort": sort, "limit": limit, "max_depth": 20}, token=token).get("comments", [])

    def create_comment(self, post_id, content, parent_id=None, token=""):
        payload = {"post_id": post_id, "content": content}
        if parent_id is not None:
            payload["parent_id"] = parent_id
        return self.post("/comment", payload, token=token)

    # --- Admin API methods ---

    def get_site(self, token=None):
        return self.get("/site", token=token)

    def list_registration_applications(self, unread_only=True, page=1, limit=50, token=None):
        return self.get("/admin/registration_application/list",
                        {"unread_only": unread_only, "page": page, "limit": limit}, token=token)

    def approve_registration(self, id, approve=True, deny_reason=None, token=None):
        payload = {"id": id, "approve": approve}
        if deny_reason:
            payload["deny_reason"] = deny_reason
        return self.put("/admin/registration_application/approve", payload, token=token)

    def list_post_reports(self, unresolved_only=True, page=1, limit=50, token=None):
        return self.get("/post/report/list",
                        {"unresolved_only": unresolved_only, "page": page, "limit": limit}, token=token)

    def list_comment_reports(self, unresolved_only=True, page=1, limit=50, token=None):
        return self.get("/comment/report/list",
                        {"unresolved_only": unresolved_only, "page": page, "limit": limit}, token=token)

    def resolve_post_report(self, report_id, resolved=True, token=None):
        return self.put("/post/report/resolve", {"report_id": report_id, "resolved": resolved}, token=token)

    def resolve_comment_report(self, report_id, resolved=True, token=None):
        return self.put("/comment/report/resolve", {"report_id": report_id, "resolved": resolved}, token=token)

    def remove_post(self, post_id, removed=True, reason=None, token=None):
        payload = {"post_id": post_id, "removed": removed}
        if reason: payload["reason"] = reason
        return self.post("/post/remove", payload, token=token)

    def remove_comment(self, comment_id, removed=True, reason=None, token=None):
        payload = {"comment_id": comment_id, "removed": removed}
        if reason: payload["reason"] = reason
        return self.post("/comment/remove", payload, token=token)

    def lock_post(self, post_id, locked=True, token=None):
        return self.post("/post/lock", {"post_id": post_id, "locked": locked}, token=token)

    def ban_user(self, person_id, ban=True, reason=None, token=None):
        payload = {"person_id": person_id, "ban": ban}
        if reason:
            payload["reason"] = reason
        return self.post("/user/ban", payload, token=token)

    def get_modlog(self, page=1, limit=50, token=None):
        return self.get("/modlog", {"page": page, "limit": limit}, token=token)


# ===== Helpers ===============================================================

def make_message_id(kind, item_id, domain):
    return f"<{kind}-{item_id}@{domain}>"

def parse_message_id(mid):
    m = re.match(r"<(post|comment|regapp|report-post|report-comment|modlog|action)-(\d+)@[^>]+>", mid)
    return (m.group(1), int(m.group(2))) if m else None

def _format_date(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")

def _safe(text):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text or "")

def _format_author(creator, domain):
    """Format author From header, marking banned users."""
    name = creator.get("name", "unknown")
    banned = creator.get("banned", False)
    if banned:
        return f"{name} [banned] <{name}@{domain}>"
    return f"{name} <{name}@{domain}>"

def convert_body(md, post_url=None):
    links, header_lines = [], []
    if post_url:
        links.append(post_url)
        is_img = any(post_url.lower().endswith(e) for e in (".jpeg",".jpg",".png",".gif",".webp",".svg"))
        header_lines.append("[image 1]" if is_img else "[main link 1]")
        header_lines.append("")

    def add_link(url):
        if url in links: return links.index(url) + 1
        links.append(url); return len(links)

    md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", lambda m: f"{m.group(1).strip()} [image {add_link(m.group(2))}]".strip(), md)
    md = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", lambda m: f"{m.group(1).strip()} [{add_link(m.group(2))}]", md)
    md = re.sub(r"(?<!\()https?://[^\s)\]>]+", lambda m: f"[{add_link(m.group(0))}]", md)
    md = re.sub(r"\*\*(.+?)\*\*", r"*\1*", md)
    md = re.sub(r"__(.+?)__", r"*\1*", md)
    md = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"\1", md)
    md = re.sub(r"(?<!_)_([^_]+?)_(?!_)", r"\1", md)
    md = re.sub(r"~~(.+?)~~", r"\1", md)
    md = re.sub(r"\^(.+?)\^", r"\1", md)
    md = re.sub(r"(?<!~)~([^~]+?)~(?!~)", r"\1", md)
    md = re.sub(r"`([^`]+)`", r"\1", md)

    out, prev_list = [], False
    for line in md.split("\n"):
        s = line.strip()
        is_list = bool(re.match(r"^[-*]\s+", s))
        if prev_list and not is_list and s: out.append("")
        prev_list = is_list
        if s == "" or s == "#": out.append(""); continue
        hm = re.match(r"^(#{1,6})\s+(.*)", s)
        if hm: out.append(hm.group(2).strip().upper()); continue
        if s.startswith(">"): out.append(textwrap.fill(s.lstrip(">").strip(), 70, initial_indent="> ", subsequent_indent="> ")); continue
        lm = re.match(r"^[-*]\s+(.*)", s)
        if lm: out.append(textwrap.fill(lm.group(1), 72, initial_indent="  - ", subsequent_indent="    ")); continue
        out.append(textwrap.fill(s, 72))

    if links:
        out.append("")
        out.append("Links:")
        for i, u in enumerate(links, 1):
            out.append(f"  [{i}] {u}")
    return "\n".join(header_lines + out)


# ===== Spool =================================================================

class Spool:
    def __init__(self, spool_dir, domain):
        self.root = Path(spool_dir)
        self.domain = domain
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = {}
        self._global_lock = threading.Lock()

    def _group_lock(self, ng):
        with self._global_lock:
            if ng not in self._locks: self._locks[ng] = threading.Lock()
            return self._locks[ng]

    def _group_dir(self, ng):
        d = self.root / ng; d.mkdir(parents=True, exist_ok=True); return d

    def save_communities(self, data):
        (self.root / "_communities.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def load_communities(self):
        p = self.root / "_communities.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def _load_index(self, ng):
        p = self._group_dir(ng) / "_index.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"next_num": 1, "articles": {}, "mid_to_num": {}}

    def _save_index(self, ng, idx):
        (self._group_dir(ng) / "_index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")

    def load_meta(self, ng):
        p = self._group_dir(ng) / "_meta.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def save_meta(self, ng, meta):
        (self._group_dir(ng) / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _art_fname(self, mid):
        return mid.strip("<>").split("@")[0] + ".json"

    def has_article(self, ng, mid):
        return (self._group_dir(ng) / self._art_fname(mid)).exists()

    def store_article(self, ng, article):
        lock = self._group_lock(ng)
        with lock:
            idx = self._load_index(ng)
            mid = article["message_id"]
            if mid in idx["mid_to_num"]:
                return idx["mid_to_num"][mid]
            num = idx["next_num"]; idx["next_num"] = num + 1
            idx["articles"][str(num)] = mid
            idx["mid_to_num"][mid] = num
            article["number"] = num
            (self._group_dir(ng) / self._art_fname(mid)).write_text(
                json.dumps(article, ensure_ascii=False), encoding="utf-8")
            self._save_index(ng, idx)
            return num

    def update_article(self, ng, article):
        """Update an existing article in-place (same number, same message-id).
        Only overwrites the article file; does not touch the index."""
        lock = self._group_lock(ng)
        with lock:
            mid = article["message_id"]
            idx = self._load_index(ng)
            if mid not in idx["mid_to_num"]:
                return False
            article["number"] = idx["mid_to_num"][mid]
            (self._group_dir(ng) / self._art_fname(mid)).write_text(
                json.dumps(article, ensure_ascii=False), encoding="utf-8")
            return True

    def _load_art(self, ng, mid):
        p = self._group_dir(ng) / self._art_fname(mid)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def get_article_by_num(self, ng, num):
        idx = self._load_index(ng)
        mid = idx["articles"].get(str(num))
        return self._load_art(ng, mid) if mid else None

    def get_article_by_mid(self, ng, mid):
        idx = self._load_index(ng)
        return self._load_art(ng, mid) if mid in idx["mid_to_num"] else None

    def find_article_across_groups(self, mid):
        for d in self.root.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                art = self.get_article_by_mid(d.name, mid)
                if art: return art
        return None

    def get_group_info(self, ng):
        nums = [int(n) for n in self._load_index(ng)["articles"].keys()]
        return (len(nums), min(nums), max(nums)) if nums else (0, 0, 0)

    def get_article_nums(self, ng):
        return sorted(int(n) for n in self._load_index(ng)["articles"].keys())


# ===== Poller ================================================================

class Poller:
    def __init__(self, api, spool, interval=900, listing_type="Local"):
        self.api, self.spool, self.interval = api, spool, interval
        self.listing_type = listing_type
        self.domain = api.domain
        self.token = None  # JWT for authenticated polling
        self._stop = threading.Event()
        self._action_counter = 0
        self._action_lock = threading.Lock()

    def set_token(self, token):
        self.token = token
        self.is_admin = False
        # Check if poller account is admin
        try:
            site = self.api.get_site(token=token)
            my_user = site.get("my_user", {})
            local_user = my_user.get("local_user_view", {}).get("local_user", {})
            if local_user.get("admin"):
                self.is_admin = True
                log.info("Poller: account is admin - admin groups enabled")
            else:
                log.warning("Poller: account is NOT admin - admin groups disabled "
                            "(registrations, reports, modlog will not sync)")
        except Exception:
            log.warning("Poller: could not verify admin status - admin groups disabled")

    def next_action_id(self):
        with self._action_lock:
            self._action_counter += 1
            return self._action_counter

    def run_forever(self):
        log.info("Poller started (interval=%ds, type=%s)", self.interval, self.listing_type)
        self._poll()
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if not self._stop.is_set(): self._poll()

    def stop(self): self._stop.set()

    def force_sync(self):
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        log.info("Poller: sync start")
        try:
            self._sync_communities()
            self._sync_all_groups()
            if self.token and self.is_admin:
                self._sync_admin_groups()
        except Exception:
            log.exception("Poller: sync error")
        log.info("Poller: sync done")

    def _sync_communities(self):
        comms = self.api.list_all_communities(listing_type=self.listing_type, token=self.token)
        log.info("Poller: %d communities", len(comms))
        self.spool.save_communities(comms)
        for cv in comms:
            c = cv["community"]
            ng = f"{self.domain}.{c['name']}"
            meta = self.spool.load_meta(ng)
            meta.update(community_id=c["id"], community_name=c["name"],
                        title=c.get("title",""), description=c.get("description",""))
            self.spool.save_meta(ng, meta)

    def _sync_all_groups(self):
        for cv in self.spool.load_communities():
            c = cv["community"]
            ng = f"{self.domain}.{c['name']}"
            try:
                self._sync_group(ng, c["name"])
            except Exception:
                log.exception("Poller: error syncing %s", ng)

    def _is_removed(self, item):
        """Check if a post or comment has been removed or deleted."""
        return item.get("removed", False) or item.get("deleted", False)

    def _sync_group(self, ng, community_name):
        posts = self.api.list_posts(community_name, limit=50, sort="New", token=self.token)
        if not posts: return
        np, nc, nu = 0, 0, 0
        for pv in reversed(posts):
            post = pv["post"]
            creator = pv["creator"]
            post_mid = make_message_id("post", post["id"], self.domain)

            if self.spool.has_article(ng, post_mid):
                # Check for edits, removals, or ban status changes
                updated = self._check_and_update_post(ng, pv)
                if updated:
                    nu += 1
                nc += self._sync_comments(ng, post["id"], post_mid,
                                          _safe(post.get("name","(no subject)")))
                continue
            art = self._build_post(pv, ng)
            self.spool.store_article(ng, art)
            np += 1
            nc += self._sync_comments(ng, post["id"], post_mid, art["subject"])
        if np or nc or nu:
            log.info("Poller: %s: +%d posts, +%d comments, ~%d updated", ng, np, nc, nu)

    def _check_and_update_post(self, ng, pv):
        """Compare a post from API with spool version; update if changed.
        Returns True if the article was updated."""
        post = pv["post"]
        creator = pv["creator"]
        post_mid = make_message_id("post", post["id"], self.domain)
        existing = self.spool.get_article_by_mid(ng, post_mid)
        if not existing:
            return False

        ex_headers = existing.get("extra_headers", {})
        old_updated = ex_headers.get("X-Lemmy-Updated", "")
        old_removed = ex_headers.get("X-Lemmy-Removed", "false")
        old_banned = ex_headers.get("X-Lemmy-Author-Banned", "false")

        new_updated = post.get("updated") or ""
        new_removed = "true" if self._is_removed(post) else "false"
        new_banned = "true" if creator.get("banned", False) else "false"

        # Nothing changed
        if (new_updated == old_updated and
                new_removed == old_removed and
                new_banned == old_banned):
            return False

        # Rebuild article with updated data
        art = self._build_post(pv, ng)
        self.spool.update_article(ng, art)
        log.debug("Poller: updated post %s (updated=%s->%s removed=%s->%s banned=%s->%s)",
                  post_mid, old_updated, new_updated, old_removed, new_removed, old_banned, new_banned)
        return True

    def _check_and_update_comment(self, ng, cv, post_mid, post_subject):
        """Compare a comment from API with spool version; update if changed.
        Returns True if the article was updated."""
        comment = cv["comment"]
        creator = cv["creator"]
        mid = make_message_id("comment", comment["id"], self.domain)
        existing = self.spool.get_article_by_mid(ng, mid)
        if not existing:
            return False

        ex_headers = existing.get("extra_headers", {})
        old_updated = ex_headers.get("X-Lemmy-Updated", "")
        old_removed = ex_headers.get("X-Lemmy-Removed", "false")
        old_banned = ex_headers.get("X-Lemmy-Author-Banned", "false")

        new_updated = comment.get("updated") or ""
        new_removed = "true" if self._is_removed(comment) else "false"
        new_banned = "true" if creator.get("banned", False) else "false"

        if (new_updated == old_updated and
                new_removed == old_removed and
                new_banned == old_banned):
            return False

        art = self._build_comment(cv, ng, post_mid, post_subject)
        self.spool.update_article(ng, art)
        log.debug("Poller: updated comment %s (updated=%s->%s removed=%s->%s banned=%s->%s)",
                  mid, old_updated, new_updated, old_removed, new_removed, old_banned, new_banned)
        return True

    def _sync_comments(self, ng, post_id, post_mid, post_subject):
        try:
            comments = self.api.list_comments(post_id, limit=200, token=self.token)
        except Exception:
            return 0
        comments.sort(key=lambda c: (len(c["comment"].get("path","0").split(".")), c["comment"]["id"]))
        n = 0
        for cv in comments:
            mid = make_message_id("comment", cv["comment"]["id"], self.domain)
            if self.spool.has_article(ng, mid):
                if self._check_and_update_comment(ng, cv, post_mid, post_subject):
                    n += 1
                continue
            self.spool.store_article(ng, self._build_comment(cv, ng, post_mid, post_subject))
            n += 1
        return n

    def _build_post(self, pv, ng):
        post, creator = pv["post"], pv["creator"]
        removed = self._is_removed(post)

        if removed:
            body = "[REMOVED]"
        else:
            body = convert_body(_safe(post.get("body","") or ""), post_url=post.get("url"))
            if not body.strip(): body = "(no content)"

        extra = {"X-Lemmy-Post-Id": str(post["id"]),
                 "X-Lemmy-Score": str(pv.get("counts",{}).get("score",0)),
                 "X-Lemmy-Author-Id": str(creator["id"]),
                 "X-Lemmy-Author-Banned": "true" if creator.get("banned", False) else "false"}
        if post.get("updated"):
            extra["X-Lemmy-Updated"] = post["updated"]
        if removed:
            extra["X-Lemmy-Removed"] = "true"

        return dict(message_id=make_message_id("post", post["id"], self.domain),
                    newsgroup=ng, number=0,
                    subject=_safe(post.get("name","(no subject)")),
                    author=_format_author(creator, self.domain),
                    date=_format_date(post.get("published","")),
                    references="", body=body,
                    extra_headers=extra)

    def _build_comment(self, cv, ng, post_mid, post_subject):
        comment, creator = cv["comment"], cv["creator"]
        removed = self._is_removed(comment)

        if removed:
            body = "[REMOVED]"
        else:
            body = convert_body(_safe(comment.get("content","") or ""))

        refs = [post_mid]
        for pid in [int(x) for x in comment.get("path","0").split(".")[1:-1] if x.isdigit()]:
            refs.append(make_message_id("comment", pid, self.domain))

        extra = {"X-Lemmy-Comment-Id": str(comment["id"]),
                 "X-Lemmy-Score": str(cv.get("counts",{}).get("score",0)),
                 "X-Lemmy-Author-Id": str(creator["id"]),
                 "X-Lemmy-Author-Banned": "true" if creator.get("banned", False) else "false"}
        if comment.get("updated"):
            extra["X-Lemmy-Updated"] = comment["updated"]
        if removed:
            extra["X-Lemmy-Removed"] = "true"

        return dict(message_id=make_message_id("comment", comment["id"], self.domain),
                    newsgroup=ng, number=0,
                    subject=f"Re: {post_subject}",
                    author=_format_author(creator, self.domain),
                    date=_format_date(comment.get("published","")),
                    references=" ".join(refs),
                    body=body,
                    extra_headers=extra)

    # --- Admin group sync ---

    def _sync_admin_groups(self):
        try:
            self._sync_registrations()
        except Exception:
            log.exception("Poller: error syncing admin.registrations")
        try:
            self._sync_reports()
        except Exception:
            log.exception("Poller: error syncing admin.reports")
        try:
            self._sync_modlog()
        except Exception:
            log.exception("Poller: error syncing admin.modlog")

    def _sync_registrations(self):
        ng = f"{self.domain}.admin.registrations"
        meta = self.spool.load_meta(ng)
        if not meta.get("admin_group"):
            meta.update(admin_group=True, title="Registration Applications",
                        description="Pending registration applications. Reply 'ok' to approve, 'deny' to reject.")
            self.spool.save_meta(ng, meta)

        try:
            resp = self.api.list_registration_applications(unread_only=True, limit=50, token=self.token)
        except Exception:
            log.debug("Poller: registration applications not available (not admin or feature disabled)")
            return

        apps = resp.get("registration_applications", [])
        n = 0
        for app_view in apps:
            app = app_view.get("registration_application", {})
            creator = app_view.get("creator", {})
            app_id = app.get("id")
            if not app_id:
                continue
            mid = make_message_id("regapp", app_id, self.domain)
            if self.spool.has_article(ng, mid):
                continue

            answer = _safe(app.get("answer", "") or "")
            username = creator.get("name", "unknown")
            person_id = creator.get("id", 0)
            published = app.get("published", "")

            subject = f'{username} - "{answer[:60]}" [REG app={app_id} person={person_id}]'
            body_lines = [
                f"Username: {username}",
                f"Registered: {published}",
                "",
                "Application text:",
                f"  {answer}" if answer else "  (no application text)",
                "",
                "Reply 'ok' or 'approve' to accept, 'deny' to reject.",
            ]

            art = dict(
                message_id=mid, newsgroup=ng, number=0,
                subject=subject,
                author=f"{username} <{username}@{self.domain}>",
                date=_format_date(published),
                references="", body="\n".join(body_lines),
                extra_headers={"X-Lemmy-App-Id": str(app_id),
                               "X-Lemmy-Person-Id": str(person_id)})
            self.spool.store_article(ng, art)
            n += 1
        if n:
            log.info("Poller: admin.registrations: +%d applications", n)

    def _sync_reports(self):
        ng = f"{self.domain}.admin.reports"
        meta = self.spool.load_meta(ng)
        if not meta.get("admin_group"):
            meta.update(admin_group=True, title="Reports",
                        description="Post and comment reports. Reply 'del' to delete, 'ban' to ban author, 'del ban' for both.")
            self.spool.save_meta(ng, meta)

        n = 0

        # Post reports
        try:
            resp = self.api.list_post_reports(unresolved_only=True, limit=50, token=self.token)
            for rv in resp.get("post_reports", []):
                report = rv.get("post_report", {})
                post = rv.get("post", {})
                creator = rv.get("creator", {})  # reporter
                post_creator = rv.get("post_creator", {})  # author of the post

                report_id = report.get("id")
                if not report_id:
                    continue
                mid = make_message_id("report-post", report_id, self.domain)
                if self.spool.has_article(ng, mid):
                    continue

                reason = _safe(report.get("reason", "") or "")
                post_title = _safe(post.get("name", "(no title)"))
                post_id = post.get("id", 0)
                author_name = post_creator.get("name", "unknown")
                author_id = post_creator.get("id", 0)
                reporter_name = creator.get("name", "unknown")

                subject = f'"{post_title}" by {author_name} [REPORT type=post report={report_id} target={post_id} author={author_id}]'
                body_lines = [
                    f"Reported by: {reporter_name}",
                    f"Reason: {reason}" if reason else "Reason: (no reason given)",
                    "",
                    "--- Original post ---",
                    f"Title: {post_title}",
                ]
                post_body = _safe(post.get("body", "") or "")
                if post_body:
                    body_lines.append("")
                    body_lines.append(post_body[:500])
                post_url = post.get("url")
                if post_url:
                    body_lines.append(f"URL: {post_url}")
                body_lines.extend(["", "Reply 'del' to delete, 'ban' to ban author, 'del ban' for both."])

                art = dict(
                    message_id=mid, newsgroup=ng, number=0,
                    subject=subject,
                    author=f"{reporter_name} <{reporter_name}@{self.domain}>",
                    date=_format_date(report.get("published", "")),
                    references="", body="\n".join(body_lines),
                    extra_headers={"X-Lemmy-Report-Id": str(report_id),
                                   "X-Lemmy-Report-Type": "post",
                                   "X-Lemmy-Target-Id": str(post_id),
                                   "X-Lemmy-Target-Author-Id": str(author_id),
                                   "X-Lemmy-Target-Author": author_name})
                self.spool.store_article(ng, art)
                n += 1
        except Exception:
            log.debug("Poller: post reports not available")

        # Comment reports
        try:
            resp = self.api.list_comment_reports(unresolved_only=True, limit=50, token=self.token)
            for rv in resp.get("comment_reports", []):
                report = rv.get("comment_report", {})
                comment = rv.get("comment", {})
                creator = rv.get("creator", {})  # reporter
                comment_creator = rv.get("comment_creator", {})  # author of the comment

                report_id = report.get("id")
                if not report_id:
                    continue
                mid = make_message_id("report-comment", report_id, self.domain)
                if self.spool.has_article(ng, mid):
                    continue

                reason = _safe(report.get("reason", "") or "")
                comment_content = _safe(comment.get("content", "") or "")
                comment_id = comment.get("id", 0)
                author_name = comment_creator.get("name", "unknown")
                author_id = comment_creator.get("id", 0)
                reporter_name = creator.get("name", "unknown")

                subject = f'"{comment_content[:50]}..." by {author_name} [REPORT type=comment report={report_id} target={comment_id} author={author_id}]'
                body_lines = [
                    f"Reported by: {reporter_name}",
                    f"Reason: {reason}" if reason else "Reason: (no reason given)",
                    "",
                    "--- Original comment ---",
                    comment_content[:1000] if comment_content else "(no content)",
                    "",
                    "Reply 'del' to delete, 'ban' to ban author, 'del ban' for both.",
                ]

                art = dict(
                    message_id=mid, newsgroup=ng, number=0,
                    subject=subject,
                    author=f"{reporter_name} <{reporter_name}@{self.domain}>",
                    date=_format_date(report.get("published", "")),
                    references="", body="\n".join(body_lines),
                    extra_headers={"X-Lemmy-Report-Id": str(report_id),
                                   "X-Lemmy-Report-Type": "comment",
                                   "X-Lemmy-Target-Id": str(comment_id),
                                   "X-Lemmy-Target-Author-Id": str(author_id),
                                   "X-Lemmy-Target-Author": author_name})
                self.spool.store_article(ng, art)
                n += 1
        except Exception:
            log.debug("Poller: comment reports not available")

        if n:
            log.info("Poller: admin.reports: +%d reports", n)

    def _sync_modlog(self):
        ng = f"{self.domain}.admin.modlog"
        meta = self.spool.load_meta(ng)
        if not meta.get("admin_group"):
            meta.update(admin_group=True, title="Moderation Log",
                        description="Instance moderation log (read-only).")
            self.spool.save_meta(ng, meta)

        try:
            resp = self.api.get_modlog(limit=50, token=self.token)
        except Exception:
            log.debug("Poller: modlog not available")
            return

        n = 0
        # Process different modlog entry types
        modlog_types = [
            ("removed_posts", "ModRemovePost", lambda e: (
                e.get("mod_remove_post", {}).get("id", 0),
                f"Post removed: \"{_safe(e.get('post', {}).get('name', '?'))}\" by {e.get('moderator', {}).get('name', '?')}",
                e.get("moderator", {}).get("name", "system"),
                e.get("mod_remove_post", {}).get("when_", ""),
                f"Post: {_safe(e.get('post', {}).get('name', '?'))}\nRemoved: {e.get('mod_remove_post', {}).get('removed', '?')}\nReason: {_safe(e.get('mod_remove_post', {}).get('reason', '') or '(none)')}"
            )),
            ("removed_comments", "ModRemoveComment", lambda e: (
                e.get("mod_remove_comment", {}).get("id", 0),
                f"Comment removed by {e.get('moderator', {}).get('name', '?')}",
                e.get("moderator", {}).get("name", "system"),
                e.get("mod_remove_comment", {}).get("when_", ""),
                f"Comment: {_safe(e.get('comment', {}).get('content', '?'))[:200]}\nRemoved: {e.get('mod_remove_comment', {}).get('removed', '?')}\nReason: {_safe(e.get('mod_remove_comment', {}).get('reason', '') or '(none)')}"
            )),
            ("banned_from_community", "ModBanFromCommunity", lambda e: (
                e.get("mod_ban_from_community", {}).get("id", 0),
                f"Community ban: {e.get('banned_person', {}).get('name', '?')} by {e.get('moderator', {}).get('name', '?')}",
                e.get("moderator", {}).get("name", "system"),
                e.get("mod_ban_from_community", {}).get("when_", ""),
                f"User: {e.get('banned_person', {}).get('name', '?')}\nBanned: {e.get('mod_ban_from_community', {}).get('banned', '?')}\nCommunity: {e.get('community', {}).get('name', '?')}\nReason: {_safe(e.get('mod_ban_from_community', {}).get('reason', '') or '(none)')}"
            )),
            ("banned", "ModBan", lambda e: (
                e.get("mod_ban", {}).get("id", 0),
                f"Instance ban: {e.get('banned_person', {}).get('name', '?')} by {e.get('moderator', {}).get('name', '?')}",
                e.get("moderator", {}).get("name", "system"),
                e.get("mod_ban", {}).get("when_", ""),
                f"User: {e.get('banned_person', {}).get('name', '?')}\nBanned: {e.get('mod_ban', {}).get('banned', '?')}\nReason: {_safe(e.get('mod_ban', {}).get('reason', '') or '(none)')}"
            )),
            ("locked_posts", "ModLockPost", lambda e: (
                e.get("mod_lock_post", {}).get("id", 0),
                f"Post locked: \"{_safe(e.get('post', {}).get('name', '?'))}\" by {e.get('moderator', {}).get('name', '?')}",
                e.get("moderator", {}).get("name", "system"),
                e.get("mod_lock_post", {}).get("when_", ""),
                f"Post: {_safe(e.get('post', {}).get('name', '?'))}\nLocked: {e.get('mod_lock_post', {}).get('locked', '?')}"
            )),
        ]

        for key, kind, extractor in modlog_types:
            for entry in resp.get(key, []):
                try:
                    entry_id, subject, mod_name, when, body = extractor(entry)
                    if not entry_id:
                        continue
                    mid = f"<modlog-{kind}-{entry_id}@{self.domain}>"
                    if self.spool.has_article(ng, mid):
                        continue
                    art = dict(
                        message_id=mid, newsgroup=ng, number=0,
                        subject=subject,
                        author=f"{mod_name} <{mod_name}@{self.domain}>",
                        date=_format_date(when),
                        references="", body=body,
                        extra_headers={"X-Lemmy-Modlog-Type": kind})
                    self.spool.store_article(ng, art)
                    n += 1
                except Exception:
                    log.debug("Poller: error processing modlog entry", exc_info=True)

        if n:
            log.info("Poller: admin.modlog: +%d entries", n)


# ===== Article formatting ====================================================

def article_head_lines(art):
    lines = [f"Message-ID: {art['message_id']}", f"Newsgroups: {art['newsgroup']}",
             f"Subject: {art['subject']}", f"From: {art['author']}",
             f"Date: {art['date']}", "Content-Type: text/plain; charset=UTF-8"]
    if art.get("references"): lines.append(f"References: {art['references']}")
    for k, v in art.get("extra_headers", {}).items(): lines.append(f"{k}: {v}")
    return lines

def article_full_lines(art):
    return article_head_lines(art) + [""] + art["body"].splitlines()


# ===== NNTP Session ==========================================================

# Admin commands for different contexts
REPORT_COMMANDS = {"del", "ban", "lock"}
REG_COMMANDS = {"ok", "approve", "deny"}

def parse_mod_command(body_text):
    """Parse any line as a moderation command. Returns set of commands or None.
    Scans all lines - first line that is exactly a valid command wins.
    Lines that contain other words are skipped (not treated as commands)."""
    valid_commands = {"del", "ban", "lock"}
    for line in body_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        words = line.lower().split()
        if words and set(words) <= valid_commands:
            return set(words)
    return None

def parse_reg_command(body_text):
    """Parse any line as a registration command. Returns command string or None.
    Scans all lines - first line that is exactly a valid command wins."""
    valid_commands = {"ok", "approve", "deny"}
    for line in body_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.lower() in valid_commands:
            return "approve" if line.lower() in ("ok", "approve") else "deny"
    return None

def parse_report_subject(subject):
    """Extract report IDs from subject line."""
    m = re.search(r"\[REPORT\s+type=(post|comment)\s+report=(\d+)\s+target=(\d+)\s+author=(\d+)\]", subject)
    if m:
        return {"type": m.group(1), "report_id": int(m.group(2)),
                "target_id": int(m.group(3)), "author_id": int(m.group(4))}
    return None

def parse_reg_subject(subject):
    """Extract registration IDs from subject line."""
    m = re.search(r"\[REG\s+app=(\d+)\s+person=(\d+)\]", subject)
    if m:
        return {"app_id": int(m.group(1)), "person_id": int(m.group(2))}
    return None


class NNTPSession:
    ADMIN_GROUPS = {"admin.registrations", "admin.reports", "admin.modlog"}

    def __init__(self, api, spool, poller, reader, writer, require_auth=True):
        self.api, self.spool, self.poller = api, spool, poller
        self.reader, self.writer = reader, writer
        self.domain = api.domain
        self.require_auth = require_auth
        self.jwt = None; self._auth_user = None
        self.is_admin = False
        self.current_group = None; self.current_article = None
        self.closed = False

    def _is_admin_group(self, ng):
        """Check if a newsgroup is an admin-only group."""
        prefix = self.domain + "."
        if ng.startswith(prefix):
            suffix = ng[len(prefix):]
            return suffix in self.ADMIN_GROUPS
        return False

    async def send(self, *lines):
        for line in lines:
            log.debug(">>> %s", line)
            self.writer.write((line + "\r\n").encode("utf-8"))
        await self.writer.drain()

    async def recv(self):
        try:
            data = await asyncio.wait_for(self.reader.readline(), timeout=600)
        except (asyncio.TimeoutError, ConnectionResetError):
            return None
        if not data: return None
        return data.decode("utf-8", errors="replace").rstrip("\r\n")

    def _community_from_ng(self, ng):
        prefix = self.domain + "."
        return ng[len(prefix):] if ng.startswith(prefix) else ng

    def _resolve_article(self, arg):
        if not arg or not arg.strip():
            if self.current_group and self.current_article is not None:
                art = self.spool.get_article_by_num(self.current_group, self.current_article)
                if art: return art, ""
            return None, "420 No current article selected"
        arg = arg.strip()
        if arg.startswith("<"):
            if self.current_group:
                art = self.spool.get_article_by_mid(self.current_group, arg)
                if art: return art, ""
            art = self.spool.find_article_across_groups(arg)
            return (art, "") if art else (None, "430 No such article")
        try:
            num = int(arg)
        except ValueError:
            return None, "423 Bad article number"
        if self.current_group:
            art = self.spool.get_article_by_num(self.current_group, num)
            if art: self.current_article = num; return art, ""
        return None, "423 No such article number"

    async def _check_admin(self):
        """Check if the authenticated user is an admin via the site API."""
        if not self.jwt:
            return False
        try:
            site = await asyncio.get_event_loop().run_in_executor(
                None, self.api.get_site, self.jwt)
            admins = site.get("admins", [])
            for admin_view in admins:
                person = admin_view.get("person", {})
                if person.get("name") == self._auth_user:
                    return True
            return False
        except Exception:
            log.debug("Admin check failed", exc_info=True)
            return False

    def _store_command_article(self, newsgroup, references_mid, subject, command_text):
        """Store the admin's command as an article in the spool."""
        cmd_id = f"{int(time.time())}-cmd-{self.poller.next_action_id()}"
        mid = f"<action-{cmd_id}@{self.domain}>"
        art = dict(
            message_id=mid, newsgroup=newsgroup, number=0,
            subject=f"Re: {subject}" if not subject.startswith("Re:") else subject,
            author=f"{self._auth_user} <{self._auth_user}@{self.domain}>",
            date=_format_date(datetime.now(timezone.utc).isoformat()),
            references=references_mid,
            body=command_text,
            extra_headers={"X-Lemmy-System": "admin-command"})
        self.spool.store_article(newsgroup, art)
        return mid

    def _generate_action_article(self, newsgroup, references_mid, subject, body_lines):
        """Generate a system feedback article in the spool."""
        action_id = f"{int(time.time())}-{self.poller.next_action_id()}"
        mid = f"<action-{action_id}@{self.domain}>"
        art = dict(
            message_id=mid, newsgroup=newsgroup, number=0,
            subject=f"Re: {subject}" if not subject.startswith("Re:") else subject,
            author=f"system <admin@{self.domain}>",
            date=_format_date(datetime.now(timezone.utc).isoformat()),
            references=references_mid,
            body="\n".join(body_lines),
            extra_headers={"X-Lemmy-System": "action-result"})
        self.spool.store_article(newsgroup, art)
        return mid

    # -- commands ------------------------------------------------------------

    async def handle_capabilities(self, _):
        await self.send("101 Capability list follows", "VERSION 2", "READER", "POST",
                        "LIST ACTIVE NEWSGROUPS", "OVER", "AUTHINFO USER", ".")

    async def handle_mode(self, arg):
        if arg and arg.upper() == "READER":
            if self.require_auth and not self.jwt:
                await self.send("201 Posting allowed after authentication")
            else:
                await self.send("200 Posting allowed")
        else:
            await self.send("501 Unknown MODE")

    async def handle_authinfo(self, arg):
        if not arg: await self.send("501 Syntax error"); return
        parts = arg.split(None, 1); subcmd = parts[0].upper(); value = parts[1] if len(parts)>1 else ""
        if subcmd == "USER":
            self._auth_user = value; await self.send("381 Password required")
        elif subcmd == "PASS":
            if not self._auth_user: await self.send("482 Out of sequence"); return
            try:
                self.jwt = await asyncio.get_event_loop().run_in_executor(None, self.api.login, self._auth_user, value)
                self.is_admin = await self._check_admin()
                log.info("User %s authenticated (admin=%s)", self._auth_user, self.is_admin)
                await self.send(f"281 Authentication accepted for {self._auth_user} – posting allowed")
            except Exception:
                await self.send("481 Authentication failed"); self._auth_user = None

    async def handle_list(self, arg):
        sub = (arg or "ACTIVE").upper().split()[0]
        comms = self.spool.load_communities()
        if sub in ("ACTIVE", ""):
            await self.send("215 List of newsgroups follows")
            for cv in comms:
                c = cv["community"]; ng = f"{self.domain}.{c['name']}"
                _, low, high = self.spool.get_group_info(ng)
                await self.send(f"{ng} {high} {low} y")
            # Admin groups - only for admins
            if self.is_admin:
                for suffix in sorted(self.ADMIN_GROUPS):
                    ng = f"{self.domain}.{suffix}"
                    count, low, high = self.spool.get_group_info(ng)
                    posting = "y" if suffix != "admin.modlog" else "n"
                    await self.send(f"{ng} {high} {low} {posting}")
            await self.send(".")
        elif sub == "NEWSGROUPS":
            await self.send("215 List of newsgroups follows")
            for cv in comms:
                c = cv["community"]; ng = f"{self.domain}.{c['name']}"
                await self.send(f"{ng} {(c.get('title') or c['name']).replace(chr(10),' ')[:80]}")
            if self.is_admin:
                for suffix in sorted(self.ADMIN_GROUPS):
                    ng = f"{self.domain}.{suffix}"
                    meta = self.spool.load_meta(ng)
                    title = meta.get("title", suffix)
                    await self.send(f"{ng} {title}")
            await self.send(".")
        else:
            await self.send("501 Syntax error")

    async def handle_group(self, arg):
        if not arg: await self.send("411 No such newsgroup"); return
        ng = arg.strip()
        if not ng.startswith(self.domain + "."): ng = f"{self.domain}.{ng}"
        # Admin group access control
        if self._is_admin_group(ng) and not self.is_admin:
            await self.send("411 No such newsgroup"); return
        meta = self.spool.load_meta(ng)
        if not meta.get("community_name") and not meta.get("admin_group"):
            await self.send("411 No such newsgroup"); return
        count, low, high = self.spool.get_group_info(ng)
        self.current_group = ng
        self.current_article = low if count > 0 else None
        await self.send(f"211 {count} {low} {high} {ng}")

    async def handle_listgroup(self, arg):
        gn = arg.split()[0] if arg else None
        if gn:
            if not gn.startswith(self.domain + "."): gn = f"{self.domain}.{gn}"
            if self._is_admin_group(gn) and not self.is_admin:
                await self.send("411 No such newsgroup"); return
            meta = self.spool.load_meta(gn)
            if not meta.get("community_name") and not meta.get("admin_group"):
                await self.send("411 No such newsgroup"); return
            self.current_group = gn
        if not self.current_group: await self.send("412 No newsgroup selected"); return
        count, low, high = self.spool.get_group_info(self.current_group)
        self.current_article = low if count > 0 else None
        await self.send(f"211 {count} {low} {high} {self.current_group}")
        for num in self.spool.get_article_nums(self.current_group): await self.send(str(num))
        await self.send(".")

    async def handle_article(self, arg):
        art, err = self._resolve_article(arg)
        if not art: await self.send(err); return
        # Check admin access for admin group articles
        if self._is_admin_group(art.get("newsgroup", "")) and not self.is_admin:
            await self.send("430 No such article"); return
        await self.send(f"220 {art['number']} {art['message_id']}")
        for line in article_full_lines(art):
            await self.send(("." + line) if line.startswith(".") else line)
        await self.send(".")

    async def handle_head(self, arg):
        art, err = self._resolve_article(arg)
        if not art: await self.send(err); return
        if self._is_admin_group(art.get("newsgroup", "")) and not self.is_admin:
            await self.send("430 No such article"); return
        await self.send(f"221 {art['number']} {art['message_id']}")
        for line in article_head_lines(art): await self.send(line)
        await self.send(".")

    async def handle_body(self, arg):
        art, err = self._resolve_article(arg)
        if not art: await self.send(err); return
        if self._is_admin_group(art.get("newsgroup", "")) and not self.is_admin:
            await self.send("430 No such article"); return
        await self.send(f"222 {art['number']} {art['message_id']}")
        for line in art["body"].splitlines():
            await self.send(("." + line) if line.startswith(".") else line)
        await self.send(".")

    async def handle_stat(self, arg):
        art, err = self._resolve_article(arg)
        if not art: await self.send(err); return
        await self.send(f"223 {art['number']} {art['message_id']}")

    async def handle_over(self, arg):
        if not self.current_group: await self.send("412 No newsgroup selected"); return
        # Check admin access
        if self._is_admin_group(self.current_group) and not self.is_admin:
            await self.send("412 No newsgroup selected"); return
        if not arg:
            if self.current_article is not None: lo = hi = self.current_article
            else: await self.send("420 No current article"); return
        elif "-" in arg:
            parts = arg.split("-", 1); lo = int(parts[0])
            _, _, gh = self.spool.get_group_info(self.current_group)
            hi = int(parts[1]) if parts[1] else gh
        else:
            lo = hi = int(arg)
        await self.send("224 Overview information follows")
        for num in self.spool.get_article_nums(self.current_group):
            if num < lo or num > hi: continue
            art = self.spool.get_article_by_num(self.current_group, num)
            if not art: continue
            bb = len(art["body"].encode("utf-8")); bl = art["body"].count("\n")+1
            await self.send("\t".join([str(num), art["subject"], art["author"], art["date"],
                                       art["message_id"], art.get("references",""), str(bb), str(bl)]))
        await self.send(".")

    async def handle_post(self, _):
        if not self.jwt: await self.send("480 Authentication required"); return
        await self.send("340 Send article to be posted")
        raw = []
        while True:
            line = await self.recv()
            if line is None: return
            if line == ".": break
            raw.append(line[1:] if line.startswith("..") else line)

        headers, body_start = {}, 0
        last_key = None
        for i, line in enumerate(raw):
            if line == "": body_start = i + 1; break
            if line[0] in (" ", "\t") and last_key:
                # Continuation line (RFC 2822 header folding)
                headers[last_key] += " " + line.strip()
            elif ":" in line:
                k, _, v = line.partition(":"); k = k.strip().lower(); v = v.strip()
                headers[k] = v; last_key = k
        body_text = "\n".join(raw[body_start:])

        newsgroup = headers.get("newsgroups","").split(",")[0].strip()
        subject = headers.get("subject","").strip()
        references = headers.get("references","").strip()
        in_reply_to = headers.get("in-reply-to","").strip()
        if not newsgroup: await self.send("441 No Newsgroups header"); return

        # --- Admin group: registrations ---
        reg_ng = f"{self.domain}.admin.registrations"
        if newsgroup == reg_ng:
            if not self.is_admin:
                await self.send("480 Authentication required"); return
            await self._handle_registration_action(subject, body_text)
            return

        # --- Admin group: reports ---
        report_ng = f"{self.domain}.admin.reports"
        if newsgroup == report_ng:
            if not self.is_admin:
                await self.send("480 Authentication required"); return
            await self._handle_report_action(subject, body_text)
            return

        # --- Admin group: modlog (read-only) ---
        modlog_ng = f"{self.domain}.admin.modlog"
        if newsgroup == modlog_ng:
            await self.send("440 Posting not allowed to modlog"); return

        # --- Normal group: check for inline mod commands ---
        community = self._community_from_ng(newsgroup)
        mod_cmds = parse_mod_command(body_text)

        if mod_cmds and self.is_admin:
            # This is a moderation command from an admin in a normal group
            await self._handle_inline_mod(newsgroup, subject, body_text, references, in_reply_to, mod_cmds)
            return

        # --- Normal posting to Lemmy ---
        try:
            reply_mid = in_reply_to or (references.split()[-1] if references else "")
            parsed = parse_message_id(reply_mid) if reply_mid else None

            # Early reject if replying to a removed article
            if reply_mid:
                parent_art = self.spool.find_article_across_groups(reply_mid)
                if parent_art and parent_art.get("extra_headers", {}).get("X-Lemmy-Removed") == "true":
                    await self.send("441 Cannot reply to a removed article")
                    return

            if parsed:
                kind, item_id = parsed
                if kind == "comment":
                    post_id = self._find_post_id(item_id)
                    if not post_id: await self.send("441 Cannot find parent post"); return
                    await asyncio.get_event_loop().run_in_executor(
                        None, self.api.create_comment, post_id, body_text, item_id, self.jwt)
                elif kind == "post":
                    await asyncio.get_event_loop().run_in_executor(
                        None, self.api.create_comment, item_id, body_text, None, self.jwt)
            else:
                if not subject: await self.send("441 Subject required"); return
                meta = self.spool.load_meta(f"{self.domain}.{community}")
                cid = meta.get("community_id")
                if not cid:
                    cv = await asyncio.get_event_loop().run_in_executor(
                        None, self.api.get_community, community, self.jwt)
                    cid = cv.get("community",{}).get("id")
                if not cid: await self.send(f"441 Community '{community}' not found"); return
                await asyncio.get_event_loop().run_in_executor(
                    None, self.api.create_post, cid, subject, body_text, None, self.jwt)
            await self.send("240 Article posted successfully")
            self.poller.force_sync()
        except Exception as exc:
            log.error("Post failed: %s", exc)
            await self.send(f"441 Posting failed: {exc}")

    # --- Admin action handlers ---

    async def _handle_registration_action(self, subject, body_text):
        """Handle approve/deny in admin.registrations group."""
        cmd = parse_reg_command(body_text)
        if not cmd:
            await self.send("441 Unknown command. Use 'ok'/'approve' or 'deny'."); return

        ids = parse_reg_subject(subject)
        if not ids:
            await self.send("441 Cannot parse registration IDs from subject"); return

        ng = f"{self.domain}.admin.registrations"
        cmd_mid = self._store_command_article(ng, "", subject, body_text.strip())
        results = []
        try:
            approve = (cmd == "approve")
            await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(self.api.approve_registration, ids["app_id"], approve=approve, token=self.jwt))
            action_word = "approved" if approve else "denied"
            results.append(f"Registration application {ids['app_id']} {action_word}")
            log.info("Admin %s: registration %s %s (person=%d)",
                     self._auth_user, ids["app_id"], action_word, ids["person_id"])
        except Exception as exc:
            results.append(f"Error: {exc}")
            log.error("Registration action failed: %s", exc)

        self._generate_action_article(ng, cmd_mid, subject, results)
        self.poller.force_sync()
        await self.send("240 Action completed")

    async def _handle_report_action(self, subject, body_text):
        """Handle del/ban/lock in admin.reports group."""
        cmds = parse_mod_command(body_text)
        if not cmds:
            await self.send("441 Unknown command. Use 'del', 'ban', or 'del ban'."); return

        ids = parse_report_subject(subject)
        if not ids:
            await self.send("441 Cannot parse report IDs from subject"); return

        ng = f"{self.domain}.admin.reports"
        cmd_mid = self._store_command_article(ng, "", subject, body_text.strip())
        results = []

        # Delete post/comment
        if "del" in cmds:
            try:
                if ids["type"] == "post":
                    await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(self.api.remove_post, ids["target_id"], token=self.jwt))
                else:
                    await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(self.api.remove_comment, ids["target_id"], token=self.jwt))
                results.append(f"{ids['type'].title()} {ids['target_id']} deleted")
            except Exception as exc:
                results.append(f"Delete {ids['type']} {ids['target_id']}: FAILED - {exc}")

        # Ban author (instance-wide)
        if "ban" in cmds:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(self.api.ban_user, ids["author_id"], reason=f"Banned via report #{ids['report_id']}", token=self.jwt))
                results.append(f"User {ids['author_id']} banned (instance-wide)")
            except Exception as exc:
                results.append(f"Ban user {ids['author_id']}: FAILED - {exc}")

        # Lock (only for posts)
        if "lock" in cmds:
            if ids["type"] == "post":
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(self.api.lock_post, ids["target_id"], token=self.jwt))
                    results.append(f"Post {ids['target_id']} locked")
                except Exception as exc:
                    results.append(f"Lock post {ids['target_id']}: FAILED - {exc}")
            else:
                results.append(f"Lock: skipped (target is a comment, not a post)")

        # Resolve report
        try:
            if ids["type"] == "post":
                await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(self.api.resolve_post_report, ids["report_id"], token=self.jwt))
            else:
                await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(self.api.resolve_comment_report, ids["report_id"], token=self.jwt))
            results.append(f"Report {ids['report_id']} resolved")
        except Exception as exc:
            results.append(f"Resolve report {ids['report_id']}: FAILED - {exc}")

        log.info("Admin %s: report action on %s %s: %s",
                 self._auth_user, ids["type"], ids["target_id"], ", ".join(str(c) for c in cmds))

        self._generate_action_article(ng, cmd_mid, subject, results)
        self.poller.force_sync()
        await self.send("240 Action completed")

    async def _handle_inline_mod(self, newsgroup, subject, body_text, references, in_reply_to, cmds):
        """Handle inline moderation commands in normal groups."""
        reply_mid = in_reply_to or (references.split()[-1] if references else "")
        if not reply_mid:
            await self.send("441 No article referenced for moderation"); return

        parsed = parse_message_id(reply_mid)
        if not parsed:
            await self.send("441 Cannot parse referenced article"); return

        kind, item_id = parsed
        cmd_mid = self._store_command_article(newsgroup, reply_mid, subject, body_text.strip())
        results = []

        # Find author ID from spool
        target_art = self.spool.find_article_across_groups(reply_mid)
        author_id = None
        if target_art:
            author_id_str = target_art.get("extra_headers", {}).get("X-Lemmy-Author-Id")
            if author_id_str:
                author_id = int(author_id_str)

        # Delete
        if "del" in cmds:
            try:
                if kind == "post":
                    await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(self.api.remove_post, item_id, token=self.jwt))
                elif kind == "comment":
                    await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(self.api.remove_comment, item_id, token=self.jwt))
                results.append(f"{kind.title()} {item_id} deleted")
            except Exception as exc:
                results.append(f"Delete {kind} {item_id}: FAILED - {exc}")

        # Ban author (instance-wide)
        if "ban" in cmds:
            if author_id:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(self.api.ban_user, author_id, reason="Banned via inline moderation", token=self.jwt))
                    author_name = target_art.get("author", "unknown") if target_art else "unknown"
                    results.append(f"User {author_name} (id={author_id}) banned (instance-wide)")
                except Exception as exc:
                    results.append(f"Ban user {author_id}: FAILED - {exc}")
            else:
                results.append(f"Ban: FAILED - could not determine author ID")

        # Lock (find post if target is comment)
        if "lock" in cmds:
            try:
                if kind == "post":
                    post_id = item_id
                else:
                    post_id = self._find_post_id(item_id)
                if post_id:
                    await asyncio.get_event_loop().run_in_executor(
                        None, functools.partial(self.api.lock_post, post_id, token=self.jwt))
                    results.append(f"Post {post_id} locked")
                else:
                    results.append(f"Lock: FAILED - could not find parent post")
            except Exception as exc:
                results.append(f"Lock: FAILED - {exc}")

        log.info("Admin %s: inline mod in %s on %s-%d: %s",
                 self._auth_user, newsgroup, kind, item_id, ", ".join(str(c) for c in cmds))

        self._generate_action_article(newsgroup, cmd_mid, subject, results)
        self.poller.force_sync()
        await self.send("240 Action completed")

    def _find_post_id(self, comment_id):
        mid = make_message_id("comment", comment_id, self.domain)
        art = self.spool.find_article_across_groups(mid)
        if art:
            refs = art.get("references","").split()
            if refs:
                p = parse_message_id(refs[0])
                if p and p[0] == "post": return p[1]
        try:
            resp = self.api.get("/comment", {"id": comment_id}, token=self.jwt)
            return resp.get("comment_view",{}).get("comment",{}).get("post_id")
        except Exception:
            return None

    async def handle_newgroups(self, _): await self.send("231 List of new newsgroups follows", ".")
    async def handle_newnews(self, _): await self.send("230 List of new articles follows", ".")

    async def handle_next(self, _):
        if not self.current_group or self.current_article is None:
            await self.send("420 No current article"); return
        nums = self.spool.get_article_nums(self.current_group)
        try: idx = nums.index(self.current_article)
        except ValueError: await self.send("420 No current article"); return
        if idx+1 >= len(nums): await self.send("421 No next article"); return
        self.current_article = nums[idx+1]
        art = self.spool.get_article_by_num(self.current_group, self.current_article)
        await self.send(f"223 {art['number']} {art['message_id']}")

    async def handle_last(self, _):
        if not self.current_group or self.current_article is None:
            await self.send("420 No current article"); return
        nums = self.spool.get_article_nums(self.current_group)
        try: idx = nums.index(self.current_article)
        except ValueError: await self.send("420 No current article"); return
        if idx <= 0: await self.send("422 No previous article"); return
        self.current_article = nums[idx-1]
        art = self.spool.get_article_by_num(self.current_group, self.current_article)
        await self.send(f"223 {art['number']} {art['message_id']}")

    async def handle_date(self, _):
        await self.send(f"111 {datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")

    async def handle_help(self, _):
        await self.send("100 Help text follows",
                        "lemmy-nntp: NNTP bridge to Lemmy with local spool.",
                        "Commands: CAPABILITIES MODE LIST GROUP LISTGROUP ARTICLE HEAD",
                        "  BODY STAT OVER/XOVER NEXT LAST AUTHINFO POST DATE HELP QUIT",
                        "",
                        "Admin moderation (in any group, admin only):",
                        "  Reply with 'del' to delete, 'ban' to ban author,",
                        "  'lock' to lock thread, or combine: 'del ban'", ".")

    async def handle_quit(self, _):
        await self.send("205 Goodbye"); self.closed = True

    COMMANDS = {
        "CAPABILITIES": handle_capabilities, "MODE": handle_mode, "AUTHINFO": handle_authinfo,
        "LIST": handle_list, "GROUP": handle_group, "LISTGROUP": handle_listgroup,
        "ARTICLE": handle_article, "HEAD": handle_head, "BODY": handle_body,
        "STAT": handle_stat, "OVER": handle_over, "XOVER": handle_over,
        "POST": handle_post, "NEWGROUPS": handle_newgroups, "NEWNEWS": handle_newnews,
        "NEXT": handle_next, "LAST": handle_last, "DATE": handle_date,
        "HELP": handle_help, "QUIT": handle_quit,
    }

    # Commands allowed without authentication
    PUBLIC_COMMANDS = {"CAPABILITIES", "MODE", "AUTHINFO", "QUIT", "HELP"}

    # Commands allowed without auth when --no-auth is used (reading commands)
    READ_COMMANDS = {"LIST", "GROUP", "LISTGROUP", "ARTICLE", "HEAD", "BODY",
                     "STAT", "OVER", "XOVER", "NEXT", "LAST", "NEWGROUPS",
                     "NEWNEWS", "DATE"}

    # Commands that always require authentication
    WRITE_COMMANDS = {"POST"}

    async def run(self):
        peer = self.writer.get_extra_info("peername")
        log.info("Connection from %s", peer)
        if self.require_auth:
            await self.send("201 lemmy-nntp server ready (authentication required)")
        else:
            await self.send("200 lemmy-nntp server ready (posting allowed after auth)")
        while not self.closed:
            line = await self.recv()
            if line is None: break
            parts = line.split(None, 1)
            if not parts: continue
            cmd = parts[0].upper(); arg = parts[1] if len(parts) > 1 else None
            # Determine if auth is needed for this command
            if not self.jwt:
                if cmd in self.PUBLIC_COMMANDS:
                    pass  # always allowed
                elif not self.require_auth and cmd in self.READ_COMMANDS:
                    pass  # reading allowed without auth in --no-auth mode
                else:
                    await self.send("480 Authentication required")
                    continue
            handler = self.COMMANDS.get(cmd)
            if handler:
                try: await handler(self, arg)
                except Exception as exc:
                    log.exception("Error in %s", cmd); await self.send(f"503 Error: {exc}")
            else:
                await self.send(f"500 Unknown command: {cmd}")
        self.writer.close()
        log.info("Connection from %s closed", peer)


# ===== Server ================================================================

async def client_handler(api, spool, poller, reader, writer, require_auth=True):
    session = NNTPSession(api, spool, poller, reader, writer, require_auth=require_auth)
    try: await session.run()
    except Exception: log.exception("Session error")
    finally: writer.close()

def build_ssl_context(certfile, keyfile):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    log.info("SSL enabled: cert=%s key=%s", certfile, keyfile)
    return ctx

async def main():
    p = argparse.ArgumentParser(description="NNTP <-> Lemmy bridge with local spool")
    p.add_argument("--instance", required=True, help="Lemmy instance URL")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=1190, help="NNTP port (default: 1190)")
    p.add_argument("--spool", default="./spool", help="Spool directory (default: ./spool)")
    p.add_argument("--interval", type=int, default=900, help="Poll interval seconds (default: 900)")
    p.add_argument("--listing-type", default="Local", choices=["Local","All","Subscribed"])
    p.add_argument("--username", default=None, help="Lemmy username for authenticated polling")
    p.add_argument("--password", default=None, help="Lemmy password for authenticated polling")
    p.add_argument("--no-auth", action="store_true", dest="no_auth",
                   help="Allow reading articles without authentication (posting still requires auth)")
    p.add_argument("--ssl-cert", default=None, help="Path to SSL certificate (PEM) for NNTPS")
    p.add_argument("--ssl-key", default=None, help="Path to SSL private key for NNTPS")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    if args.debug: logging.getLogger().setLevel(logging.DEBUG)

    # Validate SSL args
    if (args.ssl_cert is None) != (args.ssl_key is None):
        p.error("--ssl-cert and --ssl-key must be used together")

    ssl_ctx = None
    if args.ssl_cert and args.ssl_key:
        ssl_ctx = build_ssl_context(args.ssl_cert, args.ssl_key)

    require_auth = not args.no_auth

    api = LemmyAPI(args.instance)
    spool = Spool(args.spool, api.domain)
    poller = Poller(api, spool, interval=args.interval, listing_type=args.listing_type)

    # Authenticate poller if credentials provided
    if args.username and args.password:
        try:
            token = api.login(args.username, args.password)
            poller.set_token(token)
            log.info("Poller authenticated as %s", args.username)
        except Exception as exc:
            log.error("Poller login failed: %s (continuing without auth)", exc)

    proto = "NNTPS" if ssl_ctx else "NNTP"
    log.info("Instance: %s  Spool: %s  Interval: %ds  Auth required: %s  SSL: %s",
             api.base, os.path.abspath(args.spool), args.interval, require_auth, bool(ssl_ctx))

    threading.Thread(target=poller.run_forever, daemon=True).start()
    log.info("Waiting for initial sync...")
    await asyncio.sleep(2)

    server = await asyncio.start_server(
        lambda r, w: client_handler(api, spool, poller, r, w, require_auth=require_auth),
        args.host, args.port, ssl=ssl_ctx)
    log.info("%s server listening on %s:%d", proto, args.host, args.port)
    async with server: await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
