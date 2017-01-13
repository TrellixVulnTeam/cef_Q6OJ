#!/usr/bin/env python
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# Copyright (C) 2008 Evan Martin <martine@danga.com>

"""A git-command for integrating reviews on Rietveld and Gerrit."""

from __future__ import print_function

from distutils.version import LooseVersion
from multiprocessing.pool import ThreadPool
import base64
import collections
import fnmatch
import httplib
import json
import logging
import multiprocessing
import optparse
import os
import re
import stat
import sys
import tempfile
import textwrap
import traceback
import urllib
import urllib2
import urlparse
import uuid
import webbrowser
import zlib

try:
  import readline  # pylint: disable=import-error,W0611
except ImportError:
  pass

from third_party import colorama
from third_party import httplib2
from third_party import upload
import auth
import checkout
import clang_format
import commit_queue
import dart_format
import setup_color
import fix_encoding
import gclient_utils
import gerrit_util
import git_cache
import git_common
import git_footers
import owners
import owners_finder
import presubmit_support
import rietveld
import scm
import subcommand
import subprocess2
import watchlists

__version__ = '2.0'

COMMIT_BOT_EMAIL = 'commit-bot@chromium.org'
DEFAULT_SERVER = 'https://codereview.chromium.org'
POSTUPSTREAM_HOOK = '.git/hooks/post-cl-land'
DESCRIPTION_BACKUP_FILE = '~/.git_cl_description_backup'
REFS_THAT_ALIAS_TO_OTHER_REFS = {
    'refs/remotes/origin/lkgr': 'refs/remotes/origin/master',
    'refs/remotes/origin/lkcr': 'refs/remotes/origin/master',
}

# Valid extensions for files we want to lint.
DEFAULT_LINT_REGEX = r"(.*\.cpp|.*\.cc|.*\.h)"
DEFAULT_LINT_IGNORE_REGEX = r"$^"

# Buildbucket master name prefix.
MASTER_PREFIX = 'master.'

# Shortcut since it quickly becomes redundant.
Fore = colorama.Fore

# Initialized in main()
settings = None

# Used by tests/git_cl_test.py to add extra logging.
# Inside the weirdly failing test, add this:
# >>> self.mock(git_cl, '_IS_BEING_TESTED', True)
# And scroll up to see the strack trace printed.
_IS_BEING_TESTED = False


def DieWithError(message):
  print(message, file=sys.stderr)
  sys.exit(1)


def GetNoGitPagerEnv():
  env = os.environ.copy()
  # 'cat' is a magical git string that disables pagers on all platforms.
  env['GIT_PAGER'] = 'cat'
  return env


def RunCommand(args, error_ok=False, error_message=None, shell=False, **kwargs):
  try:
    return subprocess2.check_output(args, shell=shell, **kwargs)
  except subprocess2.CalledProcessError as e:
    logging.debug('Failed running %s', args)
    if not error_ok:
      DieWithError(
          'Command "%s" failed.\n%s' % (
            ' '.join(args), error_message or e.stdout or ''))
    return e.stdout


def RunGit(args, **kwargs):
  """Returns stdout."""
  return RunCommand(['git'] + args, **kwargs)


def RunGitWithCode(args, suppress_stderr=False):
  """Returns return code and stdout."""
  if suppress_stderr:
    stderr = subprocess2.VOID
  else:
    stderr = sys.stderr
  try:
    (out, _), code = subprocess2.communicate(['git'] + args,
                                             env=GetNoGitPagerEnv(),
                                             stdout=subprocess2.PIPE,
                                             stderr=stderr)
    return code, out
  except subprocess2.CalledProcessError as e:
    logging.debug('Failed running %s', args)
    return e.returncode, e.stdout


def RunGitSilent(args):
  """Returns stdout, suppresses stderr and ignores the return code."""
  return RunGitWithCode(args, suppress_stderr=True)[1]


def IsGitVersionAtLeast(min_version):
  prefix = 'git version '
  version = RunGit(['--version']).strip()
  return (version.startswith(prefix) and
      LooseVersion(version[len(prefix):]) >= LooseVersion(min_version))


def BranchExists(branch):
  """Return True if specified branch exists."""
  code, _ = RunGitWithCode(['rev-parse', '--verify', branch],
                           suppress_stderr=True)
  return not code


def time_sleep(seconds):
  # Use this so that it can be mocked in tests without interfering with python
  # system machinery.
  import time  # Local import to discourage others from importing time globally.
  return time.sleep(seconds)


def ask_for_data(prompt):
  try:
    return raw_input(prompt)
  except KeyboardInterrupt:
    # Hide the exception.
    sys.exit(1)


def _git_branch_config_key(branch, key):
  """Helper method to return Git config key for a branch."""
  assert branch, 'branch name is required to set git config for it'
  return 'branch.%s.%s' % (branch, key)


def _git_get_branch_config_value(key, default=None, value_type=str,
                                 branch=False):
  """Returns git config value of given or current branch if any.

  Returns default in all other cases.
  """
  assert value_type in (int, str, bool)
  if branch is False:  # Distinguishing default arg value from None.
    branch = GetCurrentBranch()

  if not branch:
    return default

  args = ['config']
  if value_type == bool:
    args.append('--bool')
  # git config also has --int, but apparently git config suffers from integer
  # overflows (http://crbug.com/640115), so don't use it.
  args.append(_git_branch_config_key(branch, key))
  code, out = RunGitWithCode(args)
  if code == 0:
    value = out.strip()
    if value_type == int:
      return int(value)
    if value_type == bool:
      return bool(value.lower() == 'true')
    return value
  return default


def _git_set_branch_config_value(key, value, branch=None, **kwargs):
  """Sets the value or unsets if it's None of a git branch config.

  Valid, though not necessarily existing, branch must be provided,
  otherwise currently checked out branch is used.
  """
  if not branch:
    branch = GetCurrentBranch()
    assert branch, 'a branch name OR currently checked out branch is required'
  args = ['config']
  # Check for boolean first, because bool is int, but int is not bool.
  if value is None:
    args.append('--unset')
  elif isinstance(value, bool):
    args.append('--bool')
    value = str(value).lower()
  else:
    # git config also has --int, but apparently git config suffers from integer
    # overflows (http://crbug.com/640115), so don't use it.
    value = str(value)
  args.append(_git_branch_config_key(branch, key))
  if value is not None:
    args.append(value)
  RunGit(args, **kwargs)


def _get_committer_timestamp(commit):
  """Returns unix timestamp as integer of a committer in a commit.

  Commit can be whatever git show would recognize, such as HEAD, sha1 or ref.
  """
  # Git also stores timezone offset, but it only affects visual display,
  # actual point in time is defined by this timestamp only.
  return int(RunGit(['show', '-s', '--format=%ct', commit]).strip())


def _git_amend_head(message, committer_timestamp):
  """Amends commit with new message and desired committer_timestamp.

  Sets committer timezone to UTC.
  """
  env = os.environ.copy()
  env['GIT_COMMITTER_DATE'] = '%d+0000' % committer_timestamp
  return RunGit(['commit', '--amend', '-m', message], env=env)


def add_git_similarity(parser):
  parser.add_option(
      '--similarity', metavar='SIM', type=int, action='store',
      help='Sets the percentage that a pair of files need to match in order to'
           ' be considered copies (default 50)')
  parser.add_option(
      '--find-copies', action='store_true',
      help='Allows git to look for copies.')
  parser.add_option(
      '--no-find-copies', action='store_false', dest='find_copies',
      help='Disallows git from looking for copies.')

  old_parser_args = parser.parse_args
  def Parse(args):
    options, args = old_parser_args(args)

    if options.similarity is None:
      options.similarity = _git_get_branch_config_value(
          'git-cl-similarity', default=50, value_type=int)
    else:
      print('Note: Saving similarity of %d%% in git config.'
            % options.similarity)
      _git_set_branch_config_value('git-cl-similarity', options.similarity)

    options.similarity = max(0, min(options.similarity, 100))

    if options.find_copies is None:
      options.find_copies = _git_get_branch_config_value(
          'git-find-copies', default=True, value_type=bool)
    else:
      _git_set_branch_config_value('git-find-copies', bool(options.find_copies))

    print('Using %d%% similarity for rename/copy detection. '
          'Override with --similarity.' % options.similarity)

    return options, args
  parser.parse_args = Parse


def _get_properties_from_options(options):
  properties = dict(x.split('=', 1) for x in options.properties)
  for key, val in properties.iteritems():
    try:
      properties[key] = json.loads(val)
    except ValueError:
      pass  # If a value couldn't be evaluated, treat it as a string.
  return properties


def _prefix_master(master):
  """Convert user-specified master name to full master name.

  Buildbucket uses full master name(master.tryserver.chromium.linux) as bucket
  name, while the developers always use shortened master name
  (tryserver.chromium.linux) by stripping off the prefix 'master.'. This
  function does the conversion for buildbucket migration.
  """
  if master.startswith(MASTER_PREFIX):
    return master
  return '%s%s' % (MASTER_PREFIX, master)


def _unprefix_master(bucket):
  """Convert bucket name to shortened master name.

  Buildbucket uses full master name(master.tryserver.chromium.linux) as bucket
  name, while the developers always use shortened master name
  (tryserver.chromium.linux) by stripping off the prefix 'master.'. This
  function does the conversion for buildbucket migration.
  """
  if bucket.startswith(MASTER_PREFIX):
    return bucket[len(MASTER_PREFIX):]
  return bucket


def _buildbucket_retry(operation_name, http, *args, **kwargs):
  """Retries requests to buildbucket service and returns parsed json content."""
  try_count = 0
  while True:
    response, content = http.request(*args, **kwargs)
    try:
      content_json = json.loads(content)
    except ValueError:
      content_json = None

    # Buildbucket could return an error even if status==200.
    if content_json and content_json.get('error'):
      error = content_json.get('error')
      if error.get('code') == 403:
        raise BuildbucketResponseException(
            'Access denied: %s' % error.get('message', ''))
      msg = 'Error in response. Reason: %s. Message: %s.' % (
          error.get('reason', ''), error.get('message', ''))
      raise BuildbucketResponseException(msg)

    if response.status == 200:
      if not content_json:
        raise BuildbucketResponseException(
            'Buildbucket returns invalid json content: %s.\n'
            'Please file bugs at http://crbug.com, label "Infra-BuildBucket".' %
            content)
      return content_json
    if response.status < 500 or try_count >= 2:
      raise httplib2.HttpLib2Error(content)

    # status >= 500 means transient failures.
    logging.debug('Transient errors when %s. Will retry.', operation_name)
    time_sleep(0.5 + 1.5*try_count)
    try_count += 1
  assert False, 'unreachable'


def _get_bucket_map(changelist, options, option_parser):
  """Returns a dict mapping bucket names to builders and tests,
  for triggering try jobs.
  """
  # If no bots are listed, we try to get a set of builders and tests based
  # on GetPreferredTryMasters functions in PRESUBMIT.py files.
  if not options.bot:
    change = changelist.GetChange(
        changelist.GetCommonAncestorWithUpstream(), None)
    # Get try masters from PRESUBMIT.py files.
    masters = presubmit_support.DoGetTryMasters(
        change=change,
        changed_files=change.LocalPaths(),
        repository_root=settings.GetRoot(),
        default_presubmit=None,
        project=None,
        verbose=options.verbose,
        output_stream=sys.stdout)
    if masters is None:
      return None
    return {_prefix_master(m): b for m, b in masters.iteritems()}

  if options.bucket:
    return {options.bucket: {b: [] for b in options.bot}}
  if options.master:
    return {_prefix_master(options.master): {b: [] for b in options.bot}}

  # If bots are listed but no master or bucket, then we need to find out
  # the corresponding master for each bot.
  bucket_map, error_message = _get_bucket_map_for_builders(options.bot)
  if error_message:
    option_parser.error(
        'Tryserver master cannot be found because: %s\n'
        'Please manually specify the tryserver master, e.g. '
        '"-m tryserver.chromium.linux".' % error_message)
  return bucket_map


def _get_bucket_map_for_builders(builders):
  """Returns a map of buckets to builders for the given builders."""
  map_url = 'https://builders-map.appspot.com/'
  try:
    builders_map = json.load(urllib2.urlopen(map_url))
  except urllib2.URLError as e:
    return None, ('Failed to fetch builder-to-master map from %s. Error: %s.' %
                  (map_url, e))
  except ValueError as e:
    return None, ('Invalid json string from %s. Error: %s.' % (map_url, e))
  if not builders_map:
    return None, 'Failed to build master map.'

  bucket_map = {}
  for builder in builders:
    masters = builders_map.get(builder, [])
    if not masters:
      return None, ('No matching master for builder %s.' % builder)
    if len(masters) > 1:
      return None, ('The builder name %s exists in multiple masters %s.' %
                    (builder, masters))
    bucket = _prefix_master(masters[0])
    bucket_map.setdefault(bucket, {})[builder] = []

  return bucket_map, None


def _trigger_try_jobs(auth_config, changelist, buckets, options,
                      category='git_cl_try', patchset=None):
  """Sends a request to Buildbucket to trigger try jobs for a changelist.

  Args:
    auth_config: AuthConfig for Rietveld.
    changelist: Changelist that the try jobs are associated with.
    buckets: A nested dict mapping bucket names to builders to tests.
    options: Command-line options.
  """
  assert changelist.GetIssue(), 'CL must be uploaded first'
  codereview_url = changelist.GetCodereviewServer()
  assert codereview_url, 'CL must be uploaded first'
  patchset = patchset or changelist.GetMostRecentPatchset()
  assert patchset, 'CL must be uploaded first'

  codereview_host = urlparse.urlparse(codereview_url).hostname
  authenticator = auth.get_authenticator_for_host(codereview_host, auth_config)
  http = authenticator.authorize(httplib2.Http())
  http.force_exception_to_status_code = True

  # TODO(tandrii): consider caching Gerrit CL details just like
  # _RietveldChangelistImpl does, then caching values in these two variables
  # won't be necessary.
  owner_email = changelist.GetIssueOwner()

  buildbucket_put_url = (
      'https://{hostname}/_ah/api/buildbucket/v1/builds/batch'.format(
          hostname=options.buildbucket_host))
  buildset = 'patch/{codereview}/{hostname}/{issue}/{patch}'.format(
      codereview='gerrit' if changelist.IsGerrit() else 'rietveld',
      hostname=codereview_host,
      issue=changelist.GetIssue(),
      patch=patchset)

  shared_parameters_properties = changelist.GetTryjobProperties(patchset)
  shared_parameters_properties['category'] = category
  if options.clobber:
    shared_parameters_properties['clobber'] = True
  extra_properties = _get_properties_from_options(options)
  if extra_properties:
    shared_parameters_properties.update(extra_properties)

  batch_req_body = {'builds': []}
  print_text = []
  print_text.append('Tried jobs on:')
  for bucket, builders_and_tests in sorted(buckets.iteritems()):
    print_text.append('Bucket: %s' % bucket)
    master = None
    if bucket.startswith(MASTER_PREFIX):
      master = _unprefix_master(bucket)
    for builder, tests in sorted(builders_and_tests.iteritems()):
      print_text.append('  %s: %s' % (builder, tests))
      parameters = {
          'builder_name': builder,
          'changes': [{
              'author': {'email': owner_email},
              'revision': options.revision,
          }],
          'properties': shared_parameters_properties.copy(),
      }
      if 'presubmit' in builder.lower():
        parameters['properties']['dry_run'] = 'true'
      if tests:
        parameters['properties']['testfilter'] = tests

      tags = [
          'builder:%s' % builder,
          'buildset:%s' % buildset,
          'user_agent:git_cl_try',
      ]
      if master:
        parameters['properties']['master'] = master
        tags.append('master:%s' % master)

      batch_req_body['builds'].append(
          {
              'bucket': bucket,
              'parameters_json': json.dumps(parameters),
              'client_operation_id': str(uuid.uuid4()),
              'tags': tags,
          }
      )

  _buildbucket_retry(
      'triggering try jobs',
      http,
      buildbucket_put_url,
      'PUT',
      body=json.dumps(batch_req_body),
      headers={'Content-Type': 'application/json'}
  )
  print_text.append('To see results here, run:        git cl try-results')
  print_text.append('To see results in browser, run:  git cl web')
  print('\n'.join(print_text))


def fetch_try_jobs(auth_config, changelist, buildbucket_host,
                   patchset=None):
  """Fetches try jobs from buildbucket.

  Returns a map from build id to build info as a dictionary.
  """
  assert buildbucket_host
  assert changelist.GetIssue(), 'CL must be uploaded first'
  assert changelist.GetCodereviewServer(), 'CL must be uploaded first'
  patchset = patchset or changelist.GetMostRecentPatchset()
  assert patchset, 'CL must be uploaded first'

  codereview_url = changelist.GetCodereviewServer()
  codereview_host = urlparse.urlparse(codereview_url).hostname
  authenticator = auth.get_authenticator_for_host(codereview_host, auth_config)
  if authenticator.has_cached_credentials():
    http = authenticator.authorize(httplib2.Http())
  else:
    print('Warning: Some results might be missing because %s' %
          # Get the message on how to login.
          (auth.LoginRequiredError(codereview_host).message,))
    http = httplib2.Http()

  http.force_exception_to_status_code = True

  buildset = 'patch/{codereview}/{hostname}/{issue}/{patch}'.format(
      codereview='gerrit' if changelist.IsGerrit() else 'rietveld',
      hostname=codereview_host,
      issue=changelist.GetIssue(),
      patch=patchset)
  params = {'tag': 'buildset:%s' % buildset}

  builds = {}
  while True:
    url = 'https://{hostname}/_ah/api/buildbucket/v1/search?{params}'.format(
        hostname=buildbucket_host,
        params=urllib.urlencode(params))
    content = _buildbucket_retry('fetching try jobs', http, url, 'GET')
    for build in content.get('builds', []):
      builds[build['id']] = build
    if 'next_cursor' in content:
      params['start_cursor'] = content['next_cursor']
    else:
      break
  return builds


def print_try_jobs(options, builds):
  """Prints nicely result of fetch_try_jobs."""
  if not builds:
    print('No try jobs scheduled')
    return

  # Make a copy, because we'll be modifying builds dictionary.
  builds = builds.copy()
  builder_names_cache = {}

  def get_builder(b):
    try:
      return builder_names_cache[b['id']]
    except KeyError:
      try:
        parameters = json.loads(b['parameters_json'])
        name = parameters['builder_name']
      except (ValueError, KeyError) as error:
        print('WARNING: failed to get builder name for build %s: %s' % (
              b['id'], error))
        name = None
      builder_names_cache[b['id']] = name
      return name

  def get_bucket(b):
    bucket = b['bucket']
    if bucket.startswith('master.'):
      return bucket[len('master.'):]
    return bucket

  if options.print_master:
    name_fmt = '%%-%ds %%-%ds' % (
        max(len(str(get_bucket(b))) for b in builds.itervalues()),
        max(len(str(get_builder(b))) for b in builds.itervalues()))
    def get_name(b):
      return name_fmt % (get_bucket(b), get_builder(b))
  else:
    name_fmt = '%%-%ds' % (
        max(len(str(get_builder(b))) for b in builds.itervalues()))
    def get_name(b):
      return name_fmt % get_builder(b)

  def sort_key(b):
    return b['status'], b.get('result'), get_name(b), b.get('url')

  def pop(title, f, color=None, **kwargs):
    """Pop matching builds from `builds` dict and print them."""

    if not options.color or color is None:
      colorize = str
    else:
      colorize = lambda x: '%s%s%s' % (color, x, Fore.RESET)

    result = []
    for b in builds.values():
      if all(b.get(k) == v for k, v in kwargs.iteritems()):
        builds.pop(b['id'])
        result.append(b)
    if result:
      print(colorize(title))
      for b in sorted(result, key=sort_key):
        print(' ', colorize('\t'.join(map(str, f(b)))))

  total = len(builds)
  pop(status='COMPLETED', result='SUCCESS',
      title='Successes:', color=Fore.GREEN,
      f=lambda b: (get_name(b), b.get('url')))
  pop(status='COMPLETED', result='FAILURE', failure_reason='INFRA_FAILURE',
      title='Infra Failures:', color=Fore.MAGENTA,
      f=lambda b: (get_name(b), b.get('url')))
  pop(status='COMPLETED', result='FAILURE', failure_reason='BUILD_FAILURE',
      title='Failures:', color=Fore.RED,
      f=lambda b: (get_name(b), b.get('url')))
  pop(status='COMPLETED', result='CANCELED',
      title='Canceled:', color=Fore.MAGENTA,
      f=lambda b: (get_name(b),))
  pop(status='COMPLETED', result='FAILURE',
      failure_reason='INVALID_BUILD_DEFINITION',
      title='Wrong master/builder name:', color=Fore.MAGENTA,
      f=lambda b: (get_name(b),))
  pop(status='COMPLETED', result='FAILURE',
      title='Other failures:',
      f=lambda b: (get_name(b), b.get('failure_reason'), b.get('url')))
  pop(status='COMPLETED',
      title='Other finished:',
      f=lambda b: (get_name(b), b.get('result'), b.get('url')))
  pop(status='STARTED',
      title='Started:', color=Fore.YELLOW,
      f=lambda b: (get_name(b), b.get('url')))
  pop(status='SCHEDULED',
      title='Scheduled:',
      f=lambda b: (get_name(b), 'id=%s' % b['id']))
  # The last section is just in case buildbucket API changes OR there is a bug.
  pop(title='Other:',
      f=lambda b: (get_name(b), 'id=%s' % b['id']))
  assert len(builds) == 0
  print('Total: %d try jobs' % total)


def write_try_results_json(output_file, builds):
  """Writes a subset of the data from fetch_try_jobs to a file as JSON.

  The input |builds| dict is assumed to be generated by Buildbucket.
  Buildbucket documentation: http://goo.gl/G0s101
  """

  def convert_build_dict(build):
    return {
        'buildbucket_id': build.get('id'),
        'status': build.get('status'),
        'result': build.get('result'),
        'bucket': build.get('bucket'),
        'builder_name': json.loads(
            build.get('parameters_json', '{}')).get('builder_name'),
        'failure_reason': build.get('failure_reason'),
        'url': build.get('url'),
    }

  converted = []
  for _, build in sorted(builds.items()):
      converted.append(convert_build_dict(build))
  write_json(output_file, converted)


def print_stats(similarity, find_copies, args):
  """Prints statistics about the change to the user."""
  # --no-ext-diff is broken in some versions of Git, so try to work around
  # this by overriding the environment (but there is still a problem if the
  # git config key "diff.external" is used).
  env = GetNoGitPagerEnv()
  if 'GIT_EXTERNAL_DIFF' in env:
    del env['GIT_EXTERNAL_DIFF']

  if find_copies:
    similarity_options = ['-l100000', '-C%s' % similarity]
  else:
    similarity_options = ['-M%s' % similarity]

  try:
    stdout = sys.stdout.fileno()
  except AttributeError:
    stdout = None
  return subprocess2.call(
      ['git',
       'diff', '--no-ext-diff', '--stat'] + similarity_options + args,
      stdout=stdout, env=env)


class BuildbucketResponseException(Exception):
  pass


