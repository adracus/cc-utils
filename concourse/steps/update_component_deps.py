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

import pathlib

import product.model
import util

# must point to component_descriptor directory


def current_product_descriptor():
    component_descriptor_dir = pathlib.Path(util.check_env('COMPONENT_DESCRIPTOR_DIR')).absolute()
    component_descriptor = component_descriptor_dir.joinpath('component_descriptor')
    raw = util.parse_yaml_file(component_descriptor)
    return product.model.Product.from_dict(raw)
