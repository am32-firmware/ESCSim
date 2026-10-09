from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from conftest import ROOT

MODELS = sorted((ROOT / "SITL" / "models" / "demag").glob("*.json"))
CONFIG = ROOT / "native" / "am32sim" / "src" / "sitl_config.c"


def native_keys():
    """(section, key) pairs the Renode physics accepts, from its config table
    and the list of firmware-SITL-only keys it ignores"""
    text = CONFIG.read_text()
    keys = set(re.findall(r'\{\s*"(\w+)",\s*"(\w+)",\s*CFG_', text))
    block = re.search(r"firmware_only\[\]\s*=\s*\{(.*?)\};", text, re.S)
    assert block, "firmware_only list not found"
    keys |= {("sim", k) for k in re.findall(r'"(\w+)"', block.group(1))}
    return keys


def test_demag_models_exist():
    names = {p.stem for p in MODELS}
    assert {"tbs_12s_l431", "sequre_g431", "large_12s"} <= names


@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_demag_model_keys_are_known_to_the_renode_physics(path):
    known = native_keys()
    model = json.loads(path.read_text())
    assert {"motor", "battery", "esc"} <= set(model)
    unknown = [
        f"{section}.{key}"
        for section, values in model.items()
        for key in values
        if (section, key) not in known
    ]
    assert not unknown, f"{path.name}: {unknown}"


@pytest.mark.parametrize("script", ["demag_run.py", "demag_renode.py"])
def test_demag_runners_parse_their_arguments(script):
    run = subprocess.run(
        [sys.executable, str(ROOT / "SITL" / script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "--outdir" in run.stdout
