# Copyright (c) 2018 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import time
import math
import shlex

from ensure import ensure, ensure_annotations
from urllib3.exceptions import ReadTimeoutError, ProtocolError

from kubernetes import client, watch
from kubernetes.client import (
        V1ObjectMeta, # todo: mv-out this import
)
import kubernetes.client
from kubernetes.config.kube_config import KubeConfigLoader

from util import fail, info, verbose, ensure_file_exists, ensure_not_empty, ensure_not_none
from kube.ctx import Ctx


def __add_module_command_args(parser):
    parser.add_argument('--kubeconfig', required=False)
    return parser


ctx = Ctx()

def create_namespace(namespace: str):
    namespace_helper = ctx.namespace_helper()
    return namespace_helper.create_namespace(namespace)

def delete_namespace(namespace):
    namespace_helper = ctx.namespace_helper()
    namespace_helper.delete_namespace(namespace)

def delete_namespace_unless_shoots_present(namespace):
    ensure_not_empty(namespace)
    custom_api = ctx.create_custom_api()

    result = custom_api.list_namespaced_custom_object(
      group='garden.sapcloud.io',
      version='v1',
      namespace=namespace,
      plural='shoots'
    )
    if len(result['items']) > 0:
        fail('namespace contained {} shoot(s) - not removing'.format(len(result)))

    delete_namespace(namespace)

def copy_secrets(from_ns: str, to_ns: str, secret_names: [str]):
    for arg in [from_ns, to_ns, secret_names]:
        ensure_not_empty(arg)
    info('args: from: {}, to: {}, names: {}'.format(from_ns, to_ns, secret_names))

    core_api = ctx.create_core_api()

    # new metadata used to overwrite the ones from retrieved secrets
    metadata = V1ObjectMeta(namespace=to_ns)

    for name in secret_names:
        secret = core_api.read_namespaced_secret(name=name, namespace=from_ns, export=True)
        metadata.name=name
        secret.metadata = metadata
        core_api.create_namespaced_secret(namespace=to_ns, body=secret)

def wait_for_ns(namespace):
    ensure_not_empty(namespace)

    core_api = ctx.create_core_api()
    w = watch.Watch()
    for e in w.stream(core_api.list_namespace, _request_timeout=120):
        # check if 'tis our namespace
        ns = e['object'].metadata.name
        if not ns == namespace:
            continue
        info(e['type'])
        if not e['type'] == 'ADDED':
            continue # ignore
        w.stop()

def wait_for_shoot_cluster_operation_success(namespace:str, shoot_name:str, optype:str, timeout_seconds:int=120):
    ensure_not_empty(namespace)
    ensure_not_empty(shoot_name)
    optype = ensure_not_empty(optype).lower()
    info('will wait for a maximum of {} minute(s) for cluster {} to reach state {}d'.format(
      math.ceil(timeout_seconds/60), shoot_name, optype)
    )

    def on_event(event)->(bool,str):
        shoot = event['object']
        status = shoot.get('status', None)
        if status:
            last_operation = status['lastOperation']
            operation_type = last_operation['type'].lower()
            operation_state = last_operation['state'].lower()
            operation_progress = last_operation['progress']
        else:
            # state is unknown, yet
            return False,'unknown'

        if shoot_name != shoot['metadata']['name']:
            return False,None
        if optype != operation_type:
            info('not in right optype: ' + operation_type)
            return False,operation_state
        # we reached the right operation type
        if operation_state == 'succeeded':
            return True,operation_state
        info('operation {} is {} - progress: {}%'.format(optype, operation_state, operation_progress))
        return False,operation_state

    try:
        _wait_for_shoot(namespace, on_event=on_event, expected_result='succeeded', timeout_seconds=timeout_seconds)
        info('Shoot cluster successfully reached state {}d'.format(optype))
    except RuntimeError as rte:
        fail('Shoot cluster reached a final error state: ' + str(rte))
    except ReadTimeoutError:
        fail('Shoot cluster did not reach state {}d within {} minute(s)'.format(optype, math.ceil(timeout_seconds/60)))

def wait_for_shoot_cluster_to_become_healthy(namespace:str, shoot_name:str, timeout_seconds:int=120):
    ensure_not_empty(namespace)
    ensure_not_empty(shoot_name)
    info('will wait for a maximum of {} minute(s) for cluster {} to become healthy'.format(
      math.ceil(timeout_seconds/60),
      shoot_name
      )
    )
    def on_event(event)->(bool,str):
        # TODO: this is copy-pasta from wait_for_shoot_cluster_operation_success
        #   --> remove redundancy
        shoot = event['object']
        status = shoot.get('status', None)
        if not status:
            # state is unknown, yet
            return False,'unknown'
        if shoot_name != shoot['metadata']['name']:
            return False,None

        conditions = status.get('conditions',None)
        if not conditions:
            return False,'unknown'

        health_status = [(c['type'],True if c['status'] == 'True' else False) for c in conditions]
        all_healthy = all(map(lambda s:s[1], health_status))
        if all_healthy:
            return True,'healthy'

        unhealthy_components = [c for c,s in health_status if not s]
        info('the following components are still unhealthy: {}'.format(' '.join(unhealthy_components)))
        return False,'unhealthy'

    try:
        _wait_for_shoot(namespace, on_event=on_event, expected_result='healthy', timeout_seconds=timeout_seconds)
        info('Shoot cluster became healthy')
    except RuntimeError as rte:
        fail('cluster did not become healthy')
    except ReadTimeoutError:
        fail('cluster did not become healthy within {} minute(s)'.format(math.ceil(timeout_seconds/60)))


