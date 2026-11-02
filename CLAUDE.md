# CLAUDE.md

Guidance for working on the GGL simulation engine.

## Style

The engine is written like C++ in Python: structured, abstracted, and readable by someone
learning digital logic. `logic.py` and the core of `node.py` and `edge.py` are the model to
follow. In priority order:

1. **Declared members.** Every member is assigned in its own class's `__init__`, using `None`
   if it has no value yet. No class creates a member on another class's object.
2. **No reflection on known members.** No `hasattr`, `getattr(x, 'name', default)` or
   `object.__new__`. If code needs to ask what an object is, that's a missing virtual method.
3. **One name, one meaning,** across a class hierarchy.
4. **Overrides, not type tests.** No `if kind == ...` or `if t == ...` chains. Abstract methods
   use `abc.abstractmethod`, the analog of pure virtual.
5. **Parse at the boundary.** Raw JSON dicts are converted to typed classes once. Nothing past
   that point sees a dict.
6. **Trust initialized members.** Use `or` for a value only to normalize a `None` constructor
   argument, or to choose between two meaningful values. Defaults for missing data are applied
   once, in the constructor that parses it. Past that point, trust the member.
7. **Plain control flow.** Use `if/else` statements, one assignment per line and descriptive
   names. Allow a ternary only for a trivial choice between two simple values on one short line.
8. **Small units.** Aim for functions under about 25 lines and at most 3 levels of nesting.
9. **Comments** explain what a concept is or why a choice was made. Don't narrate the next line.

Don't copy repetition from older code, such as the identical gate constructors in `logic.py` or
mutable default arguments.

The propagation hot path (`Edge.propagate`, `Node.propagate`, `read_value`) is tuned for speed.
Don't restructure it without before/after timing.

## Checks

- `uv run pytest`
- `uv run mypy`: catches members that were never declared. Annotate members and signatures on
  classes you touch.
