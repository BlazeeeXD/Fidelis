"""
Simulates a GitHub webhook delivery against your local Gateway/Webhook
service, so you can test end-to-end without exposing anything to the
internet yet.

Usage:
    export GITHUB_WEBHOOK_SECRET=devsecret
    python sign_and_send.py --url http://localhost:8000/webhook/github
"""
import argparse
import hashlib
import hmac
import os
import urllib.request
import uuid
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://localhost:8000/webhook/github")
parser.add_argument("--payload", default="pull_request_opened.json")
args = parser.parse_args()

secret = os.environ["GITHUB_WEBHOOK_SECRET"]
payload_path = Path(__file__).parent / args.payload
body = payload_path.read_bytes()

signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

req = urllib.request.Request(
    args.url,
    data=body,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": signature,
    },
)

with urllib.request.urlopen(req) as resp:
    print(resp.status, resp.read().decode())
