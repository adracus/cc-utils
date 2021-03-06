# Copyright (c) 2019 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed
# under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
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

from functools import partial
from urllib.parse import urlencode, quote_plus
import json
import time
from typing import List

import requests

from util import not_empty, not_none, none, urljoin
from http_requests import check_http_code, mount_default_adapter
from .model import (
    AnalysisResult,
    ProcessingStatus,
    ScanResult,
    Triage,
    TriageScope,
)


class ProtecodeApiRoutes(object):
    '''
    calculates API routes (URLs) for a subset of the URL endpoints exposed by
    "Protecode" (https://protecode.mo.sap.corp)

    Not intended to be instantiated by users of this module
    '''

    def __init__(self, base_url):
        self._base_url = not_empty(base_url)
        self._api_url = partial(self._url, 'api')
        self._rest_url = partial(self._url, 'rest')

    def _url(self, *parts):
        return urljoin(self._base_url, *parts)

    def apps(self, group_id, custom_attribs={}):
        url = self._api_url('apps')
        if group_id:
            url = urljoin(url, str(group_id))

        search_query = ' '.join(['meta:' + str(k) + '=' + str(v) for k,v in custom_attribs.items()])
        if search_query:
            url += '?' + urlencode({'q': search_query})

        return url

    def login(self):
        return self._url('login') + '/'

    def groups(self):
        return self._api_url('groups')

    def upload(self, file_name):
        return self._api_url('upload', quote_plus(file_name))

    def product(self, product_id: int):
        return self._api_url('product', str(product_id))

    def product_custom_data(self, product_id: int):
        return self._api_url('product', str(product_id), 'custom-data')

    def rescan(self, product_id):
        return self._api_url('product', str(product_id), 'rescan')

    def triage(self):
        return self._api_url('triage', 'vulnerability/')

    # ---- "rest" routes (undocumented API)

    def scans(self, product_id: int):
        return self._rest_url('scans', str(product_id)) + '/'


