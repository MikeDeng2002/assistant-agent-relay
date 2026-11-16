"""Participant-only web server for the study. Agent consoles run separately."""

import argparse
import json
import os
import re
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from bench import study, taskview

ACCESS = "study_access"
REFRESH = "study_refresh"
TEMPLATES = Path(__file__).parent
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Supabase:
    def __init__(self, url: str, key: str):
        endpoint = urlsplit(url)
        if (endpoint.scheme != "https" or not endpoint.hostname or not endpoint.hostname.endswith(".supabase.co")
                or endpoint.netloc != endpoint.hostname or endpoint.path not in ("", "/")
                or endpoint.query or endpoint.fragment or not key.startswith("sb_publishable_")):
            raise ValueError("Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY for your hosted project.")
        self.url = url.rstrip("/")
        self.key = key

    def request(self, path, *, token=None, body=None, raw=None, mime="application/json", download=False):
        headers = {"apikey": self.key}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None or raw is not None:
            headers["Content-Type"] = mime
        payload = json.dumps(body, allow_nan=False).encode() if body is not None else raw
        try:
            with urlopen(Request(self.url + path, data=payload, headers=headers), timeout=25) as response:
                data = response.read()
                return (data, response.headers.get("Content-Type")) if download else (json.loads(data) if data else None)
        except HTTPError as error:
            if error.code == 429:
                raise RequestError(429, "Too many requests. Wait and try again.") from None
            if path.startswith("/auth/v1/token"):
                if error.code in (400, 401, 403, 422):
                    raise RequestError(401, "Sign-in failed. Please check your email and password.") from None
            if error.code == 401:
                raise RequestError(401, "Your session expired. Sign in again.") from None
            if error.code == 403:
                raise RequestError(403, "This account cannot access that study data.") from None
            if error.code == 404 and download:
                raise RequestError(404, "Recording not found.") from None
            raise RequestError(502, "The study database could not complete the request.") from None
        except (URLError, TimeoutError):
            raise RequestError(503, "The study database is temporarily unavailable. Try again.") from None

    def member(self, token):
        rows = self.request("/rest/v1/study_members?select=user_id,pid,role", token=token)
        if not rows:
            raise RequestError(403, "This account has no study invitation. Contact the research team.")
        return rows[0]

    def rows(self, table, token, **filters):
        offset = 0
        while True:
            query = urlencode(dict(filters, limit=100, offset=offset))
            rows = self.request(f"/rest/v1/{table}?{query}", token=token)
            yield from rows
            if len(rows) < 100:
                break
            offset += len(rows)

    def save(self, token, pid, path, content):
        self.request("/rest/v1/rpc/study_save_result", token=token,
                     body={"participant": pid, "file_path": path, "payload": content})


class StudyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, origin, address, db, tasks_dir, task, agents):
        super().__init__(address, StudyHandler)
        self.origin = origin
        self.host = urlsplit(origin).netloc
        self.db = db
        self.spec = study.spec(task, tasks_dir)
        self.agents = agents


