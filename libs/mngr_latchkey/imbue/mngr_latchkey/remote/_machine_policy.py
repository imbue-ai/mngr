"""The one adjustment a host's policy gets on its way to and from the machine that enforces it.

A remote host's machine enforces this computer's copy of the host's policy as a
whole snapshot, and this computer adopts the machine's copy back on every read
(:mod:`imbue.mngr_latchkey.remote.credentials`). One baseline rule is kept out
of the machine's copy: the per-agent Minds API gate, the ``{<unauthorized
scope>: []}`` rule of
:data:`~imbue.mngr_latchkey.baseline_permissions.AGENT_BASELINE_PERMISSIONS`,
which rejects a ``/minds-api-proxy/api/v1/agents/<id>/...`` request whose agent
id the host has not registered. The machine never serves those routes itself:
it forwards each one to a desktop, whose gateway runs the same gate against the
same per-host policy. On the machine the gate rejects nothing the desktop would
not, except when no desktop is reachable, where it tells an agent the user
refused it when the truth is that the desktop is away.

The schema the rule names stays in the machine's copy: it carries the host's
registered-agent list, which is per-host state the machine owns like the rest
of the policy. So the projection is the rule alone, and it is exact: a policy
restored from the machine is the policy that was pushed, which is what lets a
read adopt the machine's copy without losing the gate here.

Both directions are applied at the wire, in
:mod:`imbue.mngr_latchkey.remote._machine`, so no caller has to remember them.
A policy that is not one this build reads travels as it is, in both
directions: validating it, and refusing it, belong to the callers that already
do that.
"""

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.baseline_permissions import SCOPE_LATCHKEY_SELF
from imbue.mngr_latchkey.baseline_permissions import SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import dump_permissions_text
from imbue.mngr_latchkey.store import load_permissions_from_text


@pure
def project_policy_for_machine(permissions_json: str) -> str:
    """``permissions_json`` without the per-agent gate rule; unchanged when it carries none."""
    config = _parsed(permissions_json)
    if config is None or not _has_gate_rule(config):
        return permissions_json
    rules = tuple(rule for rule in config.rules if SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED not in rule)
    return dump_permissions_text(config.model_copy_update(to_update(config.field_ref().rules, rules)))


@pure
def restore_policy_from_machine(permissions_json: str) -> str:
    """``permissions_json`` with the per-agent gate rule back ahead of the gateway-self rule.

    Unchanged when the rule is already there, or when the policy does not
    define the gate's schema at all (it is not a host policy this plugin
    seeded, so there is no gate to restore).
    """
    config = _parsed(permissions_json)
    if config is None or _has_gate_rule(config) or SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED not in config.schemas:
        return permissions_json
    gateway_self_index = next(
        (index for index, rule in enumerate(config.rules) if SCOPE_LATCHKEY_SELF in rule), len(config.rules)
    )
    gate_rule: dict[str, list[str]] = {SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED: []}
    rules = (*config.rules[:gateway_self_index], gate_rule, *config.rules[gateway_self_index:])
    return dump_permissions_text(config.model_copy_update(to_update(config.field_ref().rules, rules)))


@pure
def _has_gate_rule(config: LatchkeyPermissionsConfig) -> bool:
    return any(SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED in rule for rule in config.rules)


@pure
def _parsed(permissions_json: str) -> LatchkeyPermissionsConfig | None:
    try:
        return load_permissions_from_text(permissions_json)
    except LatchkeyStoreError:
        return None
