"""Methods used in aws_backend, but also useful for standalone prototyping in Jupyter"""

import argparse
import boto3
import os
import os
import random
import re
import shlex
import string
import sys
import threading
import time

from collections import Iterable
from collections import OrderedDict
from collections import defaultdict
from operator import itemgetter


EMPTY_NAME="noname"   # name to use when name attribute is missing on AWS
RETRY_INTERVAL_SEC = 1  # how long to wait before retries
RETRY_TIMEOUT_SEC = 30  # how long to wait before retrying fails
DEFAULT_PREFIX = 'ncluster'
PRIVATE_KEY_LOCATION = os.environ['HOME']+'/.ncluster'

# shortcuts to refer to util module, this lets move external code referencing
# util or u into this module unmodified
#util = sys.modules[__name__]
#u = util


def get_vpc():
  """
  Returns current VPC (ec2.Vpc object)
  https://boto3.readthedocs.io/en/latest/reference/services/ec2.html#vpc
  """
  
  return get_vpc_dict()[get_prefix()]


def get_security_group():
  """
  Returns current security group, ec2.SecurityGroup object
  https://boto3.readthedocs.io/en/latest/reference/services/ec2.html#securitygroup
"""
  return get_security_group_dict()[get_prefix()]


def get_subnet():
  return get_subnet_dict()[get_zone()]
  

def get_vpc_dict():
  """Returns dictionary of named VPCs {name: vpc}

  Assert fails if there's more than one VPC with same name."""

  client = get_ec2_client()
  response = client.describe_vpcs()
  assert is_good_response(response)

  result = OrderedDict()
  ec2 = get_ec2_resource()
  for vpc_response in response['Vpcs']:
    key = get_name(vpc_response.get('Tags', []))
    if not key or key==EMPTY_NAME:  # skip VPC's that don't have a name assigned
      continue
    
    assert key not in result, ("Duplicate VPC group %s in %s" %(key,
                                                                response))
    result[key] = ec2.Vpc(vpc_response['VpcId'])

  return result


def get_subnet_dict():
  """Returns dictionary of "availability zone" -> subnet for current VPC."""
  subnet_dict = {}
  vpc = get_vpc()
  for subnet in vpc.subnets.all():
    zone = subnet.availability_zone
    assert zone not in subnet_dict, "More than one subnet in %s, why?" %(zone,)
    subnet_dict[zone] = subnet
  return subnet_dict


def get_gateway_dict(vpc):
  """Returns dictionary of named gateways for given VPC {name: gateway}"""
  return {get_name(gateway): gateway for
          gateway in vpc.internet_gateways.all()}

def get_efs_dict():
  """Returns dictionary of {efs_name: efs_id}"""
  # there's no EC2 resource for EFS objects, so return EFS_ID instead
  # https://stackoverflow.com/questions/47870342/no-ec2-resource-for-efs-objects

  efs_client = get_efs_client()
  response = efs_client.describe_file_systems()
  assert is_good_response(response)
  result = OrderedDict()
  for efs_response in response['FileSystems']:
    fs_id = efs_response['FileSystemId']
    tag_response = efs_client.describe_tags(FileSystemId=fs_id)
    assert is_good_response(tag_response)
    key = get_name(tag_response['Tags'])
    if not key or key==EMPTY_NAME:   # skip EFS's without a name
      continue
    assert key not in result
    result[key] = fs_id

  return result


def get_placement_group_dict():
  """Returns dictionary of {placement_group_name: (state, strategy)}"""

  client = get_ec2_client()
  response = client.describe_placement_groups()
  assert is_good_response(response)

  result = OrderedDict()
  ec2 = get_ec2_resource()
  for placement_group_response in response['PlacementGroups']:
    key = placement_group_response['GroupName']
    assert key not in result, ("Duplicate placement group " +
                               key)
    #    result[key] = (placement_group_response['State'],
    #                   placement_group_response['Strategy'])
    result[key] = ec2.PlacementGroup(key)
  return result


