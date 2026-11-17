#!/bin/bash
# Embedded in the signed Shortcuts download; no Terminal window is opened.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
umask 077
origin='https://restaurant-study-demo.onrender.com'
base="$HOME/Library/Application Support/Study Connector"
mkdir -p "$base"
log="$base/shortcut-setup.log"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Get input before the download, so Run immediately gives visible feedback.
if ! /usr/bin/osascript -e '
tell application "Shortcuts"
activate
text returned of (display dialog "Copy a pairing code from the Study website and paste it below. Setup will then install the connector and open Muse. First-time setup may take a few minutes." default answer "" buttons {"Cancel", "Connect"} default button "Connect" cancel button "Cancel" with title "Connect Study · Muse")
end tell
' > "$work/code"; then
  exit 0
fi
/usr/bin/osascript -e 'display notification "Installing Study Connector. Keep Shortcuts open; Muse will open when setup finishes." with title "Study · Setting up this Mac"' || true
if ! curl --fail --silent --show-error --location --proto '=https' --connect-timeout 20 --max-time 90 \
  https://raw.githubusercontent.com/MikeDeng2002/assistant-agent-relay/codex/restaurant-study-imessage/scripts/install-study-connector.sh \
  -o "$work/install.sh" 2> "$log" || ! /bin/bash "$work/install.sh" "$origin" --terminal >> "$log" 2>&1; then
  /usr/bin/osascript - "$log" <<'APPLESCRIPT'
on run argv
  tell application "Shortcuts"
  activate
  set answer to display dialog "Installation could not finish. Check your internet connection and run Connect Study again. You do not need to open Terminal." buttons {"Close", "View details"} default button "Close" with title "Study · Setup interrupted" with icon caution
  end tell
  if button returned of answer is "View details" then
    do shell script "/usr/bin/open -a TextEdit " & quoted form of (item 1 of argv)
  end if
end run
APPLESCRIPT
  exit 0
fi
cd "$base/assistant-agent-relay"
.venv/bin/python - "$work/code" <<'PY'
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from bench.study_connector import ConnectorError, request, credential_file, store, agent_channels, CODE, STATE
from bench.study_devices import digest
from bench.config import LOCAL_AGENTS_FILE

ORIGIN = 'https://restaurant-study-demo.onrender.com'

def dialog(message, *, entry=False):
    script = '''on run argv
      tell application "Shortcuts"
      activate
      text returned of (display dialog (item 1 of argv) default answer "" buttons {"Cancel", "Connect"} default button "Connect" cancel button "Cancel" with title "Connect Study · Muse")
      end tell
    end run''' if entry else '''on run argv
      tell application "Shortcuts"
      activate
      display dialog (item 1 of argv) buttons {"OK"} default button "OK" with title "Study Connector"
      end tell
    end run'''
    result = subprocess.run(['osascript', '-e', script, message], capture_output=True, text=True)
    if result.returncode:
        # Cancel is intentional; other errors must not appear as successful pairing.
        if '-128' in result.stderr:
            raise SystemExit(0)
        raise RuntimeError('Could not display the setup window. Open Shortcuts and run Connect Study again.')
    return result.stdout.strip()

try:
    # Chrome is a separate browser profile. Never reuse the participant's regular profile.
    chrome_paths = [Path('/Applications/Google Chrome.app'), Path.home() / 'Applications/Google Chrome.app']
    if not any(path.exists() for path in chrome_paths):
        dialog('Muse needs Google Chrome. The official download page will open next. Install Chrome, then run this shortcut again. No Terminal commands are needed.')
        subprocess.run(['open', 'https://www.google.com/chrome/'], check=True)
        raise SystemExit(0)

    local = yaml.safe_load(LOCAL_AGENTS_FILE.read_text()) or {} if LOCAL_AGENTS_FILE.exists() else {}
    entries = local.setdefault('agents', {})
    if 'muse' not in entries:
        entries['muse'] = {'channel': 'muse', 'handle': 'muse.ai'}
        LOCAL_AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        store(LOCAL_AGENTS_FILE, local, yaml.safe_dump)

    code = Path(sys.argv[1]).read_text().strip()
    while True:
        if not CODE.fullmatch(code):
            code = dialog('That is not a complete pairing code. In Study, click Generate pairing code, then copy and paste all 24 characters here.', entry=True)
            continue
        try:
            current = request(ORIGIN, 'pair', {'code': code, 'agents': agent_channels()})
            break
        except ConnectorError as error:
            if error.status in (400, 403):
                code = dialog('This code expired or has already been used. Generate a new pairing code on the Study website and paste it here. Installation is complete; it will not run again.', entry=True)
            elif not error.status:
                code = dialog('The Study website could not be reached. Check your internet connection, then paste a fresh pairing code to retry.', entry=True)
            else:
                raise
    path = credential_file(ORIGIN)
    store(path, current)
    label = 'org.agentrelay.study-connector.' + digest(ORIGIN)[:12]
    launchdir = Path.home() / 'Library/LaunchAgents'
    launchdir.mkdir(parents=True, exist_ok=True)
    launchfile = launchdir / (label + '.plist')
    config = {
        'Label': label,
        'ProgramArguments': [sys.executable, '-m', 'bench.study_connector', '--origin', ORIGIN, '--if-paired'],
        'WorkingDirectory': str(Path.cwd()), 'RunAtLoad': True, 'StartInterval': 30,
        'StandardOutPath': str(STATE / 'connector.log'), 'StandardErrorPath': str(STATE / 'connector.log'),
        'EnvironmentVariables': {'PATH': os.environ['PATH']},
    }
    with launchfile.open('wb') as stream:
        plistlib.dump(config, stream)
    launchfile.chmod(0o600)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
    loaded = subprocess.run(['launchctl', 'bootstrap', domain, str(launchfile)], capture_output=True, text=True)
    if loaded.returncode:
        raise RuntimeError('Pairing succeeded, but macOS could not start the background connector. Run this shortcut again. If macOS shows a background-item notice, allow Study Connector.')

    from bench.channels.chrome import attach
    with attach() as browser:
        context = browser.contexts[0]
        page = next((p for p in context.pages if urlsplit(p.url).hostname == 'muse.ai'), None)
        if page is None:
            page = context.new_page()
            page.goto('https://muse.ai/', wait_until='domcontentloaded', timeout=60000)
        page.bring_to_front()
    dialog('This Mac is paired and Muse is open. Finish signing in to Muse, then return to Study and click Test connection. You can close Shortcuts; the connector runs in the background.')
    print('Setup complete. Sign in to Muse, then click Test connection in Study.')
except Exception as error:
    # No tokens or pairing codes are included in errors or logs.
    dialog('Setup needs attention: ' + str(error)[:400] + '\n\nRun Connect Study again to retry. No Terminal commands are needed.')
PY