class StudyHandler(BaseHTTPRequestHandler):
    server: StudyServer
    timeout = 40

    def log_message(self, format, *args):
        pass

    def respond(self, status, body, mime, *, cookies=(), location=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Permissions-Policy", "microphone=(self)")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, data, **headers):
        self.respond(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", **headers)

    def cookie(self, name):
        secure = "; Secure" if self.server.origin.startswith("https://") else ""
        return f"{name}; Path=/; HttpOnly; SameSite=Strict{secure}"

    def cookie_value(self, name):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return cookies[name].value if name in cookies else ""

    def auth(self):
        token = self.cookie_value(ACCESS)
        if not token:
            raise RequestError(401, "Sign in to continue.")
        return token, self.server.db.member(token)

    def session(self, data):
        access = data["access_token"]
        member = self.server.db.member(access)
        self.json(200, {"member": member}, cookies=(
            self.cookie(f"{ACCESS}={access}; Max-Age={int(data['expires_in'])}"),
            self.cookie(f"{REFRESH}={data['refresh_token']}; Max-Age=604800"),
        ))

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, post):
        try:
            if self.headers.get("Host") != self.server.host:
                raise RequestError(403, "Open the study at its configured web address.")
            target = urlsplit(self.path)
            if not post:
                return self.get_route(target.path)
            if self.headers.get("Origin") != self.server.origin:
                raise RequestError(403, "Open the study at its configured web address.")
            if self.headers.get_content_type() != "application/json":
                raise RequestError(415, "Send a JSON request.")
            limit = 30_000_000 if target.path == "/api/study/audio" else 1_000_000
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > limit:
                raise RequestError(413, "This submission is too large.")
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            self.post_route(target.path, body)
        except RequestError as error:
            self.json(error.status, {"error": str(error)})
        except study.StudyError as error:
            self.json(400, {"error": str(error)})
        except (ValueError, UnicodeError, RecursionError):
            self.json(400, {"error": "Invalid study request."})

    def page(self, name):
        page = (TEMPLATES / name).read_text()
        if name == "study.html":
            page = page.replace("__CONSOLE_TOKEN__", "null").replace("__HOSTED_STUDY__", "true")
        self.respond(200, page.encode(), "text/html; charset=utf-8")

    def get_route(self, path):
        db = self.server.db
        if path == "/":
            return self.respond(303, b"", "text/plain", location="/study")
        if path == "/study-auth.js":
            return self.respond(200, (TEMPLATES / "study_auth.js").read_bytes(),
                                "text/javascript; charset=utf-8")
        if path in ("/study", "/research"):
            return self.page("study.html" if path == "/study" else "study_research.html")
        token, member = self.auth()
        if path == "/api/study/session":
            return self.json(200, {"member": member})
        if path == "/api/study/spec" and member["role"] == "participant":
            return self.json(200, self.server.spec)
        if path == "/api/study/agents" and member["role"] == "participant":
            return self.json(200, {"agents": self.server.agents,
                                   "now": datetime.now(UTC).isoformat()})
        if path == "/api/study/load" and member["role"] == "participant":
            pid = member["pid"]
            rows = db.rows("study_results", token, pid=f"eq.{pid}", select="path,content",
                           path="in.(progress.json,day1/submission.json,day2/submission.json)", order="path")
            return self.json(200, {"pid": pid, "files": {r["path"]: r["content"] for r in rows}})
        if path.startswith("/api/research/"):
            if member["role"] != "researcher":
                raise RequestError(403, "Research team access is required.")
            return self.research(token, path.removeprefix("/api/research/"))
        raise RequestError(404, "Page not found.")

    def results(self, token, pid):
        files = {}
        for row in self.server.db.rows("study_results", token, pid=f"eq.{pid}", select="path,content", order="path"):
            content = row["content"]
            if row["path"] == "scenario/changes.jsonl":
                content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in content)
            files[row["path"]] = content
        return {"pid": pid, "files": files}

    def research(self, token, path):
        db = self.server.db
        if path == "results":
            people = {}
            rows = db.rows("study_results", token, select="pid,path,stage:content->>stage,task:content->>task,updated_at",
                           path="in.(day1/submission.json,day2/submission.json,progress.json)", order="pid,path")
            for row in rows:
                entry = people.setdefault(row["pid"], {"pid": row["pid"], "submitted": [], "updated": ""})
                entry["updated"] = max(entry["updated"], row["updated_at"])
                if row["path"] == "progress.json":
                    entry["stage"] = row["stage"]
                    entry["task"] = row["task"]
                else:
                    entry["submitted"].append(int(row["path"][3]))
            return self.json(200, {"participants": list(people.values())})
        pid, _, item = path.partition("/")
        study.participant_dir(pid)
        if not item:
            return self.json(200, self.results(token, pid))
        if item == "history":
            rows = db.rows("study_result_history", token, pid=f"eq.{pid}", order="id",
                           select="id,path,at,before_value,after_value")
            return self.json(200, {"history": list(rows)})
        if item.startswith("audio/"):
            object_name = quote(f"{pid}/{item.removeprefix('audio/')}", safe="/")
            name = item.removeprefix("audio/")
            if not study.AUDIO_OBJECT.fullmatch(name):
                raise RequestError(404, "Recording not found.")
            data, mime = db.request(f"/storage/v1/object/authenticated/study-audio/{object_name}",
                                    token=token, download=True)
            return self.respond(200, data, mime if mime in study.AUDIO_TYPES else "application/octet-stream")
        raise RequestError(404, "Page not found.")

    def post_route(self, path, body):
        db = self.server.db
        if path == "/api/study/login":
            email, password = body.get("email"), body.get("password")
            if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password:
                raise RequestError(400, "Enter your email address and password.")
            return self.session(db.request("/auth/v1/token?grant_type=password",
                                           body={"email": email.strip(), "password": password}))
        if path == "/api/study/refresh":
            refresh = self.cookie_value(REFRESH)
            if not refresh:
                raise RequestError(401, "Sign in to continue.")
            return self.session(db.request("/auth/v1/token?grant_type=refresh_token", body={"refresh_token": refresh}))
        if path == "/api/study/logout":
            return self.json(200, {}, cookies=(self.cookie(f"{ACCESS}=; Max-Age=0"),
                                               self.cookie(f"{REFRESH}=; Max-Age=0")))
        token, member = self.auth()
        if member["role"] != "participant":
            raise RequestError(403, "A participant account is required to save a result.")
        pid = member["pid"]
        if body.get("pid", pid) != pid:
            raise RequestError(403, "This account is assigned to another participant ID.")
        if path == "/api/study/save":
            file_path, content = study.result_payload(body)
            if isinstance(content, list):
                content = [{"at": datetime.now(UTC).isoformat()} | record for record in content]
            db.save(token, pid, file_path, content)
            return self.json(200, {"saved": {"path": file_path}})
        if path == "/api/study/audio":
            name, mime, raw = study.audio_payload(body)
            file_path = f"audio/{name}-{uuid4().hex}{study.AUDIO_TYPES[mime]}"
            object_name = quote(f"{pid}/{file_path.removeprefix('audio/')}", safe="/")
            db.request(f"/storage/v1/object/study-audio/{object_name}", token=token, raw=raw, mime=mime)
            db.save(token, pid, file_path, {"mime": mime, "bytes": len(raw)})
            return self.json(200, {"saved": {"path": file_path}})
        if path == "/api/study/submit":
            day = body.get("day")
            if type(day) is not int or day not in (1, 2):
                raise RequestError(400, "Choose study day 1 or 2.")
            file_path = f"day{day}/submission.json"
            rows = db.rows("study_results", token, pid=f"eq.{pid}", select="path,content",
                           path=f"in.(progress.json,session.json,{file_path})", order="path")
            files = {row["path"]: row["content"] for row in rows}
            if file_path in files:
                return self.json(200, {"submitted": files[file_path]})
            progress = files.get("progress.json", {})
            if progress.get("stage") != "done" or progress.get("day") != day or "session.json" not in files:
                raise RequestError(409, "Finish saving the study day before submitting.")
            db.save(token, pid, file_path, {"day": day, "submitted_at": datetime.now(UTC).isoformat()})
            result = next(db.rows("study_results", token, pid=f"eq.{pid}", select="content", path=f"eq.{file_path}"))
            return self.json(200, {"submitted": result["content"]})
        raise RequestError(404, "Page not found.")


