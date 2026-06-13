#!/usr/bin/env python3
"""
Health monitor for Docker Compose stacks.
Watches for dead containers and calls AI providers to fix them.
"""
import os, sys, subprocess, time, json, shlex, shutil
from datetime import datetime
from zoneinfo import ZoneInfo

STACK_DIR = os.environ.get("STACK_DIR")
if not STACK_DIR:
    sys.exit("STACK_DIR env var required (path to your docker-compose stack)")

LOG_FILE  = f"{STACK_DIR}/data/monitor.log"
USAGE_LOG = f"{STACK_DIR}/data/token-usage.log"

GOOGLE_CMD = shutil.which("gemini")
if not GOOGLE_CMD:
    sys.exit("`gemini` CLI not found on PATH")

CLAUDE_CMD = shutil.which("claude")
if not CLAUDE_CMD:
    sys.exit("`claude` CLI not found on PATH")

CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL", 300))
FIX_COOLDOWN     = int(os.environ.get("FIX_COOLDOWN", 3600))
FIX_MAX_ATTEMPTS = int(os.environ.get("FIX_MAX_ATTEMPTS", 20))

CONTEXT_FILE = os.environ.get("CONTEXT_FILE", "")

_hints_file = os.environ.get("RECOVERY_HINTS_FILE", "")
RECOVERY_HINTS = open(_hints_file).read() if _hints_file else ""

DENMARK_TZ = ZoneInfo("Europe/Copenhagen")

_container_fix_state: dict[str, dict] = {}


def log(msg):
    ts   = datetime.now(DENMARK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, timeout=30, input_text=None):
    try:
        kwargs = {}
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        r = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            **kwargs,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


# ─── AI caller ────────────────────────────────────────────────────────────────

TOKEN_ERRORS = [
    "rate limit", "too many requests", "overloaded",
    "insufficient_quota", "insufficient credits", "credit balance",
    "529", "429", "quota", "quota_exhausted", "terminalquotaerror",
    "usage limit", "limit reached", "resource exhausted",
]

MODEL_UNAVAILABLE_ERRORS = [
    "model not found", "unknown model", "invalid model",
    "model is not available", "not available for this account",
    "model is not supported", "not supported when using codex",
    "unsupported model",
]

AI_UNAVAILABLE_MSG = "All AI providers unavailable due to quota/rate limits or missing commands"

AI_PROVIDER_MODELS = [
    ("google", GOOGLE_CMD, ["gemini-2.5-flash"]),
    ("claude", CLAUDE_CMD, ["haiku"]),
]


