"""Computer drivers — Layer 2b (a11y) and Layer 3 (vision) using cua-driver."""
from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from browser.client import V9Client

CUA_PATH = str(Path.home() / "AppData" / "Local" / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe")

def call_cua(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a cua-driver tool through the running daemon."""
    try:
        proc = subprocess.run(
            [CUA_PATH, "call", tool, json.dumps(args)],
            capture_output=True, text=True, encoding="utf-8",
        )
    except FileNotFoundError:
        return {"error": f"cua-driver executable not found at {CUA_PATH}. Please install it."}
    except Exception as e:
        return {"error": f"cua-driver call failed: {e}"}

    if proc.returncode != 0:
        err = proc.stderr.strip() if proc.stderr else "unknown error"
        return {"error": f"{tool} failed: {err}"}
    
    text = proc.stdout.strip() if proc.stdout else ""
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return {"raw": text}

def ensure_daemon() -> None:
    """Start cua-driver serve if no daemon is running."""
    try:
        status = subprocess.run([CUA_PATH, "status"], capture_output=True, text=True, encoding="utf-8")
        if "is running" not in (status.stdout or ""):
            subprocess.Popen([CUA_PATH, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass  # We handle the missing executable in call_cua
    except Exception:
        pass

# Action vocabulary adapted for cua-driver
ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "actions"],
    "properties": {
        "thinking": {"type": "string", "description": "1–2 sentences of reasoning"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["click", "type_text", "press_key", "hotkey", "scroll", "wait", "done"],
                    },
                    "element_index": {"type": "integer"},
                    "text": {"type": "string"},
                    "key": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "direction": {"type": "string"},
                    "seconds": {"type": "number"},
                    "success": {"type": "boolean"},
                    "note": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                },
            },
        },
    },
}

SYSTEM_PROMPT_A11Y = (
    "You are a computer-driving agent. Each turn you receive a Markdown text "
    "legend of the application's visible interactive elements, formatted with "
    "[element_index N]. Make progress toward the user's goal by emitting a short list of actions. Available actions:\n"
    "  click(element_index)       — click the named element\n"
    "  type_text(element_index, text) — set value or type text into the element\n"
    "  press_key(element_index, key) — focus element and press a key like 'Return', 'Escape'\n"
    "  hotkey(keys)               — press a combination like ['control', 's']\n"
    "  scroll(direction)          — direction in ['up','down','left','right']\n"
    "  wait(seconds)              — pause to let the app settle\n"
    "  done(success, note)        — finish; success=true if the goal is met\n"
    "CRITICAL RULES:\n"
    "  - After a click or type, wait and observe the next turn's element list to "
    "    confirm the desired state before declaring done.\n"
    "  - Element indices change every turn! You must re-read the tree each turn.\n"
    "  - At most 2 actions per turn. Most turns should be ONE action.\n"
    "Be terse in `thinking` — one or two sentences."
)

SYSTEM_PROMPT_VISION = (
    "You are a computer-driving agent. Each turn you receive a screenshot of "
    "the current window with numbered marks over elements. "
    "Make progress toward the user's goal by emitting a short list of actions. Available actions:\n"
    "  click(element_index)       — click the element with the mark\n"
    "  type_text(element_index, text) — click the element and type text\n"
    "  press_key(key)             — press a key like 'Return', 'Escape'\n"
    "  hotkey(keys)               — press a combination like ['control', 's']\n"
    "  wait(seconds)              — pause to let the app settle\n"
    "  done(success, note)        — finish; success=true if the goal is met\n"
    "Return MULTIPLE actions in a turn only when their effect is obvious. "
    "Be terse in `thinking` — one or two sentences."
)

@dataclass
class StepRecord:
    turn: int
    thinking: str
    actions: list[dict]
    outcome: str
    provider: str
    model: str
    latency_ms: int
    tokens_in: int
    tokens_out: int

@dataclass
class ComputerDriverConfig:
    goal: str
    pid: int
    window_id: int
    max_steps: int = 12
    max_failures: int = 3
    artifacts_dir: Optional[str] = None
    pause_between_steps: float = 0.5
    provider: Optional[str] = None
    model: Optional[str] = None

@dataclass
class ComputerDriverResult:
    success: bool
    note: str
    steps: list[StepRecord] = field(default_factory=list)
    extracted: str = ""
    turns: int = 0

async def _dispatch_cua(action: dict, pid: int, window_id: int) -> str:
    t = action.get("type", "")
    if t == "wait":
        await asyncio.sleep(float(action.get("seconds", 0.5)))
        return "ok"
    if t == "done":
        return "ok"
    
    args = {"pid": pid, "window_id": window_id}
    if "element_index" in action:
        args["element_index"] = action["element_index"]
    if "x" in action and "y" in action:
        args["x"] = action["x"]
        args["y"] = action["y"]

    if t == "click":
        res = call_cua("click", args)
    elif t == "type_text":
        args["text"] = action.get("text", "")
        res = call_cua("type_text", args)
    elif t == "press_key":
        args["key"] = action.get("key", "Return")
        res = call_cua("press_key", args)
    elif t == "hotkey":
        args["keys"] = action.get("keys", [])
        res = call_cua("hotkey", args)
    elif t == "scroll":
        args["direction"] = action.get("direction", "down")
        res = call_cua("scroll", args)
    else:
        return f"error: unknown action {t!r}"
    
    if "error" in res:
        return res["error"]
    return "ok"

class ComputerBaseDriver:
    SYSTEM_PROMPT: str = ""
    LAYER_NAME: str = "base"
    
    def __init__(self, client: V9Client, config: ComputerDriverConfig):
        self.client = client
        self.config = config
        self.steps: list[StepRecord] = []
        ensure_daemon()
        
    def _history_text(self) -> str:
        if not self.steps:
            return "(no actions yet)"
        recent = self.steps[-5:]
        lines = []
        for s in recent:
            acts = ", ".join(
                f"{a['type']}({a.get('element_index') or a.get('text') or ''})"
                for a in s.actions[:3]
            )
            lines.append(f"turn {s.turn}: {acts} → {s.outcome}")
        return "\n".join(lines)
        
    async def _decide(self, state: dict, turn: int):
        raise NotImplementedError

    async def step(self, turn: int) -> tuple[bool, bool, str]:
        mode = "vision" if self.LAYER_NAME == "vision" else "ax"
        scan_args = {"pid": self.config.pid, "window_id": self.config.window_id, "capture_mode": mode}
        if self.LAYER_NAME == "vision" and self.config.artifacts_dir:
            from pathlib import Path
            d = Path(self.config.artifacts_dir)
            d.mkdir(parents=True, exist_ok=True)
            scan_args["screenshot_out_file"] = str(d / f"turn_{turn:02d}_marked.png")
            
        state = call_cua("get_window_state", scan_args)
        if state.get("element_count", 0) == 0:
            return False, False, f"error: empty AX tree, permissions issue or window closed. Error: {state.get('error', '')}"

        parsed, result = await self._decide(state, turn)
        if not parsed:
            rec = StepRecord(turn, "", [],
                             f"error: parsed output missing; raw={result.text[:120]!r}",
                             result.provider, result.model, result.latency_ms,
                             result.input_tokens, result.output_tokens)
            self.steps.append(rec)
            return False, False, "no parsed output"

        thinking = parsed.get("thinking", "")
        actions = parsed.get("actions") or []
        outcomes: list[str] = []
        done_seen, success_seen, done_note = False, False, ""
        
        for a in actions:
            if a.get("type") == "done":
                done_seen = True
                success_seen = bool(a.get("success", False))
                done_note = a.get("note", "")
                outcomes.append(f"done({success_seen})")
                break
            try:
                outcome = await _dispatch_cua(a, self.config.pid, self.config.window_id)
            except Exception as e:
                outcome = f"error: {type(e).__name__}: {e}"
            outcomes.append(outcome)
            if outcome.startswith("error"):
                break
            await asyncio.sleep(self.config.pause_between_steps)

        rec = StepRecord(
            turn=turn, thinking=thinking, actions=actions,
            outcome=" | ".join(outcomes) or "ok",
            provider=result.provider, model=result.model,
            latency_ms=result.latency_ms,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
        )
        self.steps.append(rec)
        return done_seen, success_seen, done_note

    async def run(self) -> ComputerDriverResult:
        failures = 0
        for turn in range(1, self.config.max_steps + 1):
            done, success, note = await self.step(turn)
            if not self.steps:
                return ComputerDriverResult(False, note or "failed to start", turns=turn)
            last = self.steps[-1]
            if "error" in last.outcome:
                failures += 1
                if failures >= self.config.max_failures:
                    return ComputerDriverResult(False, f"giveup after {failures} consecutive failures",
                                        steps=self.steps, turns=turn)
            else:
                failures = 0
            if done:
                return ComputerDriverResult(success, note, steps=self.steps, turns=turn)
        return ComputerDriverResult(False, f"step cap reached ({self.config.max_steps})",
                            steps=self.steps, turns=self.config.max_steps)

class A11yComputerDriver(ComputerBaseDriver):
    SYSTEM_PROMPT = SYSTEM_PROMPT_A11Y
    LAYER_NAME = "a11y"

    async def _decide(self, state: dict, turn: int):
        prompt = (
            f"GOAL: {self.config.goal}\n\n"
            f"INTERACTIVE ELEMENTS ({state.get('element_count', 0)}):\n{state.get('tree_markdown', '')}\n\n"
            f"RECENT ACTIONS:\n{self._history_text()}\n\n"
            f"What is the next set of actions? Use element_index to address elements."
        )
        result = await self.client.chat(
            prompt, system=self.SYSTEM_PROMPT,
            schema=ACTION_SCHEMA, schema_name="AgentOutput", max_tokens=1024,
            provider=self.config.provider, model=self.config.model,
        )
        return result.parsed, result

class VisionComputerDriver(ComputerBaseDriver):
    SYSTEM_PROMPT = SYSTEM_PROMPT_VISION
    LAYER_NAME = "vision"

    async def _decide(self, state: dict, turn: int):
        import base64
        from pathlib import Path
        
        screenshot_path = state.get("screenshot_out_file")
        data_url = ""
        if not screenshot_path or not Path(screenshot_path).exists():
             pass
        else:
             with open(screenshot_path, "rb") as f:
                 data_url = "data:image/png;base64," + base64.b64encode(f.read()).decode("utf-8")
             
        prompt = (
            f"GOAL: {self.config.goal}\n\n"
            f"RECENT ACTIONS:\n{self._history_text()}\n\n"
            f"What is the next set of actions?"
        )
        result = await self.client.vision(
            data_url, prompt, system=self.SYSTEM_PROMPT,
            schema=ACTION_SCHEMA, schema_name="AgentOutput", max_tokens=1024,
            provider=self.config.provider, model=self.config.model,
        )
        return result.parsed, result