class Settings(object):
  def __init__(self):
    self.default_server = None
    self.cc = None
    self.root = None
    self.tree_status_url = None
    self.viewvc_url = None
    self.updated = False
    self.is_gerrit = None
    self.squash_gerrit_uploads = None
    self.gerrit_skip_ensure_authenticated = None
    self.git_editor = None
    self.project = None
    self.force_https_commit_url = None
    self.pending_ref_prefix = None

  def LazyUpdateIfNeeded(self):
    """Updates the settings from a codereview.settings file, if available."""
    if not self.updated:
      # The only value that actually changes the behavior is
      # autoupdate = "false". Everything else means "true".
      autoupdate = RunGit(['config', 'rietveld.autoupdate'],
                          error_ok=True
                          ).strip().lower()

      cr_settings_file = FindCodereviewSettingsFile()
      if autoupdate != 'false' and cr_settings_file:
        LoadCodereviewSettingsFromFile(cr_settings_file)
      self.updated = True

  def GetDefaultServerUrl(self, error_ok=False):
    if not self.default_server:
      self.LazyUpdateIfNeeded()
      self.default_server = gclient_utils.UpgradeToHttps(
          self._GetRietveldConfig('server', error_ok=True))
      if error_ok:
        return self.default_server
      if not self.default_server:
        error_message = ('Could not find settings file. You must configure '
                         'your review setup by running "git cl config".')
        self.default_server = gclient_utils.UpgradeToHttps(
            self._GetRietveldConfig('server', error_message=error_message))
    return self.default_server

  @staticmethod
  def GetRelativeRoot():
    return RunGit(['rev-parse', '--show-cdup']).strip()

  def GetRoot(self):
    if self.root is None:
      self.root = os.path.abspath(self.GetRelativeRoot())
    return self.root

  def GetGitMirror(self, remote='origin'):
    """If this checkout is from a local git mirror, return a Mirror object."""
    local_url = RunGit(['config', '--get', 'remote.%s.url' % remote]).strip()
    if not os.path.isdir(local_url):
      return None
    git_cache.Mirror.SetCachePath(os.path.dirname(local_url))
    remote_url = git_cache.Mirror.CacheDirToUrl(local_url)
    # Use the /dev/null print_func to avoid terminal spew in WaitForRealCommit.
    mirror = git_cache.Mirror(remote_url, print_func = lambda *args: None)
    if mirror.exists():
      return mirror
    return None

  def GetTreeStatusUrl(self, error_ok=False):
    if not self.tree_status_url:
      error_message = ('You must configure your tree status URL by running '
                       '"git cl config".')
      self.tree_status_url = self._GetRietveldConfig(
          'tree-status-url', error_ok=error_ok, error_message=error_message)
    return self.tree_status_url

  def GetViewVCUrl(self):
    if not self.viewvc_url:
      self.viewvc_url = self._GetRietveldConfig('viewvc-url', error_ok=True)
    return self.viewvc_url

  def GetBugPrefix(self):
    return self._GetRietveldConfig('bug-prefix', error_ok=True)

  def GetIsSkipDependencyUpload(self, branch_name):
    """Returns true if specified branch should skip dep uploads."""
    return self._GetBranchConfig(branch_name, 'skip-deps-uploads',
                                 error_ok=True)

  def GetRunPostUploadHook(self):
    run_post_upload_hook = self._GetRietveldConfig(
        'run-post-upload-hook', error_ok=True)
    return run_post_upload_hook == "True"

  def GetDefaultCCList(self):
    return self._GetRietveldConfig('cc', error_ok=True)

  def GetDefaultPrivateFlag(self):
    return self._GetRietveldConfig('private', error_ok=True)

  def GetIsGerrit(self):
    """Return true if this repo is assosiated with gerrit code review system."""
    if self.is_gerrit is None:
      self.is_gerrit = self._GetConfig('gerrit.host', error_ok=True)
    return self.is_gerrit

  def GetSquashGerritUploads(self):
    """Return true if uploads to Gerrit should be squashed by default."""
    if self.squash_gerrit_uploads is None:
      self.squash_gerrit_uploads = self.GetSquashGerritUploadsOverride()
      if self.squash_gerrit_uploads is None:
        # Default is squash now (http://crbug.com/611892#c23).
        self.squash_gerrit_uploads = not (
            RunGit(['config', '--bool', 'gerrit.squash-uploads'],
                   error_ok=True).strip() == 'false')
    return self.squash_gerrit_uploads

  def GetSquashGerritUploadsOverride(self):
    """Return True or False if codereview.settings should be overridden.

    Returns None if no override has been defined.
    """
    # See also http://crbug.com/611892#c23
    result = RunGit(['config', '--bool', 'gerrit.override-squash-uploads'],
                    error_ok=True).strip()
    if result == 'true':
      return True
    if result == 'false':
      return False
    return None

  def GetGerritSkipEnsureAuthenticated(self):
    """Return True if EnsureAuthenticated should not be done for Gerrit
    uploads."""
    if self.gerrit_skip_ensure_authenticated is None:
      self.gerrit_skip_ensure_authenticated = (
          RunGit(['config', '--bool', 'gerrit.skip-ensure-authenticated'],
                 error_ok=True).strip() == 'true')
    return self.gerrit_skip_ensure_authenticated

  def GetGitEditor(self):
    """Return the editor specified in the git config, or None if none is."""
    if self.git_editor is None:
      self.git_editor = self._GetConfig('core.editor', error_ok=True)
    return self.git_editor or None

  def GetLintRegex(self):
    return (self._GetRietveldConfig('cpplint-regex', error_ok=True) or
            DEFAULT_LINT_REGEX)

  def GetLintIgnoreRegex(self):
    return (self._GetRietveldConfig('cpplint-ignore-regex', error_ok=True) or
            DEFAULT_LINT_IGNORE_REGEX)

  def GetProject(self):
    if not self.project:
      self.project = self._GetRietveldConfig('project', error_ok=True)
    return self.project

  def GetForceHttpsCommitUrl(self):
    if not self.force_https_commit_url:
      self.force_https_commit_url = self._GetRietveldConfig(
          'force-https-commit-url', error_ok=True)
    return self.force_https_commit_url

  def GetPendingRefPrefix(self):
    if not self.pending_ref_prefix:
      self.pending_ref_prefix = self._GetRietveldConfig(
          'pending-ref-prefix', error_ok=True)
    return self.pending_ref_prefix

  def _GetRietveldConfig(self, param, **kwargs):
    return self._GetConfig('rietveld.' + param, **kwargs)

  def _GetBranchConfig(self, branch_name, param, **kwargs):
    return self._GetConfig('branch.' + branch_name + '.' + param, **kwargs)

  def _GetConfig(self, param, **kwargs):
    self.LazyUpdateIfNeeded()
    return RunGit(['config', param], **kwargs).strip()


class _GitNumbererState(object):
  KNOWN_PROJECTS_WHITELIST = [
      'chromium/src',
      'external/webrtc',
      'v8/v8',
  ]

  @classmethod
  def load(cls, remote_url, remote_ref):
    """Figures out the state by fetching special refs from remote repo.
    """
    assert remote_ref and remote_ref.startswith('refs/'), remote_ref
    url_parts = urlparse.urlparse(remote_url)
    project_name = url_parts.path.lstrip('/').rstrip('git./')
    for known in cls.KNOWN_PROJECTS_WHITELIST:
      if project_name.endswith(known):
        break
    else:
      # Early exit to avoid extra fetches for repos that aren't using gnumbd.
      return cls(cls._get_pending_prefix_fallback(), None)

    # This pollutes local ref space, but the amount of objects is negligible.
    error, _ = cls._run_git_with_code([
        'fetch', remote_url,
        '+refs/meta/config:refs/git_cl/meta/config',
        '+refs/gnumbd-config/main:refs/git_cl/gnumbd-config/main'])
    if error:
      # Some ref doesn't exist or isn't accessible to current user.
      # This shouldn't happen on production KNOWN_PROJECTS_WHITELIST
      # with git-numberer.
      cls._warn('failed to fetch gnumbd and project config for %s: %s',
                remote_url, error)
      return cls(cls._get_pending_prefix_fallback(), None)
    return cls(cls._get_pending_prefix(remote_ref),
               cls._is_validator_enabled(remote_ref))

  @classmethod
  def _get_pending_prefix(cls, ref):
    error, gnumbd_config_data = cls._run_git_with_code(
        ['show', 'refs/git_cl/gnumbd-config/main:config.json'])
    if error:
      cls._warn('gnumbd config file not found')
      return cls._get_pending_prefix_fallback()

    try:
      config = json.loads(gnumbd_config_data)
      if cls.match_refglobs(ref, config['enabled_refglobs']):
        return config['pending_ref_prefix']
      return None
    except KeyboardInterrupt:
      raise
    except Exception as e:
      cls._warn('failed to parse gnumbd config: %s', e)
      return cls._get_pending_prefix_fallback()

  @staticmethod
  def _get_pending_prefix_fallback():
    global settings
    if not settings:
      settings = Settings()
    return settings.GetPendingRefPrefix()

  @classmethod
  def _is_validator_enabled(cls, ref):
    error, project_config_data = cls._run_git_with_code(
        ['show', 'refs/git_cl/meta/config:project.config'])
    if error:
      cls._warn('project.config file not found')
      return False
    # Gerrit's project.config is really a git config file.
    # So, parse it as such.
    with tempfile.NamedTemporaryFile(prefix='git_cl_proj_config') as f:
      f.write(project_config_data)
      # Make sure OS sees this, but don't close the file just yet,
      # as NamedTemporaryFile deletes it on closing.
      f.flush()

      def get_opts(x):
        code, out = cls._run_git_with_code(
            ['config', '-f', f.name, '--get-all',
             'plugin.git-numberer.validate-%s-refglob' % x])
        if code == 0:
          return out.strip().splitlines()
        return []
      enabled, disabled = map(get_opts, ['enabled', 'disabled'])
    logging.info('validator config enabled %s disabled %s refglobs for '
                 '(this ref: %s)', enabled, disabled, ref)

    if cls.match_refglobs(ref, disabled):
      return False
    return cls.match_refglobs(ref, enabled)

  @staticmethod
  def match_refglobs(ref, refglobs):
    for refglob in refglobs:
      if ref == refglob or fnmatch.fnmatch(ref, refglob):
        return True
    return False

  @staticmethod
  def _run_git_with_code(*args, **kwargs):
    # The only reason for this wrapper is easy porting of this code to CQ
    # codebase, which forked git_cl.py and checkouts.py long time ago.
    return RunGitWithCode(*args, **kwargs)

  @staticmethod
  def _warn(msg, *args):
    if args:
      msg = msg % args
    print('WARNING: %s' % msg)

  def __init__(self, pending_prefix, validator_enabled):
    # TODO(tandrii): remove pending_prefix after gnumbd is no more.
    if pending_prefix:
      if not pending_prefix.endswith('/'):
        pending_prefix += '/'
    self._pending_prefix = pending_prefix or None
    self._validator_enabled = validator_enabled or False
    logging.debug('_GitNumbererState(pending: %s, validator: %s)',
                  self._pending_prefix, self._validator_enabled)

  @property
  def pending_prefix(self):
    return self._pending_prefix

  @property
  def should_add_git_number(self):
    return self._validator_enabled and self._pending_prefix is None


def ShortBranchName(branch):
  """Convert a name like 'refs/heads/foo' to just 'foo'."""
  return branch.replace('refs/heads/', '', 1)


def GetCurrentBranchRef():
  """Returns branch ref (e.g., refs/heads/master) or None."""
  return RunGit(['symbolic-ref', 'HEAD'],
                stderr=subprocess2.VOID, error_ok=True).strip() or None


def GetCurrentBranch():
  """Returns current branch or None.

  For refs/heads/* branches, returns just last part. For others, full ref.
  """
  branchref = GetCurrentBranchRef()
  if branchref:
    return ShortBranchName(branchref)
  return None


class _CQState(object):
  """Enum for states of CL with respect to Commit Queue."""
  NONE = 'none'
  DRY_RUN = 'dry_run'
  COMMIT = 'commit'

  ALL_STATES = [NONE, DRY_RUN, COMMIT]


class _ParsedIssueNumberArgument(object):
  def __init__(self, issue=None, patchset=None, hostname=None):
    self.issue = issue
    self.patchset = patchset
    self.hostname = hostname

  @property
  def valid(self):
    return self.issue is not None


def ParseIssueNumberArgument(arg):
  """Parses the issue argument and returns _ParsedIssueNumberArgument."""
  fail_result = _ParsedIssueNumberArgument()

  if arg.isdigit():
    return _ParsedIssueNumberArgument(issue=int(arg))
  if not arg.startswith('http'):
    return fail_result
  url = gclient_utils.UpgradeToHttps(arg)
  try:
    parsed_url = urlparse.urlparse(url)
  except ValueError:
    return fail_result
  for cls in _CODEREVIEW_IMPLEMENTATIONS.itervalues():
    tmp = cls.ParseIssueURL(parsed_url)
    if tmp is not None:
      return tmp
  return fail_result


class GerritChangeNotExists(Exception):
  def __init__(self, issue, url):
    self.issue = issue
    self.url = url
    super(GerritChangeNotExists, self).__init__()

  def __str__(self):
    return 'change %s at %s does not exist or you have no access to it' % (
        self.issue, self.url)


class Changelist(object):
  """Changelist works with one changelist in local branch.

  Supports two codereview backends: Rietveld or Gerrit, selected at object
  creation.

  Notes:
    * Not safe for concurrent multi-{thread,process} use.
    * Caches values from current branch. Therefore, re-use after branch change
      with great care.
  """

  def __init__(self, branchref=None, issue=None, codereview=None, **kwargs):
    """Create a new ChangeList instance.

    If issue is given, the codereview must be given too.

    If `codereview` is given, it must be 'rietveld' or 'gerrit'.
    Otherwise, it's decided based on current configuration of the local branch,
    with default being 'rietveld' for backwards compatibility.
    See _load_codereview_impl for more details.

    **kwargs will be passed directly to codereview implementation.
    """
    # Poke settings so we get the "configure your server" message if necessary.
    global settings
    if not settings:
      # Happens when git_cl.py is used as a utility library.
      settings = Settings()

    if issue:
      assert codereview, 'codereview must be known, if issue is known'

    self.branchref = branchref
    if self.branchref:
      assert branchref.startswith('refs/heads/')
      self.branch = ShortBranchName(self.branchref)
    else:
      self.branch = None
    self.upstream_branch = None
    self.lookedup_issue = False
    self.issue = issue or None
    self.has_description = False
    self.description = None
    self.lookedup_patchset = False
    self.patchset = None
    self.cc = None
    self.watchers = ()
    self._remote = None

    self._codereview_impl = None
    self._codereview = None
    self._load_codereview_impl(codereview, **kwargs)
    assert self._codereview_impl
    assert self._codereview in _CODEREVIEW_IMPLEMENTATIONS

  def _load_codereview_impl(self, codereview=None, **kwargs):
    if codereview:
      assert codereview in _CODEREVIEW_IMPLEMENTATIONS
      cls = _CODEREVIEW_IMPLEMENTATIONS[codereview]
      self._codereview = codereview
      self._codereview_impl = cls(self, **kwargs)
      return

    # Automatic selection based on issue number set for a current branch.
    # Rietveld takes precedence over Gerrit.
    assert not self.issue
    # Whether we find issue or not, we are doing the lookup.
    self.lookedup_issue = True
    if self.GetBranch():
      for codereview, cls in _CODEREVIEW_IMPLEMENTATIONS.iteritems():
        issue = _git_get_branch_config_value(
            cls.IssueConfigKey(), value_type=int, branch=self.GetBranch())
        if issue:
          self._codereview = codereview
          self._codereview_impl = cls(self, **kwargs)
          self.issue = int(issue)
          return

    # No issue is set for this branch, so decide based on repo-wide settings.
    return self._load_codereview_impl(
        codereview='gerrit' if settings.GetIsGerrit() else 'rietveld',
        **kwargs)

  def IsGerrit(self):
    return self._codereview == 'gerrit'

  def GetCCList(self):
    """Return the users cc'd on this CL.

    Return is a string suitable for passing to git cl with the --cc flag.
    """
    if self.cc is None:
      base_cc = settings.GetDefaultCCList()
      more_cc = ','.join(self.watchers)
      self.cc = ','.join(filter(None, (base_cc, more_cc))) or ''
    return self.cc

  def GetCCListWithoutDefault(self):
    """Return the users cc'd on this CL excluding default ones."""
    if self.cc is None:
      self.cc = ','.join(self.watchers)
    return self.cc

  def SetWatchers(self, watchers):
    """Set the list of email addresses that should be cc'd based on the changed
       files in this CL.
    """
    self.watchers = watchers

  def GetBranch(self):
    """Returns the short branch name, e.g. 'master'."""
    if not self.branch:
      branchref = GetCurrentBranchRef()
      if not branchref:
        return None
      self.branchref = branchref
      self.branch = ShortBranchName(self.branchref)
    return self.branch

  def GetBranchRef(self):
    """Returns the full branch name, e.g. 'refs/heads/master'."""
    self.GetBranch()  # Poke the lazy loader.
    return self.branchref

  def ClearBranch(self):
    """Clears cached branch data of this object."""
    self.branch = self.branchref = None

  def _GitGetBranchConfigValue(self, key, default=None, **kwargs):
    assert 'branch' not in kwargs, 'this CL branch is used automatically'
    kwargs['branch'] = self.GetBranch()
    return _git_get_branch_config_value(key, default, **kwargs)

  def _GitSetBranchConfigValue(self, key, value, **kwargs):
    assert 'branch' not in kwargs, 'this CL branch is used automatically'
    assert self.GetBranch(), (
        'this CL must have an associated branch to %sset %s%s' %
          ('un' if value is None else '',
           key,
           '' if value is None else ' to %r' % value))
    kwargs['branch'] = self.GetBranch()
    return _git_set_branch_config_value(key, value, **kwargs)

  @staticmethod
  def FetchUpstreamTuple(branch):
    """Returns a tuple containing remote and remote ref,
       e.g. 'origin', 'refs/heads/master'
    """
    remote = '.'
    upstream_branch = _git_get_branch_config_value('merge', branch=branch)

    if upstream_branch:
      remote = _git_get_branch_config_value('remote', branch=branch)
    else:
      upstream_branch = RunGit(['config', 'rietveld.upstream-branch'],
                               error_ok=True).strip()
      if upstream_branch:
        remote = RunGit(['config', 'rietveld.upstream-remote']).strip()
      else:
        # Else, try to guess the origin remote.
        remote_branches = RunGit(['branch', '-r']).split()
        if 'origin/master' in remote_branches:
          # Fall back on origin/master if it exits.
          remote = 'origin'
          upstream_branch = 'refs/heads/master'
        else:
          DieWithError(
             'Unable to determine default branch to diff against.\n'
             'Either pass complete "git diff"-style arguments, like\n'
             '  git cl upload origin/master\n'
             'or verify this branch is set up to track another \n'
             '(via the --track argument to "git checkout -b ...").')

    return remote, upstream_branch

  def GetCommonAncestorWithUpstream(self):
    upstream_branch = self.GetUpstreamBranch()
    if not BranchExists(upstream_branch):
      DieWithError('The upstream for the current branch (%s) does not exist '
                   'anymore.\nPlease fix it and try again.' % self.GetBranch())
    return git_common.get_or_create_merge_base(self.GetBranch(),
                                               upstream_branch)

  def GetUpstreamBranch(self):
    if self.upstream_branch is None:
      remote, upstream_branch = self.FetchUpstreamTuple(self.GetBranch())
      if remote is not '.':
        upstream_branch = upstream_branch.replace('refs/heads/',
                                                  'refs/remotes/%s/' % remote)
        upstream_branch = upstream_branch.replace('refs/branch-heads/',
                                                  'refs/remotes/branch-heads/')
      self.upstream_branch = upstream_branch
    return self.upstream_branch

  def GetRemoteBranch(self):
    if not self._remote:
      remote, branch = None, self.GetBranch()
      seen_branches = set()
      while branch not in seen_branches:
        seen_branches.add(branch)
        remote, branch = self.FetchUpstreamTuple(branch)
        branch = ShortBranchName(branch)
        if remote != '.' or branch.startswith('refs/remotes'):
          break
      else:
        remotes = RunGit(['remote'], error_ok=True).split()
        if len(remotes) == 1:
          remote, = remotes
        elif 'origin' in remotes:
          remote = 'origin'
          logging.warn('Could not determine which remote this change is '
                       'associated with, so defaulting to "%s".' % self._remote)
        else:
          logging.warn('Could not determine which remote this change is '
                       'associated with.')
        branch = 'HEAD'
      if branch.startswith('refs/remotes'):
        self._remote = (remote, branch)
      elif branch.startswith('refs/branch-heads/'):
        self._remote = (remote, branch.replace('refs/', 'refs/remotes/'))
      else:
        self._remote = (remote, 'refs/remotes/%s/%s' % (remote, branch))
    return self._remote

  def GitSanityChecks(self, upstream_git_obj):
    """Checks git repo status and ensures diff is from local commits."""

    if upstream_git_obj is None:
      if self.GetBranch() is None:
        print('ERROR: unable to determine current branch (detached HEAD?)',
              file=sys.stderr)
      else:
        print('ERROR: no upstream branch', file=sys.stderr)
      return False

    # Verify the commit we're diffing against is in our current branch.
    upstream_sha = RunGit(['rev-parse', '--verify', upstream_git_obj]).strip()
    common_ancestor = RunGit(['merge-base', upstream_sha, 'HEAD']).strip()
    if upstream_sha != common_ancestor:
      print('ERROR: %s is not in the current branch.  You may need to rebase '
            'your tracking branch' % upstream_sha, file=sys.stderr)
      return False

    # List the commits inside the diff, and verify they are all local.
    commits_in_diff = RunGit(
        ['rev-list', '^%s' % upstream_sha, 'HEAD']).splitlines()
    code, remote_branch = RunGitWithCode(['config', 'gitcl.remotebranch'])
    remote_branch = remote_branch.strip()
    if code != 0:
      _, remote_branch = self.GetRemoteBranch()

    commits_in_remote = RunGit(
        ['rev-list', '^%s' % upstream_sha, remote_branch]).splitlines()

    common_commits = set(commits_in_diff) & set(commits_in_remote)
    if common_commits:
      print('ERROR: Your diff contains %d commits already in %s.\n'
            'Run "git log --oneline %s..HEAD" to get a list of commits in '
            'the diff.  If you are using a custom git flow, you can override'
            ' the reference used for this check with "git config '
            'gitcl.remotebranch <git-ref>".' % (
                len(common_commits), remote_branch, upstream_git_obj),
            file=sys.stderr)
      return False
    return True

  def GetGitBaseUrlFromConfig(self):
    """Return the configured base URL from branch.<branchname>.baseurl.

    Returns None if it is not set.
    """
    return self._GitGetBranchConfigValue('base-url')

  def GetRemoteUrl(self):
    """Return the configured remote URL, e.g. 'git://example.org/foo.git/'.

    Returns None if there is no remote.
    """
    remote, _ = self.GetRemoteBranch()
    url = RunGit(['config', 'remote.%s.url' % remote], error_ok=True).strip()

    # If URL is pointing to a local directory, it is probably a git cache.
    if os.path.isdir(url):
      url = RunGit(['config', 'remote.%s.url' % remote],
                   error_ok=True,
                   cwd=url).strip()
    return url

  def GetIssue(self):
    """Returns the issue number as a int or None if not set."""
    if self.issue is None and not self.lookedup_issue:
      self.issue = self._GitGetBranchConfigValue(
          self._codereview_impl.IssueConfigKey(), value_type=int)
      self.lookedup_issue = True
    return self.issue

  def GetIssueURL(self):
    """Get the URL for a particular issue."""
    issue = self.GetIssue()
    if not issue:
      return None
    return '%s/%s' % (self._codereview_impl.GetCodereviewServer(), issue)

  def GetDescription(self, pretty=False):
    if not self.has_description:
      if self.GetIssue():
        self.description = self._codereview_impl.FetchDescription()
      self.has_description = True
    if pretty:
      # Set width to 72 columns + 2 space indent.
      wrapper = textwrap.TextWrapper(width=74, replace_whitespace=True)
      wrapper.initial_indent = wrapper.subsequent_indent = '  '
      lines = self.description.splitlines()
      return '\n'.join([wrapper.fill(line) for line in lines])
    return self.description

  def GetPatchset(self):
    """Returns the patchset number as a int or None if not set."""
    if self.patchset is None and not self.lookedup_patchset:
      self.patchset = self._GitGetBranchConfigValue(
          self._codereview_impl.PatchsetConfigKey(), value_type=int)
      self.lookedup_patchset = True
    return self.patchset

  def SetPatchset(self, patchset):
    """Set this branch's patchset. If patchset=0, clears the patchset."""
    assert self.GetBranch()
    if not patchset:
      self.patchset = None
    else:
      self.patchset = int(patchset)
    self._GitSetBranchConfigValue(
        self._codereview_impl.PatchsetConfigKey(), self.patchset)

  def SetIssue(self, issue=None):
    """Set this branch's issue. If issue isn't given, clears the issue."""
    assert self.GetBranch()
    if issue:
      issue = int(issue)
      self._GitSetBranchConfigValue(
          self._codereview_impl.IssueConfigKey(), issue)
      self.issue = issue
      codereview_server = self._codereview_impl.GetCodereviewServer()
      if codereview_server:
        self._GitSetBranchConfigValue(
            self._codereview_impl.CodereviewServerConfigKey(),
            codereview_server)
    else:
      # Reset all of these just to be clean.
      reset_suffixes = [
          'last-upload-hash',
          self._codereview_impl.IssueConfigKey(),
          self._codereview_impl.PatchsetConfigKey(),
          self._codereview_impl.CodereviewServerConfigKey(),
      ] + self._PostUnsetIssueProperties()
      for prop in reset_suffixes:
        self._GitSetBranchConfigValue(prop, None, error_ok=True)
      self.issue = None
      self.patchset = None

  def GetChange(self, upstream_branch, author, local_description=False):
    if not self.GitSanityChecks(upstream_branch):
      DieWithError('\nGit sanity check failure')

    root = settings.GetRelativeRoot()
    if not root:
      root = '.'
    absroot = os.path.abspath(root)

    # We use the sha1 of HEAD as a name of this change.
    name = RunGitWithCode(['rev-parse', 'HEAD'])[1].strip()
    # Need to pass a relative path for msysgit.
    try:
      files = scm.GIT.CaptureStatus([root], '.', upstream_branch)
    except subprocess2.CalledProcessError:
      DieWithError(
          ('\nFailed to diff against upstream branch %s\n\n'
           'This branch probably doesn\'t exist anymore. To reset the\n'
           'tracking branch, please run\n'
           '    git branch --set-upstream-to origin/master %s\n'
           'or replace origin/master with the relevant branch') %
          (upstream_branch, self.GetBranch()))

    issue = self.GetIssue()
    patchset = self.GetPatchset()
    if issue and not local_description:
      description = self.GetDescription()
    else:
      # If the change was never uploaded, use the log messages of all commits
      # up to the branch point, as git cl upload will prefill the description
      # with these log messages.
      args = ['log', '--pretty=format:%s%n%n%b', '%s...' % (upstream_branch)]
      description = RunGitWithCode(args)[1].strip()

    if not author:
      author = RunGit(['config', 'user.email']).strip() or None
    return presubmit_support.GitChange(
        name,
        description,
        absroot,
        files,
        issue,
        patchset,
        author,
        upstream=upstream_branch)

  def UpdateDescription(self, description, force=False):
    self.description = description
    return self._codereview_impl.UpdateDescriptionRemote(
        description, force=force)

  def RunHook(self, committing, may_prompt, verbose, change):
    """Calls sys.exit() if the hook fails; returns a HookResults otherwise."""
    try:
      return presubmit_support.DoPresubmitChecks(change, committing,
          verbose=verbose, output_stream=sys.stdout, input_stream=sys.stdin,
          default_presubmit=None, may_prompt=may_prompt,
          rietveld_obj=self._codereview_impl.GetRieveldObjForPresubmit(),
          gerrit_obj=self._codereview_impl.GetGerritObjForPresubmit())
    except presubmit_support.PresubmitFailure as e:
      DieWithError(
          ('%s\nMaybe your depot_tools is out of date?\n'
           'If all fails, contact maruel@') % e)

  def CMDPatchIssue(self, issue_arg, reject, nocommit, directory):
    """Fetches and applies the issue patch from codereview to local branch."""
    if isinstance(issue_arg, (int, long)) or issue_arg.isdigit():
      parsed_issue_arg = _ParsedIssueNumberArgument(int(issue_arg))
    else:
      # Assume url.
      parsed_issue_arg = self._codereview_impl.ParseIssueURL(
          urlparse.urlparse(issue_arg))
    if not parsed_issue_arg or not parsed_issue_arg.valid:
      DieWithError('Failed to parse issue argument "%s". '
                   'Must be an issue number or a valid URL.' % issue_arg)
    return self._codereview_impl.CMDPatchWithParsedIssue(
        parsed_issue_arg, reject, nocommit, directory)

  def CMDUpload(self, options, git_diff_args, orig_args):
    """Uploads a change to codereview."""
    if git_diff_args:
      # TODO(ukai): is it ok for gerrit case?
      base_branch = git_diff_args[0]
    else:
      if self.GetBranch() is None:
        DieWithError('Can\'t upload from detached HEAD state. Get on a branch!')

      # Default to diffing against common ancestor of upstream branch
      base_branch = self.GetCommonAncestorWithUpstream()
      git_diff_args = [base_branch, 'HEAD']

    # Make sure authenticated to codereview before running potentially expensive
    # hooks.  It is a fast, best efforts check. Codereview still can reject the
    # authentication during the actual upload.
    self._codereview_impl.EnsureAuthenticated(force=options.force)

    # Apply watchlists on upload.
    change = self.GetChange(base_branch, None)
    watchlist = watchlists.Watchlists(change.RepositoryRoot())
    files = [f.LocalPath() for f in change.AffectedFiles()]
    if not options.bypass_watchlists:
      self.SetWatchers(watchlist.GetWatchersForPaths(files))

    if not options.bypass_hooks:
      if options.reviewers or options.tbr_owners:
        # Set the reviewer list now so that presubmit checks can access it.
        change_description = ChangeDescription(change.FullDescriptionText())
        change_description.update_reviewers(options.reviewers,
                                            options.tbr_owners,
                                            change)
        change.SetDescriptionText(change_description.description)
      hook_results = self.RunHook(committing=False,
                                may_prompt=not options.force,
                                verbose=options.verbose,
                                change=change)
      if not hook_results.should_continue():
        return 1
      if not options.reviewers and hook_results.reviewers:
        options.reviewers = hook_results.reviewers.split(',')

    # TODO(tandrii): Checking local patchset against remote patchset is only
    # supported for Rietveld. Extend it to Gerrit or remove it completely.
    if self.GetIssue() and not self.IsGerrit():
      latest_patchset = self.GetMostRecentPatchset()
      local_patchset = self.GetPatchset()
      if (latest_patchset and local_patchset and
          local_patchset != latest_patchset):
        print('The last upload made from this repository was patchset #%d but '
              'the most recent patchset on the server is #%d.'
              % (local_patchset, latest_patchset))
        print('Uploading will still work, but if you\'ve uploaded to this '
              'issue from another machine or branch the patch you\'re '
              'uploading now might not include those changes.')
        ask_for_data('About to upload; enter to confirm.')

    print_stats(options.similarity, options.find_copies, git_diff_args)
    ret = self.CMDUploadChange(options, git_diff_args, change)
    if not ret:
      if options.use_commit_queue:
        self.SetCQState(_CQState.COMMIT)
      elif options.cq_dry_run:
        self.SetCQState(_CQState.DRY_RUN)

      _git_set_branch_config_value('last-upload-hash',
                                   RunGit(['rev-parse', 'HEAD']).strip())
      # Run post upload hooks, if specified.
      if settings.GetRunPostUploadHook():
        presubmit_support.DoPostUploadExecuter(
            change,
            self,
            settings.GetRoot(),
            options.verbose,
            sys.stdout)

      # Upload all dependencies if specified.
      if options.dependencies:
        print()
        print('--dependencies has been specified.')
        print('All dependent local branches will be re-uploaded.')
        print()
        # Remove the dependencies flag from args so that we do not end up in a
        # loop.
        orig_args.remove('--dependencies')
        ret = upload_branch_deps(self, orig_args)
    return ret

  def SetCQState(self, new_state):
    """Update the CQ state for latest patchset.

    Issue must have been already uploaded and known.
    """
    assert new_state in _CQState.ALL_STATES
    assert self.GetIssue()
    return self._codereview_impl.SetCQState(new_state)

  def TriggerDryRun(self):
    """Triggers a dry run and prints a warning on failure."""
    # TODO(qyearsley): Either re-use this method in CMDset_commit
    # and CMDupload, or change CMDtry to trigger dry runs with
    # just SetCQState, and catch keyboard interrupt and other
    # errors in that method.
    try:
      self.SetCQState(_CQState.DRY_RUN)
      print('scheduled CQ Dry Run on %s' % self.GetIssueURL())
      return 0
    except KeyboardInterrupt:
      raise
    except:
      print('WARNING: failed to trigger CQ Dry Run.\n'
            'Either:\n'
            ' * your project has no CQ\n'
            ' * you don\'t have permission to trigger Dry Run\n'
            ' * bug in this code (see stack trace below).\n'
            'Consider specifying which bots to trigger manually '
            'or asking your project owners for permissions '
            'or contacting Chrome Infrastructure team at '
            'https://www.chromium.org/infra\n\n')
      # Still raise exception so that stack trace is printed.
      raise

  # Forward methods to codereview specific implementation.

  def CloseIssue(self):
    return self._codereview_impl.CloseIssue()

  def GetStatus(self):
    return self._codereview_impl.GetStatus()

  def GetCodereviewServer(self):
    return self._codereview_impl.GetCodereviewServer()

  def GetIssueOwner(self):
    """Get owner from codereview, which may differ from this checkout."""
    return self._codereview_impl.GetIssueOwner()

  def GetApprovingReviewers(self):
    return self._codereview_impl.GetApprovingReviewers()

  def GetMostRecentPatchset(self):
    return self._codereview_impl.GetMostRecentPatchset()

  def CannotTriggerTryJobReason(self):
    """Returns reason (str) if unable trigger tryjobs on this CL or None."""
    return self._codereview_impl.CannotTriggerTryJobReason()

  def GetTryjobProperties(self, patchset=None):
    """Returns dictionary of properties to launch tryjob."""
    return self._codereview_impl.GetTryjobProperties(patchset=patchset)

  def __getattr__(self, attr):
    # This is because lots of untested code accesses Rietveld-specific stuff
    # directly, and it's hard to fix for sure. So, just let it work, and fix
    # on a case by case basis.
    # Note that child method defines __getattr__ as well, and forwards it here,
    # because _RietveldChangelistImpl is not cleaned up yet, and given
    # deprecation of Rietveld, it should probably be just removed.
    # Until that time, avoid infinite recursion by bypassing __getattr__
    # of implementation class.
    return self._codereview_impl.__getattribute__(attr)


