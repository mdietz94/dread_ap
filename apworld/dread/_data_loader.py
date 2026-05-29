"""Zip-safe data file loader.

The apworld is shipped two ways:
  * Dev: installed as a folder under ``worlds/dread/`` — data
    files live at ``apworld/dread/data/*.json`` on disk.
  * End user: distributed as ``dread.apworld`` (a zip) dropped
    into Archipelago's ``custom_worlds/``. Data files live inside the zip
    at ``dread/data/*.json``.

``Path(__file__).parent / "data" / *.json`` works for the folder case but
breaks inside a zip. ``importlib.resources.files()`` handles both
transparently — for a zip-imported package it returns a ``zipfile.Path``-
like wrapper whose ``read_text()`` reads through ``zipimport``.

Use ``load_json("items.json")`` from any module under the apworld; it
resolves to the data file regardless of how the apworld was loaded.
"""
from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def load_json(name: str) -> Any:
    """Load and parse a JSON file under the apworld's ``data/`` dir.

    ``name`` is the bare filename (e.g. ``"items.json"``), no leading
    directories.
    """
    resource = files(__package__).joinpath("data").joinpath(name)
    return json.loads(resource.read_text(encoding="utf-8"))


def read_text(name: str) -> str:
    """Read a non-JSON data file as text."""
    resource = files(__package__).joinpath("data").joinpath(name)
    return resource.read_text(encoding="utf-8")
