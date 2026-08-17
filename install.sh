#!/bin/bash
# Installs (or uninstalls) TORCA as a system-wide application on
# Linux: application files + their own Python virtual environment live
# in /opt/torca, and a small launcher goes in /usr/local/bin so
# "torca" works from anywhere once /usr/local/bin is on PATH (true
# by default on essentially every Linux distro).
#
# The FIRST install needs root (sudo) -- /opt and /usr/local/bin both
# require it. After that, /opt/torca is handed over to the user who
# ran the install (chown, below) specifically so later reinstalls/
# updates -- e.g. the Ham Dashboard's own "Check for Updates" button,
# see updater.py -- don't need root at all: only the one-time creation
# of /opt/torca itself and the /usr/local/bin launcher genuinely
# need elevated permissions, not the app files themselves. Re-running
# this script as the owning user (no sudo) refreshes an existing,
# already-yours install in place.
#
# Usage:
#   sudo ./install.sh                First install (or as root any time)
#   ./install.sh                     Reinstall/update an existing install you own
#   sudo ./install.sh --uninstall    Remove everything this script installed
#   sudo ./install.sh --uninstall -y Uninstall without the confirmation prompt
set -euo pipefail

INSTALL_DIR="/opt/torca"
LAUNCHER_PATH="/usr/local/bin/torca"
SERVER_LAUNCHER_PATH="/usr/local/bin/torca-server"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
INSTALL_COMMIT_MARKER="$INSTALL_DIR/.install_commit"

UNINSTALL=false
ASSUME_YES=false
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        -y|--yes) ASSUME_YES=true ;;
        -h|--help)
            cat <<USAGE
Usage: sudo $0 [--uninstall] [-y|--yes]

  (no args)     Install (or reinstall/update) TORCA to $INSTALL_DIR
                and add a launcher at $LAUNCHER_PATH. Needs sudo only
                for a first install (or one you don't already own).
  --uninstall   Remove everything this script installed. Always needs sudo.
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

do_uninstall() {
    if [[ "$EUID" -ne 0 ]]; then
        echo "Uninstalling requires root -- try: sudo $0 --uninstall" >&2
        exit 1
    fi

    if [[ ! -d "$INSTALL_DIR" && ! -e "$LAUNCHER_PATH" && ! -e "$SERVER_LAUNCHER_PATH" ]]; then
        echo "TORCA doesn't appear to be installed (no $INSTALL_DIR or $LAUNCHER_PATH found)."
        exit 0
    fi

    echo "This will remove:"
    [[ -d "$INSTALL_DIR" ]] && echo "  $INSTALL_DIR (application files + virtual environment)"
    [[ -e "$LAUNCHER_PATH" ]] && echo "  $LAUNCHER_PATH (launcher)"
    [[ -e "$SERVER_LAUNCHER_PATH" ]] && echo "  $SERVER_LAUNCHER_PATH (launcher)"

    if [[ "$ASSUME_YES" != true ]]; then
        read -r -p "Continue? [y/N] " reply || reply="n"
        case "$reply" in
            [yY][eE][sS]|[yY]) ;;
            *) echo "Aborted."; exit 1 ;;
        esac
    fi

    rm -rf "$INSTALL_DIR"
    rm -f "$LAUNCHER_PATH"
    rm -f "$SERVER_LAUNCHER_PATH"
    echo "TORCA uninstalled."
}

