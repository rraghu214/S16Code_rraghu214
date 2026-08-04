from __future__ import annotations

import json
from pathlib import Path

import pytest

from s16code.capabilities import default_registry
from s16code.core.live_graph import Event, GraphSnapshot
from s16code.planner import GeneralAgentPlanner


class Replies:
    def __init__(self, *values: dict) -> None:
        self.values = list(values)
        self.prompts: list[dict] = []

    async def __call__(self, prompt: str, _system: str) -> dict:
        self.prompts.append(json.loads(prompt))
        return {"text": json.dumps(self.values.pop(0)), "provider": "test", "model": "planner"}


def snapshot(nodes=None, edges=()):
    return GraphSnapshot("run", False, nodes or {}, tuple(edges))


@pytest.mark.asyncio
async def test_unseen_entities_can_be_decomposed_in_parallel_without_domain_code():
    reply = Replies({"add": [
        {"id": "rust", "capability": "researcher",
         "arguments": {"query": "Rust concurrency model ownership async runtimes", "subject": "Rust"},
         "depends_on": []},
        {"id": "go", "capability": "researcher",
         "arguments": {"query": "Go concurrency model goroutines channels", "subject": "Go"},
         "depends_on": []},
    ], "cancel": [], "finish": False, "reason": "independent evidence can land together"})
    planner = GeneralAgentPlanner(reply, default_registry(),
                                  goal="Research Rust and Go, then compare their concurrency models.",
                                  review_terminal=False)
    patch = await planner.plan(snapshot(), Event(1, "run_started", None, {}))
    assert [task.input["query"] for task in patch.add] == [
        "Rust concurrency model ownership async runtimes", "Go concurrency model goroutines channels"]
    assert not patch.connect


@pytest.mark.asyncio
async def test_future_work_cannot_be_pre_spawned_before_its_inputs_exist():
    reply = Replies(
        {"add": [
            {"id": "search", "capability": "web_search", "arguments": {"query": "reliable sources"},
             "depends_on": []},
            {"id": "fetch", "capability": "fetch_url", "arguments": {"url": "https://invented.invalid"},
             "depends_on": ["search"]},
        ], "cancel": [], "finish": False, "reason": "search then fetch"},
    )
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Find and read reliable sources.",
                                  review_terminal=False)
    patch = await planner.plan(snapshot(), Event(1, "run_started", None, {}))
    assert [task.id for task in patch.add] == ["search"]
    assert planner.history[0]["accepted"] is True
    assert "held future tasks fetch" in patch.reason


@pytest.mark.asyncio
async def test_discovered_urls_can_be_fetched_on_the_next_round():
    nodes = {"search": {"id": "search", "skill": "web_search", "input": {"query": "x"},
                        "metadata": {}, "state": "succeeded",
                        "result": {"hits": [{"url": "https://example.com/a"},
                                             {"url": "https://example.com/b"}]}}}
    reply = Replies({"add": [
        {"id": "fetch_a", "capability": "fetch_url", "arguments": {"url": "https://example.com/a"},
         "depends_on": ["search"]},
        {"id": "fetch_b", "capability": "fetch_url", "arguments": {"url": "https://example.com/b"},
         "depends_on": ["search"]},
    ], "cancel": [], "finish": False, "reason": "fetch concrete URLs returned by search"})
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Read two sources.", review_terminal=False)
    patch = await planner.plan(snapshot(nodes), Event(4, "task_succeeded", "search", nodes["search"]["result"]))
    assert patch.connect == (("search", "fetch_a"), ("search", "fetch_b"))


@pytest.mark.asyncio
async def test_synthesis_waits_for_every_active_evidence_sibling():
    nodes = {
        "a": {"id": "a", "skill": "researcher", "input": {}, "metadata": {},
              "state": "succeeded", "result": {"text": "A"}},
        "b": {"id": "b", "skill": "researcher", "input": {}, "metadata": {},
              "state": "running", "result": None},
    }
    reply = Replies(
        {"add": [{"id": "too_early", "capability": "distiller", "arguments": {"query": "combine"},
                  "depends_on": ["a"]}], "cancel": [], "finish": False, "reason": "partial synthesis"},
        {"add": [], "cancel": [], "finish": False, "reason": "wait for b"},
    )
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Compare A and B", review_terminal=False)
    patch = await planner.plan(snapshot(nodes), Event(4, "task_succeeded", "a", {"text": "A"}))
    assert not patch.add
    assert "held distiller" in patch.reason


