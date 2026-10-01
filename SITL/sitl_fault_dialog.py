#!/usr/bin/env python3
'''
link fault dialog for the SITL GUI.

Breaks the link to a connected configurator on purpose, so the effect of
a lost frame, a late one or a corrupted one can be watched rather than
argued about. Everything applies live: turn a fault on while a flash is
running and the configurator meets it mid-transfer.

The tally underneath counts what has actually been done to the link,
which is what tells a configurator that coped apart from one that was
simply lucky.
'''

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout)

from sitl_faults import LinkFaults


class FaultDialog(QDialog):
    '''live controls for LinkFaults'''

    def __init__(self, faults, parent=None):
        super().__init__(parent)
        self.faults = faults
        self.setWindowTitle('Link faults')

        layout = QVBoxLayout(self)
        self.enable = QCheckBox('Apply faults')
        self.enable.setToolTip(
            'Apply the faults below to every frame between the\n'
            'configurator and the simulated ESC. Unticked, the link is\n'
            'clean however the rates are set. Safe to toggle while a\n'
            'configurator is connected, including mid-flash.')
        self.enable.setChecked(faults.enabled)
        layout.addWidget(self.enable)

        rates = QGroupBox('Per frame')
        form = QFormLayout(rates)
        self.drop = self._rate(form, 'Lost:',
                               'The frame never arrives. What a configurator\n'
                               'does next - retry, wait, or give up - is the\n'
                               'whole question.')
        self.corrupt = self._rate(form, 'Corrupted:',
                                  'One bit flipped. The CRC should catch it,\n'
                                  'so this shows whether a retry follows.')
        self.truncate = self._rate(form, 'Cut short:',
                                   'The frame arrives incomplete, which looks\n'
                                   'like a reply that never finished.')
        self.duplicate = self._rate(form, 'Sent twice:',
                                    'The frame arrives twice. A second copy\n'
                                    'can be mistaken for the answer to the\n'
                                    'next request.')
        layout.addWidget(rates)

        timing = QGroupBox('Timing')
        tform = QFormLayout(timing)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 30000)
        self.delay.setDecimals(0)
        self.delay.setSuffix(' ms')
        self.delay.setToolTip('Hold every frame this long, the way a loaded\n'
                              'flight controller answers late.')
        tform.addRow('Delay:', self.delay)
        self.jitter = QDoubleSpinBox()
        self.jitter.setRange(0, 30000)
        self.jitter.setDecimals(0)
        self.jitter.setSuffix(' ms')
        self.jitter.setToolTip('Hold each frame a random extra time up to\n'
                               'this, so the delay is not predictable.')
        tform.addRow('Jitter:', self.jitter)
        layout.addWidget(timing)

        self.direction = QComboBox()
        self.direction.addItems([LinkFaults.BOTH, LinkFaults.TO_HOST,
                                 LinkFaults.FROM_HOST])
        self.direction.setToolTip(
            '"to host" damages what the device answers, which is where\n'
            'most real trouble is. "from host" damages what the\n'
            'configurator asks for.')
        self.seed = QSpinBox()
        self.seed.setRange(0, 999999)
        self.seed.setValue(faults.seed)
        self.seed.setToolTip('The same seed replays the same faults, so a\n'
                             'failure can be shown twice.')
        which = QFormLayout()
        which.addRow('Direction:', self.direction)
        which.addRow('Seed:', self.seed)
        layout.addLayout(which)

        self.tally = QLabel('')
        self.tally.setWordWrap(True)
        layout.addWidget(self.tally)

        buttons = QHBoxLayout()
        reset = QPushButton('Reset tally')
        reset.setToolTip('Clear the counts and restart the seeded sequence.')
        reset.clicked.connect(self._reset)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        close = QPushButton('Close')
        close.clicked.connect(self.hide)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        for widget in (self.drop, self.corrupt, self.truncate, self.duplicate,
                       self.delay, self.jitter):
            widget.valueChanged.connect(self._apply)
        self.enable.toggled.connect(self._apply)
        self.direction.currentTextChanged.connect(self._apply)
        self.seed.valueChanged.connect(self._reseed)
        self._apply()

        # the tally is written by the link threads, so poll it rather than
        # signal from them
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(200)

    @staticmethod
    def _rate(form, label, tip):
        box = QDoubleSpinBox()
        box.setRange(0, 100)
        box.setDecimals(1)
        box.setSingleStep(1.0)
        box.setSuffix(' %')
        box.setToolTip(tip)
        form.addRow(label, box)
        return box

    def _apply(self):
        f = self.faults
        f.enabled = self.enable.isChecked()
        f.drop = self.drop.value() / 100.0
        f.corrupt = self.corrupt.value() / 100.0
        f.truncate = self.truncate.value() / 100.0
        f.duplicate = self.duplicate.value() / 100.0
        f.delay_ms = self.delay.value()
        f.jitter_ms = self.jitter.value()
        f.direction = self.direction.currentText()

    def _reseed(self):
        self.faults.reset(seed=self.seed.value())

    def _reset(self):
        self.faults.reset()
        self._refresh()

    def _refresh(self):
        self.tally.setText(self.faults.summary())
