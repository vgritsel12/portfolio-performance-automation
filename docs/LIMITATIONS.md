# Public-release limitations

This package preserves the core renderer/model modules, Java overlays, principal launchers and import automation source. Private client inputs, official reports based on those inputs, FX caches, runtime bundles and platform-specific delivery packages are not included.

The reproducible demo is cash-only. It does not validate security prices, buys/sells, fees, taxes, multiple currencies, grouped clients, the full dashboard publication gate, or native Windows execution. The preserved launcher may download third-party build dependencies on first use.

The existing original test file grouped independent parser tests with a private-XML setup. Twenty independent methods and their helper were extracted into `tests/test_public_model.py`; test logic was retained, formatting changed. Private-data assertions and tests requiring missing client reports were excluded.

A new synthetic XML was created through the existing CSV adapter and given fixed dashboard periods for the public demo. `PP_CLIENT_SCOPE=FULL_XML` is required by this fixture and set by `scripts/demo.py`. Its annualized IRR uses the engine's actual cash-flow dates; it is not a claim about real investment returns.

The pinned official engine generated the included report. This does not constitute independent financial certification. The portfolio owner is not credited with inventing TTWROR/IRR or authoring Portfolio Performance.
