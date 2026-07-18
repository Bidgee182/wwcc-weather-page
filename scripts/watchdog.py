#!/usr/bin/env python3
"""Workflow watchdog: auto-retry transient failures, email the admins.

Runs from .github/workflows/watchdog.yml whenever a watched workflow finishes.
Deliberately read-only on the repository - its only powers are re-running
failed jobs and sending email. It never edits code or data.

Decision table:
  success on attempt > 1      -> email "self-healed" (states what failed before)
  failure, attempt < 3, safe  -> re-run failed jobs silently (email comes later,
                                 either self-healed or needs-attention)
  failure, unsafe to retry    -> email "sent but housekeeping failed" (a re-run
                                 could double-send the email, so a human decides)
  failure, attempts exhausted -> email "needs attention"

"Unsafe" = the failed step is a git commit/housekeeping step in an email
workflow, meaning the email itself already went out; re-running the job would
run the send again from scratch and could double-send to the full list.
"""
import json
import os
import urllib.request

REPO = os.environ["GH_REPO"]
TOKEN = os.environ["GH_TOKEN"]
RUN_ID = os.environ["WF_RUN_ID"]
RUN_ATTEMPT = int(os.environ["WF_RUN_ATTEMPT"])
CONCLUSION = os.environ["WF_CONCLUSION"]
WF_NAME = os.environ["WF_NAME"]
RUN_URL = os.environ["WF_RUN_URL"]
WORKFLOW_ID = os.environ.get("WF_WORKFLOW_ID", "")
MAX_ATTEMPTS = 3

# Data-only workflow: a re-run can never send anything, always safe to retry
ALWAYS_SAFE = {"FarmBot Tank Poll"}


def gh(path, method="GET", body=None):
    if body is not None:
        data = json.dumps(body).encode()
    else:
        data = b"" if method == "POST" else None
    req = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=data,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "wwcc-watchdog",
        },
    )
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return json.loads(body) if body else {}


def failed_steps(attempt=None):
    """Names of failed jobs/steps, optionally for a specific run attempt."""
    try:
        path = ("/repos/%s/actions/runs/%s/attempts/%d/jobs" % (REPO, RUN_ID, attempt)
                if attempt else
                "/repos/%s/actions/runs/%s/jobs?filter=latest" % (REPO, RUN_ID))
        out = []
        for j in gh(path).get("jobs", []):
            if j.get("conclusion") == "failure":
                steps = [s["name"] for s in j.get("steps", [])
                         if s.get("conclusion") == "failure"]
                out.append(j["name"] + (": " + ", ".join(steps) if steps else ""))
        return "; ".join(out) or "unknown step"
    except Exception as e:  # noqa: BLE001 - report what we can
        return "could not read job details (%s)" % e


def retry_is_safe(steps_desc):
    """A commit-step failure in an email workflow means the send already
    happened; re-running the whole job would send the email again."""
    if WF_NAME in ALWAYS_SAFE:
        return True
    return "Commit" not in steps_desc


def admin_emails():
    try:
        with open("data/admin_users.json", encoding="utf-8") as f:
            return [u["email"] for u in json.load(f) if u.get("email")]
    except Exception:
        return []


def send_email(subject, body_text):
    key = os.environ.get("SENDGRID_API_KEY", "")
    sender = os.environ.get("EMAIL_FROM", "")
    tos = admin_emails()
    if not key or not sender or not tos:
        print("email skipped (missing key, from-address or recipients)")
        return
    payload = {
        "personalizations": [{"to": [{"email": t} for t in tos]}],
        "from": {"email": sender, "name": "WWCC Weather Watchdog"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body_text}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        print("sendgrid status", r.status)


def main():
    print("watchdog: %s attempt %d concluded %s" % (WF_NAME, RUN_ATTEMPT, CONCLUSION))

    if CONCLUSION == "success":
        if RUN_ATTEMPT > 1:
            was = failed_steps(attempt=RUN_ATTEMPT - 1)
            send_email(
                "[WWCC] Self-healed: %s recovered on retry" % WF_NAME,
                'The workflow "%s" failed and was retried automatically by the '
                "watchdog. The retry succeeded - no action needed.\n\n"
                "What failed on the first try: %s\n"
                "This was a transient error (usually a git push race or an API "
                "blip), attempt %d succeeded.\n\nRun: %s\n"
                % (WF_NAME, was, RUN_ATTEMPT, RUN_URL))
        return

    if CONCLUSION == "cancelled":
        # Under the shared repo-writes concurrency group, a QUEUED run gets
        # cancelled when a newer run queues behind it. A dropped email run must
        # be re-dispatched; a dropped poll is covered by the next schedule.
        # A run a person cancelled mid-execution (it has executed steps) is
        # left alone - overriding a deliberate cancel would be wrong.
        if WF_NAME in ALWAYS_SAFE:
            print("cancelled poll - next scheduled poll covers it")
            return
        try:
            jobs = gh("/repos/%s/actions/runs/%s/jobs?filter=latest"
                      % (REPO, RUN_ID)).get("jobs", [])
        except Exception:  # noqa: BLE001
            jobs = []
        if any(j.get("steps") for j in jobs):
            print("cancelled mid-run (likely by a person) - leaving it alone")
            return
        if WORKFLOW_ID:
            gh("/repos/%s/actions/workflows/%s/dispatches" % (REPO, WORKFLOW_ID),
               method="POST", body={"ref": "main"})
            print("re-dispatched: run was cancelled from the queue before starting")
        return

    if CONCLUSION != "failure":
        return  # skipped / neutral: not the watchdog's business

    steps = failed_steps()

    if not retry_is_safe(steps):
        send_email(
            "[WWCC] Check needed: %s - email sent, housekeeping failed" % WF_NAME,
            'The workflow "%s" failed at: %s\n\n'
            "The failure is in a git commit step that runs AFTER the email is "
            "sent, so the email itself most likely went out fine. The watchdog "
            "did NOT auto-retry, because re-running this job would send the "
            "email again to the full list.\n\n"
            "What to check: the sent-guard or log file for this run may not "
            "have been committed. If it was a weekly/monthly send, confirm the "
            "guard file updated, or the scheduler could double-send later.\n\n"
            "Logs: %s\n" % (WF_NAME, steps, RUN_URL))
        return

    if RUN_ATTEMPT < MAX_ATTEMPTS:
        try:
            gh("/repos/%s/actions/runs/%s/rerun-failed-jobs" % (REPO, RUN_ID),
               method="POST")
            print("re-run requested (next attempt %d)" % (RUN_ATTEMPT + 1))
            # No email yet: the outcome email arrives when the retry finishes,
            # either "self-healed" or "needs attention"
            return
        except Exception as e:  # noqa: BLE001
            print("re-run request failed:", e)

    send_email(
        "[WWCC] NEEDS ATTENTION: %s failed %d time%s" %
        (WF_NAME, RUN_ATTEMPT, "" if RUN_ATTEMPT == 1 else "s"),
        'The workflow "%s" failed at: %s\n\n'
        "Automatic retries are exhausted, so this looks like a real fault "
        "rather than a transient one, and needs a human.\n\n"
        "Logs: %s\n\n"
        "The admin page Workflows tab has the run history and manual trigger "
        "buttons.\n" % (WF_NAME, steps, RUN_URL))


if __name__ == "__main__":
    main()
