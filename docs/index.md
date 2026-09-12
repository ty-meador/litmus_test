# litmus

React-for-hardware HIL testing framework. A Floor Technician categorizes a
failure and starts the right recovery protocol without reading suite source;
a developer writes the suite that makes that possible.

- **Floor technicians:** the station page is the
  [Floor Technician guide](floor-technician.md). That is the URL that belongs
  on a station label.
- **Suite authors:** start at the [overview](overview.md), then
  [idioms](idioms.md) and [contracts](contracts.md).
- **Public surface:** [API stability](api-stability.md) is the policy;
  the [API reference](api.md) is generated from the public names' docstrings.
- **Inspector contributors:** the [target specification](inspector.md) carries the
  terminal workflow to build. The Python Inspector is not shipped yet.

Build this site locally with `make docs`. A broken cross-reference fails the
gate. Nothing hosts it: GitLab renders every page from `docs/` on `main`, and a
release branch carries the built site under `docs/site/` (`make docs-release`) so a
station can open it from a clone without a network (#138).
