#!/usr/bin/env python
# Copyright 2016 the V8 project authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""
V8 correctness fuzzer launcher script.
"""

import argparse
import hashlib
import itertools
import json
import os
import re
import sys
import traceback

import v8_commands
import v8_suppressions

CONFIGS = dict(
  default=[],
  validate_asm=['--validate-asm'], # Maybe add , '--disable-asm-warnings'
  fullcode=['--nocrankshaft', '--turbo-filter=~'],
  noturbo=['--turbo-filter=~', '--noturbo-asm'],
  noturbo_opt=['--always-opt', '--turbo-filter=~', '--noturbo-asm'],
  ignition_staging=['--ignition-staging'],
  ignition_turbo=['--ignition-staging', '--turbo'],
  ignition_turbo_opt=['--ignition-staging', '--turbo', '--always-opt'],
)

# Timeout in seconds for one d8 run.
TIMEOUT = 3

# Return codes.
RETURN_PASS = 0
RETURN_FAIL = 2

BASE_PATH = os.path.dirname(os.path.abspath(__file__))
PREAMBLE = [
  os.path.join(BASE_PATH, 'v8_mock.js'),
  os.path.join(BASE_PATH, 'v8_suppressions.js'),
]

FLAGS = ['--abort_on_stack_overflow', '--expose-gc', '--allow-natives-syntax',
         '--invoke-weak-callbacks', '--omit-quit', '--es-staging']

SUPPORTED_ARCHS = ['ia32', 'x64', 'arm', 'arm64']

# Output for suppressed failure case.
FAILURE_HEADER_TEMPLATE = """#
# V8 correctness failure
# V8 correctness configs: %(configs)s
# V8 correctness sources: %(sources)s
# V8 correctness suppression: %(suppression)s
"""

# Extended output for failure case. The 'CHECK' is for the minimizer.
FAILURE_TEMPLATE = FAILURE_HEADER_TEMPLATE + """#
# CHECK
#
# Compared %(first_config_label)s with %(second_config_label)s
#
# Flags of %(first_config_label)s:
%(first_config_flags)s
# Flags of %(second_config_label)s:
%(second_config_flags)s
#
# Difference:
%(difference)s
#
### Start of configuration %(first_config_label)s:
%(first_config_output)s
### End of configuration %(first_config_label)s
#
### Start of configuration %(second_config_label)s:
%(second_config_output)s
### End of configuration %(second_config_label)s
"""


def parse_args():
  parser = argparse.ArgumentParser()
  parser.add_argument(
    '--random-seed', type=int, required=True,
    help='random seed passed to both runs')
  parser.add_argument(
      '--first-arch', help='first architecture', default='x64')
  parser.add_argument(
      '--second-arch', help='second architecture', default='x64')
  parser.add_argument(
      '--first-config', help='first configuration', default='fullcode')
  parser.add_argument(
      '--second-config', help='second configuration', default='fullcode')
  parser.add_argument(
      '--first-d8', default='d8',
      help='optional path to first d8 executable, '
           'default: bundled in the same directory as this script')
  parser.add_argument(
      '--second-d8',
      help='optional path to second d8 executable, default: same as first')
  parser.add_argument('testcase', help='path to test case')
  options = parser.parse_args()

  # Ensure we make a sane comparison.
  assert (options.first_arch != options.second_arch or
          options.first_config != options.second_config) , (
      'Need either arch or config difference.')
  assert options.first_arch in SUPPORTED_ARCHS
  assert options.second_arch in SUPPORTED_ARCHS
  assert options.first_config in CONFIGS
  assert options.second_config in CONFIGS

  # Ensure we have a test case.
  assert (os.path.exists(options.testcase) and
          os.path.isfile(options.testcase)), (
      'Test case %s doesn\'t exist' % options.testcase)

  resources_path = os.path.join(
      os.path.dirname(options.testcase),
      'resources' + os.path.basename(options.testcase)[len('fuzz'):])
  assert os.path.exists(resources_path), (
      'Resources file %s doesn\'t exist' % resources_path)

  with open(resources_path) as f:
    resources = f.read().strip().splitlines()
    assert len(resources) == 1
    options.meta_data_path = os.path.join(
        os.path.dirname(resources_path), resources[0])
  assert os.path.exists(options.meta_data_path), (
      'Metadata %s doesn\'t exist' % options.meta_data_path)

  # Use first d8 as default for second d8.
  options.second_d8 = options.second_d8 or options.first_d8

  # Ensure absolute paths.
  options.first_d8 = os.path.abspath(options.first_d8)
  options.second_d8 = os.path.abspath(options.second_d8)

  # Ensure executables exist.
  assert os.path.exists(options.first_d8)
  assert os.path.exists(options.second_d8)

  # Ensure we use different executables when we claim we compare
  # different architectures.
  # TODO(machenbach): Infer arch from gn's build output.
  if options.first_arch != options.second_arch:
    assert options.first_d8 != options.second_d8

  return options


def test_pattern_bailout(testcase, ignore_fun):
  """Print failure state and return if ignore_fun matches testcase."""
  with open(testcase) as f:
    bug = (ignore_fun(f.read()) or '').strip()
    if bug:
      print FAILURE_HEADER_TEMPLATE % dict(
          configs='', sources='', suppression=bug)
      return True
  return False


def pass_bailout(output, step_number):
  """Print info and return if in timeout or crash pass states."""
  if output.HasTimedOut():
    # Dashed output, so that no other clusterfuzz tools can match the
    # words timeout or crash.
    print '# V8 correctness - T-I-M-E-O-U-T %d' % step_number
    return True
  if output.HasCrashed():
    print '# V8 correctness - C-R-A-S-H %d' % step_number
    return True
  return False


def fail_bailout(output, ignore_by_output_fun):
  """Print failure state and return if ignore_by_output_fun matches output."""
  bug = (ignore_by_output_fun(output.stdout) or '').strip()
  if bug:
    print FAILURE_HEADER_TEMPLATE % dict(
        configs='', sources='', suppression=bug)
    return True
  return False


def main():
  options = parse_args()

  # Suppressions are architecture and configuration specific.
  suppress = v8_suppressions.get_suppression(
      options.first_arch, options.first_config,
      options.second_arch, options.second_config,
  )

  if test_pattern_bailout(options.testcase, suppress.ignore):
    return RETURN_FAIL

  # Get metadata.
  with open(options.meta_data_path) as f:
    metadata = json.load(f)

  common_flags = FLAGS + ['--random-seed', str(options.random_seed)]
  first_config_flags = common_flags + CONFIGS[options.first_config]
  second_config_flags = common_flags + CONFIGS[options.second_config]

  def run_d8(d8, config_flags):
    args = [d8] + config_flags + PREAMBLE + [options.testcase]
    if d8.endswith('.py'):
      # Wrap with python in tests.
      args = [sys.executable] + args
    return v8_commands.Execute(
        args,
        cwd=os.path.dirname(options.testcase),
        timeout=TIMEOUT,
    )

  first_config_output = run_d8(options.first_d8, first_config_flags)

  # Early bailout based on first run's output.
  if pass_bailout(first_config_output, 1):
    return RETURN_PASS
  if fail_bailout(first_config_output, suppress.ignore_by_output1):
    return RETURN_FAIL

  second_config_output = run_d8(options.second_d8, second_config_flags)

  # Bailout based on second run's output.
  if pass_bailout(second_config_output, 2):
    return RETURN_PASS
  if fail_bailout(second_config_output, suppress.ignore_by_output2):
    return RETURN_FAIL

  difference = suppress.diff(
      first_config_output.stdout, second_config_output.stdout)
  if difference:
    # The first three entries will be parsed by clusterfuzz. Format changes
    # will require changes on the clusterfuzz side.
    first_config_label = '%s,%s' % (options.first_arch, options.first_config)
    second_config_label = '%s,%s' % (options.second_arch, options.second_config)
    hsh = lambda x: hashlib.sha1(x).hexdigest()[:8]
    print FAILURE_TEMPLATE % dict(
        configs='%s:%s' % (first_config_label, second_config_label),
        sources=','.join(map(hsh, metadata['sources'])),
        suppression='', # We can't tie bugs to differences.
        first_config_label=first_config_label,
        second_config_label=second_config_label,
        first_config_flags=' '.join(first_config_flags),
        second_config_flags=' '.join(second_config_flags),
        first_config_output=first_config_output.stdout,
        second_config_output=second_config_output.stdout,
        difference=difference,
    )
    return RETURN_FAIL

  # TODO(machenbach): Figure out if we could also return a bug in case there's
  # no difference, but one of the line suppressions has matched - and without
  # the match there would be a difference.

  print '# V8 correctness - pass'
  return RETURN_PASS


if __name__ == "__main__":
  try:
    result = main()
  except SystemExit:
    # Make sure clusterfuzz reports internal errors and wrong usage.
    # Use one label for all internal and usage errors.
    print FAILURE_HEADER_TEMPLATE % dict(
        configs='', sources='', suppression='wrong_usage')
    result = RETURN_FAIL
  except Exception as e:
    print FAILURE_HEADER_TEMPLATE % dict(
        configs='', sources='', suppression='internal_error')
    print '# Internal error: %s' % e
    traceback.print_exc(file=sys.stdout)
    result = RETURN_FAIL

  sys.exit(result)
