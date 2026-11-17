"""Shared game library, stored in the repo's GitHub Releases.

Why Releases and not commits: GitHub blocks files over 100 MiB in git, and every
game committed would stay in the repo history forever. Release files can be up to
2 GiB each, don't count toward the repo size, and aren't downloaded by `git clone`.

All games live as files on ONE release with the tag `game-library`.
  - Upload a game with "Share to library" ticked -> it's uploaded there in the background.
  - In any new Codespace, the site lists the library -> press ⬇ to fetch a game.

Auth: Codespaces sets GITHUB_TOKEN (and GITHUB_REPOSITORY) automatically, scoped to
the repo the Codespace was made from. Nothing to configure.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TAG = "game-library"
CACHE_SECS = 15                         # how often to re-check GitHub for games others shared
MAX_ASSET = 2 * 1024 ** 3 - 1          # GitHub's per-file release limit (2 GiB)
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
UPLOADS = os.environ.get("GITHUB_UPLOADS_URL", "https://uploads.github.com").rstrip("/")


def _repo_from_git():
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent, timeout=5).stdout.strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Library:
    def __init__(self, games_dir: Path, install_fn):
        self.games_dir = games_dir
        self.install_fn = install_fn            # (tmp_path, name) -> None, raises ValueError
        self.repo = os.environ.get("LIBRARY_REPO") or os.environ.get("GITHUB_REPOSITORY") or _repo_from_git()
        self.token = os.environ.get("LIBRARY_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.outbox = games_dir / ".outbox"
        self.jobs = {}                          # name -> status text shown on the page
        self.lock = threading.Lock()
        self._release = None
        self._assets = None                     # cached list (re-checked every CACHE_SECS)
        self._assets_at = 0.0

    # ---------- plumbing ----------
    @property
    def problem(self):
        if not self.repo:
            return "Can't tell which GitHub repo this is (no GITHUB_REPOSITORY and no git remote)."
        if not self.token:
            return "No GITHUB_TOKEN. Codespaces sets this automatically; outside Codespaces set LIBRARY_TOKEN."
        return None

    def _req(self, method, url, data=None, headers=None, timeout=60):
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "game-runner"}
        h.update(headers or {})
        body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
        if isinstance(data, (dict, list)):
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"message": raw[:200].decode(errors="replace")}

    def _api(self, path):
        return f"{API}/repos/{self.repo}{path}"

    def _canonical_repo(self):
        """If the repo was renamed (e.g. CalculusXL -> LinuxEmulator), GitHub redirects the old
        name for reads but uploads to the old name can fail, so switch to the current name."""
        if getattr(self, "_checked_name", False):
            return
        code, info = self._req("GET", f"{API}/repos/{self.repo}")   # GET follows the rename redirect
        if code == 200 and info and info.get("full_name"):
            self.repo = info["full_name"]
        self._checked_name = True

    def release(self):
        if self._release:
            return self._release
        self._canonical_repo()
        code, rel = self._req("GET", self._api(f"/releases/tags/{TAG}"))
        if code == 404:
            code, rel = self._req("POST", self._api("/releases"), {
                "tag_name": TAG, "name": "🎮 Game Library",
                "body": "Games shared from the EXE Runner site. Managed automatically; "
                        "don't rename this release.",
            })
        if code not in (200, 201):
            raise RuntimeError(f"GitHub said {code}: {rel.get('message') if rel else ''}")
        self._release = rel
        return rel

    # ---------- listing ----------
    def assets(self, refresh=False):
        if self._assets is not None and not refresh and time.time() - self._assets_at < CACHE_SECS:
            return self._assets
        rel = self.release()
        out, page = [], 1
        while True:
            code, items = self._req("GET", self._api(f"/releases/{rel['id']}/assets?per_page=100&page={page}"))
            if code != 200:
                raise RuntimeError(f"Listing library failed ({code})")
            out += items
            if len(items) < 100:
                break
            page += 1
        self._assets, self._assets_at = out, time.time()
        return out

    def listing(self, refresh=False):
        if self.problem:
            return {"enabled": False, "problem": self.problem, "repo": self.repo, "games": [], "jobs": self.jobs}
        try:
            games = [{"id": a["id"], "name": a["name"], "size_mb": round(a["size"] / 1e6, 1),
                      "local": (self.games_dir / Path(a["name"]).stem).exists()}
                     for a in self.assets(refresh)]
        except Exception as e:
            return {"enabled": False, "problem": str(e), "repo": self.repo, "games": [], "jobs": self.jobs}
        return {"enabled": True, "repo": self.repo, "games": games, "jobs": self.jobs}

    # ---------- upload (background) ----------
    def share(self, path: Path, name: str):
        """Called right after a game is uploaded to the site. `path` must stay readable,
        so we hard-link (or copy) it into an outbox first; the install step can then
        move/extract/delete the original freely."""
        if self.problem:
            self.jobs[name] = "not shared: " + self.problem
            return
        if path.stat().st_size > MAX_ASSET:
            self.jobs[name] = "not shared: over GitHub's 2 GB per-file limit"
            return
        self.outbox.mkdir(exist_ok=True)
        staged = self.outbox / name
        staged.unlink(missing_ok=True)
        try:
            os.link(path, staged)
        except OSError:
            shutil.copy2(path, staged)
        self.jobs[name] = "☁ uploading to library…"
        threading.Thread(target=self._upload, args=(staged, name), daemon=True).start()

    def _upload(self, staged: Path, name: str):
        try:
            with self.lock:
                if any(a["name"] == name for a in self.assets(refresh=True)):
                    self.jobs[name] = "☁ already in library"
                    return
                rel = self.release()
                size = staged.stat().st_size
                url = f"{UPLOADS}/repos/{self.repo}/releases/{rel['id']}/assets?name={urllib.parse.quote(name)}"
                with open(staged, "rb") as f:
                    code, res = self._req("POST", url, data=f, timeout=3600, headers={
                        "Content-Type": "application/octet-stream", "Content-Length": str(size)})
                if code not in (200, 201):
                    raise RuntimeError(f"GitHub said {code}: {(res or {}).get('message', '')}")
                self._assets = None
                self.jobs[name] = "☁ shared to library"
        except Exception as e:
            self.jobs[name] = f"⚠ library upload failed: {e}"
        finally:
            staged.unlink(missing_ok=True)

    # ---------- download (background) ----------
    def fetch(self, asset_id: int):
        a = next((x for x in self.assets() if x["id"] == asset_id), None)
        if not a:
            raise ValueError("Not in library")
        if self.jobs.get(a["name"], "").startswith("⬇"):
            return a["name"]
        self.jobs[a["name"]] = "⬇ downloading 0%"
        threading.Thread(target=self._download, args=(a,), daemon=True).start()
        return a["name"]

    def _download(self, a):
        name = a["name"]
        tmp = self.games_dir / ".incoming" / f"lib-{a['id']}.part"
        tmp.parent.mkdir(exist_ok=True)
        try:
            # Ask the API for the file; it answers with a redirect to GitHub's file storage,
            # which must be fetched WITHOUT our token (it rejects a second auth header).
            opener = urllib.request.build_opener(_NoRedirect)
            req = urllib.request.Request(self._api(f"/releases/assets/{a['id']}"), headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/octet-stream",
                "User-Agent": "game-runner"})
            try:
                resp = opener.open(req, timeout=60)          # some servers send the bytes directly
            except urllib.error.HTTPError as e:
                if e.code not in (301, 302, 303, 307, 308):
                    raise
                resp = urllib.request.urlopen(urllib.request.Request(
                    urllib.parse.urljoin(req.full_url, e.headers["Location"]), headers={"User-Agent": "game-runner"}), timeout=60)
            total = int(resp.headers.get("Content-Length") or a["size"] or 0)
            done = 0
            with resp, open(tmp, "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        self.jobs[name] = f"⬇ downloading {done * 100 // total}%"
            self.jobs[name] = "📦 installing…"
            self.install_fn(tmp, name)
            self.jobs[name] = "✅ downloaded, ready to play"
        except Exception as e:
            self.jobs[name] = f"⚠ download failed: {e}"
        finally:
            tmp.unlink(missing_ok=True)

    def remove(self, asset_id: int):
        a = next((x for x in (self._assets or []) if x["id"] == asset_id), None)
        if a:
            self.jobs.pop(a["name"], None)
        code, res = self._req("DELETE", self._api(f"/releases/assets/{asset_id}"))
        self._assets = None
        if code not in (200, 204):
            raise RuntimeError(f"GitHub said {code}: {(res or {}).get('message', '')}")