class _ChangelistCodereviewBase(object):
  """Abstract base class encapsulating codereview specifics of a changelist."""
  def __init__(self, changelist):
    self._changelist = changelist  # instance of Changelist

  def __getattr__(self, attr):
    # Forward methods to changelist.
    # TODO(tandrii): maybe clean up _GerritChangelistImpl and
    # _RietveldChangelistImpl to avoid this hack?
    return getattr(self._changelist, attr)

  def GetStatus(self):
    """Apply a rough heuristic to give a simple summary of an issue's review
    or CQ status, assuming adherence to a common workflow.

    Returns None if no issue for this branch, or specific string keywords.
    """
    raise NotImplementedError()

  def GetCodereviewServer(self):
    """Returns server URL without end slash, like "https://codereview.com"."""
    raise NotImplementedError()

  def FetchDescription(self):
    """Fetches and returns description from the codereview server."""
    raise NotImplementedError()

  @classmethod
  def IssueConfigKey(cls):
    """Returns branch setting storing issue number."""
    raise NotImplementedError()

  @classmethod
  def PatchsetConfigKey(cls):
    """Returns branch setting storing patchset number."""
    raise NotImplementedError()

  @classmethod
  def CodereviewServerConfigKey(cls):
    """Returns branch setting storing codereview server."""
    raise NotImplementedError()

  def _PostUnsetIssueProperties(self):
    """Which branch-specific properties to erase when unsettin issue."""
    return []

  def GetRieveldObjForPresubmit(self):
    # This is an unfortunate Rietveld-embeddedness in presubmit.
    # For non-Rietveld codereviews, this probably should return a dummy object.
    raise NotImplementedError()

  def GetGerritObjForPresubmit(self):
    # None is valid return value, otherwise presubmit_support.GerritAccessor.
    return None

  def UpdateDescriptionRemote(self, description, force=False):
    """Update the description on codereview site."""
    raise NotImplementedError()

  def CloseIssue(self):
    """Closes the issue."""
    raise NotImplementedError()

  def GetApprovingReviewers(self):
    """Returns a list of reviewers approving the change.

    Note: not necessarily committers.
    """
    raise NotImplementedError()

  def GetMostRecentPatchset(self):
    """Returns the most recent patchset number from the codereview site."""
    raise NotImplementedError()

  def CMDPatchWithParsedIssue(self, parsed_issue_arg, reject, nocommit,
                              directory):
    """Fetches and applies the issue.

    Arguments:
      parsed_issue_arg: instance of _ParsedIssueNumberArgument.
      reject: if True, reject the failed patch instead of switching to 3-way
        merge. Rietveld only.
      nocommit: do not commit the patch, thus leave the tree dirty. Rietveld
        only.
      directory: switch to directory before applying the patch. Rietveld only.
    """
    raise NotImplementedError()

  @staticmethod
  def ParseIssueURL(parsed_url):
    """Parses url and returns instance of _ParsedIssueNumberArgument or None if
    failed."""
    raise NotImplementedError()

  def EnsureAuthenticated(self, force):
    """Best effort check that user is authenticated with codereview server.

    Arguments:
      force: whether to skip confirmation questions.
    """
    raise NotImplementedError()

  def CMDUploadChange(self, options, args, change):
    """Uploads a change to codereview."""
    raise NotImplementedError()

  def SetCQState(self, new_state):
    """Update the CQ state for latest patchset.

    Issue must have been already uploaded and known.
    """
    raise NotImplementedError()

  def CannotTriggerTryJobReason(self):
    """Returns reason (str) if unable trigger tryjobs on this CL or None."""
    raise NotImplementedError()

  def GetIssueOwner(self):
    raise NotImplementedError()

  def GetTryjobProperties(self, patchset=None):
    raise NotImplementedError()


class _RietveldChangelistImpl(_ChangelistCodereviewBase):
  def __init__(self, changelist, auth_config=None, rietveld_server=None):
    super(_RietveldChangelistImpl, self).__init__(changelist)
    assert settings, 'must be initialized in _ChangelistCodereviewBase'
    if not rietveld_server:
      settings.GetDefaultServerUrl()

    self._rietveld_server = rietveld_server
    self._auth_config = auth_config
    self._props = None
    self._rpc_server = None

  def GetCodereviewServer(self):
    if not self._rietveld_server:
      # If we're on a branch then get the server potentially associated
      # with that branch.
      if self.GetIssue():
        self._rietveld_server = gclient_utils.UpgradeToHttps(
            self._GitGetBranchConfigValue(self.CodereviewServerConfigKey()))
      if not self._rietveld_server:
        self._rietveld_server = settings.GetDefaultServerUrl()
    return self._rietveld_server

  def EnsureAuthenticated(self, force):
    """Best effort check that user is authenticated with Rietveld server."""
    if self._auth_config.use_oauth2:
      authenticator = auth.get_authenticator_for_host(
          self.GetCodereviewServer(), self._auth_config)
      if not authenticator.has_cached_credentials():
        raise auth.LoginRequiredError(self.GetCodereviewServer())

  def FetchDescription(self):
    issue = self.GetIssue()
    assert issue
    try:
      return self.RpcServer().get_description(issue).strip()
    except urllib2.HTTPError as e:
      if e.code == 404:
        DieWithError(
            ('\nWhile fetching the description for issue %d, received a '
             '404 (not found)\n'
             'error. It is likely that you deleted this '
             'issue on the server. If this is the\n'
             'case, please run\n\n'
             '    git cl issue 0\n\n'
             'to clear the association with the deleted issue. Then run '
             'this command again.') % issue)
      else:
        DieWithError(
            '\nFailed to fetch issue description. HTTP error %d' % e.code)
    except urllib2.URLError as e:
      print('Warning: Failed to retrieve CL description due to network '
            'failure.', file=sys.stderr)
      return ''

  def GetMostRecentPatchset(self):
    return self.GetIssueProperties()['patchsets'][-1]

  def GetIssueProperties(self):
    if self._props is None:
      issue = self.GetIssue()
      if not issue:
        self._props = {}
      else:
        self._props = self.RpcServer().get_issue_properties(issue, True)
    return self._props

  def CannotTriggerTryJobReason(self):
    props = self.GetIssueProperties()
    if not props:
      return 'Rietveld doesn\'t know about your issue %s' % self.GetIssue()
    if props.get('closed'):
      return 'CL %s is closed' % self.GetIssue()
    if props.get('private'):
      return 'CL %s is private' % self.GetIssue()
    return None

  def GetTryjobProperties(self, patchset=None):
    """Returns dictionary of properties to launch tryjob."""
    project = (self.GetIssueProperties() or {}).get('project')
    return {
      'issue': self.GetIssue(),
      'patch_project': project,
      'patch_storage': 'rietveld',
      'patchset': patchset or self.GetPatchset(),
      'rietveld': self.GetCodereviewServer(),
    }

  def GetApprovingReviewers(self):
    return get_approving_reviewers(self.GetIssueProperties())

  def GetIssueOwner(self):
    return (self.GetIssueProperties() or {}).get('owner_email')

  def AddComment(self, message):
    return self.RpcServer().add_comment(self.GetIssue(), message)

  def GetStatus(self):
    """Apply a rough heuristic to give a simple summary of an issue's review
    or CQ status, assuming adherence to a common workflow.

    Returns None if no issue for this branch, or one of the following keywords:
      * 'error'   - error from review tool (including deleted issues)
      * 'unsent'  - not sent for review
      * 'waiting' - waiting for review
      * 'reply'   - waiting for owner to reply to review
      * 'lgtm'    - LGTM from at least one approved reviewer
      * 'commit'  - in the commit queue
      * 'closed'  - closed
    """
    if not self.GetIssue():
      return None

    try:
      props = self.GetIssueProperties()
    except urllib2.HTTPError:
      return 'error'

    if props.get('closed'):
      # Issue is closed.
      return 'closed'
    if props.get('commit') and not props.get('cq_dry_run', False):
      # Issue is in the commit queue.
      return 'commit'

    try:
      reviewers = self.GetApprovingReviewers()
    except urllib2.HTTPError:
      return 'error'

    if reviewers:
      # Was LGTM'ed.
      return 'lgtm'

    messages = props.get('messages') or []

    # Skip CQ messages that don't require owner's action.
    while messages and messages[-1]['sender'] == COMMIT_BOT_EMAIL:
      if 'Dry run:' in messages[-1]['text']:
        messages.pop()
      elif 'The CQ bit was unchecked' in messages[-1]['text']:
        # This message always follows prior messages from CQ,
        # so skip this too.
        messages.pop()
      else:
        # This is probably a CQ messages warranting user attention.
        break

    if not messages:
      # No message was sent.
      return 'unsent'
    if messages[-1]['sender'] != props.get('owner_email'):
      # Non-LGTM reply from non-owner and not CQ bot.
      return 'reply'
    return 'waiting'

  def UpdateDescriptionRemote(self, description, force=False):
    return self.RpcServer().update_description(
        self.GetIssue(), self.description)

  def CloseIssue(self):
    return self.RpcServer().close_issue(self.GetIssue())

  def SetFlag(self, flag, value):
    return self.SetFlags({flag: value})

  def SetFlags(self, flags):
    """Sets flags on this CL/patchset in Rietveld.
    """
    patchset = self.GetPatchset() or self.GetMostRecentPatchset()
    try:
      return self.RpcServer().set_flags(
          self.GetIssue(), patchset, flags)
    except urllib2.HTTPError as e:
      if e.code == 404:
        DieWithError('The issue %s doesn\'t exist.' % self.GetIssue())
      if e.code == 403:
        DieWithError(
            ('Access denied to issue %s. Maybe the patchset %s doesn\'t '
             'match?') % (self.GetIssue(), patchset))
      raise

  def RpcServer(self):
    """Returns an upload.RpcServer() to access this review's rietveld instance.
    """
    if not self._rpc_server:
      self._rpc_server = rietveld.CachingRietveld(
          self.GetCodereviewServer(),
          self._auth_config or auth.make_auth_config())
    return self._rpc_server

  @classmethod
  def IssueConfigKey(cls):
    return 'rietveldissue'

  @classmethod
  def PatchsetConfigKey(cls):
    return 'rietveldpatchset'

  @classmethod
  def CodereviewServerConfigKey(cls):
    return 'rietveldserver'

  def GetRieveldObjForPresubmit(self):
    return self.RpcServer()

  def SetCQState(self, new_state):
    props = self.GetIssueProperties()
    if props.get('private'):
      DieWithError('Cannot set-commit on private issue')

    if new_state == _CQState.COMMIT:
      self.SetFlags({'commit': '1', 'cq_dry_run': '0'})
    elif new_state == _CQState.NONE:
      self.SetFlags({'commit': '0', 'cq_dry_run': '0'})
    else:
      assert new_state == _CQState.DRY_RUN
      self.SetFlags({'commit': '1', 'cq_dry_run': '1'})


  def CMDPatchWithParsedIssue(self, parsed_issue_arg, reject, nocommit,
                              directory):
    # PatchIssue should never be called with a dirty tree.  It is up to the
    # caller to check this, but just in case we assert here since the
    # consequences of the caller not checking this could be dire.
    assert(not git_common.is_dirty_git_tree('apply'))
    assert(parsed_issue_arg.valid)
    self._changelist.issue = parsed_issue_arg.issue
    if parsed_issue_arg.hostname:
      self._rietveld_server = 'https://%s' % parsed_issue_arg.hostname

    patchset = parsed_issue_arg.patchset or self.GetMostRecentPatchset()
    patchset_object = self.RpcServer().get_patch(self.GetIssue(), patchset)
    scm_obj = checkout.GitCheckout(settings.GetRoot(), None, None, None, None)
    try:
      scm_obj.apply_patch(patchset_object)
    except Exception as e:
      print(str(e))
      return 1

    # If we had an issue, commit the current state and register the issue.
    if not nocommit:
      RunGit(['commit', '-m', (self.GetDescription() + '\n\n' +
                               'patch from issue %(i)s at patchset '
                               '%(p)s (http://crrev.com/%(i)s#ps%(p)s)'
                               % {'i': self.GetIssue(), 'p': patchset})])
      self.SetIssue(self.GetIssue())
      self.SetPatchset(patchset)
      print('Committed patch locally.')
    else:
      print('Patch applied to index.')
    return 0

  @staticmethod
  def ParseIssueURL(parsed_url):
    if not parsed_url.scheme or not parsed_url.scheme.startswith('http'):
      return None
    # Rietveld patch: https://domain/<number>/#ps<patchset>
    match = re.match(r'/(\d+)/$', parsed_url.path)
    match2 = re.match(r'ps(\d+)$', parsed_url.fragment)
    if match and match2:
      return _ParsedIssueNumberArgument(
          issue=int(match.group(1)),
          patchset=int(match2.group(1)),
          hostname=parsed_url.netloc)
    # Typical url: https://domain/<issue_number>[/[other]]
    match = re.match('/(\d+)(/.*)?$', parsed_url.path)
    if match:
      return _ParsedIssueNumberArgument(
          issue=int(match.group(1)),
          hostname=parsed_url.netloc)
    # Rietveld patch: https://domain/download/issue<number>_<patchset>.diff
    match = re.match(r'/download/issue(\d+)_(\d+).diff$', parsed_url.path)
    if match:
      return _ParsedIssueNumberArgument(
          issue=int(match.group(1)),
          patchset=int(match.group(2)),
          hostname=parsed_url.netloc)
    return None

  def CMDUploadChange(self, options, args, change):
    """Upload the patch to Rietveld."""
    upload_args = ['--assume_yes']  # Don't ask about untracked files.
    upload_args.extend(['--server', self.GetCodereviewServer()])
    upload_args.extend(auth.auth_config_to_command_options(self._auth_config))
    if options.emulate_svn_auto_props:
      upload_args.append('--emulate_svn_auto_props')

    change_desc = None

    if options.email is not None:
      upload_args.extend(['--email', options.email])

    if self.GetIssue():
      if options.title is not None:
        upload_args.extend(['--title', options.title])
      if options.message:
        upload_args.extend(['--message', options.message])
      upload_args.extend(['--issue', str(self.GetIssue())])
      print('This branch is associated with issue %s. '
            'Adding patch to that issue.' % self.GetIssue())
    else:
      if options.title is not None:
        upload_args.extend(['--title', options.title])
      if options.message:
        message = options.message
      else:
        message = CreateDescriptionFromLog(args)
        if options.title:
          message = options.title + '\n\n' + message
      change_desc = ChangeDescription(message)
      if options.reviewers or options.tbr_owners:
        change_desc.update_reviewers(options.reviewers,
                                     options.tbr_owners,
                                     change)
      if not options.force:
        change_desc.prompt(bug=options.bug)

      if not change_desc.description:
        print('Description is empty; aborting.')
        return 1

      upload_args.extend(['--message', change_desc.description])
      if change_desc.get_reviewers():
        upload_args.append('--reviewers=%s' % ','.join(
            change_desc.get_reviewers()))
      if options.send_mail:
        if not change_desc.get_reviewers():
          DieWithError("Must specify reviewers to send email.")
        upload_args.append('--send_mail')

      # We check this before applying rietveld.private assuming that in
      # rietveld.cc only addresses which we can send private CLs to are listed
      # if rietveld.private is set, and so we should ignore rietveld.cc only
      # when --private is specified explicitly on the command line.
      if options.private:
        logging.warn('rietveld.cc is ignored since private flag is specified.  '
                     'You need to review and add them manually if necessary.')
        cc = self.GetCCListWithoutDefault()
      else:
        cc = self.GetCCList()
      cc = ','.join(filter(None, (cc, ','.join(options.cc))))
      if change_desc.get_cced():
        cc = ','.join(filter(None, (cc, ','.join(change_desc.get_cced()))))
      if cc:
        upload_args.extend(['--cc', cc])

    if options.private or settings.GetDefaultPrivateFlag() == "True":
      upload_args.append('--private')

    upload_args.extend(['--git_similarity', str(options.similarity)])
    if not options.find_copies:
      upload_args.extend(['--git_no_find_copies'])

    # Include the upstream repo's URL in the change -- this is useful for
    # projects that have their source spread across multiple repos.
    remote_url = self.GetGitBaseUrlFromConfig()
    if not remote_url:
      if self.GetRemoteUrl() and '/' in self.GetUpstreamBranch():
        remote_url = '%s@%s' % (self.GetRemoteUrl(),
                                self.GetUpstreamBranch().split('/')[-1])
    if remote_url:
      remote, remote_branch = self.GetRemoteBranch()
      target_ref = GetTargetRef(remote, remote_branch, options.target_branch,
                                pending_prefix_check=True,
                                remote_url=self.GetRemoteUrl())
      if target_ref:
        upload_args.extend(['--target_ref', target_ref])

      # Look for dependent patchsets. See crbug.com/480453 for more details.
      remote, upstream_branch = self.FetchUpstreamTuple(self.GetBranch())
      upstream_branch = ShortBranchName(upstream_branch)
      if remote is '.':
        # A local branch is being tracked.
        local_branch = upstream_branch
        if settings.GetIsSkipDependencyUpload(local_branch):
          print()
          print('Skipping dependency patchset upload because git config '
                'branch.%s.skip-deps-uploads is set to True.' % local_branch)
          print()
        else:
          auth_config = auth.extract_auth_config_from_options(options)
          branch_cl = Changelist(branchref='refs/heads/'+local_branch,
                                 auth_config=auth_config)
          branch_cl_issue_url = branch_cl.GetIssueURL()
          branch_cl_issue = branch_cl.GetIssue()
          branch_cl_patchset = branch_cl.GetPatchset()
          if branch_cl_issue_url and branch_cl_issue and branch_cl_patchset:
            upload_args.extend(
                ['--depends_on_patchset', '%s:%s' % (
                     branch_cl_issue, branch_cl_patchset)])
            print(
                '\n'
                'The current branch (%s) is tracking a local branch (%s) with '
                'an associated CL.\n'
                'Adding %s/#ps%s as a dependency patchset.\n'
                '\n' % (self.GetBranch(), local_branch, branch_cl_issue_url,
                        branch_cl_patchset))

    project = settings.GetProject()
    if project:
      upload_args.extend(['--project', project])

    try:
      upload_args = ['upload'] + upload_args + args
      logging.info('upload.RealMain(%s)', upload_args)
      issue, patchset = upload.RealMain(upload_args)
      issue = int(issue)
      patchset = int(patchset)
    except KeyboardInterrupt:
      sys.exit(1)
    except:
      # If we got an exception after the user typed a description for their
      # change, back up the description before re-raising.
      if change_desc:
        backup_path = os.path.expanduser(DESCRIPTION_BACKUP_FILE)
        print('\nGot exception while uploading -- saving description to %s\n' %
              backup_path)
        backup_file = open(backup_path, 'w')
        backup_file.write(change_desc.description)
        backup_file.close()
      raise

    if not self.GetIssue():
      self.SetIssue(issue)
    self.SetPatchset(patchset)
    return 0


