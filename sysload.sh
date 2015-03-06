#!/bin/bash

nova-manage service list | awk '$3=="nova" {print $2}' | sed 's/ /\n/g' | xargs -n 1 -P 255 -I '{}' sudo -u nova ssh {} 'echo $(hostname)" "$(uptime) $(free -m | awk "NR==3")' | sort
