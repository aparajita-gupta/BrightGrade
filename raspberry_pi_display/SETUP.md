# Raspberry Pi 3 setup -- ambient deadline display

## 1. Flash the SD card (headless, from your Mac)

1. Install **Raspberry Pi Imager** (raspberrypi.com/software) if you don't
   have it.
2. Insert the SD card, open Imager.
3. Device: **Raspberry Pi 3**. OS: **Raspberry Pi OS Lite (64-bit)** --
   no desktop needed, this only runs one background script.
4. Click the gear icon (or Ctrl+Shift+X) for advanced options *before*
   writing:
   - Set hostname, e.g. `pi-display`
   - Enable SSH, set a username/password
   - Configure your WiFi SSID + password
   - Set your locale/timezone
5. Write the image, then insert the SD card into the Pi and power it on.
   Give it ~2 minutes to boot and join your WiFi.

## 2. Connect and enable I2C

From your Mac:

```
ssh <username>@pi-display.local
```

Enable the I2C interface (needed for the OLED):

```
sudo raspi-config nonint do_i2c 0
sudo reboot
```

SSH back in after it reboots, then confirm the display is detected:

```
sudo apt install -y i2c-tools
i2cdetect -y 1
```

You should see `3c` in the grid (the OLED's I2C address) -- if not,
double-check the wiring below before continuing.

## 3. Wiring

OLED (SSD1306, I2C) to the Pi's 40-pin header:

| OLED pin | Pi pin           |
|----------|------------------|
| VCC      | Pin 1 (3.3V)     |
| GND      | Pin 6 (GND)      |
| SDA      | Pin 3 (GPIO 2)   |
| SCL      | Pin 5 (GPIO 3)   |

## 4. Copy the files and install dependencies

From your Mac:

```
scp -r raspberry_pi_display <username>@pi-display.local:~/pi_display
```

Back on the Pi (over SSH):

```
cd ~/pi_display
cp pi_config.py.example pi_config.py
nano pi_config.py   # fill in your real TODOIST_API_TOKEN

sudo apt install -y python3-pip python3-venv
python3 -m venv venv
source venv/bin/activate
pip install requests luma.oled pillow
```

Quick manual test before setting up the always-on service:

```
python3 pi_display.py
```

The OLED should show "NEXT DUE" and your soonest upcoming task. Ctrl+C
to stop once you've confirmed it works.

## 5. Run it automatically on boot (systemd service)

The service file assumes the venv's python; adjust the `ExecStart` path
if you installed packages differently.

```
nano pi-display.service   # update ExecStart to use venv/bin/python3 if needed
sudo cp pi-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pi-display
sudo systemctl start pi-display
```

Check it's running and watch its logs:

```
sudo systemctl status pi-display
journalctl -u pi-display -f
```

Application-level logs (fetch results, errors) also go to
`~/pi_display/pi_display.log`.

## Troubleshooting

- **"No module named 'luma'"**: the systemd service isn't using the venv
  -- update `ExecStart` in `pi-display.service` to point at
  `/home/pi/pi_display/venv/bin/python3` instead of `/usr/bin/python3`,
  then `sudo systemctl daemon-reload && sudo systemctl restart pi-display`.
- **Blank display**: re-check `i2cdetect -y 1` shows `3c`, and the wiring
  table above.
- **Wrong countdown time**: `ACCOUNT_TIMEZONE` in `pi_display.py` is
  hardcoded to `America/Los_Angeles` (your account's real observed
  timezone, confirmed via live API testing) -- update it if you ever
  change your Todoist account's timezone setting.
