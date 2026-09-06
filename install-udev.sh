#!/bin/sh
set -eu

rule_source="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/udev/60-hantek-6254bc.rules"
rule_target="/etc/udev/rules.d/60-hantek-6254bc.rules"

install -m 0644 "$rule_source" "$rule_target"
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=04b5 --attr-match=idProduct=6cde
printf 'Installed %s\n' "$rule_target"
printf 'Unplug and reconnect the oscilloscope, then run: .venv/bin/hantek6254bc doctor\n'

