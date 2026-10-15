"""The shipped examples aimed at the bench surrogates.

Each wrapper subclasses one example and changes exactly one thing: host-key
verification is off, because the targets sit on loopback ports no ``known_hosts``
entry covers. Credentials still come from the environment, as the examples document.
"""
