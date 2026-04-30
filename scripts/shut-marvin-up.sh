#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Shut Marvin Up
# @raycast.mode silent

# Optional parameters:
# @raycast.icon 🤫
# @raycast.packageName Claude Speaks
# @raycast.description Kill any in-flight claude-speaks playback (afplay).

# Documentation:
# @raycast.author claude-speaks
# @raycast.authorURL https://github.com/ohnotnow/claude-speaks

killall afplay 2>/dev/null
exit 0