def get_security_group_dict():
  """Returns dictionary of named security groups {name: securitygroup}."""

  client = get_ec2_client()
  response = client.describe_security_groups()
  assert is_good_response(response)

  result = OrderedDict()
  ec2 = get_ec2_resource()
  for security_group_response in response['SecurityGroups']:
    key = get_name(security_group_response.get('Tags', []))
    if not key or key==EMPTY_NAME:
      continue  # ignore unnamed security groups
    #    key = security_group_response['GroupName']
    assert key not in result, ("Duplicate security group " + key)
    result[key] = ec2.SecurityGroup(security_group_response['GroupId'])

  return result


def get_keypair_dict():
  """Returns dictionary of {keypairname: keypair}"""

  client = get_ec2_client()
  response = client.describe_key_pairs()
  assert is_good_response(response)
  
  result = {}
  ec2 = get_ec2_resource()
  for keypair in response['KeyPairs']:
    keypair_name = keypair.get('KeyName', '')
    assert keypair_name not in result, "Duplicate key "+keypair_name
    result[keypair_name] = ec2.KeyPair(keypair_name)
  return result


def get_prefix():
  """Global prefix to identify ncluster created resources name used to identify ncluster created resources,
  (name of EFS, VPC, keypair prefixes), can be changed through $NCLUSTER_PREFIX for debugging purposes. """
  
  name = os.environ.get('NCLUSTER_PREFIX', DEFAULT_PREFIX)
  if name != DEFAULT_PREFIX:
    validate_prefix(name)
  return name


def get_account_number():
  return str(boto3.client('sts').get_caller_identity()['Account'])


def get_region():
  return get_session().region_name


def get_zone():
  return os.environ['AWS_ZONE']

def get_zones():
  client = get_ec2_client()
  response = client.describe_availability_zones()
  assert is_good_response(response)
  zones = []
  for avail_response in response['AvailabilityZones']:
    messages = avail_response['Messages']
    zone = avail_response['ZoneName']
    state = avail_response['State']
    assert not messages, "zone %s is broken? Has messages %s"%(zone, messages)
    assert state == 'available', "zone %s is broken? Has state %s"%(zone, state)
    zones.append(zone)
  return zones


def get_session():
  # in future can add aws profile support with Session(profile_name=...)
  return boto3.Session()


################################################################################
# keypairs
################################################################################
# For naming conventions, see
# https://docs.google.com/document/d/14-zpee6HMRYtEfQ_H_UN9V92bBQOt0pGuRKcEJsxLEA/edit#heading=h.45ok0839c0a

def get_keypair_name():
  """Returns current keypair name."""

  username = get_username()
  # todo: remove restriction?
  assert '-' not in username, "username must not contain -, change $USER"
  validate_aws_name(username)
  assert len(username)<30      # to avoid exceeding AWS 127 char limit
  return get_prefix() + '-' + username


def get_keypair():
  """Returns current keypair (ec2.KeyPairInfo)

  https://boto3.readthedocs.io/en/latest/reference/services/ec2.html#keypairinfo
  """

  return get_keypair_dict()[get_keypair_name()]


def get_keypair_fn():
  """Location of .pem file for current keypair"""

  keypair_name = get_keypair_name()
  account = get_account_number()
  region = get_region()
  fn = f'{PRIVATE_KEY_LOCATION}/{keypair_name}-{account}-{region}.pem'
  return fn


def get_vpc_name():
  return get_prefix()


def get_security_group_name():
  return get_prefix()


def get_gateway_name():
  return get_prefix()


def get_route_table_name():
  return get_prefix()


def get_efs_name():
  return get_prefix()


def get_username():
  assert 'USER' in os.environ, "why isn't USER defined?"
  return os.environ['USER']


