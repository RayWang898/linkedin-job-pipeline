"""Loaded automatically by Python when tests/fakes is on PYTHONPATH and JOBSEARCH_TEST_FAKES is set.

It swaps the three things that would leave the machine (the Anthropic SDK, the Apify client and requests), turns time.sleep into
a no-op and can pin today's date. Nothing in the engine is changed; see fakes.py for the behavior.
"""

import importlib.abc
import os
import sys

if os.environ.get("JOBSEARCH_TEST_FAKES"):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fakes

    fakes.install_basics()  # time.sleep and the pinned date apply to every module imported later

    class _PatchAfterImport(importlib.abc.MetaPathFinder):
        PATCHES = {"anthropic": fakes.patch_anthropic, "apify_client": fakes.patch_apify, "requests": fakes.patch_requests}

        def find_spec(self, name, path, target=None):
            patch = self.PATCHES.get(name)
            if patch is None:
                return None
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(name, path, target)
                if spec is not None and spec.loader is not None:
                    original = spec.loader.exec_module

                    def exec_module(module, _original=original, _patch=patch):
                        _original(module)
                        _patch(module)

                    spec.loader.exec_module = exec_module
                    return spec
            return None

    sys.meta_path.insert(0, _PatchAfterImport())
