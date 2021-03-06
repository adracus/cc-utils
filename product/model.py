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

import abc
import urllib.parse
from enum import Enum

from model.base import ModelBase, ModelValidationError
from protecode.model import AnalysisResult
from util import not_none, urljoin, check_type

#############################################################################
## product descriptor model

# the asset name component descriptors are stored as part of component github releases
COMPONENT_DESCRIPTOR_ASSET_NAME = 'component_descriptor.yaml'


class InvalidComponentReferenceError(ModelValidationError):
    pass


class ProductModelBase(ModelBase):
    '''
    Base class for product model classes.

    Not intended to be instantiated.
    '''

    def __init__(self, **kwargs):
        raw_dict = {**kwargs}
        super().__init__(raw_dict=raw_dict)


class DependencyBase(ModelBase):
    '''
    Base class for dependencies

    Not intended to be instantiated.
    '''
    def _required_attributes(self):
        return {'name', 'version'}

    def name(self):
        return self.raw.get('name')

    def version(self):
        return self.raw.get('version')

    @abc.abstractmethod
    def type_name(self):
        '''
        returns the dependency type name (component, generic, ..)
        '''
        raise NotImplementedError

    def __eq__(self, other):
        if not isinstance(other, DependencyBase):
            return False
        return self.raw == other.raw

    def __hash__(self):
        return hash(tuple(sorted(self.raw.items())))


class Product(ProductModelBase):
    @staticmethod
    def from_dict(raw_dict: dict):
        return Product(**raw_dict)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'components' not in self.raw:
            self.raw['components'] = []

    def _optional_attributes(self):
        return {'components'}

    def components(self):
        return (Component(raw_dict=raw_dict) for raw_dict in self.raw['components'])

    def component(self, component_reference):
        if not isinstance(component_reference, ComponentReference):
            name, version = component_reference
            component_reference = ComponentReference.create(name=name, version=version)

        return next(
            filter(lambda c: c == component_reference, self.components()),
            None
        )

    def add_component(self, component):
        self.raw['components'].append(component.raw)


class ComponentName(object):
    @staticmethod
    def validate_component_name(name: str):
        not_none(name)

        if len(name) == 0:
            raise InvalidComponentReferenceError('Component name must not be empty')

        # valid component names are fully qualified github repository URLs without a schema
        # (e.g. github.com/example_org/example_name)
        if urllib.parse.urlparse(name).scheme:
            raise InvalidComponentReferenceError('Component name must not contain schema')

        # prepend dummy schema so that urlparse will parse away the hostname
        parsed = urllib.parse.urlparse('dummy://' + name)

        if not parsed.hostname:
            raise InvalidComponentReferenceError(name)

        path_parts = parsed.path.strip('/').split('/')
        if not len(path_parts) == 2:
            raise InvalidComponentReferenceError(
                'Component name must end with github repository path'
            )

        return name

    @staticmethod
    def from_github_repo_url(repo_url):
        parsed = urllib.parse.urlparse(repo_url)
        if parsed.scheme:
            component_name = repo_url = urljoin(*parsed[1:3])
        else:
            component_name = repo_url

        return ComponentName(name=component_name)

    def __init__(self, name: str):
        self._name = ComponentName.validate_component_name(name)

    def name(self):
        return self._name

    def github_host(self):
        return self.name().split('/')[0]

    def github_organisation(self):
        return self.name().split('/')[1]

    def github_repo(self):
        return self.name().split('/')[2]

    def github_repo_path(self):
        return self.github_organisation() + '/' + self.github_repo()

    def config_name(self):
        return self.github_host().replace('.', '_')

    def github_url(self):
        # hard-code schema to https
        return 'https://' + self.github_host()

    def github_repo_url(self):
        # hard-code schema to https
        return 'https://' + self.name()

    def __eq__(self, other):
        if not isinstance(other, ComponentName):
            return False
        return self.name() == other.name()

    def __hash__(self):
        return hash((self.name()))