class _GerritChangelistImpl(_ChangelistCodereviewBase):
  def __init__(self, changelist, auth_config=None):
    # auth_config is Rietveld thing, kept here to preserve interface only.
    super(_GerritChangelistImpl, self).__init__(changelist)
    self._change_id = None
    # Lazily cached values.
    self._gerrit_server = None  # e.g. https://chromium-review.googlesource.com
    self._gerrit_host = None    # e.g. chromium-review.googlesource.com

  def _GetGerritHost(self):
    # Lazy load of configs.
    self.GetCodereviewServer()
    if self._gerrit_host and '.' not in self._gerrit_host:
      # Abbreviated domain like "chromium" instead of chromium.googlesource.com.
      # This happens for internal stuff http://crbug.com/614312.
      parsed = urlparse.urlparse(self.GetRemoteUrl())
      if parsed.scheme == 'sso':
        print('WARNING: using non https URLs for remote is likely broken\n'
              '  Your current remote is: %s'  % self.GetRemoteUrl())
        self._gerrit_host = '%s.googlesource.com' % self._gerrit_host
        self._gerrit_server = 'https://%s' % self._gerrit_host
    return self._gerrit_host

  def _GetGitHost(self):
    """Returns git host to be used when uploading change to Gerrit."""
    return urlparse.urlparse(self.GetRemoteUrl()).netloc

  def GetCodereviewServer(self):
    if not self._gerrit_server:
      # If we're on a branch then get the server potentially associated
      # with that branch.
      if self.GetIssue():
        self._gerrit_server = self._GitGetBranchConfigValue(
            self.CodereviewServerConfigKey())
        if self._gerrit_server:
          self._gerrit_host = urlparse.urlparse(self._gerrit_server).netloc
      if not self._gerrit_server:
        # We assume repo to be hosted on Gerrit, and hence Gerrit server
        # has "-review" suffix for lowest level subdomain.
        parts = self._GetGitHost().split('.')
        parts[0] = parts[0] + '-review'
        self._gerrit_host = '.'.join(parts)
        self._gerrit_server = 'https://%s' % self._gerrit_host
    return self._gerrit_server

  @classmethod
  def IssueConfigKey(cls):
    return 'gerritissue'

  @classmethod
  def PatchsetConfigKey(cls):
    return 'gerritpatchset'

  @classmethod
  def CodereviewServerConfigKey(cls):
    return 'gerritserver'

  def EnsureAuthenticated(self, force):
    """Best effort check that user is authenticated with Gerrit server."""
    if settings.GetGerritSkipEnsureAuthenticated():
      # For projects with unusual authentication schemes.
      # See http://crbug.com/603378.
      return
    # Lazy-loader to identify Gerrit and Git hosts.
    if gerrit_util.GceAuthenticator.is_gce():
      return
    self.GetCodereviewServer()
    git_host = self._GetGitHost()
    assert self._gerrit_server and self._gerrit_host
    cookie_auth = gerrit_util.CookiesAuthenticator()

    gerrit_auth = cookie_auth.get_auth_header(self._gerrit_host)
    git_auth = cookie_auth.get_auth_header(git_host)
    if gerrit_auth and git_auth:
      if gerrit_auth == git_auth:
        return
      print((
          'WARNING: you have different credentials for Gerrit and git hosts.\n'
          '         Check your %s or %s file for credentials of hosts:\n'
          '           %s\n'
          '           %s\n'
          '         %s') %
          (cookie_auth.get_gitcookies_path(), cookie_auth.get_netrc_path(),
           git_host, self._gerrit_host,
           cookie_auth.get_new_password_message(git_host)))
      if not force:
        ask_for_data('If you know what you are doing, press Enter to continue, '
                     'Ctrl+C to abort.')
      return
    else:
      missing = (
          [] if gerrit_auth else [self._gerrit_host] +
          [] if git_auth else [git_host])
      DieWithError('Credentials for the following hosts are required:\n'
                   '  %s\n'
                   'These are read from %s (or legacy %s)\n'
                   '%s' % (
                     '\n  '.join(missing),
                     cookie_auth.get_gitcookies_path(),
                     cookie_auth.get_netrc_path(),
                     cookie_auth.get_new_password_message(git_host)))

  def _PostUnsetIssueProperties(self):
    """Which branch-specific properties to erase when unsetting issue."""
    return ['gerritsquashhash']

  def GetRieveldObjForPresubmit(self):
    class ThisIsNotRietveldIssue(object):
      def __nonzero__(self):
        # This is a hack to make presubmit_support think that rietveld is not
        # defined, yet still ensure that calls directly result in a decent
        # exception message below.
        return False

      def __getattr__(self, attr):
        print(
            'You aren\'t using Rietveld at the moment, but Gerrit.\n'
            'Using Rietveld in your PRESUBMIT scripts won\'t work.\n'
            'Please, either change your PRESUBIT to not use rietveld_obj.%s,\n'
            'or use Rietveld for codereview.\n'
            'See also http://crbug.com/579160.' % attr)
        raise NotImplementedError()
    return ThisIsNotRietveldIssue()

  def GetGerritObjForPresubmit(self):
    return presubmit_support.GerritAccessor(self._GetGerritHost())

  def GetStatus(self):
    """Apply a rough heuristic to give a simple summary of an issue's review
    or CQ status, assuming adherence to a common workflow.

    Returns None if no issue for this branch, or one of the following keywords:
      * 'error'    - error from review tool (including deleted issues)
      * 'unsent'   - no reviewers added
      * 'waiting'  - waiting for review
      * 'reply'    - waiting for owner to reply to review
      * 'not lgtm' - Code-Review disapproval from at least one valid reviewer
      * 'lgtm'     - Code-Review approval from at least one valid reviewer
      * 'commit'   - in the commit queue
      * 'closed'   - abandoned
    """
    if not self.GetIssue():
      return None

    try:
      data = self._GetChangeDetail(['DETAILED_LABELS', 'CURRENT_REVISION'])
    except (httplib.HTTPException, GerritChangeNotExists):
      return 'error'

    if data['status'] in ('ABANDONED', 'MERGED'):
      return 'closed'

    cq_label = data['labels'].get('Commit-Queue', {})
    if cq_label:
      votes = cq_label.get('all', [])
      highest_vote = 0
      for v in votes:
        highest_vote = max(highest_vote, v.get('value', 0))
      vote_value = str(highest_vote)
      if vote_value != '0':
        # Add a '+' if the value is not 0 to match the values in the label.
        # The cq_label does not have negatives.
        vote_value = '+' + vote_value
      vote_text = cq_label.get('values', {}).get(vote_value, '')
      if vote_text.lower() == 'commit':
        return 'commit'

    lgtm_label = data['labels'].get('Code-Review', {})
    if lgtm_label:
      if 'rejected' in lgtm_label:
        return 'not lgtm'
      if 'approved' in lgtm_label:
        return 'lgtm'

    if not data.get('reviewers', {}).get('REVIEWER', []):
      return 'unsent'

    messages = data.get('messages', [])
    if messages:
      owner = data['owner'].get('_account_id')
      last_message_author = messages[-1].get('author', {}).get('_account_id')
      if owner != last_message_author:
        # Some reply from non-owner.
        return 'reply'

    return 'waiting'

  def GetMostRecentPatchset(self):
    data = self._GetChangeDetail(['CURRENT_REVISION'])
    return data['revisions'][data['current_revision']]['_number']

  def FetchDescription(self):
    data = self._GetChangeDetail(['CURRENT_REVISION'])
    current_rev = data['current_revision']
    url = data['revisions'][current_rev]['fetch']['http']['url']
    return gerrit_util.GetChangeDescriptionFromGitiles(url, current_rev)

  def UpdateDescriptionRemote(self, description, force=False):
    if gerrit_util.HasPendingChangeEdit(self._GetGerritHost(), self.GetIssue()):
      if not force:
        ask_for_data(
            'The description cannot be modified while the issue has a pending '
            'unpublished edit.  Either publish the edit in the Gerrit web UI '
            'or delete it.\n\n'
            'Press Enter to delete the unpublished edit, Ctrl+C to abort.')

      gerrit_util.DeletePendingChangeEdit(self._GetGerritHost(),
                                          self.GetIssue())
    gerrit_util.SetCommitMessage(self._GetGerritHost(), self.GetIssue(),
                                 description, notify='NONE')

  def CloseIssue(self):
    gerrit_util.AbandonChange(self._GetGerritHost(), self.GetIssue(), msg='')

  def GetApprovingReviewers(self):
    """Returns a list of reviewers approving the change.

    Note: not necessarily committers.
    """
    raise NotImplementedError()

  def SubmitIssue(self, wait_for_merge=True):
    gerrit_util.SubmitChange(self._GetGerritHost(), self.GetIssue(),
                             wait_for_merge=wait_for_merge)

  def _GetChangeDetail(self, options=None, issue=None):
    options = options or []
    issue = issue or self.GetIssue()
    assert issue, 'issue is required to query Gerrit'
    try:
      data = gerrit_util.GetChangeDetail(self._GetGerritHost(), str(issue),
                                         options, ignore_404=False)
    except gerrit_util.GerritError as e:
      if e.http_status == 404:
        raise GerritChangeNotExists(issue, self.GetCodereviewServer())
      raise
    return data

  def _GetChangeCommit(self, issue=None):
    issue = issue or self.GetIssue()
    assert issue, 'issue is required to query Gerrit'
    data = gerrit_util.GetChangeCommit(self._GetGerritHost(), str(issue))
    if not data:
      raise GerritChangeNotExists(issue, self.GetCodereviewServer())
    return data

  def CMDLand(self, force, bypass_hooks, verbose):
    if git_common.is_dirty_git_tree('land'):
      return 1
    detail = self._GetChangeDetail(['CURRENT_REVISION', 'LABELS'])
    if u'Commit-Queue' in detail.get('labels', {}):
      if not force:
        ask_for_data('\nIt seems this repository has a Commit Queue, '
                     'which can test and land changes for you. '
                     'Are you sure you wish to bypass it?\n'
                     'Press Enter to continue, Ctrl+C to abort.')

    differs = True
    last_upload = self._GitGetBranchConfigValue('gerritsquashhash')
    # Note: git diff outputs nothing if there is no diff.
    if not last_upload or RunGit(['diff', last_upload]).strip():
      print('WARNING: some changes from local branch haven\'t been uploaded')
    else:
      if detail['current_revision'] == last_upload:
        differs = False
      else:
        print('WARNING: local branch contents differ from latest uploaded '
              'patchset')
    if differs:
      if not force:
        ask_for_data(
            'Do you want to submit latest Gerrit patchset and bypass hooks?\n'
            'Press Enter to continue, Ctrl+C to abort.')
      print('WARNING: bypassing hooks and submitting latest uploaded patchset')
    elif not bypass_hooks:
      hook_results = self.RunHook(
          committing=True,
          may_prompt=not force,
          verbose=verbose,
          change=self.GetChange(self.GetCommonAncestorWithUpstream(), None))
      if not hook_results.should_continue():
        return 1

    self.SubmitIssue(wait_for_merge=True)
    print('Issue %s has been submitted.' % self.GetIssueURL())
    links = self._GetChangeCommit().get('web_links', [])
    for link in links:
      if link.get('name') == 'gitiles' and link.get('url'):
        print('Landed as %s' % link.get('url'))
        break
    return 0

  def CMDPatchWithParsedIssue(self, parsed_issue_arg, reject, nocommit,
                              directory):
    assert not reject
    assert not nocommit
    assert not directory
    assert parsed_issue_arg.valid

    self._changelist.issue = parsed_issue_arg.issue

    if parsed_issue_arg.hostname:
      self._gerrit_host = parsed_issue_arg.hostname
      self._gerrit_server = 'https://%s' % self._gerrit_host

    try:
      detail = self._GetChangeDetail(['ALL_REVISIONS'])
    except GerritChangeNotExists as e:
      DieWithError(str(e))

    if not parsed_issue_arg.patchset:
      # Use current revision by default.
      revision_info = detail['revisions'][detail['current_revision']]
      patchset = int(revision_info['_number'])
    else:
      patchset = parsed_issue_arg.patchset
      for revision_info in detail['revisions'].itervalues():
        if int(revision_info['_number']) == parsed_issue_arg.patchset:
          break
      else:
        DieWithError('Couldn\'t find patchset %i in change %i' %
                     (parsed_issue_arg.patchset, self.GetIssue()))

    fetch_info = revision_info['fetch']['http']
    RunGit(['fetch', fetch_info['url'], fetch_info['ref']])
    RunGit(['cherry-pick', 'FETCH_HEAD'])
    self.SetIssue(self.GetIssue())
    self.SetPatchset(patchset)
    print('Committed patch for change %i patchset %i locally' %
          (self.GetIssue(), self.GetPatchset()))
    return 0

  @staticmethod
  def ParseIssueURL(parsed_url):
    if not parsed_url.scheme or not parsed_url.scheme.startswith('http'):
      return None
    # Gerrit's new UI is https://domain/c/<issue_number>[/[patchset]]
    # But current GWT UI is https://domain/#/c/<issue_number>[/[patchset]]
    # Short urls like https://domain/<issue_number> can be used, but don't allow
    # specifying the patchset (you'd 404), but we allow that here.
    if parsed_url.path == '/':
      part = parsed_url.fragment
    else:
      part = parsed_url.path
    match = re.match('(/c)?/(\d+)(/(\d+)?/?)?$', part)
    if match:
      return _ParsedIssueNumberArgument(
          issue=int(match.group(2)),
          patchset=int(match.group(4)) if match.group(4) else None,
          hostname=parsed_url.netloc)
    return None

  def _GerritCommitMsgHookCheck(self, offer_removal):
    hook = os.path.join(settings.GetRoot(), '.git', 'hooks', 'commit-msg')
    if not os.path.exists(hook):
      return
    # Crude attempt to distinguish Gerrit Codereview hook from potentially
    # custom developer made one.
    data = gclient_utils.FileRead(hook)
    if not('From Gerrit Code Review' in data and 'add_ChangeId()' in data):
      return
    print('Warning: you have Gerrit commit-msg hook installed.\n'
          'It is not necessary for uploading with git cl in squash mode, '
          'and may interfere with it in subtle ways.\n'
          'We recommend you remove the commit-msg hook.')
    if offer_removal:
      reply = ask_for_data('Do you want to remove it now? [Yes/No]')
      if reply.lower().startswith('y'):
        gclient_utils.rm_file_or_tree(hook)
        print('Gerrit commit-msg hook removed.')
      else:
        print('OK, will keep Gerrit commit-msg hook in place.')

  def CMDUploadChange(self, options, args, change):
    """Upload the current branch to Gerrit."""
    if options.squash and options.no_squash:
      DieWithError('Can only use one of --squash or --no-squash')

    if not options.squash and not options.no_squash:
      # Load default for user, repo, squash=true, in this order.
      options.squash = settings.GetSquashGerritUploads()
    elif options.no_squash:
      options.squash = False

    # We assume the remote called "origin" is the one we want.
    # It is probably not worthwhile to support different workflows.
    gerrit_remote = 'origin'

    remote, remote_branch = self.GetRemoteBranch()
    # Gerrit will not support pending prefix at all.
    branch = GetTargetRef(remote, remote_branch, options.target_branch,
                          pending_prefix_check=False)

    # This may be None; default fallback value is determined in logic below.
    title = options.title

    if options.squash:
      self._GerritCommitMsgHookCheck(offer_removal=not options.force)
      if self.GetIssue():
        # Try to get the message from a previous upload.
        message = self.GetDescription()
        if not message:
          DieWithError(
              'failed to fetch description from current Gerrit change %d\n'
              '%s' % (self.GetIssue(), self.GetIssueURL()))
        if not title:
          default_title = RunGit(['show', '-s', '--format=%s', 'HEAD']).strip()
          title = ask_for_data(
              'Title for patchset [%s]: ' % default_title) or default_title
        change_id = self._GetChangeDetail()['change_id']
        while True:
          footer_change_ids = git_footers.get_footer_change_id(message)
          if footer_change_ids == [change_id]:
            break
          if not footer_change_ids:
            message = git_footers.add_footer_change_id(message, change_id)
            print('WARNING: appended missing Change-Id to change description')
            continue
          # There is already a valid footer but with different or several ids.
          # Doing this automatically is non-trivial as we don't want to lose
          # existing other footers, yet we want to append just 1 desired
          # Change-Id. Thus, just create a new footer, but let user verify the
          # new description.
          message = '%s\n\nChange-Id: %s' % (message, change_id)
          print(
              'WARNING: change %s has Change-Id footer(s):\n'
              '  %s\n'
              'but change has Change-Id %s, according to Gerrit.\n'
              'Please, check the proposed correction to the description, '
              'and edit it if necessary but keep the "Change-Id: %s" footer\n'
              % (self.GetIssue(), '\n  '.join(footer_change_ids), change_id,
                 change_id))
          ask_for_data('Press enter to edit now, Ctrl+C to abort')
          if not options.force:
            change_desc = ChangeDescription(message)
            change_desc.prompt(bug=options.bug)
            message = change_desc.description
            if not message:
              DieWithError("Description is empty. Aborting...")
          # Continue the while loop.
        # Sanity check of this code - we should end up with proper message
        # footer.
        assert [change_id] == git_footers.get_footer_change_id(message)
        change_desc = ChangeDescription(message)
      else:  # if not self.GetIssue()
        if options.message:
          message = options.message
        else:
          message = CreateDescriptionFromLog(args)
          if options.title:
            message = options.title + '\n\n' + message
        change_desc = ChangeDescription(message)
        if not options.force:
          change_desc.prompt(bug=options.bug)
        # On first upload, patchset title is always this string, while
        # --title flag gets converted to first line of message.
        title = 'Initial upload'
        if not change_desc.description:
          DieWithError("Description is empty. Aborting...")
        message = change_desc.description
        change_ids = git_footers.get_footer_change_id(message)
        if len(change_ids) > 1:
          DieWithError('too many Change-Id footers, at most 1 allowed.')
        if not change_ids:
          # Generate the Change-Id automatically.
          message = git_footers.add_footer_change_id(
              message, GenerateGerritChangeId(message))
          change_desc.set_description(message)
          change_ids = git_footers.get_footer_change_id(message)
          assert len(change_ids) == 1
        change_id = change_ids[0]

      remote, upstream_branch = self.FetchUpstreamTuple(self.GetBranch())
      if remote is '.':
        # If our upstream branch is local, we base our squashed commit on its
        # squashed version.
        upstream_branch_name = scm.GIT.ShortBranchName(upstream_branch)
        # Check the squashed hash of the parent.
        parent = RunGit(['config',
                         'branch.%s.gerritsquashhash' % upstream_branch_name],
                        error_ok=True).strip()
        # Verify that the upstream branch has been uploaded too, otherwise
        # Gerrit will create additional CLs when uploading.
        if not parent or (RunGitSilent(['rev-parse', upstream_branch + ':']) !=
                          RunGitSilent(['rev-parse', parent + ':'])):
          DieWithError(
              '\nUpload upstream branch %s first.\n'
              'It is likely that this branch has been rebased since its last '
              'upload, so you just need to upload it again.\n'
              '(If you uploaded it with --no-squash, then branch dependencies '
              'are not supported, and you should reupload with --squash.)'
              % upstream_branch_name)
      else:
        parent = self.GetCommonAncestorWithUpstream()

      tree = RunGit(['rev-parse', 'HEAD:']).strip()
      ref_to_push = RunGit(['commit-tree', tree, '-p', parent,
                            '-m', message]).strip()
    else:
      change_desc = ChangeDescription(
          options.message or CreateDescriptionFromLog(args))
      if not change_desc.description:
        DieWithError("Description is empty. Aborting...")

      if not git_footers.get_footer_change_id(change_desc.description):
        DownloadGerritHook(False)
        change_desc.set_description(self._AddChangeIdToCommitMessage(options,
                                                                     args))
      ref_to_push = 'HEAD'
      parent = '%s/%s' % (gerrit_remote, branch)
      change_id = git_footers.get_footer_change_id(change_desc.description)[0]

    assert change_desc
    commits = RunGitSilent(['rev-list', '%s..%s' % (parent,
                                                    ref_to_push)]).splitlines()
    if len(commits) > 1:
      print('WARNING: This will upload %d commits. Run the following command '
            'to see which commits will be uploaded: ' % len(commits))
      print('git log %s..%s' % (parent, ref_to_push))
      print('You can also use `git squash-branch` to squash these into a '
            'single commit.')
      ask_for_data('About to upload; enter to confirm.')

    if options.reviewers or options.tbr_owners:
      change_desc.update_reviewers(options.reviewers, options.tbr_owners,
                                   change)

    # Extra options that can be specified at push time. Doc:
    # https://gerrit-review.googlesource.com/Documentation/user-upload.html
    refspec_opts = []
    if change_desc.get_reviewers(tbr_only=True):
      print('Adding self-LGTM (Code-Review +1) because of TBRs')
      refspec_opts.append('l=Code-Review+1')

    if title:
      if not re.match(r'^[\w ]+$', title):
        title = re.sub(r'[^\w ]', '', title)
        print('WARNING: Patchset title may only contain alphanumeric chars '
              'and spaces. Cleaned up title:\n%s' % title)
        if not options.force:
          ask_for_data('Press enter to continue, Ctrl+C to abort')
      # Per doc, spaces must be converted to underscores, and Gerrit will do the
      # reverse on its side.
      refspec_opts.append('m=' + title.replace(' ', '_'))

    if options.send_mail:
      if not change_desc.get_reviewers():
        DieWithError('Must specify reviewers to send email.')
      refspec_opts.append('notify=ALL')
    else:
      refspec_opts.append('notify=NONE')

    reviewers = change_desc.get_reviewers()
    if reviewers:
      refspec_opts.extend('r=' + email.strip() for email in reviewers)

    if options.private:
      refspec_opts.append('draft')

    if options.topic:
      # Documentation on Gerrit topics is here:
      # https://gerrit-review.googlesource.com/Documentation/user-upload.html#topic
      refspec_opts.append('topic=%s' % options.topic)

    refspec_suffix = ''
    if refspec_opts:
      refspec_suffix = '%' + ','.join(refspec_opts)
      assert ' ' not in refspec_suffix, (
          'spaces not allowed in refspec: "%s"' % refspec_suffix)
    refspec = '%s:refs/for/%s%s' % (ref_to_push, branch, refspec_suffix)

    try:
      push_stdout = gclient_utils.CheckCallAndFilter(
          ['git', 'push', gerrit_remote, refspec],
          print_stdout=True,
          # Flush after every line: useful for seeing progress when running as
          # recipe.
          filter_fn=lambda _: sys.stdout.flush())
    except subprocess2.CalledProcessError:
      DieWithError('Failed to create a change. Please examine output above '
                   'for the reason of the failure. ')

    if options.squash:
      regex = re.compile(r'remote:\s+https?://[\w\-\.\/]*/(\d+)\s.*')
      change_numbers = [m.group(1)
                        for m in map(regex.match, push_stdout.splitlines())
                        if m]
      if len(change_numbers) != 1:
        DieWithError(
          ('Created|Updated %d issues on Gerrit, but only 1 expected.\n'
           'Change-Id: %s') % (len(change_numbers), change_id))
      self.SetIssue(change_numbers[0])
      self._GitSetBranchConfigValue('gerritsquashhash', ref_to_push)

    # Add cc's from the CC_LIST and --cc flag (if any).
    cc = self.GetCCList().split(',')
    if options.cc:
      cc.extend(options.cc)
    cc = filter(None, [email.strip() for email in cc])
    if change_desc.get_cced():
      cc.extend(change_desc.get_cced())
    if cc:
      gerrit_util.AddReviewers(
          self._GetGerritHost(), self.GetIssue(), cc,
          is_reviewer=False, notify=bool(options.send_mail))
    return 0

  def _AddChangeIdToCommitMessage(self, options, args):
    """Re-commits using the current message, assumes the commit hook is in
    place.
    """
    log_desc = options.message or CreateDescriptionFromLog(args)
    git_command = ['commit', '--amend', '-m', log_desc]
    RunGit(git_command)
    new_log_desc = CreateDescriptionFromLog(args)
    if git_footers.get_footer_change_id(new_log_desc):
      print('git-cl: Added Change-Id to commit message.')
      return new_log_desc
    else:
      DieWithError('ERROR: Gerrit commit-msg hook not installed.')

  def SetCQState(self, new_state):
    """Sets the Commit-Queue label assuming canonical CQ config for Gerrit."""
    vote_map = {
        _CQState.NONE:    0,
        _CQState.DRY_RUN: 1,
        _CQState.COMMIT : 2,
    }
    kwargs = {'labels': {'Commit-Queue': vote_map[new_state]}}
    if new_state == _CQState.DRY_RUN:
      # Don't spam everybody reviewer/owner.
      kwargs['notify'] = 'NONE'
    gerrit_util.SetReview(self._GetGerritHost(), self.GetIssue(), **kwargs)

  def CannotTriggerTryJobReason(self):
    try:
      data = self._GetChangeDetail()
    except GerritChangeNotExists:
      return 'Gerrit doesn\'t know about your change %s' % self.GetIssue()

    if data['status'] in ('ABANDONED', 'MERGED'):
      return 'CL %s is closed' % self.GetIssue()

  def GetTryjobProperties(self, patchset=None):
    """Returns dictionary of properties to launch tryjob."""
    data = self._GetChangeDetail(['ALL_REVISIONS'])
    patchset = int(patchset or self.GetPatchset())
    assert patchset
    revision_data = None  # Pylint wants it to be defined.
    for revision_data in data['revisions'].itervalues():
      if int(revision_data['_number']) == patchset:
        break
    else:
      raise Exception('Patchset %d is not known in Gerrit change %d' %
                      (patchset, self.GetIssue()))
    return {
      'patch_issue': self.GetIssue(),
      'patch_set': patchset or self.GetPatchset(),
      'patch_project': data['project'],
      'patch_storage': 'gerrit',
      'patch_ref': revision_data['fetch']['http']['ref'],
      'patch_repository_url': revision_data['fetch']['http']['url'],
      'patch_gerrit_url': self.GetCodereviewServer(),
    }

  def GetIssueOwner(self):
    return self._GetChangeDetail(['DETAILED_ACCOUNTS'])['owner']['email']


