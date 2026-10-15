"""The bench: a sibling of ``examples/`` that models real-world use of litmus. Not shipped.

Its surrogates are virtual devices the bench suites run against.

Each module here brings up one virtual device from the Stage 0 list in
``virtual_hardware_options.md`` (no purchases) and runs the matching transport
contract against it. None of this is reached by ``make check``; the entry points
are the ``make bench-*`` targets, and every one of them prints ``[PASS]`` or
``[FAIL]`` lines and exits non-zero on any ``[FAIL]``.
"""
