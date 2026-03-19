#!/bin/bash

# Cross-platform TTS: macOS `say`, Linux `espeak` or `spd-say`
speak() {
  if command -v say &>/dev/null; then
    say "$1"
  elif command -v espeak &>/dev/null; then
    espeak "$1"
  elif command -v spd-say &>/dev/null; then
    spd-say "$1"
  fi
}

for set in 1 2 3; do
  speak "Set $set. Get in position."
  sleep 3
  speak "Go"
  sleep 40
  speak "Done. Set $set complete."
  if [ $set -lt 3 ]; then
    speak "Rest 60 seconds"
    sleep 60
  fi
done
speak "RKC Planks done. 3 sets of 40 seconds."
