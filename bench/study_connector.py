"""Mac client for pairing with Study and running its approved agent actions."""

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

import yaml

from bench.config import LOCAL_AGENTS_FILE, PRIVATE_DIR, ROOT, SITES, ConfigError, load_agents, normalize_handle
from bench.console import Console, parse

APP = Path.home() / "Applications" / "Study Connector.app"
STATE = PRIVATE_DIR / "study-connector"
CODE = re.compile(r"[A-Za-z0-9_-]{24}")
SETUP = {
    "slack": "slack-login", "muse": "muse-login", "hark": "hark-login",
    "wajo": "wajo-login", "telegram": "telegram-login", "whatsapp": "whatsapp-link",
}
SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


class ConnectorError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(origin, action, body, credential=None):
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    req = Request(f"{origin}/api/connector/{action}", json.dumps(body).encode(), headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=10) as reply:
            return json.load(reply)
    except HTTPError as error:
        try:
            message = json.load(error).get("error")
        except (ValueError, AttributeError):
            message = None
        raise ConnectorError(message or "Study rejected the connector request.", error.code) from None
    except (URLError, TimeoutError):
        raise ConnectorError("Study is unreachable. Retrying…") from None


def credential_file(origin):
    from bench.study_devices import digest

    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STATE / f"{digest(origin)}.json"


def store(path, data, write=json.dump):
    temp = path.with_name(uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            write(data, f)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def agent_channels():
    return {a.name: a.channel for a in load_agents().values()}


def terminal_command(args):
    command = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; cd ' + shlex.quote(str(ROOT)) + " && " + shlex.join(args)
    subprocess.run(["osascript", "-e", '''on run argv
        tell application "Terminal"
            activate
            do script (item 1 of argv)
        end tell
    end run''', command], check=True, capture_output=True)


def address_dialog(name, description):
    result = subprocess.run(["osascript", "-e", '''on run argv
        set answer to display dialog (item 1 of argv) default answer "" buttons {"Cancel", "Save"} default button "Save" with title "Study Connector"
        return text returned of answer
    end run''', f"Enter the agent's {description} for {name}."], capture_output=True, text=True)
    if result.returncode:
        raise ConnectorError("Address entry cancelled. Open channel setup to try again.")
    return result.stdout.strip()


def configure_agent(name, channel):
    """Collect the address on the Mac and keep it in the ignored local config."""
    descriptions = {
        "imessage": "iMessage phone number with country code or email",
        "whatsapp": "WhatsApp phone number with country code, bot ID, or LID",
        "slack": "Dot’s Slack member ID",
        "telegram": "Telegram username",
    }
    try:
        raw = SITES[channel] if channel in SITES else address_dialog(name, descriptions[channel])
        handle = normalize_handle(raw, channel)
    except ConfigError as error:
        raise ConnectorError(str(error)) from None
    local = yaml.safe_load(LOCAL_AGENTS_FILE.read_text()) or {} if LOCAL_AGENTS_FILE.exists() else {}
    entries = local.setdefault("agents", {})
    previous = entries.get(name) or {}
    changed = previous.get("channel") != channel or previous.get("handle") != handle
    entries[name] = previous | {"channel": channel, "handle": handle}
    store(LOCAL_AGENTS_FILE, local, yaml.safe_dump)
    print(f"Saved {name} on this Mac.")
    if channel == "imessage" and changed:
        if subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True).returncode:
            subprocess.run(["xcode-select", "--install"], capture_output=True)
            raise ConnectorError("Finish installing Apple Command Line Tools in the Mac dialog, then open channel setup again.")
        subprocess.run([str(ROOT / "reader/install.sh")], check=True)
    else:
        setup_channel(channel)