def _wait_for_shoot(namespace, on_event, expected_result, timeout_seconds:int=120):
    ensure_not_empty(namespace)
    start_time = int(time.time())

    custom_api = ctx.create_custom_api()
    w = watch.Watch()
    # very, very sad: workaround until fixed:
    #    https://github.com/kubernetes-incubator/client-python/issues/124
    # (after about a minute, "some" watches (e.g. not observed when watching namespaces),
    # return without an error.
    # apart from being ugly, this has the downside that existing events will repeatedly be
    # received each time the watch is re-applied
    should_exit = False
    result = None
    while not should_exit and (start_time + timeout_seconds) > time.time():
        try:
            for e in w.stream(custom_api.list_namespaced_custom_object,
              group='garden.sapcloud.io',
              version='v1beta1',
              namespace=namespace,
              plural='shoots',
              # we need to reduce the request-timeout due to our workaround
              _request_timeout=(timeout_seconds - int(time.time() - start_time))
              ):
                should_exit,result = on_event(e)
                if should_exit:
                    w.stop()
                    if result != expected_result:
                        raise RuntimeError(result)
                    return
        except ConnectionResetError as cre:
            # ignore connection errors against k8s api endpoint (these may be temporary)
            info('connection reset error from k8s API endpoint - ignored: ' + str(cre))
        except ProtocolError as err:
            verbose('http connection error - ignored')
        except KeyError as err:
            verbose("key {} not yet available - ignored".format(str(err)))
    # handle case where timeout was exceeded, but w.stream returned erroneously (see bug
    # description above)
    raise RuntimeError(result)


def retrieve_controller_manager_log_entries(
  pod_name:str,
  namespace:str,
  only_if_newer_than_rfc3339_ts:str=None,
  filter_for_shoot_name:str=None,
  minimal_loglevel:str=None
  ):
    ensure_not_empty(namespace)
    ensure_not_empty(pod_name)
    if filter_for_shoot_name:
        ensure_not_empty(filter_for_shoot_name)

    kwargs = {'name': pod_name, 'namespace': namespace}

    passed_seconds = None
    if only_if_newer_than_rfc3339_ts:
        import dateutil.parser
        import time
        date = dateutil.parser.parse(only_if_newer_than_rfc3339_ts)
        # determine difference to "now"
        now = time.time()
        passed_seconds = int(now - date.timestamp())
        if passed_seconds < 1:
            passed_seconds = 1
        kwargs['since_seconds']=passed_seconds

    api = ctx.create_core_api()
    raw_log = api.read_namespaced_pod_log(
     **kwargs
    )

    # pylint: disable=no-member
    lines = raw_log.split('\n')
    # pylint: enable=no-member

    # filter our helm logs (format: yyyy/mm/dd ...)
    helm_log_re = re.compile(r'^\d{4}/\d{2}/\d{2}')
    lines = filter(lambda l: not helm_log_re.match(l), lines)
    def parse_line(line):
        parts = shlex.split(line)
        parts = filter(lambda s: '=' in s, parts)
        return dict(map(lambda s: s.split('=', 1), parts))
    parsed = map(parse_line, lines)
    if minimal_loglevel:
        log_levels = {'debug': 0, 'info': 1, 'warning': 2, 'error': 3, 'fatal': 4, 'panic': 5}
        minimal = log_levels[minimal_loglevel]
        parsed = filter(lambda p: log_levels[p.get('level', 'error')] >= minimal, parsed)
    if filter_for_shoot_name:
        parsed = filter(lambda p: p.get('shoot',None) == filter_for_shoot_name, parsed)
    for p in parsed:
        keys = ['time', 'level', 'msg', 'shoot']
        output = ' '.join([k + '="' + p.get(k, 'None') + '"' for k in keys])
        print(output)


def create_gcr_secret(
  namespace: str,
  name: str,
  secret_file: str,
  email: str,
  user_name: str='_json_key',
  server_url: str='https://eu.gcr.io'
):
    ensure_file_exists(secret_file)
    secret_helper = ctx.secret_helper()
    with open(secret_file, 'r') as fh:
        gcr_secret = fh.read()
        secret_helper.create_gcr_secret(
          namespace=namespace,
          name=name,
          password=gcr_secret,
          email=email,
          user_name=user_name,
          server_url=server_url
        )

def get_cluster_version_info():
    api = ctx.create_version_api()
    return api.get_code()