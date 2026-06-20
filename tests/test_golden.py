import importlib
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import config

GOLDEN_DIR = config.PROJECT_ROOT / "golden"
GOLDEN_FILES = sorted(GOLDEN_DIR.glob("*.yml"))


def _run(name, input_str, superscalar):
    config.SUPERSCALAR = superscalar
    for mod in ("translator", "machine"):
        sys.modules.pop(mod, None)
    translator = importlib.import_module("translator")
    machine = importlib.import_module("machine")

    src = config.EXAMPLES_DIR / f"{name}.forth"
    dest = config.BUILD_DIR / f"{name}.bin"
    t = translator.Translator()
    with redirect_stdout(io.StringIO()):
        t.translate(src, dest)
    machine_code = dest.with_suffix(".txt").read_text()

    interrupts = []
    if input_str:
        interrupts = [
            (config.START_TICK + i * config.TICK_INTERVAL, c) for i, c in enumerate(input_str)
        ]
        interrupts.append((config.START_TICK + len(input_str) * config.TICK_INTERVAL, "\x00"))

    dp = machine.DataPath(str(dest))
    cu = machine.ControlUnit(dp, interrupts)
    dp.cu = cu
    with redirect_stdout(io.StringIO()):
        cu.run()
    output = "".join(dp.output_buffer)
    return machine_code, output


@pytest.mark.parametrize("golden_file", GOLDEN_FILES, ids=lambda p: p.stem)
def test_golden(golden_file):
    with open(golden_file, encoding="utf-8-sig") as f:
        ref = yaml.safe_load(f)

    name = ref["name"][:-3] if ref["name"].endswith("_ss") else ref["name"]
    machine_code, output = _run(name, ref["input"], ref["superscalar"])

    assert output == ref["output"], f"вывод не совпал с эталоном {golden_file.name}"
    assert machine_code.strip() == ref["machine_code"].strip(), (
        f"машинный код не совпал с эталоном {golden_file.name}"
    )
