"""OpenCode CLI adapter. No SDK, server, credential copying or shell interpolation."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
import shutil

BACKENDS = ("codex", "opencode")
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens",
              "total_tokens", "uncached_input_tokens", "reasoning_output_tokens",
              "cache_creation_input_tokens")


def validate_backend(backend, model):
    if backend not in BACKENDS:
        raise ValueError(f"unknown worker backend: {backend}")
    if backend == "opencode" and (not isinstance(model, str) or "/" not in model
                                  or not all(model.split("/", 1)) or any(c.isspace() for c in model)):
        raise ValueError("OpenCode requires an explicit provider/model: use --worker-model (team) or --model (worker)")


def resolve_cli(cli):
    """Popen cannot execute npm's PowerShell shim; prefer its native binary."""
    if len(cli) != 1:  # Explicit program plus arguments (also useful for test doubles).
        return cli
    found = shutil.which(cli[0])
    if not found:
        raise ValueError("OpenCode executable not found; install/authenticate OpenCode or pass --opencode /path/to/opencode.exe")
    path = Path(found)
    if os.name == "nt" and path.suffix.lower() in (".ps1", ".cmd", ".bat"):
        candidates = (path.parent / "node_modules/opencode-ai/bin/opencode.exe", path.with_suffix(".exe"))
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            raise ValueError("OpenCode npm shim has no native binary; pass its executable with --opencode")
    return [str(path)]


def agent_name(role):
    return "cli-worker-" + role


def build_command(cli, task, model, effort, session_id=None):
    command = [*resolve_cli(cli), "run", "--dir", task["cwd"], "--format", "json",
               "--model", model, "--agent", agent_name(task["role"])]
    if effort:
        command += ["--variant", effort]
    if session_id:
        command += ["--session", session_id]
    return command  # The prompt is piped through stdin, never quoted into a shell.