_CODEREVIEW_IMPLEMENTATIONS = {
  'rietveld': _RietveldChangelistImpl,
  'gerrit': _GerritChangelistImpl,
}


def _add_codereview_issue_select_options(parser, extra=""):
  _add_codereview_select_options(parser)

  text = ('Operate on this issue number instead of the current branch\'s '
          'implicit issue.')
  if extra:
    text += ' '+extra
  parser.add_option('-i', '--issue', type=int, help=text)


def _process_codereview_issue_select_options(parser, options):
  _process_codereview_select_options(parser, options)
  if options.issue is not None and not options.forced_codereview:
    parser.error('--issue must be specified with either --rietveld or --gerrit')


def _add_codereview_select_options(parser):
  """Appends --gerrit and --rietveld options to force specific codereview."""
  parser.codereview_group = optparse.OptionGroup(
      parser, 'EXPERIMENTAL! Codereview override options')
  parser.add_option_group(parser.codereview_group)
  parser.codereview_group.add_option(
      '--gerrit', action='store_true',
      help='Force the use of Gerrit for codereview')
  parser.codereview_group.add_option(
      '--rietveld', action='store_true',
      help='Force the use of Rietveld for codereview')


def _process_codereview_select_options(parser, options):
  if options.gerrit and options.rietveld:
    parser.error('Options --gerrit and --rietveld are mutually exclusive')
  options.forced_codereview = None
  if options.gerrit:
    options.forced_codereview = 'gerrit'
  elif options.rietveld:
    options.forced_codereview = 'rietveld'


def _get_bug_line_values(default_project, bugs):
  """Given default_project and comma separated list of bugs, yields bug line
  values.

  Each bug can be either:
    * a number, which is combined with default_project
    * string, which is left as is.

  This function may produce more than one line, because bugdroid expects one
  project per line.

  >>> list(_get_bug_line_values('v8', '123,chromium:789'))
      ['v8:123', 'chromium:789']
  """
  default_bugs = []
  others = []
  for bug in bugs.split(','):
    bug = bug.strip()
    if bug:
      try:
        default_bugs.append(int(bug))
      except ValueError:
        others.append(bug)

  if default_bugs:
    default_bugs = ','.join(map(str, default_bugs))
    if default_project:
      yield '%s:%s' % (default_project, default_bugs)
    else:
      yield default_bugs
  for other in sorted(others):
    # Don't bother finding common prefixes, CLs with >2 bugs are very very rare.
    yield other


class ChangeDescription(object):
  """Contains a parsed form of the change description."""
  R_LINE = r'^[ \t]*(TBR|R)[ \t]*=[ \t]*(.*?)[ \t]*$'
  CC_LINE = r'^[ \t]*(CC)[ \t]*=[ \t]*(.*?)[ \t]*$'
  BUG_LINE = r'^[ \t]*(BUG)[ \t]*=[ \t]*(.*?)[ \t]*$'
  CHERRY_PICK_LINE = r'^\(cherry picked from commit [a-fA-F0-9]{40}\)$'

  def __init__(self, description):
    self._description_lines = (description or '').strip().splitlines()

  @property               # www.logilab.org/ticket/89786
  def description(self):  # pylint: disable=method-hidden
    return '\n'.join(self._description_lines)

  def set_description(self, desc):
    if isinstance(desc, basestring):
      lines = desc.splitlines()
    else:
      lines = [line.rstrip() for line in desc]
    while lines and not lines[0]:
      lines.pop(0)
    while lines and not lines[-1]:
      lines.pop(-1)
    self._description_lines = lines

  def update_reviewers(self, reviewers, add_owners_tbr=False, change=None):
    """Rewrites the R=/TBR= line(s) as a single line each."""
    assert isinstance(reviewers, list), reviewers
    if not reviewers and not add_owners_tbr:
      return
    reviewers = reviewers[:]

    # Get the set of R= and TBR= lines and remove them from the desciption.
    regexp = re.compile(self.R_LINE)
    matches = [regexp.match(line) for line in self._description_lines]
    new_desc = [l for i, l in enumerate(self._description_lines)
                if not matches[i]]
    self.set_description(new_desc)

    # Construct new unified R= and TBR= lines.
    r_names = []
    tbr_names = []
    for match in matches:
      if not match:
        continue
      people = cleanup_list([match.group(2).strip()])
      if match.group(1) == 'TBR':
        tbr_names.extend(people)
      else:
        r_names.extend(people)
    for name in r_names:
      if name not in reviewers:
        reviewers.append(name)
    if add_owners_tbr:
      owners_db = owners.Database(change.RepositoryRoot(),
        fopen=file, os_path=os.path)
      all_reviewers = set(tbr_names + reviewers)
      missing_files = owners_db.files_not_covered_by(change.LocalPaths(),
                                                     all_reviewers)
      tbr_names.extend(owners_db.reviewers_for(missing_files,
                                               change.author_email))
    new_r_line = 'R=' + ', '.join(reviewers) if reviewers else None
    new_tbr_line = 'TBR=' + ', '.join(tbr_names) if tbr_names else None

    # Put the new lines in the description where the old first R= line was.
    line_loc = next((i for i, match in enumerate(matches) if match), -1)
    if 0 <= line_loc < len(self._description_lines):
      if new_tbr_line:
        self._description_lines.insert(line_loc, new_tbr_line)
      if new_r_line:
        self._description_lines.insert(line_loc, new_r_line)
    else:
      if new_r_line:
        self.append_footer(new_r_line)
      if new_tbr_line:
        self.append_footer(new_tbr_line)

  def prompt(self, bug=None):
    """Asks the user to update the description."""
    self.set_description([
      '# Enter a description of the change.',
      '# This will be displayed on the codereview site.',
      '# The first line will also be used as the subject of the review.',
      '#--------------------This line is 72 characters long'
      '--------------------',
    ] + self._description_lines)

    regexp = re.compile(self.BUG_LINE)
    if not any((regexp.match(line) for line in self._description_lines)):
      prefix = settings.GetBugPrefix()
      values = list(_get_bug_line_values(prefix, bug or '')) or [prefix]
      for value in values:
        # TODO(tandrii): change this to 'Bug: xxx' to be a proper Gerrit footer.
        self.append_footer('BUG=%s' % value)

    content = gclient_utils.RunEditor(self.description, True,
                                      git_editor=settings.GetGitEditor())
    if not content:
      DieWithError('Running editor failed')
    lines = content.splitlines()

    # Strip off comments.
    clean_lines = [line.rstrip() for line in lines if not line.startswith('#')]
    if not clean_lines:
      DieWithError('No CL description, aborting')
    self.set_description(clean_lines)

  def append_footer(self, line):
    """Adds a footer line to the description.

    Differentiates legacy "KEY=xxx" footers (used to be called tags) and
    Gerrit's footers in the form of "Footer-Key: footer any value" and ensures
    that Gerrit footers are always at the end.
    """
    parsed_footer_line = git_footers.parse_footer(line)
    if parsed_footer_line:
      # Line is a gerrit footer in the form: Footer-Key: any value.
      # Thus, must be appended observing Gerrit footer rules.
      self.set_description(
          git_footers.add_footer(self.description,
                                 key=parsed_footer_line[0],
                                 value=parsed_footer_line[1]))
      return

    if not self._description_lines:
      self._description_lines.append(line)
      return

    top_lines, gerrit_footers, _ = git_footers.split_footers(self.description)
    if gerrit_footers:
      # git_footers.split_footers ensures that there is an empty line before
      # actual (gerrit) footers, if any. We have to keep it that way.
      assert top_lines and top_lines[-1] == ''
      top_lines, separator = top_lines[:-1], top_lines[-1:]
    else:
      separator = []  # No need for separator if there are no gerrit_footers.

    prev_line = top_lines[-1] if top_lines else ''
    if (not presubmit_support.Change.TAG_LINE_RE.match(prev_line) or
        not presubmit_support.Change.TAG_LINE_RE.match(line)):
      top_lines.append('')
    top_lines.append(line)
    self._description_lines = top_lines + separator + gerrit_footers

  def get_reviewers(self, tbr_only=False):
    """Retrieves the list of reviewers."""
    matches = [re.match(self.R_LINE, line) for line in self._description_lines]
    reviewers = [match.group(2).strip()
                 for match in matches
                 if match and (not tbr_only or match.group(1).upper() == 'TBR')]
    return cleanup_list(reviewers)

  def get_cced(self):
    """Retrieves the list of reviewers."""
    matches = [re.match(self.CC_LINE, line) for line in self._description_lines]
    cced = [match.group(2).strip() for match in matches if match]
    return cleanup_list(cced)

  def update_with_git_number_footers(self, parent_hash, parent_msg, dest_ref):
    """Updates this commit description given the parent.

    This is essentially what Gnumbd used to do.
    Consult https://goo.gl/WMmpDe for more details.
    """
    assert parent_msg  # No, orphan branch creation isn't supported.
    assert parent_hash
    assert dest_ref
    parent_footer_map = git_footers.parse_footers(parent_msg)
    # This will also happily parse svn-position, which GnumbD is no longer
    # supporting. While we'd generate correct footers, the verifier plugin
    # installed in Gerrit will block such commit (ie git push below will fail).
    parent_position = git_footers.get_position(parent_footer_map)

    # Cherry-picks may have last line obscuring their prior footers,
    # from git_footers perspective. This is also what Gnumbd did.
    cp_line = None
    if (self._description_lines and
        re.match(self.CHERRY_PICK_LINE, self._description_lines[-1])):
      cp_line = self._description_lines.pop()

    top_lines, _, parsed_footers = git_footers.split_footers(self.description)

    # Original-ify all Cr- footers, to avoid re-lands, cherry-picks, or just
    # user interference with actual footers we'd insert below.
    for i, (k, v) in enumerate(parsed_footers):
      if k.startswith('Cr-'):
        parsed_footers[i] = (k.replace('Cr-', 'Cr-Original-'), v)

    # Add Position and Lineage footers based on the parent.
    lineage = list(reversed(parent_footer_map.get('Cr-Branched-From', [])))
    if parent_position[0] == dest_ref:
      # Same branch as parent.
      number = int(parent_position[1]) + 1
    else:
      number = 1  # New branch, and extra lineage.
      lineage.insert(0, '%s-%s@{#%d}' % (parent_hash, parent_position[0],
                                         int(parent_position[1])))

    parsed_footers.append(('Cr-Commit-Position',
                           '%s@{#%d}' % (dest_ref, number)))
    parsed_footers.extend(('Cr-Branched-From', v) for v in lineage)

    self._description_lines = top_lines
    if cp_line:
      self._description_lines.append(cp_line)
    if self._description_lines[-1] != '':
      self._description_lines.append('')  # Ensure footer separator.
    self._description_lines.extend('%s: %s' % kv for kv in parsed_footers)


def get_approving_reviewers(props):
  """Retrieves the reviewers that approved a CL from the issue properties with
  messages.

  Note that the list may contain reviewers that are not committer, thus are not
  considered by the CQ.
  """
  return sorted(
      set(
        message['sender']
        for message in props['messages']
        if message['approval'] and message['sender'] in props['reviewers']
      )
  )


def FindCodereviewSettingsFile(filename='codereview.settings'):
  """Finds the given file starting in the cwd and going up.

  Only looks up to the top of the repository unless an
  'inherit-review-settings-ok' file exists in the root of the repository.
  """
  inherit_ok_file = 'inherit-review-settings-ok'
  cwd = os.getcwd()
  root = settings.GetRoot()
  if os.path.isfile(os.path.join(root, inherit_ok_file)):
    root = '/'
  while True:
    if filename in os.listdir(cwd):
      if os.path.isfile(os.path.join(cwd, filename)):
        return open(os.path.join(cwd, filename))
    if cwd == root:
      break
    cwd = os.path.dirname(cwd)


def LoadCodereviewSettingsFromFile(fileobj):
  """Parse a codereview.settings file and updates hooks."""
  keyvals = gclient_utils.ParseCodereviewSettingsContent(fileobj.read())

  def SetProperty(name, setting, unset_error_ok=False):
    fullname = 'rietveld.' + name
    if setting in keyvals:
      RunGit(['config', fullname, keyvals[setting]])
    else:
      RunGit(['config', '--unset-all', fullname], error_ok=unset_error_ok)

  if not keyvals.get('GERRIT_HOST', False):
    SetProperty('server', 'CODE_REVIEW_SERVER')
  # Only server setting is required. Other settings can be absent.
  # In that case, we ignore errors raised during option deletion attempt.
  SetProperty('cc', 'CC_LIST', unset_error_ok=True)
  SetProperty('private', 'PRIVATE', unset_error_ok=True)
  SetProperty('tree-status-url', 'STATUS', unset_error_ok=True)
  SetProperty('viewvc-url', 'VIEW_VC', unset_error_ok=True)
  SetProperty('bug-prefix', 'BUG_PREFIX', unset_error_ok=True)
  SetProperty('cpplint-regex', 'LINT_REGEX', unset_error_ok=True)
  SetProperty('force-https-commit-url', 'FORCE_HTTPS_COMMIT_URL',
              unset_error_ok=True)
  SetProperty('cpplint-ignore-regex', 'LINT_IGNORE_REGEX', unset_error_ok=True)
  SetProperty('project', 'PROJECT', unset_error_ok=True)
  SetProperty('pending-ref-prefix', 'PENDING_REF_PREFIX', unset_error_ok=True)
  SetProperty('run-post-upload-hook', 'RUN_POST_UPLOAD_HOOK',
              unset_error_ok=True)

  if 'GERRIT_HOST' in keyvals:
    RunGit(['config', 'gerrit.host', keyvals['GERRIT_HOST']])

  if 'GERRIT_SQUASH_UPLOADS' in keyvals:
    RunGit(['config', 'gerrit.squash-uploads',
            keyvals['GERRIT_SQUASH_UPLOADS']])

  if 'GERRIT_SKIP_ENSURE_AUTHENTICATED' in keyvals:
    RunGit(['config', 'gerrit.skip-ensure-authenticated',
            keyvals['GERRIT_SKIP_ENSURE_AUTHENTICATED']])

  if 'PUSH_URL_CONFIG' in keyvals and 'ORIGIN_URL_CONFIG' in keyvals:
    #should be of the form
    #PUSH_URL_CONFIG: url.ssh://gitrw.chromium.org.pushinsteadof
    #ORIGIN_URL_CONFIG: http://src.chromium.org/git
    RunGit(['config', keyvals['PUSH_URL_CONFIG'],
            keyvals['ORIGIN_URL_CONFIG']])


def urlretrieve(source, destination):
  """urllib is broken for SSL connections via a proxy therefore we
  can't use urllib.urlretrieve()."""
  with open(destination, 'w') as f:
    f.write(urllib2.urlopen(source).read())


def hasSheBang(fname):
  """Checks fname is a #! script."""
  with open(fname) as f:
    return f.read(2).startswith('#!')


# TODO(bpastene) Remove once a cleaner fix to crbug.com/600473 presents itself.
def DownloadHooks(*args, **kwargs):
  pass


def DownloadGerritHook(force):
  """Download and install Gerrit commit-msg hook.

  Args:
    force: True to update hooks. False to install hooks if not present.
  """
  if not settings.GetIsGerrit():
    return
  src = 'https://gerrit-review.googlesource.com/tools/hooks/commit-msg'
  dst = os.path.join(settings.GetRoot(), '.git', 'hooks', 'commit-msg')
  if not os.access(dst, os.X_OK):
    if os.path.exists(dst):
      if not force:
        return
    try:
      urlretrieve(src, dst)
      if not hasSheBang(dst):
        DieWithError('Not a script: %s\n'
                     'You need to download from\n%s\n'
                     'into .git/hooks/commit-msg and '
                     'chmod +x .git/hooks/commit-msg' % (dst, src))
      os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except Exception:
      if os.path.exists(dst):
        os.remove(dst)
      DieWithError('\nFailed to download hooks.\n'
                   'You need to download from\n%s\n'
                   'into .git/hooks/commit-msg and '
                   'chmod +x .git/hooks/commit-msg' % src)



def GetRietveldCodereviewSettingsInteractively():
  """Prompt the user for settings."""
  server = settings.GetDefaultServerUrl(error_ok=True)
  prompt = 'Rietveld server (host[:port])'
  prompt += ' [%s]' % (server or DEFAULT_SERVER)
  newserver = ask_for_data(prompt + ':')
  if not server and not newserver:
    newserver = DEFAULT_SERVER
  if newserver:
    newserver = gclient_utils.UpgradeToHttps(newserver)
    if newserver != server:
      RunGit(['config', 'rietveld.server', newserver])

  def SetProperty(initial, caption, name, is_url):
    prompt = caption
    if initial:
      prompt += ' ("x" to clear) [%s]' % initial
    new_val = ask_for_data(prompt + ':')
    if new_val == 'x':
      RunGit(['config', '--unset-all', 'rietveld.' + name], error_ok=True)
    elif new_val:
      if is_url:
        new_val = gclient_utils.UpgradeToHttps(new_val)
      if new_val != initial:
        RunGit(['config', 'rietveld.' + name, new_val])

  SetProperty(settings.GetDefaultCCList(), 'CC list', 'cc', False)
  SetProperty(settings.GetDefaultPrivateFlag(),
              'Private flag (rietveld only)', 'private', False)
  SetProperty(settings.GetTreeStatusUrl(error_ok=True), 'Tree status URL',
              'tree-status-url', False)
  SetProperty(settings.GetViewVCUrl(), 'ViewVC URL', 'viewvc-url', True)
  SetProperty(settings.GetBugPrefix(), 'Bug Prefix', 'bug-prefix', False)
  SetProperty(settings.GetRunPostUploadHook(), 'Run Post Upload Hook',
              'run-post-upload-hook', False)

