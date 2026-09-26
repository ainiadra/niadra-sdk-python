"""Code copied verbatim from the SDK this repository ships, for a release the harness cannot install yet.

`niadra_backing.py` is `src/niadra/backing.py` (the backed-answers rule, `niadra.backing`), which the
harness uses as a check that is the same for every system (metrics/backing.py). The harness installs the
SDK from PyPI (`niadra==0.4.0`), which predates it; once the release that has `niadra.backing` is the one
the harness installs, `metrics/backing.py` imports it from there and this copy goes. tests/test_backing.py
fails when the copy and `src/niadra/backing.py` differ.
"""