def lookup_image(wildcard):
  """Returns unique ec2.Image whose name matches wildcard
  lookup_ami('pytorch*').name => ami-29fa
  
  https://boto3.readthedocs.io/en/latest/reference/services/ec2.html#image

  Assert fails if multiple images match or no images match.
  """
  
  ec2 = get_ec2_resource()
  filter = {'Name': 'name', 'Values': [wildcard]}
  
  images = list(ec2.images.filter(Filters=[filter]))

  # Note, can add filtering by Owners as follows
  #  images = list(ec2.images.filter(Filters = [filter], Owners=['self', 'amazon']))

  assert len(images) <= 1, "Multiple images match "+str(wildcard)
  assert len(images) > 0, "No images match "+str(wildcard)
  return images[0]


def lookup_instance(name, instance_type='', states=('running', 'stopped')):
  """Looks up AWS instance for a given AWS instance name and current user, like
   simple.worker. If no instance found, returns None. """

  ec2 = get_ec2_resource()


  # TODO: add waiting so that instances in state "initializing" are supported
  instances = ec2.instances.filter(
    Filters=[{'Name': 'instance-state-name', 'Values': states}])

  prefix = get_prefix()
  username = get_username()

  # look for an existing instance matching job, ignore instances launched
  # by different user or under different resource name
  result = []
  for i in instances.all():
    instance_name = get_name(i)
    if instance_name != name:
      continue
    
    seen_prefix, seen_username = parse_key_name(i.key_name)
    if prefix != seen_prefix:
      print(f"Found {name} launched under {seen_prefix}, ignoring")
      continue
    if username != seen_username:
      print(f"Found {name} launched by {seen_username}, ignoring")
      continue

    if instance_type:
      assert i.instance_type == instance_type, f"Found existing instance for job {name} but different instance type ({i.instance_type}) than requested ({instance_type}), terminate {name} first or use new task name."
    result.append(i)

    assert len(result)<2, f"Found two instances with name {name}"
    if not result:
      return None
    else:
      return result[0]


def ssh_to_task(task):
  """Create ssh connection to task's machine

  returns Paramiko SSH client connected to host.

  """
  import paramiko

  username = task.ssh_username
  hostname = task.public_ip
  ssh_key_fn = get_keypair_fn()
  print(f"ssh -i {ssh_key_fn} {username}@{hostname}")
  pkey = paramiko.RSAKey.from_private_key_file(ssh_key_fn)
  
  ssh_client = paramiko.SSHClient()
  ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
  assert ssh_client
  
  while True:
    try:
      ssh_client.connect(hostname=hostname, username=username, pkey=pkey)
      break
    except Exception as e:
      print(f'Exception connecting to host via ssh (could be a timeout): {e}')
      time.sleep(RETRY_INTERVAL_SEC)

  return ssh_client


def parse_key_name(keyname):
  """keyname => resource, username"""
  # Relies on resource name not containing -, validated in
  # validate_resource_name
  toks = keyname.split('-')
  if len(toks)!=2:
    return None, None       # some other keyname not launched by nexus
  else:
    return toks


aws_name_regexp=re.compile('^[a-zA-Z0-9+-=._:/@]*$')
def validate_aws_name(name):
  """Validate resource name using AWS name restrictions from # http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Using_Tags.html#tag-restrictions"""
  assert len(name)<=127
  # disallow unicode characters to avoid pain
  assert name == name.encode('ascii').decode('ascii')
  assert aws_name_regexp.match(name)


resource_regexp=re.compile('^[a-z0-9]+$')
def validate_prefix(name):
  """Check that name is valid as substitute for default prefix. Since it's used in unix filenames, key names, be more conservative than AWS requirements, just allow 30 chars, lowercase only."""
  assert len(name)<=30
  assert resource_regexp.match(name)
  validate_aws_name(name)


def validate_run_name(name):
  """Name used for run. Used as part of instance name, tmux session name."""
  assert len(name)<=30
  validate_aws_name(name)


def create_name_tags(name):
  return [{'Key': 'Name', 'Value': name}]