@subcommand.usage('[repo root containing codereview.settings]')
def CMDconfig(parser, args):
  """Edits configuration for this tree."""

  print('WARNING: git cl config works for Rietveld only')
  # TODO(tandrii): remove this once we switch to Gerrit.
  # See bugs http://crbug.com/637561 and http://crbug.com/600469.
  parser.add_option('--activate-update', action='store_true',
                    help='activate auto-updating [rietveld] section in '
                         '.git/config')
  parser.add_option('--deactivate-update', action='store_true',
                    help='deactivate auto-updating [rietveld] section in '
                         '.git/config')
  options, args = parser.parse_args(args)

  if options.deactivate_update:
    RunGit(['config', 'rietveld.autoupdate', 'false'])
    return

  if options.activate_update:
    RunGit(['config', '--unset', 'rietveld.autoupdate'])
    return

  if len(args) == 0:
    GetRietveldCodereviewSettingsInteractively()
    return 0

  url = args[0]
  if not url.endswith('codereview.settings'):
    url = os.path.join(url, 'codereview.settings')

  # Load code review settings and download hooks (if available).
  LoadCodereviewSettingsFromFile(urllib2.urlopen(url))
  return 0


def CMDbaseurl(parser, args):
  """Gets or sets base-url for this branch."""
  branchref = RunGit(['symbolic-ref', 'HEAD']).strip()
  branch = ShortBranchName(branchref)
  _, args = parser.parse_args(args)
  if not args:
    print('Current base-url:')
    return RunGit(['config', 'branch.%s.base-url' % branch],
                  error_ok=False).strip()
  else:
    print('Setting base-url to %s' % args[0])
    return RunGit(['config', 'branch.%s.base-url' % branch, args[0]],
                  error_ok=False).strip()


def color_for_status(status):
  """Maps a Changelist status to color, for CMDstatus and other tools."""
  return {
    'unsent': Fore.RED,
    'waiting': Fore.BLUE,
    'reply': Fore.YELLOW,
    'lgtm': Fore.GREEN,
    'commit': Fore.MAGENTA,
    'closed': Fore.CYAN,
    'error': Fore.WHITE,
  }.get(status, Fore.WHITE)


def get_cl_statuses(changes, fine_grained, max_processes=None):
  """Returns a blocking iterable of (cl, status) for given branches.

  If fine_grained is true, this will fetch CL statuses from the server.
  Otherwise, simply indicate if there's a matching url for the given branches.

  If max_processes is specified, it is used as the maximum number of processes
  to spawn to fetch CL status from the server. Otherwise 1 process per branch is
  spawned.

  See GetStatus() for a list of possible statuses.
  """
  # Silence upload.py otherwise it becomes unwieldy.
  upload.verbosity = 0

  if fine_grained:
    # Process one branch synchronously to work through authentication, then
    # spawn processes to process all the other branches in parallel.
    if changes:
      def fetch(cl):
        try:
          return (cl, cl.GetStatus())
        except:
          # See http://crbug.com/629863.
          logging.exception('failed to fetch status for %s:', cl)
          raise
      yield fetch(changes[0])

      changes_to_fetch = changes[1:]
      if not changes_to_fetch:
        # Exit early if there was only one branch to fetch.
        return

      pool = ThreadPool(
          min(max_processes, len(changes_to_fetch))
              if max_processes is not None
              else max(len(changes_to_fetch), 1))

      fetched_cls = set()
      it = pool.imap_unordered(fetch, changes_to_fetch).__iter__()
      while True:
        try:
          row = it.next(timeout=5)
        except multiprocessing.TimeoutError:
          break

        fetched_cls.add(row[0])
        yield row

      # Add any branches that failed to fetch.
      for cl in set(changes_to_fetch) - fetched_cls:
        yield (cl, 'error')

  else:
    # Do not use GetApprovingReviewers(), since it requires an HTTP request.
    for cl in changes:
      yield (cl, 'waiting' if cl.GetIssueURL() else 'error')


def upload_branch_deps(cl, args):
  """Uploads CLs of local branches that are dependents of the current branch.

  If the local branch dependency tree looks like:
  test1 -> test2.1 -> test3.1
                   -> test3.2
        -> test2.2 -> test3.3

  and you run "git cl upload --dependencies" from test1 then "git cl upload" is
  run on the dependent branches in this order:
  test2.1, test3.1, test3.2, test2.2, test3.3

  Note: This function does not rebase your local dependent branches. Use it when
        you make a change to the parent branch that will not conflict with its
        dependent branches, and you would like their dependencies updated in
        Rietveld.
  """
  if git_common.is_dirty_git_tree('upload-branch-deps'):
    return 1

  root_branch = cl.GetBranch()
  if root_branch is None:
    DieWithError('Can\'t find dependent branches from detached HEAD state. '
                 'Get on a branch!')
  if not cl.GetIssue() or not cl.GetPatchset():
    DieWithError('Current branch does not have an uploaded CL. We cannot set '
                 'patchset dependencies without an uploaded CL.')

  branches = RunGit(['for-each-ref',
                     '--format=%(refname:short) %(upstream:short)',
                     'refs/heads'])
  if not branches:
    print('No local branches found.')
    return 0

  # Create a dictionary of all local branches to the branches that are dependent
  # on it.
  tracked_to_dependents = collections.defaultdict(list)
  for b in branches.splitlines():
    tokens = b.split()
    if len(tokens) == 2:
      branch_name, tracked = tokens
      tracked_to_dependents[tracked].append(branch_name)

  print()
  print('The dependent local branches of %s are:' % root_branch)
  dependents = []
  def traverse_dependents_preorder(branch, padding=''):
    dependents_to_process = tracked_to_dependents.get(branch, [])
    padding += '  '
    for dependent in dependents_to_process:
      print('%s%s' % (padding, dependent))
      dependents.append(dependent)
      traverse_dependents_preorder(dependent, padding)
  traverse_dependents_preorder(root_branch)
  print()

  if not dependents:
    print('There are no dependent local branches for %s' % root_branch)
    return 0

  print('This command will checkout all dependent branches and run '
        '"git cl upload".')
  ask_for_data('[Press enter to continue or ctrl-C to quit]')

  # Add a default patchset title to all upload calls in Rietveld.
  if not cl.IsGerrit():
    args.extend(['-t', 'Updated patchset dependency'])

  # Record all dependents that failed to upload.
  failures = {}
  # Go through all dependents, checkout the branch and upload.
  try:
    for dependent_branch in dependents:
      print()
      print('--------------------------------------')
      print('Running "git cl upload" from %s:' % dependent_branch)
      RunGit(['checkout', '-q', dependent_branch])
      print()
      try:
        if CMDupload(OptionParser(), args) != 0:
          print('Upload failed for %s!' % dependent_branch)
          failures[dependent_branch] = 1
      except:  # pylint: disable=bare-except
        failures[dependent_branch] = 1
      print()
  finally:
    # Swap back to the original root branch.
    RunGit(['checkout', '-q', root_branch])

  print()
  print('Upload complete for dependent branches!')
  for dependent_branch in dependents:
    upload_status = 'failed' if failures.get(dependent_branch) else 'succeeded'
    print('  %s : %s' % (dependent_branch, upload_status))
  print()

  return 0


def CMDarchive(parser, args):
  """Archives and deletes branches associated with closed changelists."""
  parser.add_option(
      '-j', '--maxjobs', action='store', type=int,
      help='The maximum number of jobs to use when retrieving review status.')
  parser.add_option(
      '-f', '--force', action='store_true',
      help='Bypasses the confirmation prompt.')
  parser.add_option(
      '-d', '--dry-run', action='store_true',
      help='Skip the branch tagging and removal steps.')
  parser.add_option(
      '-t', '--notags', action='store_true',
      help='Do not tag archived branches. '
           'Note: local commit history may be lost.')

  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  if args:
    parser.error('Unsupported args: %s' % ' '.join(args))
  auth_config = auth.extract_auth_config_from_options(options)

  branches = RunGit(['for-each-ref', '--format=%(refname)', 'refs/heads'])
  if not branches:
    return 0

  print('Finding all branches associated with closed issues...')
  changes = [Changelist(branchref=b, auth_config=auth_config)
              for b in branches.splitlines()]
  alignment = max(5, max(len(c.GetBranch()) for c in changes))
  statuses = get_cl_statuses(changes,
                             fine_grained=True,
                             max_processes=options.maxjobs)
  proposal = [(cl.GetBranch(),
               'git-cl-archived-%s-%s' % (cl.GetIssue(), cl.GetBranch()))
              for cl, status in statuses
              if status == 'closed']
  proposal.sort()

  if not proposal:
    print('No branches with closed codereview issues found.')
    return 0

  current_branch = GetCurrentBranch()

  print('\nBranches with closed issues that will be archived:\n')
  if options.notags:
    for next_item in proposal:
      print('  ' + next_item[0])
  else:
    print('%*s | %s' % (alignment, 'Branch name', 'Archival tag name'))
    for next_item in proposal:
      print('%*s   %s' % (alignment, next_item[0], next_item[1]))

  # Quit now on precondition failure or if instructed by the user, either
  # via an interactive prompt or by command line flags.
  if options.dry_run:
    print('\nNo changes were made (dry run).\n')
    return 0
  elif any(branch == current_branch for branch, _ in proposal):
    print('You are currently on a branch \'%s\' which is associated with a '
          'closed codereview issue, so archive cannot proceed. Please '
          'checkout another branch and run this command again.' %
          current_branch)
    return 1
  elif not options.force:
    answer = ask_for_data('\nProceed with deletion (Y/n)? ').lower()
    if answer not in ('y', ''):
      print('Aborted.')
      return 1

  for branch, tagname in proposal:
    if not options.notags:
      RunGit(['tag', tagname, branch])
    RunGit(['branch', '-D', branch])

  print('\nJob\'s done!')

  return 0


def CMDstatus(parser, args):
  """Show status of changelists.

  Colors are used to tell the state of the CL unless --fast is used:
    - Red      not sent for review or broken
    - Blue     waiting for review
    - Yellow   waiting for you to reply to review
    - Green    LGTM'ed
    - Magenta  in the commit queue
    - Cyan     was committed, branch can be deleted

  Also see 'git cl comments'.
  """
  parser.add_option('--field',
                    help='print only specific field (desc|id|patch|status|url)')
  parser.add_option('-f', '--fast', action='store_true',
                    help='Do not retrieve review status')
  parser.add_option(
      '-j', '--maxjobs', action='store', type=int,
      help='The maximum number of jobs to use when retrieving review status')

  auth.add_auth_options(parser)
  _add_codereview_issue_select_options(
    parser, 'Must be in conjunction with --field.')
  options, args = parser.parse_args(args)
  _process_codereview_issue_select_options(parser, options)
  if args:
    parser.error('Unsupported args: %s' % args)
  auth_config = auth.extract_auth_config_from_options(options)

  if options.issue is not None and not options.field:
    parser.error('--field must be specified with --issue')

  if options.field:
    cl = Changelist(auth_config=auth_config, issue=options.issue,
                    codereview=options.forced_codereview)
    if options.field.startswith('desc'):
      print(cl.GetDescription())
    elif options.field == 'id':
      issueid = cl.GetIssue()
      if issueid:
        print(issueid)
    elif options.field == 'patch':
      patchset = cl.GetPatchset()
      if patchset:
        print(patchset)
    elif options.field == 'status':
      print(cl.GetStatus())
    elif options.field == 'url':
      url = cl.GetIssueURL()
      if url:
        print(url)
    return 0

  branches = RunGit(['for-each-ref', '--format=%(refname)', 'refs/heads'])
  if not branches:
    print('No local branch found.')
    return 0

  changes = [
      Changelist(branchref=b, auth_config=auth_config)
      for b in branches.splitlines()]
  print('Branches associated with reviews:')
  output = get_cl_statuses(changes,
                           fine_grained=not options.fast,
                           max_processes=options.maxjobs)

  branch_statuses = {}
  alignment = max(5, max(len(ShortBranchName(c.GetBranch())) for c in changes))
  for cl in sorted(changes, key=lambda c: c.GetBranch()):
    branch = cl.GetBranch()
    while branch not in branch_statuses:
      c, status = output.next()
      branch_statuses[c.GetBranch()] = status
    status = branch_statuses.pop(branch)
    url = cl.GetIssueURL()
    if url and (not status or status == 'error'):
      # The issue probably doesn't exist anymore.
      url += ' (broken)'

    color = color_for_status(status)
    reset = Fore.RESET
    if not setup_color.IS_TTY:
      color = ''
      reset = ''
    status_str = '(%s)' % status if status else ''
    print('  %*s : %s%s %s%s' % (
          alignment, ShortBranchName(branch), color, url,
          status_str, reset))

  cl = Changelist(auth_config=auth_config)
  print()
  print('Current branch:',)
  print(cl.GetBranch())
  if not cl.GetIssue():
    print('No issue assigned.')
    return 0
  print('Issue number: %s (%s)' % (cl.GetIssue(), cl.GetIssueURL()))
  if not options.fast:
    print('Issue description:')
    print(cl.GetDescription(pretty=True))
  return 0


def colorize_CMDstatus_doc():
  """To be called once in main() to add colors to git cl status help."""
  colors = [i for i in dir(Fore) if i[0].isupper()]

  def colorize_line(line):
    for color in colors:
      if color in line.upper():
        # Extract whitespaces first and the leading '-'.
        indent = len(line) - len(line.lstrip(' ')) + 1
        return line[:indent] + getattr(Fore, color) + line[indent:] + Fore.RESET
    return line

  lines = CMDstatus.__doc__.splitlines()
  CMDstatus.__doc__ = '\n'.join(colorize_line(l) for l in lines)


def write_json(path, contents):
  with open(path, 'w') as f:
    json.dump(contents, f)


@subcommand.usage('[issue_number]')
def CMDissue(parser, args):
  """Sets or displays the current code review issue number.

  Pass issue number 0 to clear the current issue.
  """
  parser.add_option('-r', '--reverse', action='store_true',
                    help='Lookup the branch(es) for the specified issues. If '
                         'no issues are specified, all branches with mapped '
                         'issues will be listed.')
  parser.add_option('--json', help='Path to JSON output file.')
  _add_codereview_select_options(parser)
  options, args = parser.parse_args(args)
  _process_codereview_select_options(parser, options)

  if options.reverse:
    branches = RunGit(['for-each-ref', 'refs/heads',
                       '--format=%(refname:short)']).splitlines()

    # Reverse issue lookup.
    issue_branch_map = {}
    for branch in branches:
      cl = Changelist(branchref=branch)
      issue_branch_map.setdefault(cl.GetIssue(), []).append(branch)
    if not args:
      args = sorted(issue_branch_map.iterkeys())
    result = {}
    for issue in args:
      if not issue:
        continue
      result[int(issue)] = issue_branch_map.get(int(issue))
      print('Branch for issue number %s: %s' % (
          issue, ', '.join(issue_branch_map.get(int(issue)) or ('None',))))
    if options.json:
      write_json(options.json, result)
  else:
    cl = Changelist(codereview=options.forced_codereview)
    if len(args) > 0:
      try:
        issue = int(args[0])
      except ValueError:
        DieWithError('Pass a number to set the issue or none to list it.\n'
                     'Maybe you want to run git cl status?')
      cl.SetIssue(issue)
    print('Issue number: %s (%s)' % (cl.GetIssue(), cl.GetIssueURL()))
    if options.json:
      write_json(options.json, {
        'issue': cl.GetIssue(),
        'issue_url': cl.GetIssueURL(),
      })
  return 0


def CMDcomments(parser, args):
  """Shows or posts review comments for any changelist."""
  parser.add_option('-a', '--add-comment', dest='comment',
                    help='comment to add to an issue')
  parser.add_option('-i', dest='issue',
                    help="review issue id (defaults to current issue)")
  parser.add_option('-j', '--json-file',
                    help='File to write JSON summary to')
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  auth_config = auth.extract_auth_config_from_options(options)

  issue = None
  if options.issue:
    try:
      issue = int(options.issue)
    except ValueError:
      DieWithError('A review issue id is expected to be a number')

  cl = Changelist(issue=issue, codereview='rietveld', auth_config=auth_config)

  if options.comment:
    cl.AddComment(options.comment)
    return 0

  data = cl.GetIssueProperties()
  summary = []
  for message in sorted(data.get('messages', []), key=lambda x: x['date']):
    summary.append({
        'date': message['date'],
        'lgtm': False,
        'message': message['text'],
        'not_lgtm': False,
        'sender': message['sender'],
    })
    if message['disapproval']:
      color = Fore.RED
      summary[-1]['not lgtm'] = True
    elif message['approval']:
      color = Fore.GREEN
      summary[-1]['lgtm'] = True
    elif message['sender'] == data['owner_email']:
      color = Fore.MAGENTA
    else:
      color = Fore.BLUE
    print('\n%s%s  %s%s' % (
        color, message['date'].split('.', 1)[0], message['sender'],
        Fore.RESET))
    if message['text'].strip():
      print('\n'.join('  ' + l for l in message['text'].splitlines()))
  if options.json_file:
    with open(options.json_file, 'wb') as f:
      json.dump(summary, f)
  return 0


@subcommand.usage('[codereview url or issue id]')
def CMDdescription(parser, args):
  """Brings up the editor for the current CL's description."""
  parser.add_option('-d', '--display', action='store_true',
                    help='Display the description instead of opening an editor')
  parser.add_option('-n', '--new-description',
                    help='New description to set for this issue (- for stdin, '
                         '+ to load from local commit HEAD)')
  parser.add_option('-f', '--force', action='store_true',
                    help='Delete any unpublished Gerrit edits for this issue '
                         'without prompting')

  _add_codereview_select_options(parser)
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  _process_codereview_select_options(parser, options)

  target_issue = None
  if len(args) > 0:
    target_issue = ParseIssueNumberArgument(args[0])
    if not target_issue.valid:
      parser.print_help()
      return 1

  auth_config = auth.extract_auth_config_from_options(options)

  kwargs = {
      'auth_config': auth_config,
      'codereview': options.forced_codereview,
  }
  if target_issue:
    kwargs['issue'] = target_issue.issue
    if options.forced_codereview == 'rietveld':
      kwargs['rietveld_server'] = target_issue.hostname

  cl = Changelist(**kwargs)

  if not cl.GetIssue():
    DieWithError('This branch has no associated changelist.')
  description = ChangeDescription(cl.GetDescription())

  if options.display:
    print(description.description)
    return 0

  if options.new_description:
    text = options.new_description
    if text == '-':
      text = '\n'.join(l.rstrip() for l in sys.stdin)
    elif text == '+':
      base_branch = cl.GetCommonAncestorWithUpstream()
      change = cl.GetChange(base_branch, None, local_description=True)
      text = change.FullDescriptionText()

    description.set_description(text)
  else:
    description.prompt()

  if cl.GetDescription() != description.description:
    cl.UpdateDescription(description.description, force=options.force)
  return 0


def CreateDescriptionFromLog(args):
  """Pulls out the commit log to use as a base for the CL description."""
  log_args = []
  if len(args) == 1 and not args[0].endswith('.'):
    log_args = [args[0] + '..']
  elif len(args) == 1 and args[0].endswith('...'):
    log_args = [args[0][:-1]]
  elif len(args) == 2:
    log_args = [args[0] + '..' + args[1]]
  else:
    log_args = args[:]  # Hope for the best!
  return RunGit(['log', '--pretty=format:%s\n\n%b'] + log_args)


def CMDlint(parser, args):
  """Runs cpplint on the current changelist."""
  parser.add_option('--filter', action='append', metavar='-x,+y',
                    help='Comma-separated list of cpplint\'s category-filters')
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  auth_config = auth.extract_auth_config_from_options(options)

  # Access to a protected member _XX of a client class
  # pylint: disable=protected-access
  try:
    import cpplint
    import cpplint_chromium
  except ImportError:
    print('Your depot_tools is missing cpplint.py and/or cpplint_chromium.py.')
    return 1

  # Change the current working directory before calling lint so that it
  # shows the correct base.
  previous_cwd = os.getcwd()
  os.chdir(settings.GetRoot())
  try:
    cl = Changelist(auth_config=auth_config)
    change = cl.GetChange(cl.GetCommonAncestorWithUpstream(), None)
    files = [f.LocalPath() for f in change.AffectedFiles()]
    if not files:
      print('Cannot lint an empty CL')
      return 1

    # Process cpplints arguments if any.
    command = args + files
    if options.filter:
      command = ['--filter=' + ','.join(options.filter)] + command
    filenames = cpplint.ParseArguments(command)

    white_regex = re.compile(settings.GetLintRegex())
    black_regex = re.compile(settings.GetLintIgnoreRegex())
    extra_check_functions = [cpplint_chromium.CheckPointerDeclarationWhitespace]
    for filename in filenames:
      if white_regex.match(filename):
        if black_regex.match(filename):
          print('Ignoring file %s' % filename)
        else:
          cpplint.ProcessFile(filename, cpplint._cpplint_state.verbose_level,
                              extra_check_functions)
      else:
        print('Skipping file %s' % filename)
  finally:
    os.chdir(previous_cwd)
  print('Total errors found: %d\n' % cpplint._cpplint_state.error_count)
  if cpplint._cpplint_state.error_count != 0:
    return 1
  return 0


def CMDpresubmit(parser, args):
  """Runs presubmit tests on the current changelist."""
  parser.add_option('-u', '--upload', action='store_true',
                    help='Run upload hook instead of the push hook')
  parser.add_option('-f', '--force', action='store_true',
                    help='Run checks even if tree is dirty')
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  auth_config = auth.extract_auth_config_from_options(options)

  if not options.force and git_common.is_dirty_git_tree('presubmit'):
    print('use --force to check even if tree is dirty.')
    return 1

  cl = Changelist(auth_config=auth_config)
  if args:
    base_branch = args[0]
  else:
    # Default to diffing against the common ancestor of the upstream branch.
    base_branch = cl.GetCommonAncestorWithUpstream()

  cl.RunHook(
      committing=not options.upload,
      may_prompt=False,
      verbose=options.verbose,
      change=cl.GetChange(base_branch, None))
  return 0


def GenerateGerritChangeId(message):
  """Returns Ixxxxxx...xxx change id.

  Works the same way as
  https://gerrit-review.googlesource.com/tools/hooks/commit-msg
  but can be called on demand on all platforms.

  The basic idea is to generate git hash of a state of the tree, original commit
  message, author/committer info and timestamps.
  """
  lines = []
  tree_hash = RunGitSilent(['write-tree'])
  lines.append('tree %s' % tree_hash.strip())
  code, parent = RunGitWithCode(['rev-parse', 'HEAD~0'], suppress_stderr=False)
  if code == 0:
    lines.append('parent %s' % parent.strip())
  author = RunGitSilent(['var', 'GIT_AUTHOR_IDENT'])
  lines.append('author %s' % author.strip())
  committer = RunGitSilent(['var', 'GIT_COMMITTER_IDENT'])
  lines.append('committer %s' % committer.strip())
  lines.append('')
  # Note: Gerrit's commit-hook actually cleans message of some lines and
  # whitespace. This code is not doing this, but it clearly won't decrease
  # entropy.
  lines.append(message)
  change_hash = RunCommand(['git', 'hash-object', '-t', 'commit', '--stdin'],
                           stdin='\n'.join(lines))
  return 'I%s' % change_hash.strip()


