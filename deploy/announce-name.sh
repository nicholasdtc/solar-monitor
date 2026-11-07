#!/bin/sh
# Announces solar.local on the home network at this Pi's address, until stopped.
# Run by solar-monitor-name.service. The Pi keeps its own name as well.

address=$(ip -4 -o addr show scope global | awk '{ split($4, a, "/"); print a[1]; exit }')
if [ -z "$address" ]; then
	echo "No network address yet" >&2
	exit 1  # The service tries again shortly.
fi

# --no-reverse: the Pi's own name already answers "who is this address?", and two answers would clash.
exec avahi-publish --address --no-reverse solar.local "$address"
