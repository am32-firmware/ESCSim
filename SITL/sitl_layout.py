"""Screen-sized windows and scrollable settings for the SITL desktop UI."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFrame, QScrollArea


def fit_window(window, width, height):
    """Use Qt logical pixels, reserving space for window-manager decorations."""
    screen = window.screen()
    if screen is not None:
        available = screen.availableGeometry().size() - QSize(24, 64)
        width = min(width, available.width())
        height = min(height, available.height())
    window.resize(max(1, width), max(1, height))


def scroll_panel(widget):
    """Let settings scroll without forcing the containing window off screen."""
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidgetResizable(True)
    if widget.layout() is not None:
        widget.layout().setAlignment(Qt.AlignTop)
    scroll.setWidget(widget)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    return scroll


def tile_windows(controls, scope):
    """Place controls and traces alongside one another on the scope's screen."""
    screen = scope.screen()
    if screen is None:
        return
    area = screen.availableGeometry().adjusted(8, 8, -8, -8)
    gap = 8
    for window in (controls, scope):
        window.showNormal()
    margins = [w.frameGeometry().size() - w.size() for w in (controls, scope)]
    minimums = [w.minimumSizeHint().width() + m.width()
                for w, m in zip((controls, scope), margins)]
    if sum(minimums) + gap > area.width():
        return  # keep the current layout on a screen too narrow for both
    left = max(minimums[0], min(int(area.width() * .33),
                               area.width() - gap - minimums[1]))
    for window, margin, x, width in (
            (controls, margins[0], area.left(), left),
            (scope, margins[1], area.left() + left + gap, area.width() - left - gap)):
        window.resize(width - margin.width(), area.height() - margin.height())
        window.move(x, area.top())
        window.raise_()
