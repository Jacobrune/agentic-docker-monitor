# agentic-docker-monitor

A lightweight Python daemon that watches your Docker Compose stack for dead containers and delegates repair to an AI agent.

When a container goes down, the monitor spawns an agentic AI CLI (Claude Code or Gemini CLI) with the container state and recent logs as context. The AI agent has full access to the filesystem and can run shell commands — it diagnoses and fixes the issue autonomously, then the monitor verifies the container actually came back up before marking it resolved.

## How it works

The key design choice: rather than calling an LLM API and parsing a response into shell commands, this delegates to a full agentic CLI that can read context files, explore logs, and execute whatever repair commands it deems necessary. The AI is not constrained to a predefined action set — it can read config files, check logs, restart services, or pull new images.

```
poll docker compose ps every 5 min
        │
        ▼ dead container found
build prompt (state + logs + stack context)
        │
        ▼
spawn AI CLI as subprocess  ──▶  AI reads files, runs docker commands
        │
        ▼ AI process exits
verify container is healthy
        │
   ┌────┴─────┐
healthy     still down
   │              │
clear state   increment attempt counter
              cooldown 1h, max 20 attempts
              then write shutdown report + docker compose down
```

## Requirements

- Python 3.11+
- Docker Compose v2
- At least one AI CLI installed and authenticated:
  - [Claude Code CLI](https://claude.ai/code) (`claude`)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`)

## Installation

```bash
git clone https://github.com/Jacobrune/agentic-docker-monitor.git
cd agentic-docker-monitor
```

No Python dependencies beyond the standard library.

## Configuration

All configuration is via environment variables:

| Variable | Required | Description |
|---|---|---|
| `STACK_DIR` | yes | Path to your Docker Compose project directory |
| `CONTEXT_FILE` | no | Path to a markdown file describing your stack. The AI reads this for context on every fix attempt. |
| `RECOVERY_HINTS_FILE` | no | Path to a plain-text file with stack-specific recovery hints, appended to every fix prompt. |
| `CHECK_INTERVAL` | no (default 300) | Seconds between health polls |
| `FIX_COOLDOWN` | no (default 3600) | Seconds between fix attempts per container |
| `FIX_MAX_ATTEMPTS` | no (default 20) | Failed attempts before the stack is shut down |

`CONTEXT_FILE` is what makes the AI useful for your specific setup. Without it the agent works from logs alone; with it, it knows your architecture, your service dependencies, and your known failure modes.

<details>
<summary>Example CONTEXT.md</summary>

```markdown
# My Stack

Docker Compose stack on Ubuntu 22.04. Project root: /opt/mystack.

## Services
- **nginx** — reverse proxy on port 80/443. Config at ./nginx/nginx.conf.
- **app** — Node.js application. Depends on postgres being healthy.
- **postgres** — PostgreSQL 16 with persistent volume at ./data/postgres.
- **redis** — Cache. Stateless, safe to recreate.

## Known failure modes
- postgres sometimes exits during startup if the data volume isn't ready. Recreate it.
- nginx exits 1 on config syntax errors. Check ./nginx/nginx.conf before restarting.
- app container retries the DB connection for 30s on startup — give it time before intervening.

## Recovery
docker compose -f /opt/mystack/docker-compose.yml restart <name>
docker compose -f /opt/mystack/docker-compose.yml up -d --force-recreate <name>
```

</details>

## Running

```bash
export STACK_DIR=/path/to/your/stack
export CONTEXT_FILE=$STACK_DIR/CONTEXT.md  # optional but recommended
python3 monitor.py
```

As a systemd service, use an environment file to keep configuration out of the unit file:

```bash
cp .env.example /etc/agentic-docker-monitor.env
# edit /etc/agentic-docker-monitor.env
```

```ini
[Unit]
Description=Agentic Docker Monitor
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/agentic-docker-monitor
EnvironmentFile=/etc/agentic-docker-monitor.env
ExecStart=/usr/bin/python3 /path/to/agentic-docker-monitor/monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Token usage

`token-usage.py` prints a daily breakdown of AI token consumption:

```bash
python3 token-usage.py $STACK_DIR/data/token-usage.log
# or
TOKEN_USAGE_LOG=$STACK_DIR/data/token-usage.log python3 token-usage.py
```

```
Date         | Source     | In       | Cached   | Out      | Cost
-----------------------------------------------------------------
2026-06-13   | INFRA      | 4821     | 3200     | 312      | $0.0041
```

Logs are written to `$STACK_DIR/data/token-usage.log`.

## AI provider fallback

The monitor tries providers in order, skipping any that are rate-limited, quota-exhausted, or not installed:

1. Gemini CLI (`gemini-2.5-flash`)
2. Claude Code CLI (`haiku`)

If all providers are unavailable, the fix attempt is skipped and retried after the cooldown. The provider list and model order is `AI_PROVIDER_MODELS` in `monitor.py`.

## Failure handling

After `FIX_MAX_ATTEMPTS` failed attempts on a container, the monitor:

1. Writes a report to `$STACK_DIR/data/SHUTDOWN-REPORT.txt` with the last AI output, container state, and recent logs
2. Runs `docker compose down`
3. Stops itself

This is intentional — an unrecoverable stack is worse than a stopped one.

## Limitations

- Assumes single-host Docker Compose. Does not support Swarm or Kubernetes.
- The AI agent has full access to the filesystem and shell on the host. This is a deliberate design choice that enables flexible repair, and a security tradeoff you accept by running this tool. Run with a dedicated low-privilege user where possible.
- Requires the AI CLI to be authenticated and on PATH when the monitor starts. Token expiry or API key rotation requires a service restart.

## License

Released under the MIT License. See [LICENSE](LICENSE).
