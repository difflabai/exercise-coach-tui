#!/bin/bash
for set in 1 2 3; do
  say "Set $set. Get in position."
  sleep 3
  say "Go"
  sleep 40
  say "Done. Set $set complete."
  if [ $set -lt 3 ]; then
    say "Rest 60 seconds"
    sleep 60
  fi
done
say "RKC Planks done. 3 sets of 40 seconds."