def create_efs(name):
  efs_client = get_efs_client()
  token = str(int(time.time() * 1e6))  # epoch usec

  response = efs_client.create_file_system(CreationToken=token,
                                           PerformanceMode='generalPurpose')
  assert is_good_response(response)
  start_time = time.time()
  while True:
    try:

      response = efs_client.create_file_system(CreationToken=token,
                                               PerformanceMode='generalPurpose')
      assert is_good_response(response)
      time.sleep(RETRY_INTERVAL_SEC)
    except Exception as e:
      if e.response['Error']['Code'] == 'FileSystemAlreadyExists':
        break
      else:
        log_error(e)
      break

    if time.time() - start_time - RETRY_INTERVAL_SEC > RETRY_TIMEOUT_SEC:
      assert False, "Timeout exceeded creating EFS %s (%s)" % (token, name)

    time.sleep(RETRY_TIMEOUT_SEC)

  # find efs id from given token
  response = efs_client.describe_file_systems()
  assert is_good_response(response)
  fs_id = extract_attr_for_match(response['FileSystems'], FileSystemId=-1, CreationToken=token)
  response = efs_client.create_tags(FileSystemId=fs_id,
                                    Tags=create_name_tags(name))
  assert is_good_response(response)

  # make sure EFS is now visible
  efs_dict = get_efs_dict()
  assert name in efs_dict
  return efs_dict[name]


def delete_efs_by_id(efs_id):
  """Deletion sometimes fails, try several times."""
  start_time = time.time()
  efs_client = get_efs_client()
  sys.stdout.write("deleting %s ... " % (efs_id,))
  while True:
    try:
      response = efs_client.delete_file_system(FileSystemId=efs_id)
      if is_good_response(response):
        print("succeeded")
        break
      time.sleep(RETRY_INTERVAL_SEC)
    except Exception as e:
      print("Failed with %s" % (e,))
      if time.time() - start_time - RETRY_INTERVAL_SEC < RETRY_TIMEOUT_SEC:
        print("Retrying in %s sec" % (RETRY_INTERVAL_SEC,))
        time.sleep(RETRY_INTERVAL_SEC)
      else:
        print("Giving up")
        break


def extract_attr_for_match(items, **kwargs):
  """Helper method to get attribute value for an item matching some criterion.
  Specify target criteria value as dict, with target attribute having value -1

  Example:
    to extract state of vpc matching given vpc id

  response = [{'State': 'available', 'VpcId': 'vpc-2bb1584c'}]
  extract_attr_for_match(response, State=-1, VpcId='vpc-2bb1584c') #=> 'available'"""

  # find the value of attribute to return
  query_arg = None
  for arg, value in kwargs.items():
    if value == -1:
      assert query_arg is None, "Only single query arg (-1 valued) is allowed"
      query_arg = arg
  result = []

  filterset = set(kwargs.keys())
  for item in items:
    match = True
    assert filterset.issubset(
      item.keys()), "Filter set contained %s which was not in record %s" % (
    filterset.difference(item.keys()),
    item)
    for arg in item:
      if arg == query_arg:
        continue
      if arg in kwargs:
        if item[arg] != kwargs[arg]:
          match = False
          break
    if match:
      result.append(item[query_arg])
  assert len(result) <= 1, "%d values matched %s, only allow 1" % (
  len(result), kwargs)
  if result:
    return result[0]
  return None


  
def get_tags(instance):
  """Returns instance tags."""

  return get_instance_property(instance, 'tags')

def get_public_ip(instance):
  return get_instance_property(instance, 'public_ip_address')

def get_ip(instance):
  return get_instance_property(instance, 'private_ip_address')

def get_instance_property(instance, property_name):
  """Retrieves property of an instance, with retries"""

  value = None
  name = get_name(instance)
  while True:
    try:
      value = getattr(instance, property_name)
      assert value is not None
      break
    except Exception as e:
      print(f"retrieving {property_name} on {name} failed with {e}, retrying")
      time.sleep(RETRY_INTERVAL_SEC)
      instance.reload()
      continue

  return value


