"""
Tests for :mod:`otter.controller`
"""
from datetime import timedelta, datetime

import inspect
import mock

from twisted.internet import defer
from twisted.trial.unittest import TestCase

from otter import controller
from otter.json_schema import group_examples
from otter.models.interface import IScalingGroup
from otter.util.timestamp import MIN
from otter.test.utils import DeferredTestMixin, iMock, patch


class CalculateDeltaTestCase(TestCase):
    """
    Tests for :func:`otter.controller.calculate_delta`
    """

    def setUp(self):
        """
        Set the max
        """
        patcher = mock.patch.object(controller, 'MAX_ENTITIES', new=10)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_positive_change_within_min_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and max are given,
        and the desired number of servers fall between the min and the max,
        then ``calculate_delta`` returns a delta that is just the policy change.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 300}
        fake_state = {'active': dict.fromkeys(range(5)), 'pending': {}}

        self.assertEqual(5, controller.calculate_delta(fake_state, fake_config,
                                                       fake_policy))

    def test_positive_change_will_hit_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and max are given,
        and the desired number is above the max,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = {'active': dict.fromkeys(range(4)),
                      'pending': dict.fromkeys(range(4))}

        self.assertEqual(2, controller.calculate_delta(fake_state, fake_config,
                                                       fake_policy))

    def test_positive_change_but_at_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and max are given,
        and the current active + pending is at the max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = {'active': dict.fromkeys(range(5)),
                      'pending': dict.fromkeys(range(5))}

        self.assertEqual(0, controller.calculate_delta(fake_state, fake_config,
                                                       fake_policy))

    def test_positive_change_but_at_default_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and no max,
        and the current active + pending is at the default max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': None}
        fake_state = {'active': dict.fromkeys(range(5)),
                      'pending': dict.fromkeys(range(5))}

        self.assertEqual(0, controller.calculate_delta(fake_state, fake_config,
                                                       fake_policy))

    def test_negative_change_within_min_max(self):
        """
        If the policy is a scale down by a fixed number,
        and a min and max are given,
        and the desired number of servers fall between the min and the max,
        then ``calculate_delta`` returns a delta that is just the policy change.
        """
        fake_policy = {'change': -5}
        fake_config = {'minEntities': 0, 'maxEntities': 30}
        fake_state = {'active': dict.fromkeys(range(10)), 'pending': {}}

        self.assertEqual(-5, controller.calculate_delta(fake_state, fake_config,
                                                        fake_policy))

    def test_negative_change_will_hit_max(self):
        """
        If the policy is a scale down by a fixed number,
        and a min and max are given,
        and the desired number is below the min,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'change': -5}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = {'active': dict.fromkeys(range(4)),
                      'pending': dict.fromkeys(range(4))}

        self.assertEqual(-3, controller.calculate_delta(fake_state, fake_config,
                                                        fake_policy))

    def test_negative_change_but_at_min(self):
        """
        If the policy is a scale down by a fixed number,
        and a min and max are given,
        and the current active + pending is at the min already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'change': -5}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = {'active': {}, 'pending': dict.fromkeys(range(5))}

        self.assertEqual(0, controller.calculate_delta(fake_state, fake_config,
                                                       fake_policy))

    def test_percent_change_fails(self):
        """
        If 'change' is not in the scaling policy, then calculate delta doesn't
        know how to handle the policy
        """
        fake_policy = {'changePercent': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = {'active': {}, 'pending': {}}

        self.assertRaises(NotImplementedError,
                          controller.calculate_delta,
                          fake_state, fake_config, fake_policy)


class CheckCooldownsTestCase(TestCase):
    """
    Tests for :func:`otter.controller.check_cooldowns`
    """

    def mock_now(self, seconds_after_min):
        """
        Set :func:`otter.util.timestamp.now` to return a timestamp that is
        so many seconds after `datetime.min`.  Tests using this should set
        the last touched time to be MIN.
        """
        def _fake_now(timezone):
            fake_datetime = datetime.min + timedelta(seconds=seconds_after_min)
            return fake_datetime.replace(tzinfo=timezone)

        self.datetime = patch(self, 'otter.controller.datetime', spec=['now'])
        self.datetime.now.side_effect = _fake_now

    def test_check_cooldowns_global_cooldown_and_policy_cooldown_pass(self):
        """
        If both the global cooldown and policy cooldown are sufficiently long
        ago, ``check_cooldowns`` returns True.
        """
        self.mock_now(30)
        fake_config = fake_policy = {'cooldown': 0}
        fake_state = {'groupTouched': MIN, 'policyTouched': {'pol': MIN}}
        self.assertTrue(controller.check_cooldowns(fake_state, fake_config,
                                                   fake_policy, 'pol'))

    def test_check_cooldowns_global_cooldown_passes_policy_never_touched(self):
        """
        If the global cooldown was sufficiently long ago and the policy has
        never been executed (hence there is no touched time for the policy),
        ``check_cooldowns`` returns True.
        """
        self.mock_now(30)
        fake_config = {'cooldown': 0}
        fake_policy = {'cooldown': 10000000}
        fake_state = {'groupTouched': MIN, 'policyTouched': {}}
        self.assertTrue(controller.check_cooldowns(fake_state, fake_config,
                                                   fake_policy, 'pol'))

    def test_check_cooldowns_no_policy_ever_executed(self):
        """
        If no policy has ever been executed (hence there is no global touch
        time), ``check_cooldowns`` returns True.
        """
        self.mock_now(30)
        fake_config = {'cooldown': 1000000000}
        fake_policy = {'cooldown': 10000000}
        fake_state = {'groupTouched': None, 'policyTouched': {}}
        self.assertTrue(controller.check_cooldowns(fake_state, fake_config,
                                                   fake_policy, 'pol'))

    def test_check_cooldowns_global_cooldown_fails(self):
        """
        If the last time a (any) policy was executed is too recent,
        ``check_cooldowns`` returns False.
        """
        self.mock_now(1)
        fake_config = {'cooldown': 30}
        fake_policy = {'cooldown': 1000000000}
        fake_state = {'groupTouched': MIN, 'policyTouched': {}}
        self.assertFalse(controller.check_cooldowns(fake_state, fake_config,
                                                    fake_policy, 'pol'))

    def test_check_cooldowns_policy_cooldown_fails(self):
        """
        If the last time THIS policy was executed is too recent,
        ``check_cooldowns`` returns False.
        """
        self.mock_now(1)
        fake_config = {'cooldown': 1000000000}
        fake_policy = {'cooldown': 30}
        fake_state = {'groupTouched': MIN, 'policyTouched': {'pol': MIN}}
        self.assertFalse(controller.check_cooldowns(fake_state, fake_config,
                                                    fake_policy, 'pol'))


class ControllerTestCase(DeferredTestMixin, TestCase):
    """
    Tests for :func:`otter.controller.maybe_execute_scaling_policy`
    """

    def setUp(self):
        """
        Mock every method in the controller, and also the supervisor.
        Individual tests can stop the patching for the method it is testing.

        Also build a mock model that can be used for testing.
        """
        self.mocks = {}
        methods = [name for name, func in
                   inspect.getmembers(controller, inspect.isfunction)]
        for thing in ['supervisor'] + methods:
            self.mocks = patch(self, 'otter.controller.{0}'.format(thing))

        self.mock_log = mock.MagicMock()

        self.state = {
            "active": {},
            "pending": {},
            "groupTouched": None,
            "policyTouched": {},
            "paused": False
        }
        self.config = group_examples.config()[0]
        self.policy = {
            "name": "scale up by 10",
            "change": 10,
            "cooldown": 5
        }

        self.group = iMock(IScalingGroup, tenant_id='tenant', uuid='group')
        self.group.view_config.return_value = defer.succeed(self.config)
        self.group.get_policy.return_value = defer.succeed(self.policy)
        self.group.view_state.return_value = defer.succeed(self.state)

    def test_maybe_execute_scaling_policy_success(self):
        """
        If all cooldowns are all fine and ``execute_launch_config`` does not fail,
        Return value should be whatever ``execute_launch_config`` returns.
        """
        self.patches['maybe_execute_scaling_policy'].stop()

        self.mocks['calculate_delta'].return_value = 1
        self.mocks['check_cooldowns'].return_value = True
        self.mocks['execute_launch_config'].return_value = defer.succeed(
            'this should be returned')

        d = controller.maybe_execute_scaling_policy(self.mock_log, 'transaction',
                                                    self.group, 'pol1')

        result = self.successResultOf(d)
        self.assertEqual(result, 'this should be returned')

        self.mocks['check_cooldowns'].assert_called_once_with(
            self.state, self.config, self.policy, 'pol1')
        self.mocks['calculate_delta'].assert_called_once_with(
            self.state, self.config, self.policy)
        self.mocks['execute_launch_config'].assert_called_once_with(
            self.mock_log, 'transaction', self.state, self.group,
            self.mocks['calculate_delta'].return_value)

    def test_maybe_execute_scaling_policy_cooldown_failure(self):
        """
        If cooldowns are not fine, ``maybe_execute_scaling_policy`` raises a
        ``CannotExecutePolicyError`` exception
        """
        self.patches['maybe_execute_scaling_policy'].stop()

        self.mocks['calculate_delta'].return_value = 1
        self.mocks['check_cooldowns'].return_value = False
        self.mocks['execute_launch_config'].return_value = defer.succeed(
            'this should NOT be returned')

        d = controller.maybe_execute_scaling_policy(self.mock_log, 'transaction',
                                                    self.group, 'pol1')
        self.assert_deferred_failed(d, controller.CannotExecutePolicyError)
