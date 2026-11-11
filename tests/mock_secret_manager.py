#!/usr/bin/env python3
"""Local stand-in for the Scaleway Secret Manager API (MAIR-617 tests).

Serves the four calls the role k8s_instance_secrets makes, on 127.0.0.1,
from a JSON state file it rewrites after every write:

    GET  /secret-manager/v1beta1/regions/<r>/secrets?project_id=&path=
    GET  /secret-manager/v1beta1/regions/<r>/secrets/<id>/versions
    GET  /secret-manager/v1beta1/regions/<r>/secrets/<id>/versions/<rev>/access
    POST /secret-manager/v1beta1/regions/<r>/secrets/<id>/versions

State: {"secrets": [{"id", "name", "path", "project_id",
                     "versions": [{"revision", "data" (base64), "description"}]}]}
Requests without the expected X-Auth-Token get 401, like the real API.

    python3 tests/mock_secret_manager.py --port 18617 --token t --state state.json
"""

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PREFIX = r"^/secret-manager/v1beta1/regions/[a-z-]+/secrets"
LOCK = threading.Lock()


def make_handler(token, state_path):
    def load():
        with open(state_path) as f:
            return json.load(f)

    def save(state):
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if self.headers.get("X-Auth-Token") != token:
                self.reply(401, {"message": "authentication is denied"})
                return False
            return True

        def secret(self, state, secret_id):
            return next((s for s in state["secrets"] if s["id"] == secret_id), None)

        def do_GET(self):
            if not self.authorized():
                return
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            with LOCK:
                state = load()
            if re.match(PREFIX + r"$", url.path):
                found = [
                    {k: s[k] for k in ("id", "name", "path", "project_id")}
                    for s in state["secrets"]
                    if s["project_id"] == query.get("project_id")
                    and ("path" not in query or s["path"] == query["path"])
                ]
                return self.reply(200, {"secrets": found, "total_count": len(found)})
            m = re.match(PREFIX + r"/([^/]+)/versions$", url.path)
            if m:
                secret = self.secret(state, m.group(1))
                if not secret:
                    return self.reply(404, {"message": "resource is not found"})
                versions = [
                    {"revision": v["revision"], "description": v["description"], "status": "enabled"}
                    for v in secret["versions"]
                ]
                return self.reply(200, {"versions": versions, "total_count": len(versions)})
            m = re.match(PREFIX + r"/([^/]+)/versions/([^/]+)/access$", url.path)
            if m:
                secret = self.secret(state, m.group(1))
                versions = secret["versions"] if secret else []
                revision = m.group(2)
                if revision in ("latest", "latest_enabled"):
                    version = versions[-1] if versions else None
                else:
                    version = next((v for v in versions if str(v["revision"]) == revision), None)
                if not version:
                    return self.reply(404, {"message": "resource is not found"})
                return self.reply(200, {"secret_id": secret["id"], "revision": version["revision"], "data": version["data"]})
            self.reply(404, {"message": "unknown route"})

        def do_POST(self):
            if not self.authorized():
                return
            url = urlparse(self.path)
            m = re.match(PREFIX + r"/([^/]+)/versions$", url.path)
            if not m:
                return self.reply(404, {"message": "unknown route"})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with LOCK:
                state = load()
                secret = self.secret(state, m.group(1))
                if not secret:
                    return self.reply(404, {"message": "resource is not found"})
                revision = len(secret["versions"]) + 1
                secret["versions"].append(
                    {"revision": revision, "data": body["data"], "description": body.get("description", "")}
                )
                save(state)
            self.reply(200, {"secret_id": secret["id"], "revision": revision, "status": "enabled", "latest": True})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.token, args.state))
    server.serve_forever()


if __name__ == "__main__":
    main()