def get_ec2_resource():
  return get_session().resource('ec2')

def get_ec2_client():
  return get_session().client('ec2')

def get_efs_client():
  # TODO: below sometimes fails with
  # botocore.exceptions.DataNotFoundError: Unable to load data for: endpoints
  # need to add retry
  return get_session().client('efs')

def is_good_response(response):
  """Helper method to check if boto3 call was a success."""

  code = response["ResponseMetadata"]['HTTPStatusCode']
  # get response code 201 on EFS creation
  return  code >= 200 and code<300

def get_name(tags_or_instance_or_id):
  """Helper utility to extract name out of tags dictionary or intancce.
      [{'Key': 'Name', 'Value': 'nexus'}] -> 'nexus'
 
     Assert fails if there's more than one name.
     Returns '' if there's less than one name.
  """

  ec2 = get_ec2_resource()
  if hasattr(tags_or_instance_or_id, 'tags'):
    tags = tags_or_instance_or_id.tags
  elif isinstance(tags_or_instance_or_id, str):
    tags = ec2.Instance(tags_or_instance_or_id).tags
  elif tags_or_instance_or_id is None:
    return EMPTY_NAME
  else:
    assert isinstance(tags_or_instance_or_id, Iterable), "expected iterable of tags"
    tags = tags_or_instance_or_id

  if not tags:
    return EMPTY_NAME
  names = [entry['Value'] for entry in tags if entry['Key']=='Name']
  if not names:
    return ''
  if len(names)>1:
    assert False, "have more than one name: "+str(names)
  return names[0]
    
def _shell_add_echo(script):
  """Goes over each line script, adds "echo cmd" in front of each cmd.

  ls a

  becomes

  echo * ls a
  ls a
  """
  new_script = ""
  for cmd in script.split('\n'):
    cmd = cmd.strip()
    if not cmd:
      continue
    new_script+="echo \\* " + shlex.quote(cmd) + "\n"
    new_script+=cmd+"\n"
  return new_script

def _shell_strip_comment(cmd):
  """ hi # testing => hi"""
  if '#' in cmd:
    return cmd.split('#', 1)[0]
  else:
    return cmd


# augmented SFTP client that can transfer directories, from
# https://stackoverflow.com/a/19974994/419116
def _put_dir(sftp, source, target):
  ''' Uploads the contents of the source directory to the target path. The
            target directory needs to exists. All subdirectories in source are 
            created under target.
        '''
  def _safe_mkdir(sftp, path, mode=511, ignore_existing=True):
    ''' Augments mkdir by adding an option to not fail if the folder exists  '''
    try:
      sftp.mkdir(path, mode)
    except IOError:
      if ignore_existing:
        pass
      else:
        raise

  assert os.path.isdir(source)
  _safe_mkdir(sftp, target)
              
  for item in os.listdir(source):
    if os.path.isfile(os.path.join(source, item)):
      sftp.put(os.path.join(source, item), os.path.join(target, item))
    else:
      _safe_mkdir(sftp, '%s/%s' % (target, item))
      _put_dir(sftp, os.path.join(source, item), '%s/%s' % (target, item))


def wait_until_available(resource):
  """Waits until interval state becomes 'available'"""
  start_time = time.time()
  while True:
    resource.load()
    if resource.state == 'available':
      break
    time.sleep(RETRY_INTERVAL_SEC)

##########################################################################
# non-AWS specific methods
##########################################################################

from collections import Iterable
def is_iterable(k):
  return isinstance(k, Iterable)

def now_micros():
  """Return current micros since epoch as integer."""
  return int(time.time()*1e6)

def log_error(*args, **kwargs):
  print(f"Error encountered {args} {kwargs}")

def log(*args, **kwargs):
  print(f"{args} {kwargs}")

def install_pdb_handler():
  """Make CTRL+\ break into gdb."""
  
  import signal
  import pdb

  def handler(signum, frame):
    pdb.set_trace()
  signal.signal(signal.SIGQUIT, handler)
