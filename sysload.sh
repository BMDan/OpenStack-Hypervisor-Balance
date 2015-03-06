#!/bin/bash

DEF_RMTCMD='printf "%-24s %-44s %20s %s\\n" "$(hostname)" "$(uptime | sed "s/,[ ]*load average:.*//")" "$(uptime | awk -F: "{print \$NF}")" "$(free -m | awk "NR==3 {print \"Free: \" \$4 \" MB\"}")"'
RMTCMD=${1:-$DEF_RMTCMD}

if [ "$1" == "-h" -o "$1" == "--help" ]; then
  echo "Usage: $0 [command]"
  echo "  Note: In most cases, you probably want to use 'echo' and command substitution, so you'll know where"
  echo "        your output is coming from by using something like 'echo \$(hostname) \$(uptime\)'."
  echo "  Default command: '$DEF_RMTCMD'"

  exit 1
fi

nova-manage service list | awk '$3=="nova" {print $2}' | sed 's/ /\n/g' | xargs -n 1 -P 255 -I '{}' sudo -u nova ssh {} "$RMTCMD" | sort
