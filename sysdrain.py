#!/usr/bin/env python

import os
import sys
import keystoneclient.v2_0.client as ksclient
import novaclient.v1_1.client as novaclient
import time
import re
import urllib
import urllib2
import argparse

# On many of our clouds, the "host" attribute differs from the
# "hypervisor_hostname" attribute in that it lacks the domain part of the FQDN.
# For example, an instance might display the following data:
# host: hv1
# hypervisor_hostname: hv1.blackmesh.com
# So as to simplify comparison (and work around bugs like the gem located at
# https://bugs.launchpad.net/bugs/1173376), we fixup the host to equal the hv_hn
# by adding the requisite text.
# If you don't need this, simply set it to "".
hvhn_suffix = ".blackmesh.com"

# draining_hv is arguably the most important variable.  If set to None, then the
# goal of the program is to balance load between HVs.  If set to a specific HV,
# then we are trying to evacuate all virts off of that particular host.
#draining_hv = "hv1" + hvhn_suffix
draining_hv = None

# How often to check with the API to see if the instance has returned to an active
# state (in seconds).
live_migration_poll_interval = 5

# How long to wait after the instance shows "active" before we attempt to ping
# it.  Strictly speaking, this should only ever need to be zero, but a few seconds
# to make sure everything's kosher never hurt anybody.
sleep_between_hosts_time = 10

# Enable support for pre- and post-move ping tests.  If enabled, an unpingable host
# will not be moved, and a host that is unpingable after being moved will cause the
# entire script to halt.  The ping API is hilariously simple; the output should be
# the text "yes" in case of success, or anything else otherwise.  In our case, we
# put it behind HTTP Basic (i.e. htpasswd) authentication.  Actually a tristate;
# it can be False or None up here, and then we test to see whether we have the
# requisite information to change it to True, below.  False disables ping,
# regardless.
ping_is_enabled = None

#### End of configurable parameters ###

argparser = argparse.ArgumentParser(description='Move instances between OpenStack hypervisors')
argparser.add_argument('-D', '--draining-hypervisor', nargs=1, default=None, help='Specify a particular hypervisor from which to drain all instances')
args = argparser.parse_args()
if args.draining_hypervisor:
  draining_hv = args.draining_hypervisor[0]

if ping_is_enabled is not False and os.environ.get('SYSDRAIN_PINGURL_BASE') is not None:
  ping_is_enabled = True
  pingurl_username = os.environ.get('SYSDRAIN_PINGURL_USERNAME')
  pingurl_password = os.environ.get('SYSDRAIN_PINGURL_PASSWORD')
  pingurl_base = os.environ.get('SYSDRAIN_PINGURL_BASE')

  if ( pingurl_base is None ):
    print "Undefined variable.  You probably need to source '.openstack' before running this program."
    exit(1)

  if ( pingurl_username is not None and pingurl_password is not None ):
    password_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password( None, pingurl_base, pingurl_username, pingurl_password )

    auth_handler = urllib2.HTTPBasicAuthHandler(password_manager)
    url_opener = urllib2.build_opener(auth_handler)
    urllib2.install_opener(url_opener)

os_auth_url = os.environ.get('OS_AUTH_URL')
os_username = os.environ.get('OS_USERNAME')
os_password = os.environ.get('OS_PASSWORD')
os_tenant_name = os.environ.get('OS_TENANT_NAME')

if ( os_auth_url is None or os_username is None or os_password is None or os_tenant_name is None ):
  print "Undefined variable.  You probably need to source '.openstack' before running this program."
  exit(1)

def get_creds( type ):
  password_key_name = "password"
  if ( type == "nova" ):
    password_key_name = "api_key"
  tenant_key_name = "tenant_name"
  if ( type == "nova" ):
    tenant_key_name = "project_id"
  os_creds = {}
  os_creds['auth_url'] = os_auth_url
  os_creds[password_key_name] = os_password
  os_creds['username'] = os_username
  os_creds[tenant_key_name] = os_tenant_name
  return os_creds

def get_nova_creds():
  return get_creds( "nova" )

def get_keystone_creds():
  return get_creds( "keystone" )

keystone = ksclient.Client( **get_keystone_creds() )

nova = novaclient.Client( **get_nova_creds() )

# A Set of hosts that have been previously moved in a drain operation.  We won't attempt to move them again when draining; they're probably bugged somehow if
# they are still on the host we're trying to drain.  Note that hosts that get stuck in "MIGRATING" will block the script.  Thus, this only handles those that
# return to ACTIVE, but on the original, pre-move host (it's more common than you might guess, sadly).
moved_hosts = set()
# To hard-exclude a particular host from ever moving, add it here, as: moved_hosts.add('some-uuid')

