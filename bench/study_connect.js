/* The browser uses its Study login. The Mac uses a separate device credential. */
const studyConnect = (() => {
  const labels = {
    imessage: 'iMessage', whatsapp: 'WhatsApp', slack: 'Slack',
    muse: 'muse.ai', hark: 'hark.com', wajo: 'wajo.ai', telegram: 'Telegram',
  };
  const help = {
    whatsapp: 'Scan the QR code in the browser tab with WhatsApp → Settings → Linked Devices on your phone. If Go is missing, install it from the official page that opens and try setup again.',
    slack: 'Sign in to the workspace containing your Dot in the dedicated Chrome window. The connector uses your locally configured Slack app token.',
    muse: 'Sign in to Muse in the dedicated Chrome window and wait for the main chat to open.',
    hark: 'Sign in to Hark in the dedicated Chrome window.',
    wajo: 'Sign in to Wajo in the dedicated Chrome window.',
    telegram: 'Scan the QR code in the browser tab with Telegram → Settings → Devices on your phone. Enter your two-step password in the Mac pop-up if prompted. Telegram API credentials must be configured by the study administrator.',
  };
  const url = '/api/study/connection';
  let latest = { device: null, jobs: [] };
  let picker, status, direction, codeBox, dialog, codeText, setup, test, disconnect;
  let selected = '', mounted = false, requested = false, lastError = '', codeExpiry = 0, actionPending = false;
  let onUpdate, startCommand, taskNavigation;
  let shortcutSupported = false, lastShortcutMode = null;
  let autoSetupAgent = null;
  let history = [];
  let renderedMessages = null, renderedAgent = null;
  const webChannels = new Set(['hark', 'muse', 'wajo']);

  function updateView() {
    if (!mounted) return;
    const { device, jobs } = latest;
    const channel = picker.selectedOptions[0]?.dataset.channel;
    const useShortcut = shortcutSupported && Boolean(labels[channel]);
    dialog.querySelector('#connector-shortcut').hidden = !useShortcut;
    if (lastShortcutMode !== useShortcut) {
      dialog.querySelector('#connector-install-manual').open = !useShortcut;
      dialog.querySelector('#connector-terminal').open = !useShortcut;
      lastShortcutMode = useShortcut;
    }
    const available = device?.agents?.[selected] === channel;
    const verified = device?.verified?.includes(selected);
    const job = jobs.findLast(j => j.agent === selected && j.kind !== 'run');
    const pending = jobs.some(j => j.kind !== 'run' && ['queued', 'running'].includes(j.state));
    status.textContent = lastError || (!device ? 'This Mac is not paired.' :
      !device.online ? 'Paired · Mac offline' :
      pending && job?.agent === selected && ['queued', 'running'].includes(job.state) ?
        (job.kind === 'test' ? 'Waiting for the agent’s test reply…' : 'Opening channel setup on your Mac…') :
      job?.state === 'failed' ? job.error :
      verified ? 'Connected · test reply received' :
      available ? 'Mac online · ready to test' : 'Mac online · this agent is not configured on the Mac.');
    status.className = 'connector-status' + (lastError || job?.state === 'failed' ? ' is-error' : verified ? ' is-ready' : '');
    dialog.querySelector('#connector-pair-step').hidden = Boolean(device);
    dialog.querySelector('#connector-pair-step').className = 'connector-step' + (!device && codeExpiry > Date.now() ? ' is-current' : '');
    dialog.querySelector('#connector-agent-step').className = 'connector-step' + (device?.online ? ' is-current' : '');
    dialog.querySelector('#connector-conversation').hidden = !device;
    const isIMessage = channel === 'imessage';
    const showIMessageSteps = device?.online && isIMessage && !verified;
    dialog.querySelector('#connector-imessage-steps').hidden = !showIMessageSteps;
    dialog.querySelector('#connector-imessage-reader').hidden = !showIMessageSteps;
    setup.textContent = isIMessage ? 'Set up iMessage' : 'Open channel setup';
    let instructions = help[channel] || '';
    if (device?.online && !available) {
      instructions = 'Open channel setup and finish configuring this agent on your Mac. ' + instructions;
    } else if (job?.kind === 'setup' && job.state === 'done' && !verified) {
      instructions = 'Complete setup on your Mac, then click Test connection. ' + instructions;
    }
    direction.hidden = !device?.online || Boolean(showIMessageSteps) || Boolean(verified);
    direction.textContent = verified ? 'Connection verified. You can chat below or continue your study task.' : instructions;
    dialog.querySelector('#connector-next-help').textContent = verified ?
      'Ready. Click ' + (taskNavigation?.label || 'Continue to task') + ' to continue. Both selected agents need to pass the test.' :
      !device ? 'After pairing, sign in to your agent and click Test connection.' : 'Sign in, leave the message box empty, then click Test connection. Wait for the reply.';
    renderMessages();
    dialog.querySelector('#connector-install').hidden = Boolean(device);
    setup.disabled = actionPending || !device?.online || !channel || pending;
    test.disabled = actionPending || !device?.online || !available || pending;
    test.className = verified ? 'btn' : 'btn primary';
    const next = dialog.querySelector('#connector-continue');
    next.hidden = !taskNavigation || !verified;
    next.disabled = actionPending || !device?.online || jobs.some(j => ['queued', 'running'].includes(j.state));
    disconnect.hidden = !device;
    const pair = dialog.querySelector('#connector-pair');
    const hasCode = !device && Date.now() < codeExpiry;
    const reconnect = Boolean(device && !device.online);
    pair.hidden = Boolean(device);
    pair.disabled = actionPending;
    pair.className = hasCode ? 'btn small' : 'btn primary';
    pair.textContent = hasCode || codeExpiry ? 'Generate new pairing code' : 'Generate pairing code';
    dialog.querySelector('#connector-terminal').hidden = !hasCode && !reconnect;
    dialog.querySelector('#connector-terminal-title').textContent =
      reconnect ? 'Restart Study Connector in Terminal' : 'Pair this Mac in Terminal';
    dialog.querySelector('#connector-terminal-help').textContent = reconnect ?
      'On the paired Mac, paste this command into Terminal and press Return.' :
      'If Terminal is already asking for a pairing code, paste the code below. Otherwise, run this command first. The code expires after five minutes.';
    dialog.querySelector('#connector-manual-command').textContent = reconnect ? startCommand : startCommand + ' --pair';
    codeBox.hidden = !hasCode;
    dialog.querySelector('#connector-run-shortcut').hidden = !useShortcut || !hasCode;
    dialog.querySelector('#connector-code-help').textContent = useShortcut ?
      'Copy the code → Run Study setup → paste it into the pop-up. Choose Open Shortcuts if asked.' :
      'Paste this code when Terminal asks. The code expires after five minutes.';
    if (!hasCode) codeText.textContent = '';
    if (!device && codeExpiry && !hasCode && !lastError) {
      status.textContent = 'Pairing code expired. Generate a new code to continue.';
    }
  }

  function renderMessages() {
    const box = dialog.querySelector('#connector-messages');
    if (!box) return;
    const entries = history.filter(job => job.agent === selected && job.kind !== 'setup');
    dialog.querySelector('#connector-send').disabled = actionPending || !latest.device?.online ||
      !latest.device?.verified?.includes(selected) || latest.jobs.some(job => ['queued','running'].includes(job.state));
    const signature = JSON.stringify([selected, entries]);
    if (signature === renderedMessages) return;
    const followLatest = renderedAgent !== selected || box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    renderedMessages = signature;
    renderedAgent = selected;
    const fragment = document.createDocumentFragment();
    for (const job of entries) {
      const heading = document.createElement('p');
      heading.className = 'small muted';
      heading.textContent = `${job.step} · ${job.state}`;
      fragment.append(heading);
      if (job.prompt) {
        const prompt = document.createElement('p');
        prompt.textContent = `${job.sent_at ? 'You' : 'Requested message (send not confirmed)'}: ${job.prompt}`;
        fragment.append(prompt);
      }
      for (const reply of job.replies || []) {
        const message = document.createElement('p');
        message.style.whiteSpace = 'pre-wrap';
        message.textContent = `${selected}: ${reply.text}`;
        fragment.append(message);
      }
      if (job.error) {
        const error = document.createElement('p');
        error.className = 'err';
        error.textContent = job.error;
        fragment.append(error);
      }
    }
    if (!entries.length) {
      const empty = document.createElement('p');
      empty.textContent = 'No messages yet. Test this connection to see the reply here.';
      fragment.append(empty);
    }
    box.replaceChildren(fragment);
    if (followLatest) box.scrollTop = box.scrollHeight;
  }

  function requestWebSetup() {
    const channel = picker.selectedOptions[0]?.dataset.channel;
    autoSetupAgent = labels[channel] ? selected : null;
    maybeOpenWebSetup();
  }

  function maybeOpenWebSetup() {
    if (!autoSetupAgent || !dialog.open || actionPending || !latest.device?.online) return;
    if (latest.jobs.some(job => ['queued', 'running'].includes(job.state))) return;
    const agent = autoSetupAgent;
    autoSetupAgent = null;
    action(() => queue('setup', agent));
  }

  async function refresh() {
    if (requested) return;
    requested = true;
    try {
      latest = await studyAuth.api(url);
      history = onUpdate(latest) || latest.jobs;
      updateView();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      requested = false;
      maybeOpenWebSetup();
    }
  }

  async function action(fn) {
    actionPending = true;
    lastError = '';
    updateView();
    try {
      await fn();
      await refresh();
    } catch (error) {
      lastError = error.message;
    } finally {
      actionPending = false;
      updateView();
    }
  }

  function mount(element, agents, callback, localLaunch, navigation) {
    if (mounted) return;
    mounted = true;
    dialog = element;
    onUpdate = callback;
    taskNavigation = navigation;
    // The signed artifact pairs only with this hosted Study origin.
    shortcutSupported = !localLaunch && location.origin === 'https://restaurant-study-demo.onrender.com';
    picker = dialog.querySelector('#connector-agent');
    status = dialog.querySelector('#connector-status');
    direction = dialog.querySelector('#connector-help');
    codeBox = dialog.querySelector('#connector-code-box');
    codeText = dialog.querySelector('#connector-code');
    setup = dialog.querySelector('#connector-setup');
    test = dialog.querySelector('#connector-test');
    const next = dialog.querySelector('#connector-continue');
    next.textContent = navigation?.label || 'Continue to task';
    next.onclick = () => {
      if (next.disabled || !latest.device?.online || !latest.device?.verified?.includes(selected)) return;
      autoSetupAgent = null;
      dialog.close();
      taskNavigation?.continue(selected);
    };
    disconnect = dialog.querySelector('#connector-disconnect');

    for (const {name, channel} of agents) {
      if (!labels[channel]) continue;
      const label = name.charAt(0).toUpperCase() + name.slice(1);
      const option = new Option(label + ' · ' + labels[channel], name);
      option.dataset.channel = channel;
      picker.add(option);
    }
    selected = picker.value;
    picker.onchange = () => {
      selected = picker.value;
      lastError = '';
      updateView();
      requestWebSetup();
    };
    dialog.querySelector('#connector-pair').onclick = async () => {
      await action(async () => {
        const {code, expires_in} = await studyAuth.api(url + '/code', {});
        codeExpiry = Date.now() + expires_in * 1000;
        codeText.textContent = code;
        dialog.querySelector('#connector-copy-code').textContent = 'Copy pairing code';
      });
      if (!codeBox.hidden) codeBox.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    };
    const quote = value => "'" + value.replaceAll("'", "'\"'\"'") + "'";
    const installCommand = 'installer=$(mktemp) && curl -fsSL https://raw.githubusercontent.com/MikeDeng2002/assistant-agent-relay/codex/restaurant-study-imessage/scripts/install-study-connector.sh -o "$installer" && /bin/bash "$installer" ' + quote(location.origin) + ' --terminal';
    if (localLaunch) {
      dialog.querySelector('#connector-install-title').textContent = 'Install dependencies in Terminal';
      dialog.querySelector('#connector-download').hidden = true;
      dialog.querySelector('#connector-install-help').textContent =
        'From the project directory in Terminal, run this command. Then generate a pairing code below.';
    }
    const installText = dialog.querySelector('#connector-command');
    installText.textContent = localLaunch ? 'uv sync --python 3.12 --locked --no-dev' : installCommand;
    const commandText = dialog.querySelector('#connector-manual-command');
    startCommand = (localLaunch ? 'uv run python -m bench.study_connector --origin ' :
      'cd "$HOME/Library/Application Support/Study Connector/assistant-agent-relay" && .venv/bin/python -m bench.study_connector --origin ') + quote(location.origin);
    copyButton('#connector-copy-install', installText);
    copyButton('#connector-copy-start', commandText);
    copyButton('#connector-copy-code', codeText);
    setup.onclick = () => {
      autoSetupAgent = null;
      action(() => queue('setup', selected));
    };
    test.onclick = () => action(() => queue('test', selected));
    dialog.querySelector('#connector-send').onclick = () => action(async () => {
      const input = dialog.querySelector('#connector-message');
      const text = input.value.trim();
      if (!text) return;
      await queue('run', selected, {text, step: 'CONNECTION CHAT'});
      input.value = '';
    });
    disconnect.onclick = () => action(async () => {
      await studyAuth.api(url + '/disconnect', {});
      latest.device = null;
      codeExpiry = 0;
    });
    refresh();
    setInterval(refresh, 2500);
  }

  function copyButton(selector, source) {
    const button = dialog.querySelector(selector);
    const label = button.textContent;
    button.onclick = async () => {
      try {
        await navigator.clipboard.writeText(source.textContent);
        button.textContent = 'Copied';
      } catch {
        button.textContent = 'Select and copy the text above';
      }
      setTimeout(() => { button.textContent = label; }, 2000);
    };
  }

  function queue(kind, agent, extra = {}) {
    return studyAuth.api(url + '/job', {kind, agent, ...extra});
  }

  function open(dialog, agent, autoSetup = true) {
    if (agent && [...picker.options].some(option => option.value === agent)) {
      picker.value = selected = agent;
    }
    lastError = '';
    updateView();
    if (!dialog.open) dialog.showModal();
    if (autoSetup) requestWebSetup();
    else autoSetupAgent = null;
    refresh();
  }

  return {mount, open, queue};
})();
