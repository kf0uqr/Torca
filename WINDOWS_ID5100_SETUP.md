# Running TORCA on Windows and connecting an Icom ID-5100

This is a from-scratch walkthrough for a Windows machine that has never run
TORCA before, aimed specifically at connecting an Icom **ID-5100** (also sold
as "IC-5100" in some places) over USB. Support for this radio is brand new
and **has never been tested against real hardware** -- see
[README.md's "Supported radios"](README.md#supported-radios) -- so a big part
of this guide is a checklist for confirming what actually works, not just
getting the app running.

`install.sh` (the one-command installer in the README) is Linux-only. On
Windows you'll follow the README's "Manual (any platform)" install path,
expanded here with Windows-specific steps.

## 1. What you'll need

- A Windows PC (10 or 11).
- The ID-5100 itself, powered on, connected to something sane for testing --
  see the PTT warning in step 6 before transmitting into anything.
- A CI-V USB cable. Icom's own **OPC-2350LU** (or the older OPC-478UC +
  OPC-1122 combo) connects the ID-5100's CI-V mini-jack to a USB port. If a
  cable like this is already plugged in and being used with another CI-V
  program (e.g. Ham Radio Deluxe, wfview), that confirms the cable/driver
  side works and you can skip straight to step 4.
- About 15 minutes.

## 2. Install Python