@pytest.mark.asyncio
async def test_reproposing_identical_active_work_is_normalized_to_wait():
    nodes = {"research": {"id": "research", "skill": "researcher",
                          "input": {"query": "current evidence", "max_results": 3},
                          "metadata": {}, "state": "running", "result": None}}
    reply = Replies({"add": [{"id": "research_retry", "capability": "researcher",
                              "arguments": {"query": "current evidence", "max_results": 3},
                              "depends_on": []}], "cancel": [], "finish": False,
                     "reason": "retry research"})
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Research a current fact", review_terminal=False)
    patch = await planner.plan(snapshot(nodes), Event(3, "task_succeeded", "other", {}))
    assert not patch.add


@pytest.mark.asyncio
async def test_partial_frontier_outcome_does_not_spawn_an_unbounded_second_wave():
    nodes = {
        "listing": {"id": "listing", "skill": "researcher", "input": {}, "metadata": {},
                    "state": "succeeded", "result": {"text": "candidate"}},
        "reviews": {"id": "reviews", "skill": "researcher", "input": {}, "metadata": {},
                    "state": "running", "result": None},
    }
    reply = Replies({"add": [{"id": "warranty", "capability": "researcher",
                              "arguments": {"query": "candidate warranty"}, "depends_on": ["listing"]}],
                     "cancel": [], "finish": False, "reason": "follow partial evidence"})
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Compare a product",
                                  review_terminal=False)
    patch = await planner.plan(snapshot(nodes), Event(4, "task_succeeded", "listing", {"text": "candidate"}))
    assert not patch.add
    assert "active frontier finishes" in patch.reason


@pytest.mark.asyncio
async def test_unknown_capability_is_rejected_and_repaired():
    reply = Replies(
        {"add": [{"id": "shell", "capability": "run_shell", "arguments": {"command": "rm -rf /"},
                  "depends_on": []}], "cancel": [], "finish": False, "reason": "try shell"},
        {"add": [{"id": "answer", "capability": "answer_with_evidence",
                  "arguments": {"query": "Explain that shell access is unavailable."}, "depends_on": []}],
         "cancel": [], "finish": False, "reason": "answer within available authority"},
    )
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Delete the machine.", review_terminal=False)
    patch = await planner.plan(snapshot(), Event(1, "run_started", None, {}))
    assert [task.skill for task in patch.add] == ["answer_with_evidence"]
    assert planner.history[0]["accepted"] is False


@pytest.mark.asyncio
async def test_terminal_answer_is_blocked_when_generic_evidence_review_finds_missing_coverage():
    reply = Replies(
        {"add": [{"id": "answer", "capability": "answer_with_evidence",
                  "arguments": {"query": "recommend a current product"}, "depends_on": []}],
         "cancel": [], "finish": False, "reason": "answer now"},
        {"ready": False, "missing": ["current price", "independent review", "warranty"],
         "reason": "purchase constraints lack evidence"},
        {"add": [{"id": "research", "capability": "researcher",
                  "arguments": {"query": "current product price independent review warranty"},
                  "depends_on": []}], "cancel": [], "finish": False,
         "reason": "gather the missing evidence"},
    )
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Recommend a current product with warranty")
    patch = await planner.plan(snapshot(), Event(1, "run_started", None, {}))
    assert [task.skill for task in patch.add] == ["researcher"]
    review = next(item for item in planner.history if item.get("kind") == "evidence_review")
    assert review["ready"] is False


@pytest.mark.asyncio
async def test_side_effect_requires_explicit_run_authority():
    reply = Replies(
        {"add": [{"id": "write", "capability": "write_file",
                  "arguments": {"path": "out.txt", "content": "x"}}]},
        {"add": [{"id": "answer", "capability": "answer_with_evidence",
                  "arguments": {"query": "Explain that write authority was not granted."}}]},
    )
    planner = GeneralAgentPlanner(reply, default_registry(), goal="Describe a possible file",
                                  review_terminal=False)
    patch = await planner.plan(snapshot(), Event(1, "run_started", None, {}))
    assert [task.skill for task in patch.add] == ["answer_with_evidence"]
    assert "lacks explicit run authority" in planner.history[0]["error"]


def test_runtime_contains_no_prompt_router_or_benchmark_case_logic():
    source = (Path(__file__).parents[1] / "s16code" / "runtime.py").read_text()
    assert "_work_intent" not in source
    assert "DeterministicPlanner" not in source
    assert "family-friendly things to do in Tokyo" not in source
    assert "populations? of" not in source