def GetTargetRef(remote, remote_branch, target_branch, pending_prefix_check,
                 remote_url=None):
  """Computes the remote branch ref to use for the CL.

  Args:
    remote (str): The git remote for the CL.
    remote_branch (str): The git remote branch for the CL.
    target_branch (str): The target branch specified by the user.
    pending_prefix_check (bool): If true, determines if pending_prefix should be
      used.
    remote_url (str): Only used for checking if pending_prefix should be used.
  """
  if not (remote and remote_branch):
    return None

  if target_branch:
    # Cannonicalize branch references to the equivalent local full symbolic
    # refs, which are then translated into the remote full symbolic refs
    # below.
    if '/' not in target_branch:
      remote_branch = 'refs/remotes/%s/%s' % (remote, target_branch)
    else:
      prefix_replacements = (
        ('^((refs/)?remotes/)?branch-heads/', 'refs/remotes/branch-heads/'),
        ('^((refs/)?remotes/)?%s/' % remote,  'refs/remotes/%s/' % remote),
        ('^(refs/)?heads/',                   'refs/remotes/%s/' % remote),
      )
      match = None
      for regex, replacement in prefix_replacements:
        match = re.search(regex, target_branch)
        if match:
          remote_branch = target_branch.replace(match.group(0), replacement)
          break
      if not match:
        # This is a branch path but not one we recognize; use as-is.
        remote_branch = target_branch
  elif remote_branch in REFS_THAT_ALIAS_TO_OTHER_REFS:
    # Handle the refs that need to land in different refs.
    remote_branch = REFS_THAT_ALIAS_TO_OTHER_REFS[remote_branch]

  # Create the true path to the remote branch.
  # Does the following translation:
  # * refs/remotes/origin/refs/diff/test -> refs/diff/test
  # * refs/remotes/origin/master -> refs/heads/master
  # * refs/remotes/branch-heads/test -> refs/branch-heads/test
  if remote_branch.startswith('refs/remotes/%s/refs/' % remote):
    remote_branch = remote_branch.replace('refs/remotes/%s/' % remote, '')
  elif remote_branch.startswith('refs/remotes/%s/' % remote):
    remote_branch = remote_branch.replace('refs/remotes/%s/' % remote,
                                          'refs/heads/')
  elif remote_branch.startswith('refs/remotes/branch-heads'):
    remote_branch = remote_branch.replace('refs/remotes/', 'refs/')

  if pending_prefix_check:
    # If a pending prefix exists then replace refs/ with it.
    state = _GitNumbererState.load(remote_url, remote_branch)
    if state.pending_prefix:
      remote_branch = remote_branch.replace('refs/', state.pending_prefix)
  return remote_branch


def cleanup_list(l):
  """Fixes a list so that comma separated items are put as individual items.

  So that "--reviewers joe@c,john@c --reviewers joa@c" results in
  options.reviewers == sorted(['joe@c', 'john@c', 'joa@c']).
  """
  items = sum((i.split(',') for i in l), [])
  stripped_items = (i.strip() for i in items)
  return sorted(filter(None, stripped_items))


@subcommand.usage('[args to "git diff"]')
def CMDupload(parser, args):
  """Uploads the current changelist to codereview.

  Can skip dependency patchset uploads for a branch by running:
    git config branch.branch_name.skip-deps-uploads True
  To unset run:
    git config --unset branch.branch_name.skip-deps-uploads
  Can also set the above globally by using the --global flag.
  """
  parser.add_option('--bypass-hooks', action='store_true', dest='bypass_hooks',
                    help='bypass upload presubmit hook')
  parser.add_option('--bypass-watchlists', action='store_true',
                    dest='bypass_watchlists',
                    help='bypass watchlists auto CC-ing reviewers')
  parser.add_option('-f', action='store_true', dest='force',
                    help="force yes to questions (don't prompt)")
  parser.add_option('--message', '-m', dest='message',
                    help='message for patchset')
  parser.add_option('-b', '--bug',
                    help='pre-populate the bug number(s) for this issue. '
                         'If several, separate with commas')
  parser.add_option('--message-file', dest='message_file',
                    help='file which contains message for patchset')
  parser.add_option('--title', '-t', dest='title',
                    help='title for patchset')
  parser.add_option('-r', '--reviewers',
                    action='append', default=[],
                    help='reviewer email addresses')
  parser.add_option('--cc',
                    action='append', default=[],
                    help='cc email addresses')
  parser.add_option('-s', '--send-mail', action='store_true',
                    help='send email to reviewer(s) and cc(s) immediately')
  parser.add_option('--emulate_svn_auto_props',
                    '--emulate-svn-auto-props',
                    action="store_true",
                    dest="emulate_svn_auto_props",
                    help="Emulate Subversion's auto properties feature.")
  parser.add_option('-c', '--use-commit-queue', action='store_true',
                    help='tell the commit queue to commit this patchset')
  parser.add_option('--private', action='store_true',
                    help='set the review private (rietveld only)')
  parser.add_option('--target_branch',
                    '--target-branch',
                    metavar='TARGET',
                    help='Apply CL to remote ref TARGET.  ' +
                         'Default: remote branch head, or master')
  parser.add_option('--squash', action='store_true',
                    help='Squash multiple commits into one (Gerrit only)')
  parser.add_option('--no-squash', action='store_true',
                    help='Don\'t squash multiple commits into one ' +
                         '(Gerrit only)')
  parser.add_option('--topic', default=None,
                    help='Topic to specify when uploading (Gerrit only)')
  parser.add_option('--email', default=None,
                    help='email address to use to connect to Rietveld')
  parser.add_option('--tbr-owners', dest='tbr_owners', action='store_true',
                    help='add a set of OWNERS to TBR')
  parser.add_option('-d', '--cq-dry-run', dest='cq_dry_run',
                    action='store_true',
                    help='Send the patchset to do a CQ dry run right after '
                         'upload.')
  parser.add_option('--dependencies', action='store_true',
                    help='Uploads CLs of all the local branches that depend on '
                         'the current branch')

  orig_args = args
  add_git_similarity(parser)
  auth.add_auth_options(parser)
  _add_codereview_select_options(parser)
  (options, args) = parser.parse_args(args)
  _process_codereview_select_options(parser, options)
  auth_config = auth.extract_auth_config_from_options(options)

  if git_common.is_dirty_git_tree('upload'):
    return 1

  options.reviewers = cleanup_list(options.reviewers)
  options.cc = cleanup_list(options.cc)

  if options.message_file:
    if options.message:
      parser.error('only one of --message and --message-file allowed.')
    options.message = gclient_utils.FileRead(options.message_file)
    options.message_file = None

  if options.cq_dry_run and options.use_commit_queue:
    parser.error('only one of --use-commit-queue and --cq-dry-run allowed.')

  # For sanity of test expectations, do this otherwise lazy-loading *now*.
  settings.GetIsGerrit()

  cl = Changelist(auth_config=auth_config, codereview=options.forced_codereview)
  return cl.CMDUpload(options, args, orig_args)


def WaitForRealCommit(remote, pushed_commit, local_base_ref, real_ref):
  print()
  print('Waiting for commit to be landed on %s...' % real_ref)
  print('(If you are impatient, you may Ctrl-C once without harm)')
  target_tree = RunGit(['rev-parse', '%s:' % pushed_commit]).strip()
  current_rev = RunGit(['rev-parse', local_base_ref]).strip()
  mirror = settings.GetGitMirror(remote)

  loop = 0
  while True:
    sys.stdout.write('fetching (%d)...        \r' % loop)
    sys.stdout.flush()
    loop += 1

    if mirror:
      mirror.populate()
    RunGit(['retry', 'fetch', remote, real_ref], stderr=subprocess2.VOID)
    to_rev = RunGit(['rev-parse', 'FETCH_HEAD']).strip()
    commits = RunGit(['rev-list', '%s..%s' % (current_rev, to_rev)])
    for commit in commits.splitlines():
      if RunGit(['rev-parse', '%s:' % commit]).strip() == target_tree:
        print('Found commit on %s' % real_ref)
        return commit

    current_rev = to_rev


def PushToGitPending(remote, pending_ref):
  """Fetches pending_ref, cherry-picks current HEAD on top of it, pushes.

  Returns:
    (retcode of last operation, output log of last operation).
  """
  assert pending_ref.startswith('refs/'), pending_ref
  local_pending_ref = 'refs/git-cl/' + pending_ref[len('refs/'):]
  cherry = RunGit(['rev-parse', 'HEAD']).strip()
  code = 0
  out = ''
  max_attempts = 3
  attempts_left = max_attempts
  while attempts_left:
    if attempts_left != max_attempts:
      print('Retrying, %d attempts left...' % (attempts_left - 1,))
    attempts_left -= 1

    # Fetch. Retry fetch errors.
    print('Fetching pending ref %s...' % pending_ref)
    code, out = RunGitWithCode(
        ['retry', 'fetch', remote, '+%s:%s' % (pending_ref, local_pending_ref)])
    if code:
      print('Fetch failed with exit code %d.' % code)
      if out.strip():
        print(out.strip())
      continue

    # Try to cherry pick. Abort on merge conflicts.
    print('Cherry-picking commit on top of pending ref...')
    RunGitWithCode(['checkout', local_pending_ref], suppress_stderr=True)
    code, out = RunGitWithCode(['cherry-pick', cherry])
    if code:
      print('Your patch doesn\'t apply cleanly to ref \'%s\', '
            'the following files have merge conflicts:' % pending_ref)
      print(RunGit(['diff', '--name-status', '--diff-filter=U']).strip())
      print('Please rebase your patch and try again.')
      RunGitWithCode(['cherry-pick', '--abort'])
      return code, out

    # Applied cleanly, try to push now. Retry on error (flake or non-ff push).
    print('Pushing commit to %s... It can take a while.' % pending_ref)
    code, out = RunGitWithCode(
        ['retry', 'push', '--porcelain', remote, 'HEAD:%s' % pending_ref])
    if code == 0:
      # Success.
      print('Commit pushed to pending ref successfully!')
      return code, out

    print('Push failed with exit code %d.' % code)
    if out.strip():
      print(out.strip())
    if IsFatalPushFailure(out):
      print('Fatal push error. Make sure your .netrc credentials and git '
            'user.email are correct and you have push access to the repo.')
      return code, out

  print('All attempts to push to pending ref failed.')
  return code, out


def IsFatalPushFailure(push_stdout):
  """True if retrying push won't help."""
  return '(prohibited by Gerrit)' in push_stdout


@subcommand.usage('DEPRECATED')
def CMDdcommit(parser, args):
  """DEPRECATED: Used to commit the current changelist via git-svn."""
  message = ('git-cl no longer supports committing to SVN repositories via '
             'git-svn. You probably want to use `git cl land` instead.')
  print(message)
  return 1


@subcommand.usage('[upstream branch to apply against]')
def CMDland(parser, args):
  """Commits the current changelist via git.

  In case of Gerrit, uses Gerrit REST api to "submit" the issue, which pushes
  upstream and closes the issue automatically and atomically.

  Otherwise (in case of Rietveld):
    Squashes branch into a single commit.
    Updates commit message with metadata (e.g. pointer to review).
    Pushes the code upstream.
    Updates review and closes.
  """
  parser.add_option('--bypass-hooks', action='store_true', dest='bypass_hooks',
                    help='bypass upload presubmit hook')
  parser.add_option('-m', dest='message',
                    help="override review description")
  parser.add_option('-f', action='store_true', dest='force',
                    help="force yes to questions (don't prompt)")
  parser.add_option('-c', dest='contributor',
                    help="external contributor for patch (appended to " +
                         "description and used as author for git). Should be " +
                         "formatted as 'First Last <email@example.com>'")
  add_git_similarity(parser)
  auth.add_auth_options(parser)
  (options, args) = parser.parse_args(args)
  auth_config = auth.extract_auth_config_from_options(options)

  cl = Changelist(auth_config=auth_config)

  # TODO(tandrii): refactor this into _RietveldChangelistImpl method.
  if cl.IsGerrit():
    if options.message:
      # This could be implemented, but it requires sending a new patch to
      # Gerrit, as Gerrit unlike Rietveld versions messages with patchsets.
      # Besides, Gerrit has the ability to change the commit message on submit
      # automatically, thus there is no need to support this option (so far?).
      parser.error('-m MESSAGE option is not supported for Gerrit.')
    if options.contributor:
      parser.error(
          '-c CONTRIBUTOR option is not supported for Gerrit.\n'
          'Before uploading a commit to Gerrit, ensure it\'s author field is '
          'the contributor\'s "name <email>". If you can\'t upload such a '
          'commit for review, contact your repository admin and request'
          '"Forge-Author" permission.')
    if not cl.GetIssue():
      DieWithError('You must upload the change first to Gerrit.\n'
                   '  If you would rather have `git cl land` upload '
                   'automatically for you, see http://crbug.com/642759')
    return cl._codereview_impl.CMDLand(options.force, options.bypass_hooks,
                                       options.verbose)

  current = cl.GetBranch()
  remote, upstream_branch = cl.FetchUpstreamTuple(cl.GetBranch())
  if remote == '.':
    print()
    print('Attempting to push branch %r into another local branch!' % current)
    print()
    print('Either reparent this branch on top of origin/master:')
    print('  git reparent-branch --root')
    print()
    print('OR run `git rebase-update` if you think the parent branch is ')
    print('already committed.')
    print()
    print('  Current parent: %r' % upstream_branch)
    return 1

  if not args:
    # Default to merging against our best guess of the upstream branch.
    args = [cl.GetUpstreamBranch()]

  if options.contributor:
    if not re.match('^.*\s<\S+@\S+>$', options.contributor):
      print("Please provide contibutor as 'First Last <email@example.com>'")
      return 1

  base_branch = args[0]

  if git_common.is_dirty_git_tree('land'):
    return 1

  # This rev-list syntax means "show all commits not in my branch that
  # are in base_branch".
  upstream_commits = RunGit(['rev-list', '^' + cl.GetBranchRef(),
                             base_branch]).splitlines()
  if upstream_commits:
    print('Base branch "%s" has %d commits '
          'not in this branch.' % (base_branch, len(upstream_commits)))
    print('Run "git merge %s" before attempting to land.' % base_branch)
    return 1

  merge_base = RunGit(['merge-base', base_branch, 'HEAD']).strip()
  if not options.bypass_hooks:
    author = None
    if options.contributor:
      author = re.search(r'\<(.*)\>', options.contributor).group(1)
    hook_results = cl.RunHook(
        committing=True,
        may_prompt=not options.force,
        verbose=options.verbose,
        change=cl.GetChange(merge_base, author))
    if not hook_results.should_continue():
      return 1

    # Check the tree status if the tree status URL is set.
    status = GetTreeStatus()
    if 'closed' == status:
      print('The tree is closed.  Please wait for it to reopen. Use '
            '"git cl land --bypass-hooks" to commit on a closed tree.')
      return 1
    elif 'unknown' == status:
      print('Unable to determine tree status.  Please verify manually and '
            'use "git cl land --bypass-hooks" to commit on a closed tree.')
      return 1

  change_desc = ChangeDescription(options.message)
  if not change_desc.description and cl.GetIssue():
    change_desc = ChangeDescription(cl.GetDescription())

  if not change_desc.description:
    if not cl.GetIssue() and options.bypass_hooks:
      change_desc = ChangeDescription(CreateDescriptionFromLog([merge_base]))
    else:
      print('No description set.')
      print('Visit %s/edit to set it.' % (cl.GetIssueURL()))
      return 1

  # Keep a separate copy for the commit message, because the commit message
  # contains the link to the Rietveld issue, while the Rietveld message contains
  # the commit viewvc url.
  if cl.GetIssue():
    change_desc.update_reviewers(cl.GetApprovingReviewers())

  commit_desc = ChangeDescription(change_desc.description)
  if cl.GetIssue():
    # Xcode won't linkify this URL unless there is a non-whitespace character
    # after it. Add a period on a new line to circumvent this. Also add a space
    # before the period to make sure that Gitiles continues to correctly resolve
    # the URL.
    commit_desc.append_footer('Review-Url: %s .' % cl.GetIssueURL())
  if options.contributor:
    commit_desc.append_footer('Patch from %s.' % options.contributor)

  print('Description:')
  print(commit_desc.description)

  branches = [merge_base, cl.GetBranchRef()]
  if not options.force:
    print_stats(options.similarity, options.find_copies, branches)

  # We want to squash all this branch's commits into one commit with the proper
  # description. We do this by doing a "reset --soft" to the base branch (which
  # keeps the working copy the same), then landing that.
  MERGE_BRANCH = 'git-cl-commit'
  CHERRY_PICK_BRANCH = 'git-cl-cherry-pick'
  # Delete the branches if they exist.
  for branch in [MERGE_BRANCH, CHERRY_PICK_BRANCH]:
    showref_cmd = ['show-ref', '--quiet', '--verify', 'refs/heads/%s' % branch]
    result = RunGitWithCode(showref_cmd)
    if result[0] == 0:
      RunGit(['branch', '-D', branch])

  # We might be in a directory that's present in this branch but not in the
  # trunk.  Move up to the top of the tree so that git commands that expect a
  # valid CWD won't fail after we check out the merge branch.
  rel_base_path = settings.GetRelativeRoot()
  if rel_base_path:
    os.chdir(rel_base_path)

  # Stuff our change into the merge branch.
  # We wrap in a try...finally block so if anything goes wrong,
  # we clean up the branches.
  retcode = -1
  pushed_to_pending = False
  pending_ref = None
  revision = None
  try:
    RunGit(['checkout', '-q', '-b', MERGE_BRANCH])
    RunGit(['reset', '--soft', merge_base])
    if options.contributor:
      RunGit(
          [
            'commit', '--author', options.contributor,
            '-m', commit_desc.description,
          ])
    else:
      RunGit(['commit', '-m', commit_desc.description])

    remote, branch = cl.FetchUpstreamTuple(cl.GetBranch())
    mirror = settings.GetGitMirror(remote)
    if mirror:
      pushurl = mirror.url
      git_numberer = _GitNumbererState.load(pushurl, branch)
    else:
      pushurl = remote  # Usually, this is 'origin'.
      git_numberer = _GitNumbererState.load(
          RunGit(['config', 'remote.%s.url' % remote]).strip(), branch)

    if git_numberer.should_add_git_number:
      # TODO(tandrii): run git fetch in a loop + autorebase when there there
      # is no pending ref to push to?
      logging.debug('Adding git number footers')
      parent_msg = RunGit(['show', '-s', '--format=%B', merge_base]).strip()
      commit_desc.update_with_git_number_footers(merge_base, parent_msg,
                                                 branch)
      # Ensure timestamps are monotonically increasing.
      timestamp = max(1 + _get_committer_timestamp(merge_base),
                      _get_committer_timestamp('HEAD'))
      _git_amend_head(commit_desc.description, timestamp)
      change_desc = ChangeDescription(commit_desc.description)
      # If gnumbd is sitll ON and we ultimately push to branch with
      # pending_prefix, gnumbd will modify footers we've just inserted with
      # 'Original-', which is annoying but still technically correct.

    pending_prefix = git_numberer.pending_prefix
    if not pending_prefix or branch.startswith(pending_prefix):
      # If not using refs/pending/heads/* at all, or target ref is already set
      # to pending, then push to the target ref directly.
      # NB(tandrii): I think branch.startswith(pending_prefix) never happens
      # in practise. I really tried to create a new branch tracking
      # refs/pending/heads/master directly and git cl land failed long before
      # reaching this. Disagree? Comment on http://crbug.com/642493.
      if pending_prefix:
        print('\n\nYOU GOT A CHANCE TO WIN A FREE GIFT!\n\n'
              'Grab your .git/config, add instructions how to reproduce '
              'this, and post it to http://crbug.com/642493.\n'
              'The first reporter gets a free "Black Swan" book from '
              'tandrii@\n\n')
      retcode, output = RunGitWithCode(
          ['push', '--porcelain', pushurl, 'HEAD:%s' % branch])
      pushed_to_pending = pending_prefix and branch.startswith(pending_prefix)
    else:
      # Cherry-pick the change on top of pending ref and then push it.
      assert branch.startswith('refs/'), branch
      assert pending_prefix[-1] == '/', pending_prefix
      pending_ref = pending_prefix + branch[len('refs/'):]
      retcode, output = PushToGitPending(pushurl, pending_ref)
      pushed_to_pending = (retcode == 0)

    if retcode == 0:
      revision = RunGit(['rev-parse', 'HEAD']).strip()
    logging.debug(output)
  except:  # pylint: disable=bare-except
    if _IS_BEING_TESTED:
      logging.exception('this is likely your ACTUAL cause of test failure.\n'
                        + '-' * 30 + '8<' + '-' * 30)
      logging.error('\n' + '-' * 30 + '8<' + '-' * 30 + '\n\n\n')
    raise
  finally:
    # And then swap back to the original branch and clean up.
    RunGit(['checkout', '-q', cl.GetBranch()])
    RunGit(['branch', '-D', MERGE_BRANCH])

  if not revision:
    print('Failed to push. If this persists, please file a bug.')
    return 1

  killed = False
  if pushed_to_pending:
    try:
      revision = WaitForRealCommit(remote, revision, base_branch, branch)
      # We set pushed_to_pending to False, since it made it all the way to the
      # real ref.
      pushed_to_pending = False
    except KeyboardInterrupt:
      killed = True

  if cl.GetIssue():
    to_pending = ' to pending queue' if pushed_to_pending else ''
    viewvc_url = settings.GetViewVCUrl()
    if not to_pending:
      if viewvc_url and revision:
        change_desc.append_footer(
            'Committed: %s%s' % (viewvc_url, revision))
      elif revision:
        change_desc.append_footer('Committed: %s' % (revision,))
    print('Closing issue '
          '(you may be prompted for your codereview password)...')
    cl.UpdateDescription(change_desc.description)
    cl.CloseIssue()
    props = cl.GetIssueProperties()
    patch_num = len(props['patchsets'])
    comment = "Committed patchset #%d (id:%d)%s manually as %s" % (
        patch_num, props['patchsets'][-1], to_pending, revision)
    if options.bypass_hooks:
      comment += ' (tree was closed).' if GetTreeStatus() == 'closed' else '.'
    else:
      comment += ' (presubmit successful).'
    cl.RpcServer().add_comment(cl.GetIssue(), comment)

  if pushed_to_pending:
    _, branch = cl.FetchUpstreamTuple(cl.GetBranch())
    print('The commit is in the pending queue (%s).' % pending_ref)
    print('It will show up on %s in ~1 min, once it gets a Cr-Commit-Position '
          'footer.' % branch)

  if os.path.isfile(POSTUPSTREAM_HOOK):
    RunCommand([POSTUPSTREAM_HOOK, merge_base], error_ok=True)

  return 1 if killed else 0


@subcommand.usage('<patch url or issue id or issue url>')
def CMDpatch(parser, args):
  """Patches in a code review."""
  parser.add_option('-b', dest='newbranch',
                    help='create a new branch off trunk for the patch')
  parser.add_option('-f', '--force', action='store_true',
                    help='with -b, clobber any existing branch')
  parser.add_option('-d', '--directory', action='store', metavar='DIR',
                    help='Change to the directory DIR immediately, '
                         'before doing anything else. Rietveld only.')
  parser.add_option('--reject', action='store_true',
                    help='failed patches spew .rej files rather than '
                        'attempting a 3-way merge. Rietveld only.')
  parser.add_option('-n', '--no-commit', action='store_true', dest='nocommit',
                    help='don\'t commit after patch applies. Rietveld only.')


  group = optparse.OptionGroup(
      parser,
      'Options for continuing work on the current issue uploaded from a '
      'different clone (e.g. different machine). Must be used independently '
      'from the other options. No issue number should be specified, and the '
      'branch must have an issue number associated with it')
  group.add_option('--reapply', action='store_true', dest='reapply',
                   help='Reset the branch and reapply the issue.\n'
                        'CAUTION: This will undo any local changes in this '
                        'branch')

  group.add_option('--pull', action='store_true', dest='pull',
                    help='Performs a pull before reapplying.')
  parser.add_option_group(group)

  auth.add_auth_options(parser)
  _add_codereview_select_options(parser)
  (options, args) = parser.parse_args(args)
  _process_codereview_select_options(parser, options)
  auth_config = auth.extract_auth_config_from_options(options)


  if options.reapply :
    if options.newbranch:
      parser.error('--reapply works on the current branch only')
    if len(args) > 0:
      parser.error('--reapply implies no additional arguments')

    cl = Changelist(auth_config=auth_config,
                    codereview=options.forced_codereview)
    if not cl.GetIssue():
      parser.error('current branch must have an associated issue')

    upstream = cl.GetUpstreamBranch()
    if upstream == None:
      parser.error('No upstream branch specified. Cannot reset branch')

    RunGit(['reset', '--hard', upstream])
    if options.pull:
      RunGit(['pull'])

    return cl.CMDPatchIssue(cl.GetIssue(), options.reject, options.nocommit,
                            options.directory)

  if len(args) != 1 or not args[0]:
    parser.error('Must specify issue number or url')

  # We don't want uncommitted changes mixed up with the patch.
  if git_common.is_dirty_git_tree('patch'):
    return 1

  if options.newbranch:
    if options.force:
      RunGit(['branch', '-D', options.newbranch],
             stderr=subprocess2.PIPE, error_ok=True)
    RunGit(['new-branch', options.newbranch])
  elif not GetCurrentBranch():
    DieWithError('A branch is required to apply patch. Hint: use -b option.')

  cl = Changelist(auth_config=auth_config, codereview=options.forced_codereview)

  if cl.IsGerrit():
    if options.reject:
      parser.error('--reject is not supported with Gerrit codereview.')
    if options.nocommit:
      parser.error('--nocommit is not supported with Gerrit codereview.')
    if options.directory:
      parser.error('--directory is not supported with Gerrit codereview.')

  return cl.CMDPatchIssue(args[0], options.reject, options.nocommit,
                          options.directory)