def setup_channel(channel):
    if channel == "imessage":
        subprocess.run(["open", "-a", "Messages"], check=True)
        if subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True).returncode:
            subprocess.run(["xcode-select", "--install"], capture_output=True)
            raise ConnectorError("Finish installing Apple Command Line Tools in the Mac dialog, then open channel setup again.")
        domain = f"gui/{os.getuid()}/local.personal-agent-benchmark.bench-reader"
        if subprocess.run(["launchctl", "print", domain], capture_output=True).returncode:
            terminal_command([str(ROOT / "reader/install.sh")])
        subprocess.run(["open", SETTINGS], check=True)
        return
    command = SETUP[channel]
    python = ROOT / ".venv/bin/python"
    prefix = [str(python) if python.exists() else sys.executable, "-m", "bench.cli"]
    if channel in {"hark", "muse", "wajo"}:
        try:
            result = subprocess.run(prefix + [command], capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            raise ConnectorError("Sign-in timed out. Finish signing in, then click Open channel setup again.") from None
        if result.returncode:
            raise ConnectorError("Could not complete sign-in. Check the dedicated Chrome window and try Open channel setup again.")
        return
    if channel == "whatsapp":
        from bench.channels.whatsapp import BRIDGE_DIR

        if not (BRIDGE_DIR / "bench-whatsapp").exists():
            if not shutil.which("go"):
                subprocess.run(["open", "https://go.dev/dl/"], check=True)
                raise ConnectorError("Install Go using the macOS installer that just opened, then open channel setup again. WhatsApp needs it to build the connection program.")
            command_line = shlex.join(prefix + ["whatsapp-install"]) + " && " + shlex.join(prefix + [command])
            terminal_command(["/bin/sh", "-c", command_line])
            return
    terminal_command(prefix + [command])


def replies_for(snapshot, job):
    if not job["sent_at"] or not snapshot:
        return []
    sent = parse(job["sent_at"])
    for agent in snapshot["agents"]:
        if agent["name"] != job["agent"] or agent.get("channel") != job["channel"]:
            continue
        out = []
        for item in agent["items"]:
            item_id = item.get("id") or item.get("guid")
            if (item["type"] == "message" and not item.get("from_me") and not item.get("service")
                    and item_id is not None and item.get("text") and parse(item["date"]) >= sent):
                out.append({"id": str(item_id)[:160], "at": item["date"], "text": item["text"][:8_000]})
        return out[-60:]
    return []


def run_job(origin, credential, job):
    def report(state, *, sent_at=None, replies=None, error=None):
        body = {"id": job["id"], "state": state, "sent_at": sent_at, "replies": replies or [], "error": error}
        for attempt in range(3):
            try:
                return request(origin, "report", body, credential)["accepted"]
            except ConnectorError as exc:
                if exc.status or attempt == 2:
                    raise
                time.sleep(1)

    agent = load_agents().get(job["agent"])
    configured = agent and agent.channel == job["channel"]
    if job["kind"] == "setup":
        try:
            if configured:
                setup_channel(agent.channel)
            elif job["channel"] in {"hark", "muse", "wajo"}:
                configure_agent(job["agent"], job["channel"])
            else:
                terminal_command([sys.executable, "-m", "bench.study_connector", "--origin", origin,
                                  "--configure-agent", job["agent"], "--channel", job["channel"]])
        except ConnectorError as error:
            report("failed", error=str(error))
        except (OSError, subprocess.SubprocessError):
            report("failed", error="Could not open setup. Open the channel on your Mac and try again.")
        else:
            report("done")
        return

    if not configured:
        report("failed", error="This agent is missing from the Mac’s configuration. Open channel setup and try again.")
        return

    console = Console({agent.name: agent}, snapshot_links=False, jobs_dir=STATE / "jobs")
    console.start()
    try:
        local = console.submit({
            "agents": [agent.name], "text": job["prompt"], "mode": "run",
            "task": job["task"], "step": job["step"],
        })[0]
        deadline = time.monotonic() + (75 if job["kind"] == "test" else agent.max_wait_seconds + 45)
        while time.monotonic() < deadline:
            console.refresh()
            state = console.state()
            progress = next(item for item in state["jobs"] if item["id"] == local.id)
            replies = replies_for(state["snapshot"], progress)
            sent_at = progress["sent_at"]
            if job["kind"] == "test":
                expected = job["prompt"].split("reply exactly: ", 1)[-1]
                matched = [r for r in replies if r["text"].strip().lower() == expected.lower()]
                if matched:
                    report("done", sent_at=sent_at, replies=matched)
                    return
            if progress["state"] in ("done", "failed", "stopped", "cancelled"):
                if progress["state"] != "done" or not sent_at:
                    report("failed", sent_at=sent_at, replies=replies,
                           error="The send could not be confirmed. Open channel setup, then check the conversation before retrying.")
                elif job["kind"] == "test":
                    report("failed", sent_at=sent_at, error="The test message was sent, but the expected reply did not arrive.")
                else:
                    report("done", sent_at=sent_at, replies=replies)
                return
            if not report("running", sent_at=sent_at, replies=replies if job["kind"] == "run" else None):
                return
            time.sleep(2)
        report("failed", sent_at=sent_at, replies=replies,
               error="The agent did not finish in time. Check the conversation before retrying.")
    finally:
        console.close(grace=5)


def install(origin):
    """Register the custom URL with Launch Services for this trusted Study origin."""
    if sys.platform != "darwin":
        raise ConnectorError("Study Connector installation requires macOS.")
    APP.parent.mkdir(exist_ok=True)
    launcher = Path(STATE) / "launch"
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix = [sys.executable, "-m", "bench.study_connector", "--origin", origin, "--if-paired"]
    launcher.write_text('#!/bin/sh\nexport PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"\ncd ' + shlex.quote(str(ROOT)) + " || exit 1\nexec " + shlex.join(prefix) + ' "$@"\n')
    launcher.chmod(0o700)
    path = json.dumps(str(launcher))
    apple_script = f'''on run
        my launchConnector("")
    end run
    on open location incoming
        set prefix to "study-connector://pair/"
        if incoming starts with prefix then
            set pairCode to text ((length of prefix) + 1) thru -1 of incoming
            if (length of pairCode) is 24 then
                set codeEnding to text -6 thru -1 of pairCode
                set siteName to {json.dumps(origin)}
                try
                    display dialog "Connect this Mac to " & siteName & "? Match the last six characters in Study: " & codeEnding buttons {{"Cancel", "Connect"}} default button "Connect" cancel button "Cancel" with title "Study Connector"
                    my launchConnector(pairCode)
                on error number -128
                    return
                end try
            end if
        else if incoming is "study-connector://open" then
            my launchConnector("")
        end if
    end open location
    on launchConnector(pairCode)
        set commandLine to quoted form of {path}
        if pairCode is not "" then set commandLine to commandLine & " --code " & quoted form of pairCode
        do shell script commandLine & " > /dev/null 2>&1 &"
    end launchConnector'''
    subprocess.run(["osacompile", "-o", str(APP), "-e", apple_script], check=True)
    plist = APP / "Contents/Info.plist"
    with plist.open("rb") as stream:
        info = plistlib.load(stream)
    info["CFBundleIdentifier"] = "org.agentrelay.study-connector"
    info["CFBundleDisplayName"] = "Study Connector"
    info["CFBundleURLTypes"] = [{"CFBundleURLName": "Study Connector", "CFBundleURLSchemes": ["study-connector"]}]
    with plist.open("wb") as stream:
        plistlib.dump(info, stream)
    subprocess.run(["codesign", "--force", "--sign", "-", str(APP)], check=True)
    subprocess.run(["open", "-a", str(APP)], check=True)
    # Restore an already approved pairing when the user next signs into this Mac.
    from bench.study_devices import digest
    label = "org.agentrelay.study-connector." + digest(origin)[:12]
    agents_dir = Path.home() / "Library/LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{label}.plist"
    log = STATE / "connector.log"
    with agent_file.open("wb") as stream:
        plistlib.dump({"Label": label, "ProgramArguments": prefix,
                      "WorkingDirectory": str(ROOT), "RunAtLoad": True,
                      "StartInterval": 30, "StandardOutPath": str(log),
                      "StandardErrorPath": str(log),
                      "EnvironmentVariables": {"PATH": str(Path.home() / ".local/bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}, stream)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(agent_file)], check=True)
    print(f"Installed {APP}. Return to Study and click Open Study Connector.")


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--code")
    parser.add_argument("--configure-agent", help=argparse.SUPPRESS)
    parser.add_argument("--channel", choices=sorted(set(SETUP) | {"imessage"}), help=argparse.SUPPRESS)
    parser.add_argument("--if-paired", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    origin = args.origin.rstrip("/")
    url = urlsplit(origin)
    if (origin != f"{url.scheme}://{url.netloc}" or url.username or not url.hostname
            or (url.scheme != "https" and (url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1")))):
        parser.error("Enter the Study website’s HTTPS origin, or a local development address.")
    try:
        if args.configure_agent:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", args.configure_agent) or not args.channel:
                parser.error("Choose an agent name and channel.")
            configure_agent(args.configure_agent, args.channel)
            return
        if args.install:
            install(origin)
            return
        path = credential_file(origin)
        current = json.loads(path.read_text()) if path.exists() else {}
        if args.if_paired and not args.code and not current:
            return
        code = args.code if args.code is not None else (None if current else input("Study pairing code: ").strip())
        if code is not None:
            if not CODE.fullmatch(code):
                raise ConnectorError("Enter the code shown in Study.")
            current = request(origin, "pair", {"code": code, "agents": agent_channels()})
            store(path, current)
            print("Mac paired. Return to Study.", flush=True)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # the running connector will reload this credential
            loop(origin, path)
    except KeyboardInterrupt:
        pass
    except ConnectorError as error:
        parser.exit(1, f"{error}\n")


def loop(origin, path):
    future = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        while True:
            if future is not None and future.done():
                try:
                    future.result()
                except Exception as exc:
                    print(f"Study task stopped: {exc}", file=sys.stderr, flush=True)
                future = None
            credential = json.loads(path.read_text())["credential"]
            try:
                result = request(origin, "poll", {"agents": agent_channels(), "busy": future is not None}, credential)
                if result["job"]:
                    future = executor.submit(run_job, origin, credential, result["job"])
            except ConnectorError as error:
                if error.status == 401:
                    if json.loads(path.read_text())["credential"] == credential:
                        raise
                    continue
                print(error, file=sys.stderr, flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
