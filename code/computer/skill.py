"""Session 10: the Computer skill — cascade wrapper around the layered drivers."""
from __future__ import annotations

import time
from pathlib import Path

from schemas import AgentResult, ComputerOutput, NodeSpec
from browser.client import V9Client
from computer.driver import (
    call_cua, 
    ComputerDriverConfig, 
    A11yComputerDriver, 
    VisionComputerDriver,
    ensure_daemon
)

class ComputerSkill:
    NAME = "computer"

    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "computer",
                 a11y_provider_pin: str | None = "gemini",
                 vision_provider_pin: str | None = None,
                 artifacts_root: str | None = None,
                 max_steps_a11y: int = 12,
                 max_steps_vision: int = 12,
                 wall_clock_s: float = 90.0,
                 session: str | None = None):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.a11y_provider_pin = a11y_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.max_steps_a11y = max_steps_a11y
        self.max_steps_vision = max_steps_vision
        self.wall_clock_s = wall_clock_s
        self.session = session
        ensure_daemon()

    def _find_window(self, app_name: str) -> tuple[int, int, str]:
        # returns pid, window_id, error
        import time
        # Try finding it running first
        apps = call_cua("list_apps", {})
        if "error" in apps:
            return 0, 0, apps["error"]
        if "apps" not in apps:
            return 0, 0, "could not list apps"
        
        target_pid = 0
        for app in apps["apps"]:
            name = (app.get("name") or "").lower()
            bundle = (app.get("bundle_id") or "").lower()
            if app_name.lower() in name or app_name.lower() in bundle:
                target_pid = app["pid"]
                break
        
        if not target_pid:
            # Launch
            res = call_cua("launch_app", {"name": app_name})
            if "error" in res and "bundle_id" not in res:
                res = call_cua("launch_app", {"bundle_id": app_name})
            if "error" in res:
                return 0, 0, f"failed to launch {app_name}: {res['error']}"
            target_pid = res.get("pid", 0)
            time.sleep(2)
            
        if not target_pid:
            return 0, 0, f"could not resolve pid for {app_name}"
            
        # On Windows bring to front works well.
        call_cua("bring_to_front", {"pid": target_pid})
        time.sleep(1)
            
        # Find window
        windows = call_cua("list_windows", {})
        if "windows" not in windows:
            return 0, 0, "could not list windows"
            
        for w in windows["windows"]:
            title = (w.get("title") or "").lower()
            if w.get("pid") == target_pid or app_name.lower() in title:
                return w.get("pid", target_pid), w.get("window_id", 0), ""
                
        # Windows UWP apps might not appear in list_windows. Return pid anyway.
        return target_pid, 0, ""

    async def run(self, node: NodeSpec) -> AgentResult:
        app_name = node.metadata.get("app_name")
        if not app_name and node.inputs:
            app_name = node.inputs[0]
        goal = node.metadata.get("goal") or "interact with app"
        force_path = node.metadata.get("force_path")
        
        if not app_name:
            return self._pack_error("", goal, "interaction_failed", "no app_name given")
            
        t0 = time.time()
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag, session=self.session)
        artifacts_dir = str(self.artifacts_root / f"computer_{int(t0)}") if self.artifacts_root else None

        pid, window_id, err = self._find_window(app_name)
        if err:
            return self._pack_error(app_name, goal, "interaction_failed", err, elapsed=time.time() - t0)

        # Layer 1: Extract
        if force_path == "extract" or not force_path:
            state = call_cua("get_window_state", {"pid": pid, "window_id": window_id, "capture_mode": "ax"})
            if "error" not in state and state.get("element_count", 0) > 0:
                content = state.get("tree_markdown", "")
                if self._is_useful_extract(content, goal):
                    return self._pack(app_name, goal, "extract", turns=0, content=content, elapsed=time.time() - t0)

        # Layer 2a: Deterministic
        selectors = node.metadata.get("selectors") or []
        if selectors:
            det = await self._try_deterministic(pid, window_id, app_name, goal, selectors)
            if det is not None:
                if det.success:
                    det.elapsed_s = time.time() - t0
                    return det
                else:
                    return self._pack_error(app_name, goal, "interaction_failed", det.error or "deterministic failed", elapsed=time.time() - t0)

        # Layer 2b: a11y
        if force_path != "vision":
            cfg = ComputerDriverConfig(goal=goal, pid=pid, window_id=window_id, max_steps=self.max_steps_a11y, artifacts_dir=artifacts_dir, provider=self.a11y_provider_pin)
            drv = A11yComputerDriver(client, cfg)
            a11y_res = await drv.run()
            if a11y_res.success:
                return self._pack_driver("a11y", app_name, goal, a11y_res, elapsed=time.time() - t0)
        else:
            a11y_res = None

        # Layer 3: vision
        cfg = ComputerDriverConfig(goal=goal, pid=pid, window_id=window_id, max_steps=self.max_steps_vision, artifacts_dir=artifacts_dir, provider=self.vision_provider_pin)
        drv = VisionComputerDriver(client, cfg)
        vis_res = await drv.run()
        if vis_res.success:
            return self._pack_driver("vision", app_name, goal, vis_res, elapsed=time.time() - t0)
            
        last_err = (vis_res.note if vis_res else "") or (a11y_res.note if a11y_res else "") or "all layers exhausted"
        return self._pack_error(app_name, goal, "interaction_failed", last_err, elapsed=time.time() - t0)
        
    def _is_useful_extract(self, content: str, goal: str) -> bool:
        if len(content) < 50:
            return False
        interactive_verbs = ("click", "fill", "select", "type", "drag",
                             "filter", "sort", "submit", "navigate", "compute")
        if any(v in goal.lower() for v in interactive_verbs):
            return False
        return True

    async def _try_deterministic(self, pid: int, window_id: int, app_name: str, goal: str, selectors: list[dict]) -> AgentResult | None:
        import asyncio
        from computer.driver import _dispatch_cua
        for step in selectors:
            res = await _dispatch_cua(step, pid, window_id)
            if res != "ok":
                return self._pack_error(app_name, goal, "interaction_failed", f"deterministic step failed: {res}")
            await asyncio.sleep(0.5)
        
        state = call_cua("get_window_state", {"pid": pid, "window_id": window_id, "capture_mode": "ax"})
        content = state.get("tree_markdown", "")
        return self._pack(app_name, goal, "deterministic", turns=len(selectors), content=content)

    def _pack(self, app_name, goal, path, *, turns, content=None, actions=None, elapsed=0.0) -> AgentResult:
        out = ComputerOutput(
            app_name=app_name, goal=goal, path=path, turns=turns,
            content=content, actions=actions or []
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_driver(self, path, app_name, goal, drv_result, *, elapsed) -> AgentResult:
        out = ComputerOutput(
            app_name=app_name, goal=goal, path=path,
            turns=getattr(drv_result, "turns", 0) or 0,
            content=getattr(drv_result, "extracted", None) or None,
            actions=[{"turn": s.turn, "actions": s.actions, "outcome": s.outcome} for s in getattr(drv_result, "steps", [])]
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_error(self, app_name, goal, code, msg, *, elapsed=0.0) -> AgentResult:
        out = ComputerOutput(
            app_name=app_name or "", goal=goal, path="extract", turns=0, content=None,
        )
        return AgentResult(
            success=False, agent_name=self.NAME,
            output=out.model_dump(), error=msg, error_code=code,
            elapsed_s=elapsed,
        )
