"""Drift guard between this app's inline Anthropic pricing and ``mngr_usage``'s table.

``mngr_usage`` derives token->USD cost from a curated per-token table whose
Anthropic entries are mirrored verbatim from this app's inline ``LITELLM_CONFIG``
pricing. The two can't share a single source by import: ``app.py`` is deployed
into a Modal image that installs none of the imbue packages, so it must carry the
prices inline (the same reason it doesn't rely on litellm's bundled map). This
test makes the "mirrored verbatim" claim enforceable -- changing an Anthropic
price on either side without the other fails here.

Contract: every Anthropic model this app prices must exist in
``mngr_usage``'s table with identical per-token prices. ``mngr_usage`` may carry
*additional* models (other providers, or Anthropic models this proxy doesn't
route) -- those are not constrained here.
"""

import importlib.util
from pathlib import Path
from typing import Any

from imbue.mngr_usage.pricing import MODEL_PRICING

_THIS_DIR = Path(__file__).parent


def _load_deployed_model_list() -> list[dict[str, Any]]:
    """Load LITELLM_CONFIG['model_list'] from the Modal app module (app.py)."""
    app_path = _THIS_DIR / "app.py"
    spec = importlib.util.spec_from_file_location("modal_litellm_app_for_pricing_drift", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LITELLM_CONFIG["model_list"]


def test_mngr_usage_anthropic_pricing_matches_modal_litellm_inline_pricing() -> None:
    deployed_model_list = _load_deployed_model_list()
    # Sanity: the proxy registers a non-trivial Anthropic set, so an empty/renamed
    # model_list can't make this test vacuously pass.
    assert len(deployed_model_list) >= 4

    for entry in deployed_model_list:
        model_name = entry["model_name"]
        params = entry["litellm_params"]
        key = f"anthropic/{model_name}"
        assert key in MODEL_PRICING, (
            f"modal_litellm prices {model_name!r} but mngr_usage's table has no {key!r} "
            f"-- add it to mngr_usage/pricing.py (or this is drift)"
        )
        prices = MODEL_PRICING[key]
        assert prices.input_cost_per_token == params["input_cost_per_token"], f"input price drift for {key}"
        assert prices.output_cost_per_token == params["output_cost_per_token"], f"output price drift for {key}"
        assert prices.cache_read_input_token_cost == params["cache_read_input_token_cost"], (
            f"cache_read price drift for {key}"
        )
        assert prices.cache_creation_input_token_cost == params["cache_creation_input_token_cost"], (
            f"cache_creation price drift for {key}"
        )