def GetTreeStatus(url=None):
  """Fetches the tree status and returns either 'open', 'closed',
  'unknown' or 'unset'."""
  url = url or settings.GetTreeStatusUrl(error_ok=True)
  if url:
    status = urllib2.urlopen(url).read().lower()
    if status.find('closed') != -1 or status == '0':
      return 'closed'
    elif status.find('open') != -1 or status == '1':
      return 'open'
    return 'unknown'
  return 'unset'


def GetTreeStatusReason():
  """Fetches the tree status from a json url and returns the message
  with the reason for the tree to be opened or closed."""
  url = settings.GetTreeStatusUrl()
  json_url = urlparse.urljoin(url, '/current?format=json')
  connection = urllib2.urlopen(json_url)
  status = json.loads(connection.read())
  connection.close()
  return status['message']


def CMDtree(parser, args):
  """Shows the status of the tree."""
  _, args = parser.parse_args(args)
  status = GetTreeStatus()
  if 'unset' == status:
    print('You must configure your tree status URL by running "git cl config".')
    return 2

  print('The tree is %s' % status)
  print()
  print(GetTreeStatusReason())
  if status != 'open':
    return 1
  return 0


def CMDtry(parser, args):
  """Triggers try jobs using either BuildBucket or CQ dry run."""
  group = optparse.OptionGroup(parser, 'Try job options')
  group.add_option(
      '-b', '--bot', action='append',
      help=('IMPORTANT: specify ONE builder per --bot flag. Use it multiple '
            'times to specify multiple builders. ex: '
            '"-b win_rel -b win_layout". See '
            'the try server waterfall for the builders name and the tests '
            'available.'))
  group.add_option(
      '-B', '--bucket', default='',
      help=('Buildbucket bucket to send the try requests.'))
  group.add_option(
      '-m', '--master', default='',
      help=('Specify a try master where to run the tries.'))
  group.add_option(
      '-r', '--revision',
      help='Revision to use for the try job; default: the revision will '
           'be determined by the try recipe that builder runs, which usually '
           'defaults to HEAD of origin/master')
  group.add_option(
      '-c', '--clobber', action='store_true', default=False,
      help='Force a clobber before building; that is don\'t do an '
           'incremental build')
  group.add_option(
      '--project',
      help='Override which project to use. Projects are defined '
           'in recipe to determine to which repository or directory to '
           'apply the patch')
  group.add_option(
      '-p', '--property', dest='properties', action='append', default=[],
      help='Specify generic properties in the form -p key1=value1 -p '
           'key2=value2 etc. The value will be treated as '
           'json if decodable, or as string otherwise. '
           'NOTE: using this may make your try job not usable for CQ, '
           'which will then schedule another try job with default properties')
  group.add_option(
      '--buildbucket-host', default='cr-buildbucket.appspot.com',
      help='Host of buildbucket. The default host is %default.')
  parser.add_option_group(group)
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  auth_config = auth.extract_auth_config_from_options(options)

  # Make sure that all properties are prop=value pairs.
  bad_params = [x for x in options.properties if '=' not in x]
  if bad_params:
    parser.error('Got properties with missing "=": %s' % bad_params)

  if args:
    parser.error('Unknown arguments: %s' % args)

  cl = Changelist(auth_config=auth_config)
  if not cl.GetIssue():
    parser.error('Need to upload first')

  error_message = cl.CannotTriggerTryJobReason()
  if error_message:
    parser.error('Can\'t trigger try jobs: %s' % error_message)

  if options.bucket and options.master:
    parser.error('Only one of --bucket and --master may be used.')

  buckets = _get_bucket_map(cl, options, parser)

  # If no bots are listed and we couldn't get a list based on PRESUBMIT files,
  # then we default to triggering a CQ dry run (see http://crbug.com/625697).
  if not buckets:
    if options.verbose:
      print('git cl try with no bots now defaults to CQ Dry Run.')
    return cl.TriggerDryRun()

  for builders in buckets.itervalues():
    if any('triggered' in b for b in builders):
      print('ERROR You are trying to send a job to a triggered bot. This type '
            'of bot requires an initial job from a parent (usually a builder). '
            'Instead send your job to the parent.\n'
            'Bot list: %s' % builders, file=sys.stderr)
      return 1

  patchset = cl.GetMostRecentPatchset()
  # TODO(tandrii): Checking local patchset against remote patchset is only
  # supported for Rietveld. Extend it to Gerrit or remove it completely.
  if not cl.IsGerrit() and patchset != cl.GetPatchset():
    print('Warning: Codereview server has newer patchsets (%s) than most '
          'recent upload from local checkout (%s). Did a previous upload '
          'fail?\n'
          'By default, git cl try uses the latest patchset from '
          'codereview, continuing to use patchset %s.\n' %
          (patchset, cl.GetPatchset(), patchset))

  try:
    _trigger_try_jobs(auth_config, cl, buckets, options, 'git_cl_try',
                      patchset)
  except BuildbucketResponseException as ex:
    print('ERROR: %s' % ex)
    return 1
  return 0


def CMDtry_results(parser, args):
  """Prints info about try jobs associated with current CL."""
  group = optparse.OptionGroup(parser, 'Try job results options')
  group.add_option(
      '-p', '--patchset', type=int, help='patchset number if not current.')
  group.add_option(
      '--print-master', action='store_true', help='print master name as well.')
  group.add_option(
      '--color', action='store_true', default=setup_color.IS_TTY,
      help='force color output, useful when piping output.')
  group.add_option(
      '--buildbucket-host', default='cr-buildbucket.appspot.com',
      help='Host of buildbucket. The default host is %default.')
  group.add_option(
      '--json', help='Path of JSON output file to write try job results to.')
  parser.add_option_group(group)
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  if args:
    parser.error('Unrecognized args: %s' % ' '.join(args))

  auth_config = auth.extract_auth_config_from_options(options)
  cl = Changelist(auth_config=auth_config)
  if not cl.GetIssue():
    parser.error('Need to upload first')

  patchset = options.patchset
  if not patchset:
    patchset = cl.GetMostRecentPatchset()
    if not patchset:
      parser.error('Codereview doesn\'t know about issue %s. '
                   'No access to issue or wrong issue number?\n'
                   'Either upload first, or pass --patchset explicitely' %
                   cl.GetIssue())

    # TODO(tandrii): Checking local patchset against remote patchset is only
    # supported for Rietveld. Extend it to Gerrit or remove it completely.
    if not cl.IsGerrit() and patchset != cl.GetPatchset():
      print('Warning: Codereview server has newer patchsets (%s) than most '
            'recent upload from local checkout (%s). Did a previous upload '
            'fail?\n'
            'By default, git cl try-results uses the latest patchset from '
            'codereview, continuing to use patchset %s.\n' %
            (patchset, cl.GetPatchset(), patchset))
  try:
    jobs = fetch_try_jobs(auth_config, cl, options.buildbucket_host, patchset)
  except BuildbucketResponseException as ex:
    print('Buildbucket error: %s' % ex)
    return 1
  if options.json:
    write_try_results_json(options.json, jobs)
  else:
    print_try_jobs(options, jobs)
  return 0


@subcommand.usage('[new upstream branch]')
def CMDupstream(parser, args):
  """Prints or sets the name of the upstream branch, if any."""
  _, args = parser.parse_args(args)
  if len(args) > 1:
    parser.error('Unrecognized args: %s' % ' '.join(args))

  cl = Changelist()
  if args:
    # One arg means set upstream branch.
    branch = cl.GetBranch()
    RunGit(['branch', '--set-upstream-to', args[0], branch])
    cl = Changelist()
    print('Upstream branch set to %s' % (cl.GetUpstreamBranch(),))

    # Clear configured merge-base, if there is one.
    git_common.remove_merge_base(branch)
  else:
    print(cl.GetUpstreamBranch())
  return 0


def CMDweb(parser, args):
  """Opens the current CL in the web browser."""
  _, args = parser.parse_args(args)
  if args:
    parser.error('Unrecognized args: %s' % ' '.join(args))

  issue_url = Changelist().GetIssueURL()
  if not issue_url:
    print('ERROR No issue to open', file=sys.stderr)
    return 1

  webbrowser.open(issue_url)
  return 0


def CMDset_commit(parser, args):
  """Sets the commit bit to trigger the Commit Queue."""
  parser.add_option('-d', '--dry-run', action='store_true',
                    help='trigger in dry run mode')
  parser.add_option('-c', '--clear', action='store_true',
                    help='stop CQ run, if any')
  auth.add_auth_options(parser)
  _add_codereview_issue_select_options(parser)
  options, args = parser.parse_args(args)
  _process_codereview_issue_select_options(parser, options)
  auth_config = auth.extract_auth_config_from_options(options)
  if args:
    parser.error('Unrecognized args: %s' % ' '.join(args))
  if options.dry_run and options.clear:
    parser.error('Make up your mind: both --dry-run and --clear not allowed')

  cl = Changelist(auth_config=auth_config, issue=options.issue,
                  codereview=options.forced_codereview)
  if options.clear:
    state = _CQState.NONE
  elif options.dry_run:
      # TODO(qyearsley): Use cl.TriggerDryRun.
    state = _CQState.DRY_RUN
  else:
    state = _CQState.COMMIT
  if not cl.GetIssue():
    parser.error('Must upload the issue first')
  cl.SetCQState(state)
  return 0


def CMDset_close(parser, args):
  """Closes the issue."""
  _add_codereview_issue_select_options(parser)
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  _process_codereview_issue_select_options(parser, options)
  auth_config = auth.extract_auth_config_from_options(options)
  if args:
    parser.error('Unrecognized args: %s' % ' '.join(args))
  cl = Changelist(auth_config=auth_config, issue=options.issue,
                  codereview=options.forced_codereview)
  # Ensure there actually is an issue to close.
  cl.GetDescription()
  cl.CloseIssue()
  return 0


def CMDdiff(parser, args):
  """Shows differences between local tree and last upload."""
  parser.add_option(
      '--stat',
      action='store_true',
      dest='stat',
      help='Generate a diffstat')
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  auth_config = auth.extract_auth_config_from_options(options)
  if args:
    parser.error('Unrecognized args: %s' % ' '.join(args))

  # Uncommitted (staged and unstaged) changes will be destroyed by
  # "git reset --hard" if there are merging conflicts in CMDPatchIssue().
  # Staged changes would be committed along with the patch from last
  # upload, hence counted toward the "last upload" side in the final
  # diff output, and this is not what we want.
  if git_common.is_dirty_git_tree('diff'):
    return 1

  cl = Changelist(auth_config=auth_config)
  issue = cl.GetIssue()
  branch = cl.GetBranch()
  if not issue:
    DieWithError('No issue found for current branch (%s)' % branch)
  TMP_BRANCH = 'git-cl-diff'
  base_branch = cl.GetCommonAncestorWithUpstream()

  # Create a new branch based on the merge-base
  RunGit(['checkout', '-q', '-b', TMP_BRANCH, base_branch])
  # Clear cached branch in cl object, to avoid overwriting original CL branch
  # properties.
  cl.ClearBranch()
  try:
    rtn = cl.CMDPatchIssue(issue, reject=False, nocommit=False, directory=None)
    if rtn != 0:
      RunGit(['reset', '--hard'])
      return rtn

    # Switch back to starting branch and diff against the temporary
    # branch containing the latest rietveld patch.
    cmd = ['git', 'diff']
    if options.stat:
      cmd.append('--stat')
    cmd.extend([TMP_BRANCH, branch, '--'])
    subprocess2.check_call(cmd)
  finally:
    RunGit(['checkout', '-q', branch])
    RunGit(['branch', '-D', TMP_BRANCH])

  return 0


def CMDowners(parser, args):
  """Interactively find the owners for reviewing."""
  parser.add_option(
      '--no-color',
      action='store_true',
      help='Use this option to disable color output')
  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  auth_config = auth.extract_auth_config_from_options(options)

  author = RunGit(['config', 'user.email']).strip() or None

  cl = Changelist(auth_config=auth_config)

  if args:
    if len(args) > 1:
      parser.error('Unknown args')
    base_branch = args[0]
  else:
    # Default to diffing against the common ancestor of the upstream branch.
    base_branch = cl.GetCommonAncestorWithUpstream()

  change = cl.GetChange(base_branch, None)
  return owners_finder.OwnersFinder(
      [f.LocalPath() for f in
          cl.GetChange(base_branch, None).AffectedFiles()],
      change.RepositoryRoot(), author,
      fopen=file, os_path=os.path,
      disable_color=options.no_color).run()


def BuildGitDiffCmd(diff_type, upstream_commit, args):
  """Generates a diff command."""
  # Generate diff for the current branch's changes.
  diff_cmd = ['diff', '--no-ext-diff', '--no-prefix', diff_type,
              upstream_commit, '--' ]

  if args:
    for arg in args:
      if os.path.isdir(arg) or os.path.isfile(arg):
        diff_cmd.append(arg)
      else:
        DieWithError('Argument "%s" is not a file or a directory' % arg)

  return diff_cmd

def MatchingFileType(file_name, extensions):
  """Returns true if the file name ends with one of the given extensions."""
  return bool([ext for ext in extensions if file_name.lower().endswith(ext)])

@subcommand.usage('[files or directories to diff]')
def CMDformat(parser, args):
  """Runs auto-formatting tools (clang-format etc.) on the diff."""
  CLANG_EXTS = ['.cc', '.cpp', '.h', '.m', '.mm', '.proto', '.java']
  GN_EXTS = ['.gn', '.gni', '.typemap']
  parser.add_option('--full', action='store_true',
                    help='Reformat the full content of all touched files')
  parser.add_option('--dry-run', action='store_true',
                    help='Don\'t modify any file on disk.')
  parser.add_option('--python', action='store_true',
                    help='Format python code with yapf (experimental).')
  parser.add_option('--diff', action='store_true',
                    help='Print diff to stdout rather than modifying files.')
  opts, args = parser.parse_args(args)

  # Normalize any remaining args against the current path, so paths relative to
  # the current directory are still resolved as expected.
  args = [os.path.join(os.getcwd(), arg) for arg in args]

  # git diff generates paths against the root of the repository.  Change
  # to that directory so clang-format can find files even within subdirs.
  rel_base_path = settings.GetRelativeRoot()
  if rel_base_path:
    os.chdir(rel_base_path)

  # Grab the merge-base commit, i.e. the upstream commit of the current
  # branch when it was created or the last time it was rebased. This is
  # to cover the case where the user may have called "git fetch origin",
  # moving the origin branch to a newer commit, but hasn't rebased yet.
  upstream_commit = None
  cl = Changelist()
  upstream_branch = cl.GetUpstreamBranch()
  if upstream_branch:
    upstream_commit = RunGit(['merge-base', 'HEAD', upstream_branch])
    upstream_commit = upstream_commit.strip()

  if not upstream_commit:
    DieWithError('Could not find base commit for this branch. '
                 'Are you in detached state?')

  changed_files_cmd = BuildGitDiffCmd('--name-only', upstream_commit, args)
  diff_output = RunGit(changed_files_cmd)
  diff_files = diff_output.splitlines()
  # Filter out files deleted by this CL
  diff_files = [x for x in diff_files if os.path.isfile(x)]

  clang_diff_files = [x for x in diff_files if MatchingFileType(x, CLANG_EXTS)]
  python_diff_files = [x for x in diff_files if MatchingFileType(x, ['.py'])]
  dart_diff_files = [x for x in diff_files if MatchingFileType(x, ['.dart'])]
  gn_diff_files = [x for x in diff_files if MatchingFileType(x, GN_EXTS)]

  top_dir = os.path.normpath(
      RunGit(["rev-parse", "--show-toplevel"]).rstrip('\n'))

  # Set to 2 to signal to CheckPatchFormatted() that this patch isn't
  # formatted. This is used to block during the presubmit.
  return_value = 0

  if clang_diff_files:
    # Locate the clang-format binary in the checkout
    try:
      clang_format_tool = clang_format.FindClangFormatToolInChromiumTree()
    except clang_format.NotFoundError as e:
      DieWithError(e)

    if opts.full:
      cmd = [clang_format_tool]
      if not opts.dry_run and not opts.diff:
        cmd.append('-i')
      stdout = RunCommand(cmd + clang_diff_files, cwd=top_dir)
      if opts.diff:
        sys.stdout.write(stdout)
    else:
      env = os.environ.copy()
      env['PATH'] = str(os.path.dirname(clang_format_tool))
      try:
        script = clang_format.FindClangFormatScriptInChromiumTree(
            'clang-format-diff.py')
      except clang_format.NotFoundError as e:
        DieWithError(e)

      cmd = [sys.executable, script, '-p0']
      if not opts.dry_run and not opts.diff:
        cmd.append('-i')

      diff_cmd = BuildGitDiffCmd('-U0', upstream_commit, clang_diff_files)
      diff_output = RunGit(diff_cmd)

      stdout = RunCommand(cmd, stdin=diff_output, cwd=top_dir, env=env)
      if opts.diff:
        sys.stdout.write(stdout)
      if opts.dry_run and len(stdout) > 0:
        return_value = 2

  # Similar code to above, but using yapf on .py files rather than clang-format
  # on C/C++ files
  if opts.python:
    yapf_tool = gclient_utils.FindExecutable('yapf')
    if yapf_tool is None:
      DieWithError('yapf not found in PATH')

    if opts.full:
      if python_diff_files:
        cmd = [yapf_tool]
        if not opts.dry_run and not opts.diff:
          cmd.append('-i')
        stdout = RunCommand(cmd + python_diff_files, cwd=top_dir)
        if opts.diff:
          sys.stdout.write(stdout)
    else:
      # TODO(sbc): yapf --lines mode still has some issues.
      # https://github.com/google/yapf/issues/154
      DieWithError('--python currently only works with --full')

  # Dart's formatter does not have the nice property of only operating on
  # modified chunks, so hard code full.
  if dart_diff_files:
    try:
      command = [dart_format.FindDartFmtToolInChromiumTree()]
      if not opts.dry_run and not opts.diff:
        command.append('-w')
      command.extend(dart_diff_files)

      stdout = RunCommand(command, cwd=top_dir)
      if opts.dry_run and stdout:
        return_value = 2
    except dart_format.NotFoundError as e:
      print('Warning: Unable to check Dart code formatting. Dart SDK not '
            'found in this checkout. Files in other languages are still '
            'formatted.')

  # Format GN build files. Always run on full build files for canonical form.
  if gn_diff_files:
    cmd = ['gn', 'format' ]
    if opts.dry_run or opts.diff:
      cmd.append('--dry-run')
    for gn_diff_file in gn_diff_files:
      gn_ret = subprocess2.call(cmd + [gn_diff_file],
                                shell=sys.platform == 'win32',
                                cwd=top_dir)
      if opts.dry_run and gn_ret == 2:
        return_value = 2  # Not formatted.
      elif opts.diff and gn_ret == 2:
        # TODO this should compute and print the actual diff.
        print("This change has GN build file diff for " + gn_diff_file)
      elif gn_ret != 0:
        # For non-dry run cases (and non-2 return values for dry-run), a
        # nonzero error code indicates a failure, probably because the file
        # doesn't parse.
        DieWithError("gn format failed on " + gn_diff_file +
                     "\nTry running 'gn format' on this file manually.")

  return return_value


@subcommand.usage('<codereview url or issue id>')
def CMDcheckout(parser, args):
  """Checks out a branch associated with a given Rietveld or Gerrit issue."""
  _, args = parser.parse_args(args)

  if len(args) != 1:
    parser.print_help()
    return 1

  issue_arg = ParseIssueNumberArgument(args[0])
  if not issue_arg.valid:
    parser.print_help()
    return 1
  target_issue = str(issue_arg.issue)

  def find_issues(issueprefix):
    output = RunGit(['config', '--local', '--get-regexp',
                     r'branch\..*\.%s' % issueprefix],
                     error_ok=True)
    for key, issue in [x.split() for x in output.splitlines()]:
      if issue == target_issue:
        yield re.sub(r'branch\.(.*)\.%s' % issueprefix, r'\1', key)

  branches = []
  for cls in _CODEREVIEW_IMPLEMENTATIONS.values():
    branches.extend(find_issues(cls.IssueConfigKey()))
  if len(branches) == 0:
    print('No branch found for issue %s.' % target_issue)
    return 1
  if len(branches) == 1:
    RunGit(['checkout', branches[0]])
  else:
    print('Multiple branches match issue %s:' % target_issue)
    for i in range(len(branches)):
      print('%d: %s' % (i, branches[i]))
    which = raw_input('Choose by index: ')
    try:
      RunGit(['checkout', branches[int(which)]])
    except (IndexError, ValueError):
      print('Invalid selection, not checking out any branch.')
      return 1

  return 0


def CMDlol(parser, args):
  # This command is intentionally undocumented.
  print(zlib.decompress(base64.b64decode(
      'eNptkLEOwyAMRHe+wupCIqW57v0Vq84WqWtXyrcXnCBsmgMJ+/SSAxMZgRB6NzE'
      'E2ObgCKJooYdu4uAQVffUEoE1sRQLxAcqzd7uK2gmStrll1ucV3uZyaY5sXyDd9'
      'JAnN+lAXsOMJ90GANAi43mq5/VeeacylKVgi8o6F1SC63FxnagHfJUTfUYdCR/W'
      'Ofe+0dHL7PicpytKP750Fh1q2qnLVof4w8OZWNY')))
  return 0


class OptionParser(optparse.OptionParser):
  """Creates the option parse and add --verbose support."""
  def __init__(self, *args, **kwargs):
    optparse.OptionParser.__init__(
        self, *args, prog='git cl', version=__version__, **kwargs)
    self.add_option(
        '-v', '--verbose', action='count', default=0,
        help='Use 2 times for more debugging info')

  def parse_args(self, args=None, values=None):
    options, args = optparse.OptionParser.parse_args(self, args, values)
    levels = [logging.WARNING, logging.INFO, logging.DEBUG]
    logging.basicConfig(level=levels[min(options.verbose, len(levels) - 1)])
    return options, args


def main(argv):
  if sys.hexversion < 0x02060000:
    print('\nYour python version %s is unsupported, please upgrade.\n' %
          (sys.version.split(' ', 1)[0],), file=sys.stderr)
    return 2

  # Reload settings.
  global settings
  settings = Settings()

  colorize_CMDstatus_doc()
  dispatcher = subcommand.CommandDispatcher(__name__)
  try:
    return dispatcher.execute(OptionParser(), argv)
  except auth.AuthenticationError as e:
    DieWithError(str(e))
  except urllib2.HTTPError as e:
    if e.code != 500:
      raise
    DieWithError(
        ('AppEngine is misbehaving and returned HTTP %d, again. Keep faith '
          'and retry or visit go/isgaeup.\n%s') % (e.code, str(e)))
  return 0


if __name__ == '__main__':
  # These affect sys.stdout so do it outside of main() to simplify mocks in
  # unit testing.
  fix_encoding.fix_encoding()
  setup_color.init()
  try:
    sys.exit(main(sys.argv[1:]))
  except KeyboardInterrupt:
    sys.stderr.write('interrupted\n')
    sys.exit(1)