def _log_usage(model: str, usage: dict, cost: float, source: str):
    ts     = datetime.now(DENMARK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    inp    = usage.get("input_tokens", 0)
    out    = usage.get("output_tokens", 0)
    cached = usage.get("cache_read_input_tokens", 0)
    line   = f"{ts} | {model:6s} | {source:9s} | in={inp} cached={cached} out={out} | ${cost:.4f}\n"
    try:
        with open(USAGE_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass
    log(f"Tokens [{model}] ({source}): in={inp} cached={cached} out={out} cost=${cost:.4f}")


def _build_ai_command(provider: str, cmd: str, model: str, prompt: str):
    stack_dir  = shlex.quote(STACK_DIR)
    exe        = shlex.quote(cmd)
    model_arg  = shlex.quote(model)
    prompt_arg = shlex.quote(prompt)

    if provider == "google":
        return f"cd {stack_dir} && {exe} --model {model_arg} --output-format stream-json --prompt {prompt_arg}", None
    if provider == "claude":
        return f"cd {stack_dir} && {exe} --model {model_arg} --output-format stream-json --verbose --print {prompt_arg}", None
    raise ValueError(f"Unknown AI provider: {provider}")


def _extract_ai_result(provider: str, model: str, out: str, source: str):
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("type") != "result":
            continue

        if obj.get("is_error"):
            return False, obj.get("result") or "AI functional error", True

        usage = obj.get("usage")
        if isinstance(usage, dict):
            _log_usage(model, usage, obj.get("total_cost_usd", 0.0), source)
        return True, obj.get("result", out), True

    return True, out, False


def _run_ai_session(prompt, source: str = "AUTO"):
    saw_retryable_failure = False

    for provider, cmd, models in AI_PROVIDER_MODELS:
        for current_model in models:
            log(f"Starting [{provider}:{current_model}] session...")
            ai_cmd, input_text = _build_ai_command(provider, cmd, current_model, prompt)
            rc, out, err = run(ai_cmd, timeout=3600, input_text=input_text)

            combined_err = (out + err).lower()
            is_quota_error = any(e in combined_err for e in TOKEN_ERRORS)
            is_missing_cmd = rc == 127 or (
                rc != 0
                and (
                    "command not found" in combined_err
                    or "executable file not found" in combined_err
                    or "no such file or directory" in combined_err
                )
            )
            is_model_unavailable = rc != 0 and any(e in combined_err for e in MODEL_UNAVAILABLE_ERRORS)

            if is_quota_error or is_missing_cmd or is_model_unavailable:
                saw_retryable_failure = True
                log(f"{provider}:{current_model} unavailable/quota hit. Trying next option...")
                continue

            if rc != 0:
                log(f"Session failed with {provider}:{current_model} rc={rc}: {err[:200]}")
                return False, err or out or f"{provider}:{current_model} failed", {
                    "provider": provider,
                    "model": current_model,
                }

            success, result, _ = _extract_ai_result(provider, current_model, out, source)
            if not success:
                log(f"AI reported functional error for {provider}:{current_model}")
                return False, result, {"provider": provider, "model": current_model}

            log(f"Session completed successfully with {provider}:{current_model}")
            return True, result, {"provider": provider, "model": current_model}

    if saw_retryable_failure:
        return False, AI_UNAVAILABLE_MSG, None
    return False, "All AI providers failed or exhausted", None


def _fix_container(key: str, container_name: str, prompt: str):
    state = _container_fix_state.get(key, {"attempts": 0, "last_try": 0, "last_output": "not available"})

    if time.time() - state["last_try"] < FIX_COOLDOWN:
        remaining = int((FIX_COOLDOWN - (time.time() - state["last_try"])) / 60)
        log(f"'{key}' on cooldown — {remaining}m until next attempt")
        return

    if state["attempts"] >= FIX_MAX_ATTEMPTS:
        log(f"'{key}' exhausted all attempts — shutting down")
        _shutdown_stack(key, state)
        return

    success, output, _ = _run_ai_session(prompt, source="INFRA")
    state["last_try"] = time.time()

    if success:
        if _is_container_healthy(container_name):
            log(f"'{key}' verified healthy after AI fix")
            _container_fix_state.pop(key, None)
            return
        log(f"'{key}' AI session succeeded but container still unhealthy — counting as failed attempt")

    if output == AI_UNAVAILABLE_MSG:
        log(f"'{key}' skipped: all AI providers unavailable/quota-limited")
        _container_fix_state[key] = state
        return

    state["attempts"] += 1
    state["last_output"] = output
    _container_fix_state[key] = state
    log(f"'{key}' AI fix failed ({state['attempts']}/{FIX_MAX_ATTEMPTS})")

    if state["attempts"] >= FIX_MAX_ATTEMPTS:
        log(f"'{key}' exhausted all attempts — shutting down")
        _shutdown_stack(key, state)


def _shutdown_stack(reason: str, state: dict = None):
    report_path = f"{STACK_DIR}/data/SHUTDOWN-REPORT.txt"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rc1, ps, _   = run(f"docker compose -f {STACK_DIR}/docker-compose.yml ps 2>&1")
    rc2, logs, _ = run(f"docker compose -f {STACK_DIR}/docker-compose.yml logs --tail=50 2>&1")

    attempts = (state or {}).get("attempts", 0)
    last_out = (state or {}).get("last_output", "not available")

    report = f"""CONTAINER AUTO-RECOVERY FAILURE REPORT
Generated : {ts}
Issue     : {reason}
Result    : AI provider fallback failed {attempts}/{FIX_MAX_ATTEMPTS} attempts.
            Stack has been shut down to avoid further issues.

TO RESTART: Fix the problem manually, then run:
  docker compose -f {STACK_DIR}/docker-compose.yml up -d

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LAST AI OUTPUT:
{last_out}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTAINER STATE AT SHUTDOWN:
{ps}

RECENT LOGS:
{logs}
"""
    with open(report_path, "w") as f:
        f.write(report)

    log(f"Shutdown report written to {report_path}")
    log("Shutting down all containers...")
    run(f"docker compose -f {STACK_DIR}/docker-compose.yml down", timeout=60)
    log("Stack is down. Read SHUTDOWN-REPORT.txt, fix manually, then docker compose up -d")


# ─── Health checks ─────────────────────────────────────────────────────────────

def _is_container_healthy(name: str) -> bool:
    rc, out, _ = run(
        f"docker compose -f {STACK_DIR}/docker-compose.yml ps --all --format '{{{{.Name}}}}\t{{{{.State}}}}'"
    )
    if rc != 0:
        return False
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == name and parts[1] in ("running", "healthy"):
            return True
    return False


def check_containers():
    rc, out, _ = run(f"docker compose -f {STACK_DIR}/docker-compose.yml ps --all --format '{{{{.Name}}}}\t{{{{.State}}}}\t{{{{.Status}}}}'")
    if rc != 0:
        return

    dead: list[tuple[str, str]] = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        cname, cstate = parts[0], parts[1]
        if cstate not in ("running", "healthy"):
            dead.append((cname, cstate))

    if not dead:
        for key in list(_container_fix_state):
            _container_fix_state.pop(key, None)
        return

    dead_display = [f"{n} ({s})" for n, s in dead]
    log(f"Dead containers: {dead_display}")
    rc2, ps, _   = run(f"docker compose -f {STACK_DIR}/docker-compose.yml ps 2>&1")
    rc3, logs, _ = run(f"docker compose -f {STACK_DIR}/docker-compose.yml logs --tail=30 2>&1")

    context_section = f"Read {CONTEXT_FILE} for full context.\n" if CONTEXT_FILE else ""
    hints_section   = f"\n{RECOVERY_HINTS}\n" if RECOVERY_HINTS else ""

    for cname, cstate in dead:
        key    = f"container:{cname} ({cstate})"
        prompt = f"""You are fixing a Docker Compose stack at {STACK_DIR}.
{context_section}
## ISSUE: Containers are not running

Dead containers: {dead_display}

docker compose ps:
{ps}

Recent logs:
{logs}

Fix by restarting the affected containers:
  docker compose -f {STACK_DIR}/docker-compose.yml restart <name>
{hints_section}"""
        _fix_container(key, cname, prompt)


# ─── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log("Container monitor started")
    last_health_check = 0

    while True:
        try:
            now = time.time()
            if now - last_health_check >= CHECK_INTERVAL:
                log("Running health checks...")
                check_containers()
                last_health_check = now
                log("Health checks complete")

        except Exception as e:
            log(f"Monitor error: {e}")

        time.sleep(60)


if __name__ == "__main__":
    main()