def prepare_env(env, role):
    result = dict(os.environ if env is None else env)
    try:
        config = json.loads(result.get("OPENCODE_CONFIG_CONTENT") or "{}")
    except ValueError:
        raise ValueError("OPENCODE_CONFIG_CONTENT must be a JSON object for this runner") from None
    if not isinstance(config, dict) or not isinstance(config.get("agent", {}), dict):
        raise ValueError("OpenCode inline config and agent configuration must be objects")
    permissions = {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow",
                   "list": "allow", "lsp": "allow", "webfetch": "allow", "websearch": "allow",
                   "edit": "allow" if role == "implement" else "deny",
                   "bash": "allow" if role == "implement" else "deny",
                   "task": "deny", "skill": "deny", "question": "deny",
                   "external_directory": "deny"}
    config.setdefault("agent", {})[agent_name(role)] = {
        "description": "Bounded CLI worker managed by the Python coordinator",
        "mode": "primary", "permission": permissions,
    }
    result["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    return result


def profile_identity(env):
    source = os.environ if env is None else env
    keys = ("HOME", "USERPROFILE", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
            "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT")
    # Config may contain secrets. Persist only a fingerprint, never its values.
    values = {key: source.get(key) for key in keys}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def step_usage(tokens):
    if not isinstance(tokens, dict) or not isinstance(tokens.get("cache"), dict):
        return None
    values = [tokens.get(k) for k in ("input", "output", "reasoning")]
    values += [tokens["cache"].get(k) for k in ("read", "write")]
    if any(type(v) is not int or v < 0 for v in values):
        return None
    uncached, visible, reasoning, cached, created = values
    # OpenCode excludes cache read/write from input, and reasoning from output.
    inputs, outputs = uncached + cached + created, visible + reasoning
    return dict(input_tokens=inputs, output_tokens=outputs, cached_input_tokens=cached,
                cache_creation_input_tokens=created, reasoning_output_tokens=reasoning,
                uncached_input_tokens=inputs - cached, total_tokens=inputs + outputs)


def add_usage(current, previous):
    if current is None or previous is None:
        return None
    return {key: current[key] + previous[key] for key in USAGE_KEYS}


def parse_events(path):
    errors, commands, parts, sessions = [], [], {}, set()
    malformed = 0
    if not path.exists():
        return dict(session_id=None, usage=None, completed_turns=0, malformed_lines=0,
                    errors=["missing events"], commands=[], messages=[])
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError()
            sid = event.get("sessionID")
            if not isinstance(sid, str) or not sid:
                raise ValueError()
            sessions.add(sid)
            kind = event.get("type")
            if kind == "error":
                errors.append(json.dumps(event.get("error"), ensure_ascii=False))
                continue
            if kind not in ("text", "tool_use", "step_start", "step_finish", "reasoning"):
                continue
            part = event.get("part")
            if (not isinstance(part, dict) or not isinstance(part.get("id"), str)
                    or not isinstance(part.get("messageID"), str) or part.get("sessionID") != sid):
                raise ValueError()
            if kind == "tool_use" and (not isinstance(part.get("state"), dict)
                    or not isinstance(part["state"].get("input", {}), dict)
                    or not isinstance(part["state"].get("metadata", {}), dict)):
                raise ValueError()
            ident = part["id"]
            if ident in parts:
                if parts[ident] != (kind, part):
                    errors.append("conflicting duplicate event part: " + ident)
                continue
            parts[ident] = (kind, part)
        except (ValueError, TypeError):
            malformed += 1
    steps = [part for kind, part in parts.values() if kind == "step_finish"]
    terminal = steps[-1] if steps else None
    completed = int(bool(terminal and terminal.get("reason") == "stop"))
    if terminal and terminal.get("reason") not in ("stop", "tool-calls"):
        errors.append("OpenCode ended with reason: " + str(terminal.get("reason")))
    if len(sessions) > 1:
        errors.append("multiple OpenCode session IDs in one invocation")
    if malformed:
        errors.append("malformed OpenCode event stream")
    # Every started step must finish, including intermediate tool-call steps.
    starts = [p for kind, p in parts.values() if kind == "step_start"]
    if Counter(p["messageID"] for p in starts) != Counter(p["messageID"] for p in steps):
        errors.append("incomplete OpenCode steps")
    usages = [step_usage(s.get("tokens")) for s in steps]
    measured = completed and not errors and usages and all(u is not None for u in usages)
    usage = {key: sum(u[key] for u in usages) for key in USAGE_KEYS} if measured else None
    final_message = terminal.get("messageID") if completed else None
    messages = []
    for kind, part in parts.values():
        if kind == "text" and part["messageID"] == final_message and isinstance(part.get("text"), str):
            messages.append(part["text"])
        if kind == "tool_use" and part.get("tool") == "bash":
            state = part.get("state") or {}
            inputs, metadata = state.get("input") or {}, state.get("metadata") or {}
            code = metadata.get("exit")
            commands.append(dict(command=inputs.get("command"),
                                 exit_code=code if type(code) is int else None))
    return dict(session_id=next(iter(sessions)) if len(sessions) == 1 else None,
                usage=usage, completed_turns=completed, malformed_lines=malformed,
                errors=errors, commands=commands, messages=messages)


def validate_schema(value, schema):
    """Validate the JSON Schema subset used by this runner, without dependencies."""
    if "anyOf" in schema:
        return any(validate_schema(value, s) for s in schema["anyOf"])
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int,
               "null": value is None}
    if not any(matches.get(k, False) for k in kinds):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            return False
        return all(key not in properties or validate_schema(item, properties[key]) for key, item in value.items())
    if isinstance(value, list):
        return (len(value) <= schema.get("maxItems", len(value))
                and all(validate_schema(item, schema["items"]) for item in value))
    return True


def final_report(messages, schema):
    text = "".join(messages).strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if validate_schema(value, schema) else None
