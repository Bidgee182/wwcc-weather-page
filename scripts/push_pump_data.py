"""
Push changed pump data files to GitHub via the Contents API.
Atomic per-file, no git push required - never conflicts with other workflows.

Required env vars:
  GH_TOKEN           - GitHub token with contents:write
  GITHUB_REPOSITORY  - owner/repo
  CHANGED_FILES      - newline-delimited list of changed file paths
"""
import base64, json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone

token = os.environ["GH_TOKEN"]
repo  = os.environ["GITHUB_REPOSITORY"]
files = [f for f in os.environ.get("CHANGED_FILES", "").strip().split("\n")
         if f and os.path.exists(f)]

if not files:
    print("No changed files to push")
    sys.exit(0)

commit_msg = "Grundfos poll " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
api_base   = f"https://api.github.com/repos/{repo}/contents"
hdr_get    = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
hdr_put    = {**hdr_get, "Content-Type": "application/json"}

pushed = 0
failed = 0

for f in files:
    url = f"{api_base}/{f}"

    sha = ""
    try:
        req = urllib.request.Request(url, headers=hdr_get)
        with urllib.request.urlopen(req, timeout=30) as resp:
            sha = json.load(resp).get("sha", "")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sha = ""   # new file - PUT without sha creates it
            print(f"New file: {f}")
        else:
            print(f"SKIP {f}: GET failed (HTTP {e.code})")
            failed += 1
            continue
    except Exception as e:
        print(f"SKIP {f}: {e}")
        failed += 1
        continue

    with open(f, "rb") as fh:
        content = base64.b64encode(fh.read()).decode("ascii")

    body_dict = {"message": commit_msg, "content": content}
    if sha:                    # existing file - must include sha; new file - omit it
        body_dict["sha"] = sha
    payload = json.dumps(body_dict).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="PUT", headers=hdr_put)
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"Pushed: {f}")
            pushed += 1
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        print(f"WARNING: push failed for {f}: HTTP {e.code} -- {body}")
        failed += 1
    except Exception as e:
        print(f"WARNING: push failed for {f}: {e}")
        failed += 1

print(f"Result: {pushed} pushed, {failed} failed")
if failed:
    print("NOTE: failed files will retry next poll")
