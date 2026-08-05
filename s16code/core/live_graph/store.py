"""NetworkX live graphs persisted as one atomic JSON checkpoint per run."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph

from .core import Event, GraphPatch, GraphSnapshot, NodeState, TaskSpec


class GraphMutationError(ValueError):
    pass


class GraphStore:
    """A small, inspectable graph store without a second database.

    NetworkX is the graph engine. JSON is only its durable envelope. Every
    mutation replaces one run checkpoint atomically, which is enough for the
    laptop-sized autonomous runs this harness teaches.
    """

    FORMAT = "s16-networkx-run-v1"

    def __init__(self, path: str | Path):
        # Older callers use names such as graph.sqlite. Keep that public API,
        # but the path is now a directory containing one JSON file per run.
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def close(self) -> None:
        """There is no connection to close."""

    def _path(self, run_id: str) -> Path:
        return self.path / f"{hashlib.sha256(run_id.encode()).hexdigest()}.json"

    @staticmethod
    def _new(run_id: str, context: dict[str, Any]) -> dict[str, Any]:
        graph = nx.DiGraph()
        graph.graph.update(run_id=run_id, finished=False, context=context)
        return {"format": GraphStore.FORMAT, "version": 0, "graph": graph,
                "events": [], "applied_triggers": {}}

    def _load(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id)
        if not path.exists():
            raise KeyError(run_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("format") != self.FORMAT:
            raise GraphMutationError(f"unsupported graph checkpoint: {path}")
        raw["graph"] = json_graph.node_link_graph(raw["graph"], edges="edges")
        if raw["graph"].graph.get("run_id") != run_id:
            raise GraphMutationError("checkpoint run id does not match its lookup key")
        return raw

    def _save(self, state: dict[str, Any]) -> None:
        graph: nx.DiGraph = state["graph"]
        path = self._path(str(graph.graph["run_id"]))
        serial = {**state, "version": int(state["version"]) + 1,
                  "graph": json_graph.node_link_data(graph, edges="edges")}
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=self.path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(serial, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        state["version"] = serial["version"]

    @staticmethod
    def _event(state: dict[str, Any], kind: str, node_id: str | None,
               payload: dict[str, Any]) -> Event:
        raw = {"sequence": len(state["events"]) + 1, "kind": kind, "node_id": node_id,
               "payload": payload, "recorded_at": datetime.now(UTC).isoformat()}
        state["events"].append(raw)
        return Event(**raw)

    @staticmethod
    def _as_event(raw: dict[str, Any]) -> Event:
        return Event(raw["sequence"], raw["kind"], raw.get("node_id"), raw["payload"],
                     raw.get("recorded_at"))

    def start(self, run_id: str, *, context: dict[str, Any] | None = None) -> bool:
        with self._lock:
            if self._path(run_id).exists():
                return False
            state = self._new(run_id, context or {})
            self._event(state, "run_started", None, {})
            self._save(state)
            return True

    def context(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._load(run_id)["graph"].graph.get("context", {}))

    def resume(self, run_id: str) -> None:
        with self._lock:
            state = self._load(run_id)
            for _, node in state["graph"].nodes(data=True):
                if node["state"] == NodeState.RUNNING:
                    node["state"] = NodeState.PENDING
            self._event(state, "run_resumed", None, {})
            self._save(state)

    def latest_event(self, run_id: str) -> Event | None:
        with self._lock:
            events = self._load(run_id)["events"]
            return self._as_event(events[-1]) if events else None

    def snapshot(self, run_id: str) -> GraphSnapshot:
        with self._lock:
            graph = self._load(run_id)["graph"]
            nodes = {str(node_id): {"id": str(node_id), **dict(value)}
                     for node_id, value in sorted(graph.nodes(data=True), key=lambda pair: str(pair[0]))}
            edges = tuple(sorted((str(parent), str(child)) for parent, child in graph.edges))
            return GraphSnapshot(run_id, bool(graph.graph.get("finished")), nodes, edges)

    def ready(self, run_id: str, *, limit: int) -> list[TaskSpec]:
        with self._lock:
            graph = self._load(run_id)["graph"]
            ready: list[TaskSpec] = []
            for node_id in sorted(graph.nodes, key=str):
                node = graph.nodes[node_id]
                if node["state"] != NodeState.PENDING:
                    continue
                if all(graph.nodes[parent]["state"] == NodeState.SUCCEEDED
                       for parent in graph.predecessors(node_id)):
                    ready.append(TaskSpec(str(node_id), node["skill"], node["input"], node["metadata"]))
                    if len(ready) == limit:
                        break
            return ready

    def mark_running(self, run_id: str, tasks: list[TaskSpec]) -> None:
        with self._lock:
            state = self._load(run_id)
            graph = state["graph"]
            for task in tasks:
                if graph.nodes[task.id]["state"] != NodeState.PENDING:
                    continue
                graph.nodes[task.id]["state"] = NodeState.RUNNING
                self._event(state, "task_started", task.id,
                            {"skill": task.skill, "agent": task.metadata.get("agent", task.skill)})
            self._save(state)

    def record_outcome(self, run_id: str, node_id: str, success: bool,
                       payload: dict[str, Any]) -> Event:
        with self._lock:
            state = self._load(run_id)
            node = state["graph"].nodes[node_id]
            if node["state"] != NodeState.RUNNING:
                raise GraphMutationError(f"cannot record outcome for {node_id}: it is not running")
            node["state"] = NodeState.SUCCEEDED if success else NodeState.FAILED
            node["result"] = payload
            event = self._event(state, "task_succeeded" if success else "task_failed", node_id, payload)
            self._save(state)
            return event

    def record_waiting(self, run_id: str, node_id: str, wait: dict[str, Any]) -> Event:
        """Park a launched task so it no longer consumes an executor worker."""
        with self._lock:
            state = self._load(run_id)
            node = state["graph"].nodes[node_id]
            if node["state"] != NodeState.RUNNING:
                raise GraphMutationError(f"cannot wait {node_id}: it is not running")
            node["state"], node["wait"] = NodeState.WAITING, wait
            event = self._event(state, "task_waiting", node_id, wait)
            self._save(state)
            return event

    def complete_waiting(self, handle: str, event_type: str, payload: dict[str, Any],
                         *, success: bool = True) -> tuple[str, str, Event] | None:
        """Complete one parked task; a duplicate delivery becomes a no-op."""
        with self._lock:
            matches: list[tuple[str, dict[str, Any], str]] = []
            for path in self.path.glob("*.json"):
                raw = json.loads(path.read_text(encoding="utf-8"))
                graph = json_graph.node_link_graph(raw["graph"], edges="edges")
                for node_id, node in graph.nodes(data=True):
                    wait = node.get("wait") or {}
                    if (node.get("state") == NodeState.WAITING and wait.get("handle") == handle
                            and wait.get("event_type") == event_type):
                        raw["graph"] = graph
                        matches.append((str(graph.graph["run_id"]), raw, str(node_id)))
            if not matches:
                return None
            if len(matches) != 1:
                raise GraphMutationError(f"wait handle {handle!r} is not unique")
            run_id, state, node_id = matches[0]
            node = state["graph"].nodes[node_id]
            node["state"] = NodeState.SUCCEEDED if success else NodeState.FAILED
            node["result"] = payload
            node.pop("wait", None)
            self._event(state, "external_event_received", node_id,
                        {"handle": handle, "event_type": event_type})
            event = self._event(state, "task_succeeded" if success else "task_failed", node_id, payload)
            self._save(state)
            return run_id, node_id, event

    def pending_planner_events(self, run_id: str) -> list[Event]:
        with self._lock:
            state = self._load(run_id)
            applied = state["applied_triggers"]
            return [self._as_event(raw) for raw in state["events"]
                    if raw["kind"] in {"run_started", "task_succeeded", "task_failed"}
                    and str(raw["sequence"]) not in applied]

    def apply_patch(self, run_id: str, patch: GraphPatch, *, trigger_event: int) -> bool:
        with self._lock:
            state = self._load(run_id)
            graph: nx.DiGraph = state["graph"]
            trigger = str(trigger_event)
            if trigger in state["applied_triggers"]:
                return False
            if graph.graph.get("finished"):
                raise GraphMutationError("cannot mutate a finished graph")
            existing_ids = set(graph.nodes)
            add_ids = [task.id for task in patch.add]
            if len(add_ids) != len(set(add_ids)) or existing_ids.intersection(add_ids):
                raise GraphMutationError("patch adds duplicate task id")
            all_ids = existing_ids.union(add_ids)
            for parent, child in patch.connect:
                if parent not in all_ids or child not in all_ids:
                    raise GraphMutationError(f"edge {parent}->{child} references unknown task")
                if parent == child:
                    raise GraphMutationError("a task cannot depend on itself")
            for node_id in (*patch.cancel, *patch.wait, *patch.resume):
                if node_id not in all_ids:
                    raise GraphMutationError(f"patch references unknown task {node_id}")
            for node_id in patch.cancel:
                if graph.nodes[node_id]["state"] not in {
                    NodeState.PENDING, NodeState.WAITING, NodeState.RUNNING
                }:
                    raise GraphMutationError(f"can only cancel active task {node_id}")
            for node_id in patch.wait:
                if node_id in existing_ids and graph.nodes[node_id]["state"] != NodeState.PENDING:
                    raise GraphMutationError(f"can only wait pending task {node_id}")
            for node_id in patch.resume:
                if node_id in existing_ids and graph.nodes[node_id]["state"] != NodeState.WAITING:
                    raise GraphMutationError(f"can only resume waiting task {node_id}")

            for task in patch.add:
                graph.add_node(task.id, skill=task.skill, input=task.input, metadata=task.metadata,
                               state=NodeState.PENDING, result=None)
            graph.add_edges_from(patch.connect)
            if not nx.is_directed_acyclic_graph(graph):
                raise GraphMutationError("patch would create a dependency cycle")
            for node_id in patch.cancel:
                was_running = graph.nodes[node_id]["state"] == NodeState.RUNNING
                graph.nodes[node_id]["state"] = NodeState.CANCELLED
                self._event(state, "task_cancelled", node_id,
                            {"reason": patch.reason, "was_running": was_running})
            for node_id in patch.wait:
                graph.nodes[node_id]["state"] = NodeState.WAITING
            for node_id in patch.resume:
                graph.nodes[node_id]["state"] = NodeState.PENDING
            if patch.finish:
                for node_id, node in graph.nodes(data=True):
                    if node["state"] in {NodeState.PENDING, NodeState.WAITING, NodeState.RUNNING}:
                        was_running = node["state"] == NodeState.RUNNING
                        node["state"] = NodeState.CANCELLED
                        self._event(state, "task_cancelled", str(node_id),
                                    {"reason": "graph finished", "was_running": was_running})
                graph.graph["finished"] = True
            patch_payload = {"trigger_event": trigger_event, "reason": patch.reason, "add": add_ids,
                             "connect": list(patch.connect), "cancel": list(patch.cancel),
                             "wait": list(patch.wait), "resume": list(patch.resume),
                             "finish": patch.finish, **patch.metadata}
            self._event(state, "graph_patched", None, patch_payload)
            state["applied_triggers"][trigger] = patch_payload
            self._save(state)
            return True

    def is_finished(self, run_id: str) -> bool:
        with self._lock:
            return bool(self._load(run_id)["graph"].graph.get("finished"))

    def node_state(self, run_id: str, node_id: str) -> str:
        with self._lock:
            graph = self._load(run_id)["graph"]
            if node_id not in graph:
                raise KeyError(f"unknown task {node_id!r} in run {run_id!r}")
            return str(graph.nodes[node_id]["state"])

    def events(self, run_id: str) -> list[Event]:
        with self._lock:
            return [self._as_event(raw) for raw in self._load(run_id)["events"]]

    def record_external_event(self, run_id: str, kind: str, node_id: str,
                              payload: dict[str, Any]) -> Event:
        with self._lock:
            state = self._load(run_id)
            event = self._event(state, kind, node_id, payload)
            self._save(state)
            return event
