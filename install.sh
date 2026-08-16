#!/bin/bash
# Installs (or uninstalls) Radione as a system-wide application on
# Linux: application files + their own Python virtual environment
# live in /opt/radione, and a small launcher goes in /usr/local/bin so
# "radione" works from anywhere once /usr/local/bin is on PATH (true
# by default on essentially every Linux distro). Must be run as root
# (sudo) -- both target locations require it.
#
# Usage:
#   sudo ./install.sh                Install (or reinstall/upgrade)
#   sudo ./install.sh --uninstall    Remove everything this script installed
#   sudo ./install.sh --uninstall -y Uninstall without the confirmation prompt
set -euo pipefail

INSTALL_DIR="/opt/radione"
LAUNCHER_PATH="/usr/local/bin/radione"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

UNINSTALL=false
ASSUME_YES=false
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        -y|--yes) ASSUME_YES=true ;;
        -h|--help)
            cat <<USAGE
Usage: sudo $0 [--uninstall] [-y|--yes]

  (no args)     Install (or reinstall/upgrade) Radione to $INSTALL_DIR
                and add a launcher at $LAUNCHER_PATH.
  --uninstall   Remove everything this script installed.
  -y, --yes     Don't prompt for confirmation (uninstall only).
USAGE
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg (see --help)" >&2
            exit 1
            ;;
    esac
done

if [[ "$EUID" -ne 0 ]]; then
    echo "This script must be run as root -- try: sudo $0 $*" >&2
    exit 1
fi

do_uninstall() {
    if [[ ! -d "$INSTALL_DIR" && ! -e "$LAUNCHER_PATH" ]]; then
        echo "Radione doesn't appear to be installed (no $INSTALL_DIR or $LAUNCHER_PATH found)."
        exit 0
    fi

    echo "This will remove:"
    [[ -d "$INSTALL_DIR" ]] && echo "  $INSTALL_DIR (application files + virtual environment)"
    [[ -e "$LAUNCHER_PATH" ]] && echo "  $LAUNCHER_PATH (launcher)"

    if [[ "$ASSUME_YES" != true ]]; then
        read -r -p "Continue? [y/N] " reply || reply="n"
        case "$reply" in
            [yY][eE][sS]|[yY]) ;;
            *) echo "Aborted."; exit 1 ;;
        esac
    fi

    rm -rf "$INSTALL_DIR"
    rm -f "$LAUNCHER_PATH"
    echo "Radione uninstalled."
}

do_install() {
    if [[ ! -f "$SCRIPT_DIR/main.py" || ! -f "$SCRIPT_DIR/requirements.txt" ]]; then
        echo "main.py/requirements.txt not found next to this script -- run it from a Radione checkout." >&2
        exit 1
    fi

    command -v python3 >/dev/null 2>&1 || {
        echo "python3 is required but wasn't found on this system." >&2
        exit 1
    }

    python_minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
    python_major="$(python3 -c 'import sys; print(sys.version_info[0])')"
    if (( python_major < 3 || (python_major == 3 && python_minor < 9) )); then
        echo "Python 3.9+ is required (found $(python3 --version))." >&2
        exit 1
    fi

    if ! python3 -m venv --help >/dev/null 2>&1; then
        echo "python3's 'venv' module isn't available. On Debian/Ubuntu:" >&2
        echo "  sudo apt install python3-venv" >&2
        echo "Then re-run this script." >&2
        exit 1
    fi

    if [[ -d "$INSTALL_DIR" ]]; then
        echo "Existing installation found at $INSTALL_DIR -- removing before reinstalling."
        rm -rf "$INSTALL_DIR"
    fi

    echo "Copying application files to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    if [[ -d "$SCRIPT_DIR/.git" ]] && command -v git >/dev/null 2>&1; then
        # Copies exactly the git-tracked files (with directory
        # structure intact) -- automatically excludes this repo's own
        # venv artifacts (.gitignore'd: bin/, lib/, etc, since this
        # checkout doubles as a dev venv itself) and any stray
        # untracked local files, with no separate exclude list to keep
        # in sync by hand.
        git -C "$SCRIPT_DIR" ls-files -z \
            | tar -C "$SCRIPT_DIR" --null -T - -cf - \
            | tar -xf - -C "$INSTALL_DIR"
    else
        echo "No git checkout detected next to this script -- falling back to a plain copy."
        command -v rsync >/dev/null 2>&1 || {
            echo "rsync is required for a non-git install. Install it and re-run." >&2
            exit 1
        }
        rsync -a \
            --exclude '.git' \
            --exclude 'bin' \
            --exclude 'lib' \
            --exclude 'lib64' \
            --exclude 'include' \
            --exclude 'pyvenv.cfg' \
            --exclude '__pycache__' \
            --exclude '*.pyc' \
            "$SCRIPT_DIR"/ "$INSTALL_DIR"/
    fi
    # The installer itself has no business living in the installed
    # copy -- it's a one-time setup tool, not part of the running app.
    rm -f "$INSTALL_DIR/$SCRIPT_NAME"

    echo "Creating a Python virtual environment in $INSTALL_DIR..."
    python3 -m venv "$INSTALL_DIR"

    echo "Installing dependencies (this may take a minute)..."
    "$INSTALL_DIR/bin/pip" install --upgrade pip
    "$INSTALL_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

    echo "Installing launcher to $LAUNCHER_PATH..."
    cat > "$LAUNCHER_PATH" <<EOF
#!/bin/bash
# Launches Radione. Installed by install.sh -- re-run that script
# (don't hand-edit this file) to reinstall or uninstall.
exec "$INSTALL_DIR/bin/python3" "$INSTALL_DIR/main.py" "\$@"
EOF
    chmod 755 "$LAUNCHER_PATH"

    echo
    echo "Radione installed successfully."
    echo "Run it with: radione"
    echo "Uninstall any time with: sudo $0 --uninstall"
}

if [[ "$UNINSTALL" == true ]]; then
    do_uninstall
else
    do_install
fi
