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
from concurrent.futures import ThreadPoolExecutor

from util import CliHints, parse_yaml_file, ctx, info
from product.model import Product
from product.scanning import ProtecodeUtil
import protecode.client

def upload_product_images(
    protecode_cfg_name: str,
    product_cfg_file: CliHints.existing_file(),
    parallel_jobs: int=4,
    ):
    cfg_factory = ctx().cfg_factory()
    protecode_cfg = cfg_factory.protecode(protecode_cfg_name)
    protecode_api = protecode.client.from_cfg(protecode_cfg)
    protecode_util = ProtecodeUtil(protecode_api=protecode_api, group_id=5)

    product_model = Product.from_dict(
        name='gardener-product',
        raw_dict=parse_yaml_file(product_cfg_file)
    )

    executor = ThreadPoolExecutor(max_workers=parallel_jobs)
    tasks = _create_tasks(product_model, protecode_util)
    results = executor.map(lambda task: task(), tasks)

    for result in results:
        info('result: {r}'.format(r=result))

def _create_tasks(product_model, protecode_util):
    for component in product_model.components():
        info('processing component: {c}:{v}'.format(c=component.name(), v=component.version()))
        for container_image in component.container_images():
            info('processing container image: {c}:{ci}:{v}'.format(
                c=component.name(),
                ci=container_image.name(),
                v=container_image.version(),
                )
            )
            def upload_image():
                result = protecode_util.upload_image(container_image, component)
                return result
            yield upload_image