1. Download Python 3.11 or later from [python.org/downloads](https://www.python.org/downloads/) (3.9+ works, but a current release avoids ecosystem quirks).
2. Run the installer. **Check "Add python.exe to PATH"** on the first screen -- this is the single most common thing people miss, and everything below assumes it's checked.
3. Confirm it worked -- open **Command Prompt** (Start menu, type `cmd`) and run:

   ```bash
   python --version
   ```

   You should see `Python 3.11.x` (or whatever version you installed), not an error.

## 3. Install the CI-V USB driver

The OPC-2350LU and most Icom CI-V-USB cables use a **Silicon Labs CP210x**
USB-to-serial chip. Windows sometimes finds this automatically via Windows
Update; if not:

1. Download the CP210x VCP driver from [Silicon Labs' site](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers) (Windows package).
2. Install it, then plug in the CI-V cable (or power-cycle the radio if it was already plugged in).
3. Open **Device Manager** (Start menu → type "Device Manager") → **Ports (COM & LPT)**. You should see something like `Silicon Labs CP210x USB to UART Bridge (COM5)`. **Write down that COM number** -- you'll need it in step 6.

If Device Manager shows an "Unknown Device" or a yellow warning triangle instead, the driver install didn't take -- retry step 1 before moving on, since nothing past this point will work without a valid COM port.

## 4. Get the app

If you have `git` for Windows installed:

```bash
git clone https://github.com/kf0uqr/Torca.git
cd Torca
```

Otherwise, download the zip: go to <https://github.com/kf0uqr/Torca>, click the green **Code** button → **Download ZIP**, extract it, and open Command Prompt inside the extracted folder (Shift+right-click the folder → "Open PowerShell/Command window here", or type the path into `cd`).

## 5. Set up the virtual environment and install dependencies

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Your prompt should now start with `(venv)`. This install pulls in PySide6 (the GUI), rigplane (radio control, including the new ID-5100 support), and several other packages -- it'll take a minute or two.

**A likely snag:** `opuslib` (used for Remote Access audio streaming, not needed for basic radio control) is a Python wrapper around a native `libopus` library that Windows doesn't ship with. It finds that library via the OS's standard DLL search order, which means the file has to go in one of two places, not just anywhere in the project folder:

- **`venv\Scripts\opus.dll`** -- the same folder as `python.exe` inside the virtual environment. This is the most reliable option.
- Any folder already on your **PATH** environment variable.

Dropping it into the `Torca` project folder next to `main.py` does *not* reliably work -- the current working directory is a lower-priority part of Windows' DLL search order. Get a prebuilt `opus.dll` (e.g. from [the opus-tools Windows builds](http://opus-codec.org/downloads/)) and place it in `venv\Scripts\`. This only affects Remote Access's audio streaming -- CI-V frequency/mode/PTT/meter testing below doesn't touch it.

## 6. Before connecting: radio-side CI-V settings

On the ID-5100 itself (or its controller head), check these under
**Menu → Function → CI-V** (per the radio's own manual):

- **CI-V Address**: leave at its default, `8Ch`. (TORCA's ID-5100 profile assumes this default; if it's been changed, use the actual value in the Connect dialog's CI-V Address field instead.)
- **CI-V Baud Rate**: set this to **19200** (not "Auto"). TORCA's ID-5100 profile defaults to 19200, and being explicit on both ends avoids any auto-baud handshake ambiguity.
- **CI-V Transceive**: doesn't need to be ON for TORCA to control the radio; leave it at its default unless you have another reason to change it.

## 7. Launch TORCA

Still in the same Command Prompt (with `(venv)` showing):

```bash
python main.py
```

First launch shows the **Operator Profile** dialog -- enter a callsign and location (or use the GPS-from-address/IP lookup button) and continue. This just feeds satellite/logging features and isn't required to work before testing the radio connection. After that, the **Ham Dashboard** opens.

## 8. Connect the ID-5100

1. Click the **Radios** button on the dashboard, then start a new connection.
2. **Radio**: select `ID-5100` from the dropdown.
3. **Connection**: select `USB (Serial)` -- this should already be the default once ID-5100 is picked, since it has no LAN interface.
4. **Serial Port**: enter the COM port from step 3 (e.g. `COM5`).
5. **Baud Rate**: should already show `19200` (ID-5100's own default) -- leave it unless you changed the radio's own CI-V baud rate to something else.
6. **CI-V Address (hex)**: should already show `8C` -- leave it unless the radio's own CI-V address was changed from default.
7. **Satellite Role**: `Non-Sat` is fine for bench testing (this radio isn't a satellite full-duplex radio).
8. Accept the dialog to connect.

If it fails to connect: double check the COM port number (it can shift if the cable is unplugged and replugged into a different USB port), and make sure no other program (wfview, Ham Radio Deluxe, another TORCA instance) is holding the same COM port open.

## 9. Testing checklist

Since this profile has never touched real hardware, work through this in
order and note anything that looks wrong -- that feedback is exactly what
turns "should work" into "confirmed working" (see the README's note on how
IC-7610/X6200/FTX-1 were verified the same way).

**Read-only checks first (safe, no risk to the radio):**

- [ ] Frequency shown in TORCA matches what's on the radio's own display.
- [ ] Change frequency on the radio's dial -- TORCA's display updates to match within a couple seconds.
- [ ] Change frequency from TORCA (type into the frequency display, or use the tuning knob widget) -- the radio's own display updates to match.
- [ ] Switch VFO A/B on the radio -- TORCA follows.
- [ ] Change mode on the radio (FM ↔ FM-N ↔ DV) -- TORCA's mode display follows. AM/AM-N are receive-only on this radio (airband monitoring) -- don't expect to transmit in those.
- [ ] Change mode from TORCA -- the radio follows.
- [ ] Tune to an active repeater or simplex frequency with signal on it and confirm the **S-meter** in TORCA moves with received signal strength.
- [ ] Adjust **AF level** (volume) and **squelch** from TORCA and confirm the radio's own front-panel behavior matches (squelch opens/closes at the expected point, volume changes).

**Transmit checks (only after the read-only checks look right, and only into a dummy load or a frequency/context where it's safe and legal to key up):**

- [ ] PTT from TORCA keys the radio (confirm via the radio's own TX indicator, not just TORCA's UI).
- [ ] The **RF power meter** in TORCA moves and roughly matches the RF Power level set (Low/Mid/High).
- [ ] Change RF power level from TORCA and confirm the radio's own display/output follows.
- [ ] Mic gain adjustment from TORCA has an audible effect on TX audio (have someone listen, or use a second radio).
- [ ] If you use repeaters with CTCSS: set a repeater tone/TSQL frequency from TORCA and confirm the repeater tone actually gets sent (e.g. the repeater responds, or check with a service monitor/second radio).
- [ ] VOX on/off and VOX gain from TORCA behave as expected.

**Not implemented in this pass** (so don't expect these to work yet --
they're out of scope for now, not bugs):

- D-STAR-specific features: MY callsign, DV memory channels, GPS/D-PRS.
- DTCS and digital squelch (DSQL/CSQL) -- only analog repeater tone/TSQL are wired up.
- Spectrum scope, waterfall, PBT/notch/NR/NB, attenuator/preamp, RIT/XIT, tuner -- this radio doesn't have any of these, so there's nothing to test.

## 10. Reporting back

If anything in the checklist above didn't work as described -- wrong meter
scaling, a control that silently does nothing, frequency reading back wrong,
anything -- that's genuinely useful information. Note exactly which checklist
item failed and what happened instead; that's enough to track down and fix a
CI-V mismatch against the radio's real behavior.
