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

import unittest
import pytest

import product.util as util
import product.model as model

# functions under test
greatest_crefs = util.greatest_references
diff_components = util.diff_components


def component_ref(name, version, prefix='gh.com/o/'):
    return model.ComponentReference.create(name=prefix + name, version=version)


@pytest.fixture
def cref():
    return component_ref


class ProductUtilTest(unittest.TestCase):
    def setUp(self):
        self.cref1 = component_ref(name='c1', version='1.2.3')
        self.cref2 = component_ref(name='c2', version='2.2.3')

    def test_greatest_references(self):
        # trivial case: single cref
        result = list(greatest_crefs((self.cref1,)))
        self.assertSequenceEqual(result, (self.cref1,))

        # trivial case: two crefs, different
        result = list(greatest_crefs((self.cref2, self.cref1)))
        self.assertSequenceEqual(result, (self.cref2, self.cref1))

        # non-trivial case: duplicate component name, different versions
        cref1_greater = model.ComponentReference.create(name=self.cref1.name(), version='9.0.9')
        cref1_lesser = model.ComponentReference.create(name=self.cref1.name(), version='0.0.1')

        result = set(greatest_crefs((self.cref1, self.cref2, cref1_greater, cref1_lesser)))
        self.assertSetEqual({cref1_greater, self.cref2}, result)

    def test_greatest_references_argument_validation(self):
        # None-check
        with self.assertRaises(ValueError):
            next(greatest_crefs(None))

        # reject elements that are not component references
        with self.assertRaises(ValueError):
            non_component_element = 42 # int is not of type product.model.ComponentReference
            next(greatest_crefs((self.cref1, non_component_element)))


def test_diff_components(cref):
    left_components = (
        cref('c1', '1.2.3'),
        cref('c2', '1.2.3'),
        cref('c3', '1.2.3'),
    )
    right_components = (
        cref('c1', '2.2.3'), # version changed
        cref('c2', '1.2.3'), # no change
        #cref('c3', '1.2.3'), # missing on right
        cref('c4', '1.2.3'), # added on right
    )

    result = diff_components(left_components, right_components)

    assert result.crefs_only_left == {cref('c3', '1.2.3'), cref('c1', '1.2.3')}
    assert result.crefs_only_right == {cref('c4', '1.2.3'), cref('c1', '2.2.3')}
    assert result.crefpairs_version_changed == {(cref('c1', '1.2.3'), cref('c1', '2.2.3'))}
    assert result.names_only_left == {'gh.com/o/c3'}
    assert result.names_only_right == {'gh.com/o/c4'}
    assert result.names_version_changed == {'gh.com/o/c1'}
