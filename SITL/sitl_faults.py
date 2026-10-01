'''
Deliberate faults on the link between a configurator and the simulated ESC.

A configurator that works against a clean link is not the same as one
that works against a real setup. USB serial drops frames, a loaded flight
controller answers late, and a marginal signal wire corrupts what comes
back. Those are the conditions the failures people report are made of,
and none of them can be produced by asking the simulation to behave.

FaultyEndpoint wraps the endpoint the USB device is served on, so the
same faults apply whichever way a configurator is reaching the ESC: the
fake flight controller passing BLHeli 4-way through (msp_stub_fc), the
1-wire linker bridge for direct mode (sitl_serial_bridge), and either
transport underneath, a pty or the virtual USB device.

LinkFaults is live: the GUI mutates it while a configurator is connected,
so a session can be broken and mended to show what each fault does.
'''

import random
import threading
import time


class LinkFaults(object):
    '''what to do to the link, and a tally of what has been done

    Rates are per frame, 0 disabling that fault. "to host" is the device
    answering a configurator, "from host" the configurator asking. The
    seed makes a run repeatable, which matters when the point is to show
    the same failure twice.
    '''

    TO_HOST = 'to host'
    FROM_HOST = 'from host'
    BOTH = 'both'

    def __init__(self, seed=1):
        self.lock = threading.Lock()
        self.enabled = False
        self.drop = 0.0
        self.corrupt = 0.0
        self.truncate = 0.0
        self.duplicate = 0.0
        self.delay_ms = 0.0
        self.jitter_ms = 0.0
        self.direction = self.BOTH
        self.seed = seed
        self.rng = random.Random(seed)
        self.stats = {}

    def reset(self, seed=None):
        '''forget the tally and start the same sequence again'''
        with self.lock:
            if seed is not None:
                self.seed = seed
            self.rng = random.Random(self.seed)
            self.stats = {}

    def summary(self):
        with self.lock:
            if not self.stats:
                return 'no faults injected yet'
            return ', '.join('%s %u' % kv for kv in sorted(self.stats.items()))

    def _count(self, name):
        self.stats[name] = self.stats.get(name, 0) + 1

    def affects(self, direction):
        return self.enabled and self.direction in (direction, self.BOTH)

    def apply(self, data, direction, log=None):
        '''return the frames to actually deliver, which may be none

        Any delay is taken by the caller, which is the thread already
        carrying that frame, so it holds up exactly what a slow link
        would and nothing else.
        '''
        if not data or not self.affects(direction):
            return [data]

        with self.lock:
            rng = self.rng
            if rng.random() < self.drop:
                self._count('dropped')
                if log:
                    log('fault: dropped %u bytes %s' % (len(data), direction))
                return []

            out = bytearray(data)
            if len(out) > 1 and rng.random() < self.truncate:
                cut = rng.randrange(1, len(out))
                self._count('truncated')
                if log:
                    log('fault: cut a %s frame %u -> %u bytes'
                        % (direction, len(out), cut))
                out = out[:cut]

            if rng.random() < self.corrupt:
                i = rng.randrange(len(out))
                out[i] ^= 1 << rng.randrange(8)
                self._count('corrupted')
                if log:
                    log('fault: flipped a bit in %s byte %u' % (direction, i))

            frames = [bytes(out)]
            if rng.random() < self.duplicate:
                self._count('duplicated')
                if log:
                    log('fault: sent a %s frame twice' % direction)
                frames.append(bytes(out))

            held = self.delay_ms + (rng.uniform(0, self.jitter_ms)
                                    if self.jitter_ms else 0.0)
            if held > 0:
                self._count('delayed')

        # outside the lock: holding it through a sleep would serialise the
        # other direction behind this frame, which no real link does
        if held > 0:
            if log:
                log('fault: held a %s frame %.0fms' % (direction, held))
            time.sleep(held / 1000.0)
        return frames


class FaultyEndpoint(object):
    '''an endpoint that misbehaves, wrapping one that does not

    Only what the stubs use is forwarded: read, write, drain, close and
    path. The endpoint itself is left alone, so whoever created it can
    still attach and detach the USB device through it.
    '''

    def __init__(self, endpoint, faults, log=None):
        self.endpoint = endpoint
        self.faults = faults
        self.log = log

    @property
    def path(self):
        return self.endpoint.path

    def read(self, timeout=0.1):
        data = self.endpoint.read(timeout)
        if not data:
            return data
        frames = self.faults.apply(data, LinkFaults.FROM_HOST, self.log)
        return b''.join(frames)

    def drain(self):
        out = b''
        while True:
            chunk = self.read(0)
            if not chunk:
                return out
            out += chunk

    def write(self, data):
        for frame in self.faults.apply(data, LinkFaults.TO_HOST, self.log):
            self.endpoint.write(frame)

    def close(self):
        self.endpoint.close()
