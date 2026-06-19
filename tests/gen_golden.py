import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import yaml
import config


class _Literal(str):
    pass


def _literal_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(_Literal, _literal_representer)

GOLDEN_DIR = config.PROJECT_ROOT / "golden"

LOG_HEAD = 20
LOG_TAIL = 20

PROGRAMS = {
    "hello": "",
    "cat": "cat",
    "hello_user_name": "Bob",
    "sort": "",
    "add64": "",
    "prob2": "",
    "exectoken": "",
    "typetest": "",
    "superscalar_demo": "",
}


def trim_log(lines):
    if len(lines) <= LOG_HEAD + LOG_TAIL:
        return list(lines)
    skipped = len(lines) - LOG_HEAD - LOG_TAIL
    head = lines[:LOG_HEAD]
    tail = lines[-LOG_TAIL:]
    marker = f"... [пропущено {skipped} тактов] ..."
    return head + [marker] + tail


def run_program(name, input_str):
    for mod in ("translator", "machine"):
        sys.modules.pop(mod, None)
    import machine
    import translator

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

    log = [str(e) for e in cu.log]
    output = "".join(dp.output_buffer)
    return machine_code, log, output


def write_golden(name, input_str, suffix):
    machine_code, log, output = run_program(name, input_str)
    source = (config.EXAMPLES_DIR / f"{name}.forth").read_text()

    data = {
        "name": name + suffix,
        "superscalar": config.SUPERSCALAR,
        "source": _Literal(source),
        "input": input_str,
        "output": output,
        "machine_code": _Literal(machine_code),
        "log": _Literal("\n".join(trim_log(log))),
    }

    GOLDEN_DIR.mkdir(exist_ok=True)
    out = GOLDEN_DIR / f"{name}{suffix}.yml"
    with open(out, "w") as f:
        yaml.dump(
            data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=120,
        )
    print(f"  {out.name}: {len(log)} тактов -> журнал обрезан, вывод={output!r}")


def main():
    suffix = "_ss" if config.SUPERSCALAR else ""
    mode = "СУПЕРСКАЛЯР" if config.SUPERSCALAR else "ОБЫЧНЫЙ"
    print(f"Генерация эталонов [{mode}], суффикс '{suffix}':")
    for name, inp in PROGRAMS.items():
        write_golden(name, inp, suffix)


if __name__ == "__main__":
    main()