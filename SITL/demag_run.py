#!/usr/bin/env python3
"""Run a demag benchmark or a throttle sweep against the native SITL, headless.

Each run gets a fresh EEPROM built from the recipe's settings, private UDP
ports and its own output directory: result.json (settings, model, command,
per-stage metrics and checks), model.json, a 50 us trace (trace.csv.gz), the
firmware log and the watched firmware variables (watch.json.gz). The trace
is observational; nothing in it is fed back to the firmware.

    # a recipe from demag_bench.py
    python3 SITL/demag_run.py --outdir out/overload --benchmark demag_full_overload --hold 5

    # a throttle sweep on a bench-calibrated model (see DEMAG.md)
    python3 SITL/demag_run.py --sitl /path/to/AM32/obj/AM32_AM32_SITL_CAN_2.21.elf \\
        --outdir out/sweep --model demag/tbs_12s_l431 --sweep 200:600:40 --hold 2 \\
        --loop-ns 250 --advance 2 --set MOTOR_KV=27 --set MOTOR_POLES=14 --set MIN_DUTY_CYCLE=4

The SITL binary defaults to the one am32_paths finds. A binary given with
--sitl inside an AM32 checkout's obj/ takes its EEPROM defaults from that
checkout unless AM32_ROOT is set. Exit status 0 means every check passed.
"""
import argparse
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent

# MCU timing profiles for the SITL. g071 and f051 model a Cortex-M0+/M0:
# slower register reads and interrupt entry, the comparator handler that
# holds an in-window edge pending, the interval timer width, and the demag
# guard's M0 policy limits. They need a SITL that knows these sim keys.
PROFILES = {
    'g431': {},
    'g071': dict(isr_read_ns=250, irq_latency_ns=500, comparator_hold_pending=True, interval_timer_bits=16,
                 demag_max_advance_level=16, demag_min_wait_ticks=16, demag_deadline_margin_ticks=8),
    'f051': dict(isr_read_ns=330, irq_latency_ns=700, comparator_hold_pending=True, interval_timer_bits=32,
                 demag_max_advance_level=16, demag_min_wait_ticks=16, demag_deadline_margin_ticks=8),
}

# firmware variables recorded through the SITL watch; the demag guard's
# counters are read when the binary has them
WATCHED = ['duty_cycle', 'commutation_interval', 'desync_happened', 'thiszctime', 'rising',
           'demag_active', 'demag_latched', 'demag_cap', 'demag_valid', 'demag_predicted',
           'demag_warnings', 'demag_faults', 'demag_level', 'demag_advance_level', 'demag_adv_offset',
           'demag_late', 'demag_late_max', 'demag_commutation_late', 'demag_commutation_late_max',
           'demag_switch_delay_max']

TRACE_COLUMNS = ['t_s', 'rpm', 'theta_e', 'ia', 'ib', 'ic', 'va', 'vb', 'vc', 'vbus', 'ibus',
                 'mode_a', 'mode_b', 'mode_c', 'bemf_a', 'bemf_b', 'bemf_c', 'duty', 'desync',
                 'torque_nm', 'pwm_compare_duty']


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def firmware_root(sitl):
    """the checkout whose EEPROM defaults match the binary"""
    if os.environ.get('AM32_ROOT'):
        return Path(os.environ['AM32_ROOT'])
    checkout = sitl.parent.parent
    if (checkout / 'Inc' / 'version.h').is_file():
        os.environ['AM32_ROOT'] = str(checkout)
        return checkout
    return None


def git(root, *args):
    try:
        return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def model_path(name):
    path = Path(name)
    if name.endswith('.json'):
        return path
    return HERE / 'models' / f'{name}.json'


