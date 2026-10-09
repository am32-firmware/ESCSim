#!/usr/bin/env python3
"""Run a demag benchmark against AM32 firmware emulated in Renode, headless.

The Renode target generator boots the real ARM firmware with modelled timers,
comparator and bridge, the same motor physics as the native SITL (built with
make -C native/am32sim) and the same UDP input and state ports. This sends
DShot through the input port, reads rotor speed from the state stream and
reads firmware globals through the Renode monitor at the addresses
arm-none-eabi-nm gives for the ELF. The state stream carries no back-EMF,
duty or diode channels, so the checks are sustained rotation and no desync
counter increment while powered, plus the demag guard's counters when the
firmware has them.

    python3 SITL/demag_renode.py --target VIMDRONES_L431 \\
        --elf /path/to/AM32/obj/AM32_VIMDRONES_L431_2.21.elf --outdir out/l431 \\
        --model demag/tbs_12s_l431 --throttle-cap 300 --hold 2 --physics-us 2 \\
        --set MOTOR_KV=27 --set MOTOR_POLES=14 --set MIN_DUTY_CYCLE=4

--physics-us below the generator's default of 20 resolves sub-sector timing
at a proportional cost in speed. The targets header defaults to the one in
the ELF's firmware checkout.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

WATCHED = ['desync_happened', 'bemf_timeout_happened', 'duty_cycle', 'commutation_interval', 'zero_crosses',
           'demag_active', 'demag_cap', 'demag_valid', 'demag_predicted', 'demag_warnings', 'demag_faults',
           'demag_level', 'demag_advance_level', 'demag_late', 'demag_late_max', 'demag_commutation_late',
           'demag_commutation_late_max', 'demag_switch_delay_max']


def free_port(kind=socket.SOCK_DGRAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def symbol_table(elf, names, nm='arm-none-eabi-nm'):
    out = subprocess.check_output([nm, '-S', str(elf)], text=True)
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[3] in names:
            table[parts[3]] = (int(parts[0], 16), int(parts[1], 16))
    return table


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--target', required=True, help='AM32 target name, e.g. VIMDRONES_L431')
    ap.add_argument('--elf', type=Path, required=True)
    ap.add_argument('--targets-file', type=Path, help="default: Inc/targets.h in the ELF's firmware checkout")
    ap.add_argument('--outdir', type=Path, required=True)
    ap.add_argument('--benchmark', default='demag_full_duty', help='recipe key from demag_bench.py')
    ap.add_argument('--model', help='model name under SITL/models (e.g. demag/tbs_12s_l431) or a JSON path')
    ap.add_argument('--sim', action='append', default=[], metavar='KEY=VALUE', help='override a sim key of the model')
    ap.add_argument('--set', action='append', default=[], metavar='NAME=VALUE', help='override an EEPROM setting')
    ap.add_argument('--loads', help='comma separated load_k_omega2 values replacing the recipe load stages')
    ap.add_argument('--hold', type=float, default=2.0, help='extended hold in seconds')
    ap.add_argument('--throttle-cap', type=int, help='clamp every recipe stage throttle to this value')
    ap.add_argument('--stage-scale', type=float, default=1.0, help='scale every stage duration')
    ap.add_argument('--physics-us', type=int, default=20, help='bridge physics step in microseconds')
    args = ap.parse_args()

    out = args.outdir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    elf = args.elf.resolve()
    checkout = elf.parent.parent
    if not os.environ.get('AM32_ROOT') and (checkout / 'Inc' / 'version.h').is_file():
        os.environ['AM32_ROOT'] = str(checkout)
    targets_file = (args.targets_file or Path(os.environ.get('AM32_ROOT', checkout)) / 'Inc' / 'targets.h').resolve()
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(ROOT / 'src'))
    import sitl_dshot as sd
    import sitl_params
    from dataclasses import replace
    from sitl_benchmarks import registry, eeprom_image
    from sitl_gui_backend import SimStream
    from escsim.renode.monitor import MonitorClient

    recipe = registry()[args.benchmark]
    settings = dict(recipe.settings)
    for item in args.set:
        name, value = item.split('=', 1)
        settings[name] = int(value)
    if args.throttle_cap is not None:
        recipe = replace(recipe, stages=tuple(replace(st, throttle=min(st.throttle, args.throttle_cap))
                                              for st in recipe.stages))
    if args.loads:
        loads = [float(v) for v in args.loads.split(',')]
        idx = [i for i, st in enumerate(recipe.stages) if st.load is not None]
        if len(loads) != len(idx):
            raise SystemExit(f'--loads needs {len(idx)} values for {recipe.key}')
        stages = list(recipe.stages)
        for i, v in zip(idx, loads):
            stages[i] = replace(stages[i], load=v)
        recipe = replace(recipe, stages=tuple(stages))
    (out / 'eeprom.bin').write_bytes(eeprom_image(sitl_params.base_image(), settings))
    model_name = args.model or recipe.model
    model_file = Path(model_name) if model_name.endswith('.json') else HERE / 'models' / f'{model_name}.json'
    model = json.loads(model_file.read_text())
    for item in args.sim:
        key, value = item.split('=', 1)
        model.setdefault('sim', {})[key] = parse_value(value)
    (out / 'model.json').write_text(json.dumps(model, indent=2) + '\n')

    syms = symbol_table(elf, WATCHED)
    gui_port, state_port = free_port(), free_port()
    while state_port == gui_port:
        state_port = free_port()
    monitor_port = free_port(socket.SOCK_STREAM)
    command = [sys.executable, '-m', 'escsim.renode.generator', args.target, '--link',
               '--gui-port', str(gui_port), '--gui-state-port', str(state_port), '--monitor-port', str(monitor_port),
               '--gui-dshot-us', '250', '--elf', str(elf), '--eeprom', str(out / 'eeprom.bin'),
               '--model', str(out / 'model.json'), '--targets-file', str(targets_file),
               '--physics-us', str(args.physics_us)]
    result = dict(backend='renode', target=args.target, benchmark=recipe.key, command=command, settings=settings,
                  model=model, stages=[], symbols={k: v[0] for k, v in syms.items()})
    log = (out / 'renode.log').open('w')
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src') + os.pathsep + os.environ.get('PYTHONPATH', ''))
    proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    tx = sim = mon = None
    sending = threading.Event()
    throttle = [0]
    last_t = [-1.0]
    try:
        mon = MonitorClient('127.0.0.1', monitor_port)
        deadline = time.monotonic() + 120
        while True:
            try:
                mon.connect(timeout=10)
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Renode did not start; see renode.log')
                time.sleep(1)

        def read(name):
            if name not in syms:
                return None
            addr, size = syms[name]
            cmd = {1: 'ReadByte', 2: 'ReadWord'}.get(size, 'ReadDoubleWord')
            text = mon.command(f'sysbus {cmd} 0x{addr:08X}', timeout=10)
            for tok in text.replace('\r', '\n').split('\n'):
                tok = tok.strip()
                if tok.startswith('0x'):
                    return int(tok, 16)
            return None

        tx = sd.InputPort('127.0.0.1', gui_port)
        sending.set()

        def sender():
            while sending.is_set():
                tx.send_dshot(throttle[0], ptype=sd.TYPE_DSHOT300)
                time.sleep(.002)
        threading.Thread(target=sender, daemon=True).start()
        sim = SimStream('127.0.0.1', state_port, period_us=100, maxlen=200000)
        sim.enabled = True

        def collect():
            if proc.poll() is not None:
                raise RuntimeError(f'emulator exited {proc.returncode}')
            with sim.lock:
                batch = list(sim.samples)
                sim.samples.clear()
            got = []
            for s in batch:
                if s[0] <= last_t[0]:
                    continue
                last_t[0] = s[0]
                got.append((s[0], s[1] * 60 / 6.283185307))
            return got

        deadline = time.monotonic() + 240
        while last_t[0] < 0:
            collect()
            if time.monotonic() > deadline:
                raise RuntimeError('no state stream from Renode')
            time.sleep(.05)
        scale = args.stage_scale
        stages = [(s.throttle, s.seconds * scale, s.load, s.label or f'dshot-{s.throttle}') for s in recipe.stages]
        stages += [(stages[-1][0], args.hold * scale, None, 'extended hold')]
        if recipe.key == 'demag_full_overload':
            stages += [(2047, .3 * scale, model['motor']['load_k_omega2'], 'load release')]
        stages += [(0, .5 * scale, None, 'stop')]
        for index, (value, seconds, load, label) in enumerate(stages):
            if load is not None:
                model['motor']['load_k_omega2'] = load
                path = out / f'model-{index}.json'
                path.write_text(json.dumps(model, indent=2) + '\n')
                sim.load_model(str(path))
            throttle[0] = value
            start = last_t[0]
            wall = time.monotonic() + max(60, seconds * 200)
            got = []
            while last_t[0] - start < seconds:
                got.extend(collect())
                if time.monotonic() > wall:
                    raise RuntimeError(f'stage timed out: {label}')
                time.sleep(.02)
            tail = [r for r in got if r[0] >= last_t[0] - min(.1, seconds / 2)]
            metrics = dict(label=label, throttle=value, load=load, start=start, end=last_t[0], samples=len(got),
                           mean_rpm=sum(r[1] for r in tail) / max(1, len(tail)),
                           min_rpm=min((r[1] for r in tail), default=0),
                           firmware={n: read(n) for n in WATCHED if n in syms})
            result['stages'].append(metrics)
            print(json.dumps(metrics), flush=True)

        all_stages = result['stages']
        powered = all_stages[:-1]
        partial = bool(args.throttle_cap and args.throttle_cap < 1000)
        work = ([s for s in all_stages if s['label'] == 'extended hold'] if partial
                else [s for s in all_stages if s['throttle'] >= 1000])

        def counter(stage, name):
            value = stage['firmware'].get(name)
            return 1 if value is None else value  # an unreadable counter is a failure, not a zero
        result['desyncs_at_stop'] = counter(all_stages[-1], 'desync_happened') - counter(powered[-1], 'desync_happened')
        checks = dict(completed=True,
                      no_desync=counter(powered[-1], 'desync_happened') == 0,
                      sustained_rotation=bool(work) and all(
                          (s['mean_rpm'] > 500 and s['min_rpm'] > .5 * s['mean_rpm']) if partial else s['min_rpm'] > 3000
                          for s in work))
        if 'demag_faults' in syms:
            last = powered[-1]['firmware']
            checks.update(no_guard_exit=last.get('demag_faults') == 0,
                          no_late_service=last.get('demag_late') == 0,
                          no_late_commutation=last.get('demag_commutation_late') == 0)
        result['checks'] = checks
        result['pass'] = all(checks.values())
    except Exception as ex:
        result.update(error=str(ex), **{'pass': False})
    finally:
        throttle[0] = 0
        sending.clear()
        if tx:
            tx.close()
        if sim:
            sim.close()
        if mon:
            try:
                mon.command('quit', timeout=5)
            except Exception:
                pass
            mon.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'pass': result['pass'], 'checks': result.get('checks'),
                      'desyncs_at_stop': result.get('desyncs_at_stop'), 'error': result.get('error')}))
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
