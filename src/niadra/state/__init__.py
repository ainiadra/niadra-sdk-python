"""Typed state in the agent's process: niadra-expr, the language of the type registry's conditions, timers,
keys and readings, evaluated the same way the server evaluates it, over the four logical values of a field.
`niadra.state.expr` parses, resolves and evaluates an expression; `niadra.state.logic` holds the logical
values. Both are pure: no I/O and no clock but the one an evaluation is given.
"""
