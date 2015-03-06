#!/bin/bash

DEF_RMTCMD='printf "%-24s %-44s %20s %s\\n" "$(hostname)" "$(uptime | sed "s/,[ ]*load average:.*//")" "$(uptime | awk -F: "{print \$NF}")" "$(free -m | awk "NR==3 {print \"Free: \" \$4 \" MB\"}")"'
RMTCMD=${1:-$DEF_RMTCMD}

nova-manage service list | awk '$3=="nova" {print $2}' | sed 's/ /\n/g' | xargs -n 1 -P 255 -I '{}' sudo -u nova ssh {} "$RMTCMD" | sort
