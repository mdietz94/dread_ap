"""Tripwire: the vendored open-dread-rando must carry the multi-model
action-set-ref fix (Hanubia/Itorash scenario-load crash).

WHY THIS EXISTS: any multi-model (progressive) pickup placed in
``s080_shipyard`` or ``s090_skybase`` crashes the game on scenario load
(strlen(NULL) in cazadora.nss). Root cause, proven via full Ryujinx repro
+ A/B on Dread 2.1.0 (seed AP-31983554, Progressive Spin at s080
item_missiletank_000): upstream open-dread-rando's
``ActorPickup.patch_model()`` multi-model branch PREPENDS the models'
bmsas to ``action_set_refs`` and KEEPS the template's
``actors/items/itemsphere/charclasses/timeline.bmsas`` ref — but
``patch()`` never ``ensure_present()``'d that kept ref, and s080/s090 are
the only scenarios whose pkgs don't already contain the itemsphere
assets, so actor init dereferences an unresolvable action set.
Single-model pickups REPLACE ``action_set_refs[0]`` and were never
affected.

The fix lives on our fork
(github.com/mdietz94/open-dread-rando, branch fix/multi-model-timeline-bmsas):
``ActorPickup.patch()`` ensures every entry of the final
``new_template.action_set_refs`` in each level pkg. Upstream was
deliberately NOT PR'd (owner decision) — so nothing upstream protects us,
and this test is the only guard. It fails if the vendored submodule ever
regresses to an open-dread-rando without the fix (e.g. a naive "update to
upstream main" bump).

The assertion is source-level (no romfs, no game files): it parses the
vendored ``pickups/pickup.py`` and requires that ``ActorPickup.patch``
ensure_present()s either

  * every entry of ``new_template.action_set_refs`` (our robust fork
    variant: ``for <x> in new_template.action_set_refs:
    editor.ensure_present(level_pkg, <x>)``), or
  * the literal ``actors/items/itemsphere/charclasses/timeline.bmsas``
    (the minimal one-liner variant, accepted in case upstream ever lands
    that form and we re-pin to it).

Run with:  python -m pytest apworld/dread/tests/test_odr_multimodel_fix.py -v
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread._vendor import vendored_open_dread_rando_src  # noqa: E402

TIMELINE_BMSAS = "actors/items/itemsphere/charclasses/timeline.bmsas"


@pytest.fixture(scope="module")
def pickup_py_source() -> str:
    src_dir = vendored_open_dread_rando_src()
    if src_dir is None:
        pytest.skip(
            "vendored open-dread-rando not checked out — run "
            "`git submodule update --init vendor/open-dread-rando`"
        )
    return (src_dir / "open_dread_rando" / "pickups" / "pickup.py").read_text(
        encoding="utf-8"
    )


def _actor_pickup_patch_node(source: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ActorPickup":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "patch":
                    return item
    raise AssertionError("ActorPickup.patch not found in vendored pickup.py")


def _ensure_present_second_args(node: ast.AST) -> list[ast.expr]:
    """All second arguments of editor.ensure_present(...) calls under node."""
    args = []
    for call in ast.walk(node):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "ensure_present"
            and len(call.args) >= 2
        ):
            args.append(call.args[1])
    return args


def _is_action_set_refs_attr(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Attribute) and expr.attr == "action_set_refs"


def test_vendored_odr_ensures_kept_action_set_refs(pickup_py_source):
    """ActorPickup.patch must ensure_present the template's kept action-set
    refs (the itemsphere timeline.bmsas) in every level pkg — the
    s080/s090 multi-model crash fix."""
    patch_fn = _actor_pickup_patch_node(pickup_py_source)

    # Variant A (our fork): a for-loop over new_template.action_set_refs
    # whose body ensure_present()s the loop variable.
    for loop in ast.walk(patch_fn):
        if (
            isinstance(loop, ast.For)
            and _is_action_set_refs_attr(loop.iter)
            and isinstance(loop.target, ast.Name)
        ):
            for arg in _ensure_present_second_args(loop):
                if isinstance(arg, ast.Name) and arg.id == loop.target.id:
                    return  # robust variant present

    # Variant B (minimal): a literal ensure_present of the timeline bmsas.
    for arg in _ensure_present_second_args(patch_fn):
        if isinstance(arg, ast.Constant) and arg.value == TIMELINE_BMSAS:
            return  # one-liner variant present

    raise AssertionError(
        "vendored open-dread-rando is missing the multi-model action-set-ref "
        "fix: ActorPickup.patch() neither ensure_present()s every entry of "
        f"new_template.action_set_refs nor the literal {TIMELINE_BMSAS!r}. "
        "Without it, any multi-model (progressive) pickup placed in "
        "s080_shipyard/s090_skybase crashes the game on scenario load. "
        "Re-pin vendor/open-dread-rando to a commit carrying the fix "
        "(mdietz94/open-dread-rando fix/multi-model-timeline-bmsas) — see "
        "vendor/CHANGES.md."
    )


def test_multi_model_branch_still_keeps_template_refs(pickup_py_source):
    """Documents the shape the fix guards against: the multi-model branch of
    patch_model PREPENDS (``action_set_refs[:0] = ...``) rather than
    replacing, so the template's own refs survive into the final bmsad.
    If this ever stops holding, the fix (and this file) can be re-evaluated."""
    assert "action_set_refs[:0]" in pickup_py_source, (
        "multi-model branch no longer prepends to action_set_refs — upstream "
        "restructured patch_model; re-verify the s080/s090 crash fix still "
        "applies and update test_odr_multimodel_fix.py accordingly"
    )