def interval_stats(watch, names, t0, t1):
    """mean accepted crossing interval of rising and falling sectors over
    [t0, t1], from the thiszctime and rising watches: boards that accept the
    falling-sector crossing early alternate, and the ratio is a calibration
    target for the comparator front-end model"""
    if 'thiszctime' not in names or 'rising' not in names:
        return {}
    zcs = [(t, v) for t, v in watch.get(names.index('thiszctime')) if t0 <= t <= t1]
    pol = watch.get(names.index('rising'))
    rising, falling = [], []
    prev, j = None, 0
    for t, v in zcs:
        if prev is not None and v != prev and 0 < v < 60000:
            while j + 1 < len(pol) and pol[j + 1][0] <= t:
                j += 1
            if pol and pol[j][0] <= t:
                (rising if pol[j][1] else falling).append(v)
        prev = v
    out = dict(interval_n=len(rising) + len(falling))
    if rising and falling:
        out.update(interval_r=sum(rising) / len(rising), interval_f=sum(falling) / len(falling))
        out['interval_ratio'] = out['interval_r'] / out['interval_f']
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sitl', type=Path, help='SITL binary (default: the one am32_paths finds)')
    ap.add_argument('--outdir', type=Path, required=True)
    ap.add_argument('--benchmark', default='demag_full_duty', help='recipe key from demag_bench.py')
    ap.add_argument('--model', help='model name under SITL/models (e.g. demag/tbs_12s_l431) or a JSON path')
    ap.add_argument('--sim', action='append', default=[], metavar='KEY=VALUE',
                    help='override a sim key of the model, e.g. comparator_noise_mv=25')
    ap.add_argument('--profile', choices=sorted(PROFILES), default='g431', help='MCU timing profile')
    ap.add_argument('--loop-ns', type=int, default=500, help='SITL main-loop pacing, sim.loop_time_ns')
    ap.add_argument('--set', action='append', default=[], metavar='NAME=VALUE',
                    help='override an EEPROM setting, e.g. MOTOR_POLES=28')
    ap.add_argument('--advance', type=int, help='shorthand for --set ADVANCE_LEVEL=N')
    ap.add_argument('--hold', type=float, default=1.0, help='extended hold, and each sweep step, in seconds')
    ap.add_argument('--release', type=float, default=0.3, help='load release after the overload recipe, seconds')
    ap.add_argument('--loads', help='comma separated load_k_omega2 values replacing the recipe load stages')
    ap.add_argument('--throttle-cap', type=int, help='clamp every recipe stage throttle to this value')
    ap.add_argument('--sweep', metavar='START:STOP:STEP',
                    help='replace the recipe after its arming stage with a throttle sweep')
    ap.add_argument('--chop', action='store_true', help='add a low throttle chop and recovery before the stop')
    ap.add_argument('--scope', action='store_true',
                    help='end with a 500 ns observer-only capture after the extended hold; no stop test')
    args = ap.parse_args()

    out = args.outdir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.sitl:
        args.sitl = args.sitl.resolve()
        fw_root = firmware_root(args.sitl)
    sys.path.insert(0, str(HERE))
    import am32_paths
    if not args.sitl:
        args.sitl = Path(am32_paths.sitl_binary()).resolve()
        fw_root = firmware_root(args.sitl)
    fw_root = fw_root or Path(am32_paths.am32_root())
    import sitl_dshot as sd
    import sitl_params
    from dataclasses import replace
    from sitl_benchmarks import registry, eeprom_image
    from sitl_gui_backend import SimStream
    from sitl_scope import ScopeFrame
    from run_test import WatchStream, elf_symbol_sizes

    recipe = registry()[args.benchmark]
    settings = dict(recipe.settings)
    if args.advance is not None:
        settings['ADVANCE_LEVEL'] = args.advance
    for item in args.set:
        name, value = item.split('=', 1)
        settings[name] = int(value)
    seed = eeprom_image(sitl_params.base_image(), settings)
    (out / 'eeprom.bin').write_bytes(seed)

    model = json.loads(model_path(args.model or recipe.model).read_text())
    sim_cfg = model.setdefault('sim', {})
    sim_cfg['loop_time_ns'] = args.loop_ns
    sim_cfg.update(PROFILES[args.profile])
    for item in args.sim:
        key, value = item.split('=', 1)
        sim_cfg[key] = parse_value(value)
    if args.throttle_cap is not None:
        recipe = replace(recipe, stages=tuple(replace(st, throttle=min(st.throttle, args.throttle_cap))
                                              for st in recipe.stages))
    if args.loads:
        loads = [float(v) for v in args.loads.split(',')]
        load_stages = [i for i, st in enumerate(recipe.stages) if st.load is not None]
        if len(loads) != len(load_stages):
            raise SystemExit(f'--loads needs {len(load_stages)} values for {recipe.key}')
        stages = list(recipe.stages)
        for i, v in zip(load_stages, loads):
            stages[i] = replace(stages[i], load=v)
        recipe = replace(recipe, stages=tuple(stages))
    (out / 'model.json').write_text(json.dumps(model, indent=2) + '\n')
    original_load = model['motor']['load_k_omega2']

    input_port, state_port = free_port(), free_port()
    while state_port == input_port:
        state_port = free_port()
    command = [str(args.sitl), '--config', str(out / 'model.json'), '--eeprom', str(out / 'eeprom.bin'),
               '--input-port', str(input_port), '--state-port', str(state_port),
               '--input-type', '1', '--can-uri', 'none', '--speedup', '1', '--exit-on-reset', '--verbose']
    result = dict(benchmark=recipe.key, profile=args.profile, settings=settings, model=model, command=command,
                  sitl_sha256=hashlib.sha256(args.sitl.read_bytes()).hexdigest(),
                  firmware_revision=(git(fw_root, 'rev-parse', 'HEAD') or '').strip() or None,
                  escsim_revision=(git(HERE, 'rev-parse', 'HEAD') or '').strip() or None,
                  stages=[], checks={}, sample_period_us=50, scope_only=args.scope)
    diff = git(fw_root, 'diff', 'HEAD')
    result['firmware_dirty'] = bool(diff)
    if diff:
        (out / 'source.patch').write_text(diff)

    sizes = elf_symbol_sizes(args.sitl)
    variables = [(n, sizes[n], False, None) for n in WATCHED if n in sizes]
    names = [v[0] for v in variables]
    proc = sim = watch = tx = thread = None
    sending = threading.Event()
    throttle = [0]
    fine_samples = None
    last_t = -1
    log = (out / 'firmware.log').open('w')
    trace = gzip.open(out / 'trace.csv.gz', 'wt', newline='')
    writer = csv.writer(trace)
    writer.writerow(TRACE_COLUMNS)
    try:
        proc = subprocess.Popen(command, stdout=log, stderr=log)
        tx = sd.InputPort('127.0.0.1', input_port)
        sending.set()

        def sender():
            while sending.is_set():
                tx.send_dshot(throttle[0], ptype=sd.TYPE_DSHOT300)
                time.sleep(.001)
        thread = threading.Thread(target=sender, daemon=True)
        thread.start()
        sim = SimStream('127.0.0.1', state_port, period_us=50, maxlen=100000)
        sim.scope_enabled = True
        sim.enabled = True
        watch = WatchStream('127.0.0.1', state_port, variables, min_period_ns=50000)
        if watch.wait_resolved() != [1] * len(variables):
            raise RuntimeError('variable watch did not resolve')

        def collect():
            nonlocal last_t
            if proc.poll() is not None:
                raise RuntimeError(f'firmware exited {proc.returncode}')
            with sim.lock:
                batch = list(sim.samples)
                sim.samples.clear()
            rows = []
            for s in batch:
                if s[0] < last_t:
                    raise RuntimeError('simulation clock reset')
                if s[0] == last_t:
                    continue
                last_t = s[0]
                if len(s) < 25:
                    raise RuntimeError('scope v3 data missing')
                rpm = s[1] * 60 / (2 * math.pi)
                torque = sum(s[4 + i] * s[15 + i] for i in range(3)) / s[1] if abs(s[1]) > 1 else 0
                driven = any(m in (2, 3, 5, 6, 7) for m in s[12])
                row = [s[0], rpm, s[3], *s[4:12], *s[12], *s[15:18], s[23] if driven else 0, s[24], torque, s[23]]
                writer.writerow(row)
                rows.append(row)
                if fine_samples is not None:
                    fine_samples.append(s)
            return rows

        deadline = time.monotonic() + 15
        while last_t < 0:
            collect()
            if time.monotonic() > deadline:
                raise RuntimeError('no state stream')
            time.sleep(.01)

        stages = [(s.throttle, s.seconds, s.load, s.label or f'dshot-{s.throttle}') for s in recipe.stages]
        if args.sweep:
            lo, hi, step = (int(v) for v in args.sweep.split(':'))
            stages = stages[:1] + [(t, args.hold, None, f'sweep-{t}') for t in range(lo, hi + 1, step)]
        stages += [(stages[-1][0], args.hold, None, 'extended hold')]
        if recipe.key == 'demag_full_overload':
            stages += [(2047, args.release, original_load, 'load release')]
        if args.chop:
            stages += [(150, .5, None, 'low throttle chop'), (1000, 1.0, None, 'throttle recovery')]
        stages += [(0, .5, None, 'stop')]
        for index, (value, seconds, load, label) in enumerate(stages):
            if load is not None:
                model['motor']['load_k_omega2'] = load
                path = out / f'model-{index}.json'
                path.write_text(json.dumps(model, indent=2) + '\n')
                sim.load_model(str(path))
            throttle[0] = value
            start = last_t
            deadline = time.monotonic() + max(30, seconds * 20)
            rows = []
            while last_t - start < seconds:
                rows.extend(collect())
                if time.monotonic() > deadline:
                    raise RuntimeError(f'stage timed out: {label}')
                time.sleep(.005)
            tail = [r for r in rows if r[0] >= last_t - min(.1, seconds / 2)]
            values = {n: watch.get(i)[-1][1] for i, n in enumerate(names) if watch.get(i)}
            metrics = dict(label=label, throttle=value, load=load, start=start, end=last_t,
                           samples=len(rows), firmware=values,
                           mean_rpm=sum(r[1] for r in tail) / max(1, len(tail)),
                           min_rpm=min((r[1] for r in tail), default=0),
                           mean_duty=sum(r[17] for r in tail) / max(1, len(tail)),
                           mean_torque_nm=sum(r[19] for r in tail) / max(1, len(tail)),
                           max_phase_current_a=max((abs(r[i]) for r in rows for i in (3, 4, 5)), default=0),
                           desyncs=max((r[18] for r in rows), default=0),
                           max_sample_gap_s=max((b[0] - a[0] for a, b in zip(rows, rows[1:])), default=0))
            metrics.update(interval_stats(watch, names, last_t - min(1.0, seconds / 2), last_t))
            result['stages'].append(metrics)
            print(json.dumps(metrics), flush=True)
            if args.scope and label == 'extended hold':
                sim.set_speedup(.01)
                sim.period_us = .5
                sim.sock.sendto(struct.pack('<HBBI', sim.MAGIC_CMD, 0, 2, 500), sim.addr)
                # drain the old-rate backlog, then capture an observer-only window
                for _ in range(10):
                    collect()
                    time.sleep(.01)
                fine_samples = []
                capture_start = last_t
                capture_wall = time.monotonic() + 10
                while last_t - capture_start < .002:
                    collect()
                    if time.monotonic() > capture_wall:
                        raise RuntimeError('fine capture timeout')
                    time.sleep(.005)
                frame = ScopeFrame(tuple(fine_samples), fine_samples[0][0], 'Commutation',
                                   fine_samples[-1][0] - fine_samples[0][0], 0)
                frame.save(out / 'scope.csv', dict(benchmark=recipe.key, source='observer only'))
                result['scope_measurements'] = [frame.measurements(p) for p in range(3)]
                fine_samples = None
                break

        all_stages = result['stages']
        stopped = all_stages[-1]['label'] == 'stop'
        powered = all_stages[:-1] if stopped else all_stages
        partial = bool(args.sweep or (args.throttle_cap and args.throttle_cap < 1000))
        if partial:
            work = [s for s in all_stages if s['label'].startswith('sweep-') or s['label'] == 'extended hold']
        else:
            work = [s for s in all_stages if s['throttle'] >= 1000]
        # demag guard predictions per guarded sector, step by step through a sweep
        worst_predicted = 0.0
        prev = None
        for s in all_stages:
            fw = s['firmware']
            if s['label'].startswith('sweep-') and prev is not None and 'demag_valid' in fw:
                valid = fw['demag_valid'] - prev['demag_valid']
                s['predicted_per_valid'] = (fw['demag_predicted'] - prev['demag_predicted']) / max(1, valid)
                worst_predicted = max(worst_predicted, s['predicted_per_valid'])
            prev = fw if 'demag_valid' in fw else prev
        # the desync counter is cumulative; a count first reached at the
        # zero-throttle stop is reported apart from the powered stages
        result['desyncs_at_stop'] = all_stages[-1]['desyncs'] - powered[-1]['desyncs'] if stopped else 0
        result['checks'] = dict(
            completed=True,
            no_desync=powered[-1]['desyncs'] == 0,
            no_latched_fault=all(s['firmware'].get('demag_latched', 0) == 0 for s in work),
            sustained_rotation=bool(work) and all(
                (s['mean_rpm'] > 500 and s['min_rpm'] > .5 * s['mean_rpm']) if partial else s['min_rpm'] > 3000
                for s in work),
            sweep_predictions_bounded=worst_predicted <= .02,
            positive_drive=all(s['mean_torque_nm'] > 0 and s['mean_duty'] > .05 for s in work),
            coarse_capture_contiguous=all(s['max_sample_gap_s'] < .005 for s in all_stages),
            zero_duty_on_stop=args.scope or all_stages[-1]['mean_duty'] < .01)
        result['pass'] = all(result['checks'].values())
    except Exception as ex:
        result.update(error=str(ex), **{'pass': False})
    finally:
        throttle[0] = 0
        sending.clear()
        if thread:
            thread.join(timeout=2)
        if tx:
            tx.close()
        if watch:
            with gzip.open(out / 'watch.json.gz', 'wt') as f:
                json.dump({n: watch.get(i) for i, n in enumerate(names)}, f)
            watch.close()
        if sim:
            sim.close()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        trace.close()
        log.close()
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'pass': result['pass'], 'checks': result.get('checks'),
                      'desyncs_at_stop': result.get('desyncs_at_stop'), 'error': result.get('error')}))
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
