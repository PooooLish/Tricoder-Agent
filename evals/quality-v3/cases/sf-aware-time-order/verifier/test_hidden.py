from copy import deepcopy
import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from time_order import sort_events
        x=[{'timestamp':'2024-01-02T00:00:00Z','id':2},{'timestamp':'2024-01-01T00:00:00Z','id':1}]
        before = deepcopy(x)
        expected = deepcopy([x[1], x[0]])
        self.assertEqual(expected, sort_events(x))
        self.assertEqual(before, x)
        # id is only a test label, not a required business field.
        x = [{'timestamp': '2024-01-01T00:00:00Z', 'nested': {'items': [1, {'v': 2}]}}]
        expected = deepcopy(x)
        self.assertEqual(expected, sort_events(x))
        self.assertEqual(expected, x)

    def test_boundary(self):
        from time_order import sort_events
        self.assertEqual([],sort_events([]))
        x=[{'timestamp':'2024-01-01T02:00:00+02:00','id':1},{'timestamp':'2024-01-01T00:00:00Z','id':2},{'timestamp':'2023-12-31T19:00:00-05:00','id':3}]
        before = deepcopy(x)
        self.assertEqual(before, sort_events(x))
        self.assertEqual(before, x)

    def test_regression(self):
        from time_order import sort_events
        x=[{'timestamp':'2024-01-01T00:30:00-02:00','id':'late','nested':{'x':[{'v':1},2]}},{'timestamp':'2024-01-01T01:00:00+02:00','id':'early','nested':{'x':[]}}]
        before = deepcopy(x)
        expected = deepcopy([x[1], x[0]])
        self.assertEqual(expected, sort_events(x))
        self.assertEqual(before, x)
