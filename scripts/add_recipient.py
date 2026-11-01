#!/usr/bin/env python3
"""Add (or ensure) an email address in an encrypted recipients stream.

Decrypts data/email_recipients.enc.json with the RECIPIENTS_KEY env var, appends
the given address to the given stream (default board_to), and re-encrypts in the
SAME format the admin dashboard writes ({v, iv-hex, ct-base64}; AES-256-GCM with a
PBKDF2-HMAC-SHA256 key over salt 'wwcc-recipients-v1', 100000 iters). It prints
only per-stream COUNTS - never the addresses or the key - so nothing sensitive
lands in the Actions log.

If the target stream is empty in the encrypted file (e.g. the list is still coming
from the legacy GitHub-secret fallback), it is SEEDED from --seed first, so the
existing recipients are preserved rather than replaced.

Usage:
  python scripts/add_recipient.py --email a@b.com [--stream board_to] [--seed "x@y,z@w"]
"""
import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SALT = b'wwcc-recipients-v1'
PATH = Path(__file__).resolve().parent.parent / 'data' / 'email_recipients.enc.json'
STREAM_KEYS = ['gk_to', 'gk_cc', 'gk_bcc',
               'committee_to', 'committee_cc', 'committee_bcc',
               'lake_to', 'lake_cc', 'lake_bcc',
               'board_to', 'board_cc', 'board_bcc']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--email', required=True)
    ap.add_argument('--stream', default='board_to')
    ap.add_argument('--seed', default='')
    ap.add_argument('--by', default='github-action')
    args = ap.parse_args()

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    passphrase = os.environ.get('RECIPIENTS_KEY', '')
    if not passphrase:
        sys.exit('RECIPIENTS_KEY not set - cannot edit the encrypted recipients file')
    if args.stream not in STREAM_KEYS:
        sys.exit(f'Unknown stream: {args.stream}')
    if not PATH.exists():
        sys.exit(f'{PATH} not found')

    kd = hashlib.pbkdf2_hmac('sha256', passphrase.encode(), SALT, 100000, 32)
    blob = json.loads(PATH.read_text(encoding='utf-8'))
    try:
        pt = AESGCM(kd).decrypt(bytes.fromhex(blob['iv']),
                                base64.b64decode(blob['ct']), None)
    except Exception:
        sys.exit('Could not decrypt - RECIPIENTS_KEY is wrong for this file')

    data = json.loads(pt.decode('utf-8')) or {}
    streams = data.get('streams') or {}

    current = [a.strip() for a in (streams.get(args.stream) or []) if a and a.strip()]
    if not current and args.seed:
        current = [a.strip() for a in args.seed.split(',') if a.strip()]

    email = args.email.strip()
    if any(a.lower() == email.lower() for a in current):
        print(f'{email} already present in {args.stream} - no change ({args.stream}:{len(current)})')
        return

    current.append(email)
    streams[args.stream] = current
    data['streams'] = streams

    summary = ' '.join(f'{k}:{len(streams.get(k) or [])}' for k in STREAM_KEYS)
    log = data.get('change_log') or []
    log.append({'ts': datetime.now(timezone.utc).isoformat(),
                'by': args.by, 'summary': summary})
    data['change_log'] = log[-100:]
    data['updated'] = datetime.now(timezone.utc).isoformat()

    iv = os.urandom(12)
    ct = AESGCM(kd).encrypt(iv, json.dumps(data).encode('utf-8'), None)
    out = {'v': 1, 'iv': iv.hex(), 'ct': base64.b64encode(ct).decode('ascii')}
    PATH.write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(f'Added 1 address to {args.stream}. Stream counts -> {summary}')


if __name__ == '__main__':
    main()
