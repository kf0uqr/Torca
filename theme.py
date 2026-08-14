"""
Applies an overall dark theme to the whole app -- the Fusion style plus
a matching QPalette. Call apply_dark_theme(app) once, right after
creating the QApplication and before showing any windows.
"""

from PySide6.QtGui import QPalette, QColor

def apply_dark_theme(app):
    """Applies an overall dark theme to the whole app -- the Fusion style
    plus a matching QPalette, which QDialog/QWidget/etc all pick up
    automatically (so this covers the connection dialog too, not just the
    main window). The custom-painted widgets (scope, waterfall, meters,
    tuning knob) already draw their own dark backgrounds regardless of
    this; this is specifically for the standard Qt widgets -- buttons,
    labels, combo boxes, sliders, dialogs -- that would otherwise use
    whatever light/native theme the OS provides and look mismatched next
    to those."""
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(45, 45, 48))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(30, 30, 32))
    palette.setColor(QPalette.AlternateBase, QColor(45, 45, 48))
    palette.setColor(QPalette.ToolTipBase, QColor(45, 45, 48))
    palette.setColor(QPalette.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.Button, QColor(55, 55, 58))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.BrightText, QColor(255, 80, 80))
    palette.setColor(QPalette.Link, QColor(90, 160, 255))
    palette.setColor(QPalette.Highlight, QColor(60, 120, 200))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))

    # Disabled states need their own explicit entries -- otherwise
    # disabled widgets (very common in this app before a radio connects)
    # can end up nearly invisible against a dark background.
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.Base, QColor(40, 40, 42))

    app.setPalette(palette)

    # A couple of things Fusion+QPalette alone doesn't fully cover.
    app.setStyleSheet("""
        QToolTip {
            color: #dcdcdc;
            background-color: #2d2d30;
            border: 1px solid #555555;
        }
    """)


