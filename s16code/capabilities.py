"""The complete capability surface exposed to the S16 planner.

The planner sees descriptions and machine-checkable argument contracts. Workers
remain ordinary Python callables owned by the runtime; a model can select a
capability, but it cannot invent one or bypass its validation boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


class CapabilityError(ValueError):
    """A proposed task does not satisfy the advertised capability contract."""


@dataclass(frozen=True)
class Argument:
    kind: str
    description: str
    required: bool = True
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    item_kind: str | None = None

    def manifest(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind, "description": self.description}
        if not self.required:
            result["required"] = False
            if self.default is not None:
                result["default"] = self.default
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["enum"] = list(self.choices)
        if self.item_kind:
            result["items"] = {"type": self.item_kind}
        return result


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    arguments: dict[str, Argument] = field(default_factory=dict)
    role: str | None = None
    side_effect: bool = False
    terminal_for: tuple[str, ...] = ()

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": {name: spec.manifest() for name, spec in self.arguments.items()},
            "side_effect": self.side_effect,
            "terminal_for": list(self.terminal_for),
        }


class CapabilityRegistry:
    def __init__(self, capabilities: list[Capability]) -> None:
        self._items = {item.name: item for item in capabilities}
        if len(self._items) != len(capabilities):
            raise ValueError("capability names must be unique")

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def get(self, name: str) -> Capability:
        try:
            return self._items[name]
        except KeyError as error:
            raise CapabilityError(f"unknown capability {name!r}") from error

    def manifest(self) -> list[dict[str, Any]]:
        return [item.manifest() for item in self._items.values()]

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def terminal_skills(self, respond_as: str) -> set[str]:
        return {item.name for item in self._items.values() if respond_as in item.terminal_for}

    def validate(self, name: str, values: Any) -> dict[str, Any]:
        capability = self.get(name)
        if not isinstance(values, dict):
            raise CapabilityError(f"arguments for {name} must be an object")
        unknown = set(values).difference(capability.arguments)
        if unknown:
            raise CapabilityError(f"unsupported arguments for {name}: {sorted(unknown)}")
        clean: dict[str, Any] = {}
        for key, spec in capability.arguments.items():
            if key not in values:
                if spec.required:
                    raise CapabilityError(f"{name} requires argument {key!r}")
                if spec.default is not None:
                    clean[key] = spec.default
                continue
            value = values[key]
            if spec.kind == "string":
                if not isinstance(value, str) or not value.strip():
                    raise CapabilityError(f"{name}.{key} must be a non-empty string")
                value = value.strip()
                if spec.maximum is not None and len(value) > spec.maximum:
                    raise CapabilityError(f"{name}.{key} exceeds {spec.maximum} characters")
            elif spec.kind == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise CapabilityError(f"{name}.{key} must be an integer")
                if spec.minimum is not None and value < spec.minimum:
                    raise CapabilityError(f"{name}.{key} must be >= {spec.minimum}")
                if spec.maximum is not None and value > spec.maximum:
                    raise CapabilityError(f"{name}.{key} must be <= {spec.maximum}")
            elif spec.kind == "array":
                if not isinstance(value, list):
                    raise CapabilityError(f"{name}.{key} must be an array")
                if spec.minimum is not None and len(value) < spec.minimum:
                    raise CapabilityError(f"{name}.{key} needs at least {spec.minimum} items")
                if spec.maximum is not None and len(value) > spec.maximum:
                    raise CapabilityError(f"{name}.{key} permits at most {spec.maximum} items")
                if spec.item_kind == "string" and not all(isinstance(item, str) and item.strip() for item in value):
                    raise CapabilityError(f"every {name}.{key} item must be a non-empty string")
                value = [item.strip() if isinstance(item, str) else item for item in value]
            elif spec.kind == "boolean":
                if not isinstance(value, bool):
                    raise CapabilityError(f"{name}.{key} must be a boolean")
            else:
                raise CapabilityError(f"unsupported contract type {spec.kind!r}")
            if spec.choices and value not in spec.choices:
                raise CapabilityError(f"{name}.{key} must be one of {list(spec.choices)}")
            clean[key] = value

        if name == "fetch_url":
            parsed = urlparse(clean["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CapabilityError("fetch_url.url must be an absolute http(s) URL")
        if name == "a2a_delegate":
            parsed = urlparse(clean["agent_url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CapabilityError("a2a_delegate.agent_url must be an absolute http(s) URL")
        if name == "launch_job":
            parsed = urlparse(clean["endpoint"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CapabilityError("launch_job.endpoint must be an absolute http(s) URL")
        return clean


def default_registry() -> CapabilityRegistry:
    def string(description: str, **kwargs: Any) -> Argument:
        return Argument("string", description, **kwargs)

    def integer(description: str, **kwargs: Any) -> Argument:
        return Argument("integer", description, **kwargs)

    def boolean(description: str, **kwargs: Any) -> Argument:
        return Argument("boolean", description, **kwargs)

    return CapabilityRegistry([
        Capability("memory_recall", "Retrieve authorised facts, prior episodes, playbooks, and indexed document chunks relevant to a query.",
                   {"query": string("What must be recalled from durable scoped memory.", maximum=20_000)}),
        Capability("remember_explicit_fact", "Persist a fact only when the user explicitly asked to remember or correct it.",
                   {"text": string("The minimal fact to preserve, without conversational filler.", maximum=20_000)},
                   side_effect=True),
        Capability("web_search", "Search the public web and return titles, URLs, and snippets. Use fetch_url later when page text is needed.",
                   {"query": string("A complete standalone search query.", maximum=2_000),
                    "max_results": integer("Number of results.", required=False, default=3, minimum=1, maximum=5)}),
        Capability("fetch_url", "Read one concrete HTTP(S) URL discovered in the request or a previous outcome.",
                   {"url": string("Absolute HTTP(S) URL.", maximum=4_000)}),
        Capability("researcher", "A bounded specialist agent: search, read the returned pages, and synthesize supported claims with URLs. Launch several together for genuinely independent questions; make queries favor primary sources.",
                   {"query": string("A precise, standalone research question containing its subject and requested evidence.", maximum=4_000),
                    "max_results": integer("Number of search results.", required=False, default=3, minimum=1, maximum=5),
                    "subject": string("Short label for attribution.", required=False, maximum=200)}, role="researcher"),
        Capability("read_file", "Read a UTF-8 file inside the configured sandbox. Paths are relative to the sandbox.",
                   {"path": string("Sandbox-relative file path.", maximum=2_000)}),
        Capability("write_file", "Write a UTF-8 text artifact inside the configured sandbox, then return its path and SHA-256 digest. Use only when the user explicitly requests a file or a repair.",
                   {"path": string("Sandbox-relative destination path.", maximum=2_000),
                    "content": string("Complete text to write.", maximum=60_000),
                    "overwrite": boolean("Whether an existing file may be replaced.", required=False, default=False)},
                   side_effect=True),
        Capability("copy_file", "Copy one sandbox file byte-for-byte to another sandbox path and verify the source and destination digests. Prefer this over regenerating content when exact preservation is requested.",
                   {"source": string("Existing sandbox-relative source path.", maximum=2_000),
                    "destination": string("Sandbox-relative destination path.", maximum=2_000),
                    "overwrite": boolean("Whether an existing destination may be replaced.", required=False, default=False)},
                   side_effect=True),
        Capability("file_sha256", "Compute the SHA-256 digest and byte size of one sandbox file for deterministic artifact verification.",
                   {"path": string("Sandbox-relative file path.", maximum=2_000)}),
        Capability("verify_artifact", "Read and SHA-256 verify one file URI created by this run's artifact-producing capabilities.",
                   {"uri": string("A file:// URI returned by a completed capability.", maximum=4_000)}),
        Capability("calculate", "Evaluate a bounded arithmetic expression deterministically. Supports numbers, arithmetic, comparisons, and min/max/sum/round/abs.",
                   {"expression": string("Standalone arithmetic expression, with no assignments or imports.", maximum=2_000)}),
        Capability("query_csv", "Load one or more sandbox CSV files into an in-memory database and run one read-only SELECT query. Use for joins, grouping, filtering, and multi-row arithmetic instead of many scalar calculations.",
                   {"files": Argument("array", "Sandbox-relative CSV paths; each filename stem becomes its SQL table name.",
                                      minimum=1, maximum=10, item_kind="string"),
                    "sql": string("One read-only SQLite SELECT or WITH query.", maximum=10_000)}),
        Capability("current_datetime", "Return the current date and time in an IANA timezone. Use when relative dates or the meaning of current/today must be resolved.",
                   {"timezone": string("IANA timezone such as UTC or Asia/Kolkata.", required=False, default="UTC", maximum=100)}),
        Capability("date_shift", "Add or subtract an exact number of calendar days from an ISO date.",
                   {"date": string("ISO-8601 date (YYYY-MM-DD).", maximum=10),
                    "days": integer("Signed number of calendar days.", minimum=-10000, maximum=10000)}),
        Capability("list_directory", "List immediate subdirectories and files with one suffix inside a sandbox directory; use returned concrete paths in later tasks.",
                   {"path": string("Sandbox-relative directory.", maximum=2_000),
                    "suffix": string("File suffix including the leading dot.", required=False, default=".md", maximum=32)}),
        Capability("index_file", "Semantically chunk, embed, and atomically index one sandbox file into scoped memory.",
                   {"path": string("Sandbox-relative file path.", maximum=2_000)}, side_effect=True),
        Capability("create_calendar_events", "Create local iCalendar artifacts for explicit ISO dates requested by the user.",
                   {"title": string("Human-readable event title.", maximum=300),
                    "dates": Argument("array", "ISO-8601 dates (YYYY-MM-DD).", minimum=1, maximum=20, item_kind="string")},
                   side_effect=True),
        Capability("list_channels", "Ask GLC for the currently installed channel adapters and their connection state. The list is discovered at runtime, never encoded in the planner.",
                   {}),
        Capability("send_channel_message", "Send one text message through an installed GLC channel. Use only when the request or an authorised subscription explicitly identifies the channel and recipient.",
                   {"channel": string("Channel name returned by list_channels or supplied by authorised context.", maximum=100),
                    "recipient_id": string("Provider-native recipient, conversation, address, or account identifier.", maximum=2_000),
                    "text": string("Message to send.", maximum=20_000),
                    "thread_id": string("Existing provider thread identifier when replying in context.", required=False, maximum=2_000),
                    "voice_audio_ref": string("Existing audio artifact reference for a voice-capable channel.", required=False, maximum=4_000)},
                   side_effect=True),
        Capability("a2a_delegate", "Discover a remote A2A agent, verify its Agent Card under local trust policy, and delegate one task.",
                   {"agent_url": string("Base URL of the remote agent.", maximum=4_000),
                    "message": string("Self-contained delegated task.", maximum=20_000)}, role="delegate"),
        Capability("launch_job", "Launch any program or agent that implements the asynchronous job contract. It returns immediately with a durable handle; a later signed completion event resumes this graph.",
                   {"endpoint": string("HTTP(S) endpoint that accepts the generic job envelope.", maximum=4_000),
                    "task": string("Self-contained work request, with desired output contract.", maximum=20_000)},
                   role="delegate", side_effect=True),
        Capability("request_approval", "Pause this run and ask a human one concrete question before an irreversible or ambiguous action. A later approval event resumes from the response.",
                   {"question": string("Decision the human must make, including relevant consequences.", maximum=4_000),
                    "choices": Argument("array", "Short allowed responses.", required=False,
                                        maximum=10, item_kind="string")},
                   role="approval", side_effect=True),
        Capability("retriever", "Retrieve scoped memory and have a specialist summarize only the retrieved evidence.",
                   {"query": string("A precise retrieval question.", maximum=20_000)}, role="retriever"),
        Capability("distiller", "Synthesize completed upstream outcomes into the information needed by the goal.",
                   {"query": string("What to synthesize and the required output shape.", maximum=20_000)}, role="distiller"),
        Capability("summariser", "Condense completed upstream outcomes without introducing unsupported claims.",
                   {"query": string("What to summarize and desired emphasis.", maximum=20_000)}, role="summariser"),
        Capability("coder_validator", "Check completed upstream output for structural, numerical, or comparison errors.",
                   {"query": string("Explicit validation criteria.", maximum=20_000)}, role="coder_validator"),
        Capability("content", "Produce structured domain-neutral content for a UI from the goal and completed upstream outcomes.",
                   {"query": string("UI content goal.", maximum=20_000)}, role="content"),
        Capability("compose_surface", "Compose and validate an A2UI surface from completed upstream outcomes.",
                   {"query": string("Interface goal and important presentation constraints.", maximum=20_000)},
                   role="ui_composer", terminal_for=("ui",)),
        Capability("answer_with_evidence", "Produce the final grounded text answer from all completed graph outcomes. Use directly for tasks requiring no tools.",
                   {"query": string("The user's goal, including the requested answer format.", maximum=20_000)},
                   role="answer", terminal_for=("text",)),
    ])
