"""Per-harness transcript-line grammars (#1309).

One module per flavour — ``claude``, ``codex``, ``grok``, ``pi``,
``antigravity``, ``copilot`` — each exporting an ``EntryBuilder`` and a
``LineKey``; ``_shared.py`` holds the generic parsing primitives two or
more of them (or ``session_transcript.py``'s own paging/image code) use.
``session_transcript.py`` imports every flavour's pair to build its
:data:`session_transcript.FLAVORS` registry — see that module for the
paging/tail/image API and the registry itself.
"""