do_install() {
    if [[ ! -f "$SCRIPT_DIR/main.py" || ! -f "$SCRIPT_DIR/requirements.txt" ]]; then
        echo "main.py/requirements.txt not found next to this script -- run it from a TORCA checkout." >&2
        exit 1
    fi

    # A first install (nothing at $INSTALL_DIR yet, or it exists but
    # this user can't write to it -- e.g. a pre-chown install from an
    # older version of this script) needs root, to create it under
    # /opt and to write the /usr/local/bin launcher. An update to an
    # install this user already owns needs neither.
    existing_writable=false
    if [[ -d "$INSTALL_DIR" && -w "$INSTALL_DIR" ]]; then
        existing_writable=true
    fi
    if [[ "$existing_writable" != true && "$EUID" -ne 0 ]]; then
        echo "First install (or one you don't already own) needs root -- try: sudo $0 $*" >&2
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
        # Clear the directory's CONTENTS rather than removing and
        # recreating the directory itself: removing a directory ENTRY
        # needs write access to its PARENT (/opt, which stays root-
        # owned) -- an owning-but-non-root user updating their own
        # install has write access to $INSTALL_DIR's contents, not to
        # /opt itself, so `rm -rf "$INSTALL_DIR"` would fail there even
        # though everything inside it is theirs.
        echo "Existing installation found at $INSTALL_DIR -- refreshing in place."
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    else
        mkdir -p "$INSTALL_DIR"
    fi

    echo "Copying application files to $INSTALL_DIR..."
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
        git -C "$SCRIPT_DIR" rev-parse HEAD > "$INSTALL_COMMIT_MARKER"
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
        # No commit to record -- updater.py treats a missing marker as
        # "unknown version" and just re-offers the latest each check.
    fi
    # The installer itself has no business living in the installed
    # copy -- it's a one-time setup tool, not part of the running app.
    rm -f "$INSTALL_DIR/$SCRIPT_NAME"

    echo "Creating a Python virtual environment in $INSTALL_DIR..."
    python3 -m venv "$INSTALL_DIR"

    echo "Installing dependencies (this may take a minute)..."
    "$INSTALL_DIR/bin/pip" install --upgrade pip
    "$INSTALL_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

    if [[ "$EUID" -eq 0 ]]; then
        echo "Installing launcher to $LAUNCHER_PATH..."
        cat > "$LAUNCHER_PATH" <<EOF
#!/bin/bash
# Launches TORCA. Installed by install.sh -- re-run that script
# (don't hand-edit this file) to reinstall or uninstall.
exec "$INSTALL_DIR/bin/python3" "$INSTALL_DIR/main.py" "\$@"
EOF
        chmod 755 "$LAUNCHER_PATH"

        echo "Installing launcher to $SERVER_LAUNCHER_PATH..."
        cat > "$SERVER_LAUNCHER_PATH" <<EOF
#!/bin/bash
# Shares a locally-connected radio over the network via rigplane's own
# web server -- see README.md's remote-connection section. Runs through
# torca_server.py (disables rigplane's periodic unselected-VFO-slot
# refresh -- see that file's docstring) rather than exec'ing rigplane
# directly. Installed by install.sh -- re-run that script (don't
# hand-edit this file) to reinstall or uninstall.
exec "$INSTALL_DIR/bin/python3" "$INSTALL_DIR/torca_server.py" "\$@"
EOF
        chmod 755 "$SERVER_LAUNCHER_PATH"

        if [[ -n "${SUDO_USER:-}" ]]; then
            echo "Handing $INSTALL_DIR over to $SUDO_USER (so future updates don't need root)..."
            chown -R "$SUDO_USER":"$(id -gn "$SUDO_USER")" "$INSTALL_DIR"
        else
            echo "Note: running as root directly (not via sudo), so $INSTALL_DIR stays root-owned --"
            echo "future reinstalls/updates will need sudo too."
        fi
    else
        # Updating an install we already own -- the launchers' content
        # never changes release to release (they just exec a fixed
        # path), and ownership is already correct, so there's nothing
        # under /usr/local/bin or root-owned to touch.
        echo "Updated as $(whoami) -- launchers at $LAUNCHER_PATH / $SERVER_LAUNCHER_PATH unchanged (their content never needs to)."
    fi

    echo
    echo "TORCA installed successfully."
    echo "Run it with: torca"
    echo "Share a locally-connected radio over the network with: torca-server (see README.md)"
    echo "Uninstall any time with: sudo $0 --uninstall"
}

if [[ "$UNINSTALL" == true ]]; then
    do_uninstall
else
    do_install
fi