# Main movement loop
while ( True ):
  serverlist = nova.servers.list(search_opts={'all_tenants':1})
  
  #print serverlist
  
  hvlist = nova.hypervisors.list()
  
  hvcount = len(hvlist)
  
  if ( hvcount <= 0 ):
    print "Too few hypervisors."
    exit(1)
  
  hvresources = {}
  hvflavorarray = {}
  
  for hv in hvlist:
    hvname = hv._info['hypervisor_hostname']
    hvresources[hvname] = {}
    hvresources[hvname]['ram'] = {'total': 0, 'idbyval': {}}
    hvresources[hvname]['vcpus'] = {'total': 0, 'idbyval': {}}
    hvresources[hvname]['disk'] = {'total': 0, 'idbyval': {}}
    hvresources[hvname]['instances'] = {'total': 0}
    hvresources[hvname]['info'] = hv._info.copy()
  
  for server in serverlist:
    serverinfo = server._info.copy()
  
    server_name = serverinfo['name']
    server_uuid = serverinfo['id']
  
    server_host = serverinfo['OS-EXT-SRV-ATTR:host']
    server_hvhn = serverinfo['OS-EXT-SRV-ATTR:hypervisor_hostname']
  
    server_status = serverinfo['status']
    server_task_state = serverinfo['OS-EXT-STS:task_state']
  
    server_flavor = serverinfo['flavor']['id']
  
    if ( server_status != 'ACTIVE' ):
      print "Skipping '" + server_name + "' [" + server_uuid + "] due to non-ACTIVE state (" + server_status + ")."
      continue
  
    if ( server_task_state is not None ):
      print "Skipping '" + server_name + "' [" + server_uuid + "] due to active task state (" + server_task_state + ")."
      continue
  
    if ( server_host + hvhn_suffix != server_hvhn ):
      print "Skipping '" + server_name + "' [" + server_uuid + "] due to HVHN<->Host mismatch (" + server_host + hvhn_suffix + " != " + server_hvhn + ")."
      continue
  
    flavor = nova.flavors.get(server_flavor)
  
  #  print flavor._info.copy()
  # {u'name': u'2x4x60', u'links': [{u'href': u'http://1.2.3.4:8774/v2/0123456789abcdef0123456789abcdef/flavors/a1b2c3d4-e5f6-a1b2-c3d4-a1b2c3d4e5f6', u'rel': u'self'}, {u'href': u'http://1.2.3.4:8774/0123456789abcdef0123456789abcdef/flavors/a1b2c3d4-e5f6-a1b2-c3d4-a1b2c3d4e5f6', u'rel': u'bookmark'}], u'ram': 4096, u'OS-FLV-DISABLED:disabled': False, u'vcpus': 2, u'swap': u'', u'os-flavor-access:is_public': True, u'rxtx_factor': 1.0, u'OS-FLV-EXT-DATA:ephemeral': 0, u'disk': 60, u'id': u'a1b2c3d4-e5f6-a1b2-c3d4-a1b2c3d4e5f6'}

    if server_uuid in moved_hosts and draining_hv is not None and draining_hv == server_hvhn:
      print server_uuid + " does not appear to have been moved successfully, earlier.  This is probably a bug that you want to investigate."
  
    thisram = flavor._info['ram']
    hvresources[server_hvhn]['ram']['total'] += thisram
    if server_uuid not in moved_hosts:
      hvresources[server_hvhn]['ram']['idbyval'][thisram] = server_uuid
  
    thisvcpus = flavor._info['vcpus']
    hvresources[server_hvhn]['vcpus']['total'] += thisvcpus
    if server_uuid not in moved_hosts:
      hvresources[server_hvhn]['vcpus']['idbyval'][thisvcpus] = server_uuid
  
    thisdisk = 0
    try:
      thisdisk += flavor._info['swap']
    except TypeError:
      pass
    thisdisk += flavor._info['disk']
    hvresources[server_hvhn]['disk']['total'] += thisdisk
    if server_uuid not in moved_hosts:
      hvresources[server_hvhn]['disk']['idbyval'][thisdisk] = server_uuid

    hvresources[server_hvhn]['instances']['total'] += 1
  
  # Upon further reflection, we only ever need one of a given size, and
  # overwriting doesn't matter, so we just stick it in the appropriate key,
  # instead of trying to maintain a list.
  ####  try:
  ####    hvresources[server_hvhn]['disk']['idbyval'][thisdisk] is None
  ####  except KeyError:
  ####    hvresources[server_hvhn]['disk']['idbyval'][thisdisk] = []
  ####
  ####  hvresources[server_hvhn]['disk']['idbyval'][thisdisk].append(server_uuid)
  
  hvs_pct = {'vcpus': {}, 'ram': {}, 'disk': {}}
  
  hvs_considered = 0
  hvs_average = {'instances': 0, 'vcpus': 0, 'vcpuspct': 0, 'ram': 0, 'rampct': 0, 'disk': 0}
  
  formatstr = "{:<28} {:<9} {:>5} {:>8} x {:<5} {:>6} x {:<10}"
  print formatstr.format("Hypervisor Hostname", "Virts", "vCPUs", "", "RAMGB", "", "Disk+Swap")
  for hv in sorted(hvresources.iterkeys()):
    total_vcpus = hvresources[hv]['vcpus']['total']
    hvs_average['vcpus'] = ( hvs_average['vcpus'] * hvs_considered + total_vcpus )
    pct_vcpus = int(total_vcpus * 100 / hvresources[hv]['info']['vcpus'])
    hvs_average['vcpuspct'] = ( hvs_average['vcpuspct'] * hvs_considered + pct_vcpus )
    hvs_pct['vcpus'][hv] = pct_vcpus
  
    total_ram = hvresources[hv]['ram']['total']
    hvs_average['ram'] = ( hvs_average['ram'] * hvs_considered + total_ram )
    pct_ram = int(total_ram * 100 / hvresources[hv]['info']['memory_mb'])
    hvs_average['rampct'] = ( hvs_average['rampct'] * hvs_considered + pct_ram )
    hvs_pct['ram'][hv] = pct_ram
  
    total_disk = hvresources[hv]['disk']['total']
    hvs_average['disk'] = ( hvs_average['disk'] * hvs_considered + total_disk )

    total_instances = hvresources[hv]['instances']['total']
    hvs_average['instances'] = ( hvs_average['instances'] * hvs_considered + total_instances )

    hvs_considered += 1
  
    for hva_type in hvs_average.iterkeys():
      hvs_average[hva_type] /= hvs_considered
  
    print formatstr.format(hv + ":", total_instances, total_vcpus, "(" + str(int(total_vcpus * 100 / hvresources[hv]['info']['vcpus'])) + "%)", total_ram / 1024, "(" + str(int(total_ram * 100 / hvresources[hv]['info']['memory_mb'])) + "%)", total_disk )
  
  print formatstr.format( "Averages:", hvs_average['instances'], hvs_average['vcpus'], "(" + str(hvs_average['vcpuspct']) + "%)", hvs_average['ram'] / 1024, "(" + str(hvs_average['rampct']) + "%)", hvs_average['disk'] )
  
  min_val = None
  min_val_hv = None
  max_val = 0
  max_val_hv = None
  for hv_name in hvs_pct['ram'].iterkeys():
    if ( hvs_pct['ram'][hv_name] > max_val ):
      max_val = hvs_pct['ram'][hv_name]
      max_val_hv = hv_name
    if ( min_val is None or hvs_pct['ram'][hv_name] < min_val ):
      if hv_name == draining_hv:
        continue
      # Want to force a particular destination?  if ( hv_name != "myfavoritedsthv" + hvhn_suffix ): continue
      min_val = hvs_pct['ram'][hv_name]
      min_val_hv = hv_name
  
  if ( min_val is None or min_val_hv is None or max_val_hv is None ):
    print "Unable to find any candidates to be moved; did I actually find any HVs at all?"
    exit(0)
  
  if ( draining_hv is not None ):
    max_val_hv = draining_hv
    max_val = None
  
  if ( max_val_hv == min_val_hv ):
    print "No good candidates for movement; I'd just move something onto itself!"
    exit(0)
  
  if ( max_val is not None ):
    target_delta_pct = max_val - hvs_average['rampct']
    target_delta_absolute = float(target_delta_pct * hvresources[max_val_hv]['info']['memory_mb'] / 100)
  
    target_delta_absolute_on_min_val_hv = hvresources[min_val_hv]['info']['memory_mb'] - hvresources[min_val_hv]['ram']['total']
  
    # N.b. that this can result in suboptimal behavior (e.g. not moving a VM when we otherwise might want to) when we have
    # HVs with wildly differing memory sizes.
    # HV 1: 1 *M*B used of 1024 *M*B
    # HV 2: 250 GB used of 512 GB
    # HV 3: 511 GB used of 512 GB
    # The algorithm will choose to try to find a ~511 MB candidate to move, and if not able to find one, will not move anything,
    # whereas it's quite likely that we could move some of HV 3's load onto HV 2.  Fixing this likely requires getting to this point
    # and then dropping back into the loop, above, but with one HV (#1) knocked out of consideration as a target, iff we are unable
    # to find any movement candidates (after all, if we can find a 511 MB candidate, we'll then move something bigger off of HV 3
    # onto HV 2 the next time we're run).
    target_delta_absolute = min( target_delta_absolute, target_delta_absolute_on_min_val_hv )
    
    print "Looking for an opportunity to move up to " + str(target_delta_absolute) + " MB (" + str(target_delta_pct) + "%) of RAM"
  elif ( draining_hv is not None ):
    print "Aiming to empty out " + draining_hv + "."
  else:
    print "Something odd is afoot; I'm not sure what I'm trying to do, exactly.  Bailing."
    exit(1)
  
  current_donor = None
  current_donor_size = 0
  for candidate_donor_size in hvresources[max_val_hv]['ram']['idbyval'].iterkeys():
    if ( current_donor is None or
     ( candidate_donor_size > current_donor_size and
      ( draining_hv is not None or candidate_donor_size <= target_delta_absolute ) ) ):
      current_donor = hvresources[max_val_hv]['ram']['idbyval'][candidate_donor_size]
      current_donor_size = candidate_donor_size

  if current_donor is None:
    print "I want to move something off of " + max_val_hv + " onto " + min_val_hv + ", but there don't appear to be any candidate instances on the former."
    exit(0)
  
  current_donor_name = nova.servers.get(current_donor)._info['name']
  print "I would recommend moving " + current_donor + " (" + current_donor_name + ") to " + min_val_hv + " in order to transfer " + str(current_donor_size)  + " MB of RAM from " + max_val_hv + "."

  # Not perfect, since really we want to escape special characters, but should
  # work for most reasonable hvhn names, and this isn't something that a client
  # could ever control.  If that doesn't apply to your environment, improve
  # this!
  bare_min_val_hv = re.sub(hvhn_suffix + "$", "", min_val_hv)

  # Keep track of the fact that we have tried to move this host, so we don't try again (in draining mode) if it errors out.
  if draining_hv is not None:
    moved_hosts.add(current_donor)

  if ping_is_enabled:
    current_donor_name_fqdn = current_donor_name
    if current_donor_name.find('.') == -1:
      current_donor_name_fqdn += hvhn_suffix
    pingurl = pingurl_base + "/ping.php?hostname=" + urllib.quote(current_donor_name_fqdn)
    try:
      ping_result = urllib2.urlopen(pingurl).read()
    except:
      ping_result = "fail"

    if ping_result != "yes":
      print "Failed to ping " + current_donor_name_fqdn + " prior to beginning migration."
      if draining_hv is not None:
        print "Skipping " + current_donor_name_fqdn + " due to failed pre-migration ping."
        continue
      print "Attempting migration anyway; disabling post-migration ping check."

  nova.servers.live_migrate(server=current_donor, host=bare_min_val_hv, disk_over_commit=False, block_migration=False)

  sys.stdout.write('Live migration of ' + current_donor_name + ' commenced.  Polling on instance returning to active state.')
  
  while True:
    donor_object = nova.servers.get(current_donor)._info
    if donor_object['status'] == "ACTIVE":
      break
    sys.stdout.write('.')
    sys.stdout.flush()
    time.sleep(live_migration_poll_interval)

  if donor_object['OS-EXT-SRV-ATTR:hypervisor_hostname'] == min_val_hv:
    print " Migration complete!"
  else:
    print " Error(s) encountered, instance not moved successfully."

  # Let the migration settle a bit before looping again.
  # Courtesy of http://stackoverflow.com/questions/3160699/python-progress-bar
  sys.stdout.write("Pausing " + str(sleep_between_hosts_time) + " seconds to let HVs settle: ")
  sys.stdout.write("[%s]" % (" " * sleep_between_hosts_time))
  sys.stdout.flush()
  sys.stdout.write("\b" * (sleep_between_hosts_time+1)) # return to start of line, after '['

  for i in xrange(sleep_between_hosts_time):
    time.sleep(1)
    # update the bar
    sys.stdout.write("*")
    sys.stdout.flush()

  sys.stdout.write("\n")
  sys.stdout.flush()

  if ping_is_enabled and ping_result == "yes":
    try:
      ping_result = urllib2.urlopen(pingurl).read()
    except:
      ping_result = "fail"

    if ping_result != "yes":
      print "EMERGENCY!  " + current_donor_name_fqdn + " is not pingable post-migration.  Stopping script.  FIX THIS!"
      exit(1)
      #TODO: Make this die even more loudly.  (Email?  Growl notification?)
    else:
      print "Non-destructive operation confirmed; " + current_donor_name_fqdn + " is still pingable."