class ProtecodeApi(object):
    def __init__(self, api_routes, basic_credentials, tls_verify=False):
        self._routes = not_none(api_routes)
        self._credentials = not_none(basic_credentials)
        self._auth = (basic_credentials.username(), basic_credentials.passwd())
        self._tls_verify = tls_verify
        self._session_id = None
        self._session = requests.Session()
        mount_default_adapter(
            session=self._session,
        )

        self._csrf_token = None

    def set_maximum_concurrent_connections(self, maximum_concurrent_connections: int):
        # mount new adapter with new parameters
        mount_default_adapter(
            session=self._session,
            max_pool_size=maximum_concurrent_connections,
        )

    @check_http_code
    def _request(self, method, *args, **kwargs):
        if 'headers' in kwargs:
            headers = kwargs['headers']
            del kwargs['headers']
        else:
            headers = {}

        if 'url' in kwargs:
            url = kwargs.get('url')
        else:
            url = args[0]

        if self._session_id:
            cookies = {
                'sessionid': self._session_id,
                'csrftoken': self._csrf_token,
            }
            headers['X-CSRFTOKEN'] = self._csrf_token
            headers['referer'] = url
        else:
            cookies = None

        auth = self._auth

        return partial(
            method,
            verify=self._tls_verify,
            auth=auth,
            headers=headers,
            cookies=cookies,
        )(*args, **kwargs)

    @check_http_code
    def _get(self, *args, **kwargs):
        return self._request(self._session.get, *args, **kwargs)

    @check_http_code
    def _post(self, *args, **kwargs):
        return self._request(self._session.post, *args, **kwargs)

    @check_http_code
    def _put(self, *args, **kwargs):
        return self._request(self._session.put, *args, **kwargs)

    @check_http_code
    def _delete(self, *args, **kwargs):
        return self._request(self._session.delete, *args, **kwargs)

    @check_http_code
    def _patch(self, *args, **kwargs):
        return self._request(self._session.patch, *args, **kwargs)

    def _metadata_dict(self, custom_attributes):
        '''
        replaces "invalid" underscore characters (setting metadata fails silently if
        those are present). Note: dash characters are implcitly converted to underscore
        by protecode.
        '''
        return {
            'META-' + str(k).replace('_', '-'): v
            for k,v in custom_attributes.items()
        }

    def upload(self, application_name, group_id, data, custom_attribs={}) -> AnalysisResult:
        url = self._routes.upload(file_name=application_name)
        headers = {'Group': str(group_id)}
        headers.update(self._metadata_dict(custom_attribs))

        result = self._put(
            url=url,
            headers=headers,
            data=data,
        )

        return AnalysisResult(raw_dict=result.json().get('results'))

    def delete_product(self, product_id: int):
        url = self._routes.product(product_id=product_id)

        self._delete(
            url=url,
        )

    def scan_result(self, product_id: int) -> AnalysisResult:
        url = self._routes.product(product_id=product_id)

        result = self._get(
            url=url,
        ).json()['results']

        return AnalysisResult(raw_dict=result)

    def wait_for_scan_result(self, product_id: int, polling_interval_seconds=60):
        def scan_finished():
            result = self.scan_result(product_id=product_id)
            if result.status() in (ProcessingStatus.READY, ProcessingStatus.FAILED):
                return result
            return False

        result = scan_finished()
        while not result:
            # keep polling until result is ready
            time.sleep(polling_interval_seconds)
            result = scan_finished()
        return result

    def list_apps(self, group_id, custom_attribs={}) -> List[AnalysisResult]:
        url = self._routes.apps(group_id=group_id, custom_attribs=custom_attribs)

        result = self._get(
            url=url,
        )
        return [AnalysisResult(p) for p in result.json().get('products')]

    def set_metadata(self, product_id: int, custom_attribs: dict):
        url = self._routes.product_custom_data(product_id=product_id)
        headers = self._metadata_dict(custom_attribs)

        result = self._post(
            url=url,
            headers=headers,
        )
        return result.json()

    def metadata(self, product_id: int):
        url = self._routes.product_custom_data(product_id=product_id)

        result = self._post(
            url=url,
            headers={},
        )
        return result.json().get('custom_data', {})

    def add_triage(
        self,
        triage: Triage,
        scope: TriageScope=None,
        product_id=None,
        group_id=None
    ):
        '''
        adds an existing Protecode triage to a specified target. The existing triage is usually
        retrieved from an already uploaded product (which is represented by `AnalysisResult`).
        This method is offered to support "transporting" existing triages.

        Note that - depending on the effective target scope, the `product_id`, `group_id` formal
        parameters are either required or forbidden.

        @param triage: the triage to "copy"
        @param scope: if given, overrides the triage's scope
        @param product_id: target product_id. required iff scope in FN, FH, R
        @param group_id: target group_id. required iff scope is G(ROUP)
        '''
        url = self._routes.triage()

        # if no scope is set, use the one from passed triage
        scope = scope if scope else triage.scope()

        # depending on the scope, different arguments are required
        if scope == TriageScope.ACCOUNT_WIDE:
            none(product_id)
            none(group_id)
        elif scope in (TriageScope.FILE_NAME, TriageScope.FILE_HASH, TriageScope.RESULT):
            not_none(product_id)
            none(group_id)
        elif scope == TriageScope.GROUP:
            none(product_id)
            not_none(group_id)
        else:
            raise NotImplementedError()

        # "copy" data from existing triage
        triage_dict = {
            'component': triage.component_name(),
            'version': triage.component_version(),
            'vulns': [triage.vulnerability_id()],
            'scope': triage.scope().value,
            'reason': triage.reason(),
            'description': triage.description(),
        }

        if product_id:
            triage_dict['product_id'] = product_id

        if group_id:
            triage_dict['group_id'] = group_id

        self._put(
            url=url,
            json=triage_dict,
        )

    # --- "rest" routes (undocumented API)

    def login(self):
        url = self._routes.login()

        result = self._post(
            url=url,
            data={
                'username': self._credentials.username(),
                'password': self._credentials.passwd(),
            },
            auth=None,
        )

        # session-id is returned in first response
        if not result.history:
            raise RuntimeError('authentication failed:' + str(result.text))

        relevant_response = result.history[0]

        self._session_id = relevant_response.cookies.get('sessionid')
        self._csrf_token = relevant_response.cookies.get('csrftoken')
        if not self._session_id:
            raise RuntimeError('authentication failed: ' + str(relevant_response.text))

    def scan_result_short(self, product_id: int):
        url = self._routes.scans(product_id)

        result = self._get(
            url=url,
        )
        return ScanResult(raw_dict=result.json())

    def set_product_name(self, product_id: int, name: str):
        url = self._routes.scans(product_id)

        self._patch(
            url=url,
            data=json.dumps({'name': name,}),
            headers={'Content-Type': 'application/json'},
        )

    def rescan(self, product_id: int):
        url = self._routes.rescan(product_id)
        self._post(
            url=url,
        )


def from_cfg(protecode_cfg, connection_pool_size=12):
    not_none(protecode_cfg)
    routes = ProtecodeApiRoutes(base_url=protecode_cfg.api_url())
    api = ProtecodeApi(
        api_routes=routes,
        basic_credentials=protecode_cfg.credentials(),
        tls_verify=protecode_cfg.tls_verify(),
    )
    return api
