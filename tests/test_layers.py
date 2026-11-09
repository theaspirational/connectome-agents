from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'roles/connectome_agent/module_utils'))
from connectome_config import merge_layers


class LayerTests(unittest.TestCase):
    def test_order_recursive_lists_and_input_immutability(self):
        layers = [{'x': {'a': 1, 'b': 1}, 'list': [1, 2]}, {'x': {'a': 2}},
                  {'x': {'a': 3}, 'list': []}, {'x': {'a': 4}}]
        before = deepcopy(layers)
        result = merge_layers(layers)
        self.assertEqual(result['x'], {'a': 4, 'b': 1})
        self.assertEqual(result['list'], [])
        self.assertEqual(layers, before)

    def test_null_tombstones_prevent_resurrection(self):
        result = merge_layers([{'x': {'a': 1, 'b': 2}, 'y': 3},
                               {'x': None, 'y': None}, {}, {'x': {'c': 4}}])
        self.assertEqual(result['x'], {'c': 4})
        self.assertNotIn('y', result)
        result = merge_layers([{'x': {'a': 1, 'b': 2}}, {}, {}, {'x': {'a': None}}])
        self.assertEqual(result['x'], {'b': 2})

    def test_disabled_removed_from_build_input(self):
        result = merge_layers([{'connectors': {'a': {'enabled': True, 'repo': 'test'}},
                                'modules': {'b': {'enabled': True}}}, {}, {},
                               {'connectors': {'a': {'enabled': False}},
                                'modules': {'b': {'enabled': False}}}])
        self.assertEqual(result['connectors'], {})
        self.assertEqual(result['modules'], {})

    def test_disable_can_be_overridden_by_higher_layer(self):
        result = merge_layers([{'modules': {'a': {'enabled': False}}}, {}, {},
                               {'modules': {'a': {'enabled': True}}}])
        self.assertIn('a', result['modules'])

    def test_bad_layers_and_nonboolean_enabled_rejected(self):
        for layers in ([{}, {}], [{}, {}, [], {}],
                       [{}, {}, {}, {'modules': {'x': {'enabled': 'false'}}}]):
            with self.assertRaises(ValueError):
                merge_layers(layers)


if __name__ == '__main__':
    unittest.main()