def main():
    parser = argparse.ArgumentParser(description="Run the hosted study site without the local agent console.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--origin", default=os.environ.get("STUDY_ORIGIN"))
    parser.add_argument("--task", default=os.environ.get("STUDY_TASK", "Restaurant"))
    parser.add_argument("--tasks-dir", type=Path, default=Path(os.environ.get("STUDY_TASKS_DIR", taskview.TASKS_DIR)))
    parser.add_argument("--agents", default=os.environ.get("STUDY_AGENTS", "instinct:imessage"))
    args = parser.parse_args()
    origin = args.origin or f"http://127.0.0.1:{args.port}"
    address = urlsplit(origin)
    if (origin != f"{address.scheme}://{address.netloc}" or not address.hostname
            or (address.scheme != "https"
                and (args.bind != "127.0.0.1" or address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost")))):
        parser.error("Set STUDY_ORIGIN to the site's HTTPS origin, without a path.")
    agents = []
    for value in args.agents.split(","):
        name, _, channel = value.strip().partition(":")
        if not name or not re.fullmatch(study.AGENT, name):
            parser.error("Set STUDY_AGENTS to comma-separated names, optionally followed by :channel.")
        agents.append({"name": name, "channel": channel})
    try:
        db = Supabase(os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""))
        server = StudyServer(origin, (args.bind, args.port), db, args.tasks_dir, args.task, agents)
    except (ValueError, study.StudyError) as error:
        parser.error(str(error))
    print(f"Study website: {origin}/study", flush=True)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
