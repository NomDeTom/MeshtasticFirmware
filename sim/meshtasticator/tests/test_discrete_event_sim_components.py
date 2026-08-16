import unittest

import lib.discrete_event_sim_components as dsc

class TestDiscreteSimComponents(unittest.TestCase):
    '''Mostly sanity-checking of very simple sim components
    '''

    # If other classes get more complex than simple bundles of data, add tests

    def test_counter(self):
        '''counter sanity tests
        '''
        c = dsc.Counter()

        self.assertEqual(c.peek(), 0, 'expected default counter start')

        n = c.get()
        self.assertEqual(n, 1, 'expected first sequence number')

        c.get()
        c.get()
        n = c.get()
        self.assertEqual(n, 4, 'expected counter increases')

        n = c.peek()
        self.assertEqual(n, 4, 'peeking does not change counter value, and returns correct value')

        c = dsc.Counter(10)
        n = c.get()
        self.assertEqual(n, 11, 'can change default start')