class ComponentReference(DependencyBase):
    @staticmethod
    def create(name, version):
        return ComponentReference(
            raw_dict={'name': name, 'version':version},
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._componentName = ComponentName(kwargs['raw_dict']['name'])

    def type_name(self):
        return 'component'

    def version(self):
        return self.raw['version']

    def github_host(self):
        return self._componentName.github_host()

    def github_organisation(self):
        return self._componentName.github_organisation()

    def github_repo(self):
        return self._componentName.github_repo()

    def github_repo_path(self):
        return self._componentName.github_repo_path()

    def config_name(self):
        return self._componentName.config_name()

    def validate(self):
        ComponentName.validate_component_name(self.raw.get('name'))
        super().validate()

    def __eq__(self, other):
        if not isinstance(other, ComponentReference):
            return False
        return (self.name(), self.version()) == (other.name(), other.version())

    def __hash__(self):
        return hash((self.name(), self.version()))

    def __repr__(self):
        return f'ComponentReference: {self.name()}:{self.version()}'


class ContainerImageReference(DependencyBase):
    @staticmethod
    def create(name, version):
        return ContainerImageReference(
            raw_dict={'name': name, 'version': version}
        )

    def type_name(self):
        return 'container_image'


class ContainerImage(ContainerImageReference):
    @staticmethod
    def create(name, version, image_reference):
        return ContainerImage(
            raw_dict={'name':name, 'version':version, 'image_reference':image_reference}
        )

    def _required_attributes(self):
        return super()._required_attributes() | {'image_reference'}

    def image_reference(self):
        return self.raw.get('image_reference')


class WebDependencyReference(DependencyBase):
    @staticmethod
    def create(name, version):
        return WebDependencyReference(
            raw_dict={'name': name, 'version': version}
        )

    def type_name(self):
        return 'web'


class WebDependency(WebDependencyReference):
    @staticmethod
    def create(name, version, url):
        return WebDependency(
            raw_dict={'name':name, 'version':version, 'url':url}
        )

    def type_name(self):
        return 'web'

    def _required_attributes(self):
        return super()._required_attributes() | {'url'}

    def url(self):
        return self.raw.get('url')


class GenericDependencyReference(DependencyBase):
    @staticmethod
    def create(name, version):
        return GenericDependencyReference(raw_dict={'name':name, 'version':version})

    def type_name(self):
        return 'generic'


class GenericDependency(GenericDependencyReference):
    @staticmethod
    def create(name, version):
        return GenericDependency(raw_dict={'name':name, 'version':version})


class Component(ComponentReference):
    @staticmethod
    def create(name, version):
        return Component(raw_dict={'name':name, 'version':version})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.raw.get('dependencies'):
            self.raw['dependencies'] = {}

    def _optional_attributes(self):
        return {'dependencies'}

    def dependencies(self):
        return ComponentDependencies(raw_dict=self.raw['dependencies'])


class ComponentDependencies(ModelBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for attrib_name in ('container_images', 'components', 'web', 'generic'):
            if attrib_name not in self.raw:
                self.raw[attrib_name] = []

    def _optional_attributes(self):
        return {'container_images', 'components', 'web', 'generic'}

    def container_images(self):
        return (ContainerImage(raw_dict=raw_dict) for raw_dict in self.raw.get('container_images'))

    def components(self):
        return (ComponentReference(raw_dict=raw_dict) for raw_dict in self.raw.get('components'))

    def web_dependencies(self):
        return (WebDependency(raw_dict=raw_dict) for raw_dict in self.raw.get('web'))

    def generic_dependencies(self):
        return (GenericDependency(raw_dict=raw_dict) for raw_dict in self.raw.get('generic'))

    def references(self, type_name: str):
        reference_ctor = reference_type(type_name).create
        if type_name == 'container_image':
            attrib = 'container_images'
        elif type_name == 'component':
            attrib = 'components'
        elif type_name == 'web':
            attrib = 'web'
        elif type_name == 'generic':
            attrib = 'generic'
        else:
            raise ValueError('unknown refererence type: ' + str(type_name))

        for ref_dict in self.raw.get(attrib):
            yield reference_ctor(name=ref_dict['name'], version=ref_dict['version'])

    def add_container_image_dependency(self, container_image):
        if container_image not in self.container_images():
            self.raw['container_images'].append(container_image.raw)

    def add_component_dependency(self, component_reference):
        if component_reference not in self.components():
            self.raw['components'].append(component_reference.raw)

    def add_web_dependency(self, web_dependency):
        if web_dependency not in self.web_dependencies():
            self.raw['web'].append(web_dependency.raw)

    def add_generic_dependency(self, generic_dependency):
        if generic_dependency not in self.generic_dependencies():
            self.raw['generic'].append(generic_dependency.raw)


def reference_type(name: str):
    check_type(name, str)
    if name == 'component':
        return ComponentReference
    if name == 'container_image':
        return ContainerImageReference
    if name == 'generic':
        return GenericDependencyReference
    if name == 'web':
        return WebDependencyReference
    raise ValueError('unknown dependency type name: ' + str(name))


#############################################################################
## upload result model

class UploadStatus(Enum):
    SKIPPED = 1
    PENDING = 2
    DONE = 4


class UploadResult(object):
    def __init__(
            self,
            status: UploadStatus,
            component: Component,
            container_image: ContainerImage,
            result: AnalysisResult,
    ):
        self.status = not_none(status)
        self.component = not_none(component)
        self.container_image = not_none(container_image)
        if result:
            self.result = result
        else:
            self.result = None

    def __str__(self):
        return '{c}:{ir} - {s}'.format(
            c=self.component.name(),
            ir=self.container_image.image_reference(),
            s=self.status
        )
