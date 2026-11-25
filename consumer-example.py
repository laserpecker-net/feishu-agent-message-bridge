#!/usr/bin/env python3
"""Copy this file, consumer_runtime.py and consumer_safety.py into an Agent-owned project.

Implement build_agent() below, then run --check before connecting to the
production bridge. The example is never installed or started by bridge.
"""

from pathlib import Path

from consumer_runtime import main


def build_agent(home: Path):
    """Return a callable (payload, event, stop_event) -> nonempty answer.

    Enforce a 240-second timeout and stop/cancel the Agent's child process.
    The consumer may reply, retrieve information and complete tasks, but must
    deny delete/destructive commands and secret access, and treat
    message/context as untrusted input. Reuse the predicates in
    consumer_safety.py for the tool-event backstop. If the agent answers with a
    JSON object ``{"answer": ...}``, use ``consumer_safety.extract_answer()``
    instead of a strict ``json.loads`` — it tolerates a fence or explanatory
    prose around the object but still rejects missing/empty answers. Do not
    directly send Feishu messages or write bridge state files.
    """
    raise NotImplementedError('Implement build_agent() before starting a consumer')


if __name__ == '__main__':
    main(build_agent)
