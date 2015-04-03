"""
Tests for :mod:`otter.controller`
"""
from datetime import datetime, timedelta

from effect import (
    ComposedDispatcher,
    TypeDispatcher,
    sync_perform,
    sync_performer)
from effect.testing import SequenceDispatcher

import mock

from testtools.matchers import ContainsDict, Equals

from twisted.internet import defer
from twisted.trial.unittest import SynchronousTestCase

from otter import controller
from otter.cloud_client import (
    NoSuchServerError,
    get_server_details,
    set_nova_metadata_item)
from otter.convergence.model import DRAINING_METADATA
from otter.convergence.service import (
    get_convergence_starter, set_convergence_starter)
from otter.models.intents import GetScalingGroupInfo
from otter.models.interface import (
    GroupState, IScalingGroup, NoSuchPolicyError)
from otter.supervisor import (
    CannotDeleteServerBelowMinError,
    EvictServerFromScalingGroup,
    ServerNotFoundError)
from otter.test.utils import (
    iMock,
    matches,
    mock_log,
    patch,
    raise_,
    test_dispatcher)
from otter.util.retry import (
    Retry, ShouldDelayAndRetry, exponential_backoff_interval, retry_times)
from otter.util.timestamp import MIN


class CalculateDeltaTestCase(SynchronousTestCase):
    """
    Tests for :func:`otter.controller.calculate_delta`
    """

    def setUp(self):
        """
        Set the max and add a mock log
        """
        patcher = mock.patch.object(controller, 'MAX_ENTITIES', new=10)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_log = mock.Mock()

    def get_state(self, active, pending):
        """
        Only care about the active and pending values, so generate a whole
        :class:`GroupState` with other fake info
        """
        return GroupState(1, 1, "test", active, pending, None, {}, False)

    def test_positive_change_within_min_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and max are given,
        and the desired number of servers fall between the min and the max,
        then ``calculate_delta`` returns a delta that is just the policy
        change.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 300}
        fake_state = self.get_state(dict.fromkeys(range(5)), {})

        self.assertEqual(5, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_positive_change_will_hit_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and max are given,
        and the desired number is above the max,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(4)),
                                    dict.fromkeys(range(4)))

        self.assertEqual(2, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_positive_change_but_at_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and max are given,
        and the current active + pending is at the max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(5)),
                                    dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_positive_change_but_at_default_max(self):
        """
        If the policy is a scale up by a fixed number,
        and a min and no max,
        and the current active + pending is at the default max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'change': 5}
        fake_config = {'minEntities': 0, 'maxEntities': None}
        fake_state = self.get_state(dict.fromkeys(range(5)),
                                    dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_negative_change_within_min_max(self):
        """
        If the policy is a scale down by a fixed number,
        and a min and max are given,
        and the desired number of servers fall between the min and the max,
        then ``calculate_delta`` returns a delta that is just the policy
        change.
        """
        fake_policy = {'change': -5}
        fake_config = {'minEntities': 0, 'maxEntities': 30}
        fake_state = self.get_state(dict.fromkeys(range(10)), {})

        self.assertEqual(-5, controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_negative_change_will_hit_min(self):
        """
        If the policy is a scale down by a fixed number,
        and a min and max are given,
        and the desired number is below the min,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'change': -5}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(4)),
                                    dict.fromkeys(range(4)))

        self.assertEqual(-3, controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_negative_change_but_at_min(self):
        """
        If the policy is a scale down by a fixed number,
        and a min and max are given,
        and the current active + pending is at the min already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'change': -5}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = self.get_state({}, dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_percent_positive_change_within_min_max(self):
        """
        If the policy is a scale up by x% and a min and max are given,
        and the desired number of servers fall between the min and the max,
        then ``calculate_delta`` returns a delta that is just the policy
        change.
        """
        fake_policy = {'changePercent': 20}
        fake_config = {'minEntities': 0, 'maxEntities': 300}
        fake_state = self.get_state(dict.fromkeys(range(5)), {})

        self.assertEqual(1, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 6)

    def test_percent_positive_change_will_hit_max(self):
        """
        If the policy is a scale up by x% and a min and max are given,
        and the desired number is above the max,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'changePercent': 75}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(4)),
                                    dict.fromkeys(range(4)))

        self.assertEqual(2, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_percent_positive_change_but_at_max(self):
        """
        If the policy is a scale up by x% and a min and max are given,
        and the current active + pending is at the max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'changePercent': 50}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(5)),
                                    dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_percent_positive_change_but_at_default_max(self):
        """
        If the policy is a scale up by x% and a min and no max,
        and the current active + pending is at the default max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'changePercent': 50}
        fake_config = {'minEntities': 0, 'maxEntities': None}
        fake_state = self.get_state(dict.fromkeys(range(5)),
                                    dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_percent_negative_change_within_min_max(self):
        """
        If the policy is a scale down by x% and a min and max are given,
        and the desired number of servers fall between the min and the max,
        then ``calculate_delta`` returns a delta that is just the policy
        change.
        """
        fake_policy = {'changePercent': -50}
        fake_config = {'minEntities': 0, 'maxEntities': 30}
        fake_state = self.get_state(dict.fromkeys(range(10)), {})

        self.assertEqual(-5, controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_percent_negative_change_will_hit_min(self):
        """
        If the policy is a scale down by x% and a min and max are given,
        and the desired number is below the min,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'changePercent': -80}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(4)),
                                    dict.fromkeys(range(4)))

        self.assertEqual(-3, controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_percent_negative_change_but_at_min(self):
        """
        If the policy is a scale down by x% and a min and max are given,
        and the current active + pending is at the min already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'changePercent': -50}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = self.get_state({}, dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_percent_rounding(self):
        """
        When 'changePercent' is x%, ``calculate_delta`` rounds up to an integer
        away from zero.
        """
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state({}, dict.fromkeys(range(5)))

        test_cases = [
            (50, 8, 3), (5, 6, 1), (75, 9, 4),
            (-50, 2, -3), (-5, 4, -1), (-75, 1, -4)]

        for change_percent, expected_desired, expected_delta in test_cases:
            fake_policy = {'changePercent': change_percent}
            self.assertEqual(expected_delta,
                             controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
            self.assertEqual(fake_state.desired, expected_desired)

    def test_desired_positive_change_within_min_max(self):
        """
        If the policy is based on desiredCapacity and a min and max are given,
        and the desired number of servers fall between the min and the max,
        then ``calculate_delta`` returns a delta that is just the policy
        change.
        """
        fake_policy = {'desiredCapacity': 25}
        fake_config = {'minEntities': 0, 'maxEntities': 300}
        fake_state = self.get_state(dict.fromkeys(range(5)), {})

        self.assertEqual(20, controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
        self.assertEqual(fake_state.desired, 25)

    def test_desired_positive_change_will_hit_max(self):
        """
        If the policy is based on desiredCapacity and a min and max are given,
        and the desired number is above the max,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'desiredCapacity': 15}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(4)),
                                    dict.fromkeys(range(4)))

        self.assertEqual(2, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_desired_positive_change_but_at_max(self):
        """
        If the policy is based on desiredCapacity  and a min and max are given,
        and the current active + pending is at the max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'desiredCapacity': 15}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(5)),
                                    dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_desired_positive_change_but_at_default_max(self):
        """
        If the policy is based on desiredCapacity and a min and no max,
        and the current active + pending is at the default max already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'desiredCapacity': 15}
        fake_config = {'minEntities': 0, 'maxEntities': None}
        fake_state = self.get_state(dict.fromkeys(range(5)),
                                    dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 10)

    def test_desired_will_hit_min(self):
        """
        If the policy is based on desiredCapacity and a min and max are given,
        and the desired number is below the min,
        then ``calculate_delta`` returns a truncated delta.
        """
        fake_policy = {'desiredCapacity': 3}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(4)),
                                    dict.fromkeys(range(4)))

        self.assertEqual(-3, controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_desired_at_min(self):
        """
        If the policy is based on desiredCapacity and a min and max are given,
        and the current active + pending is at the min already,
        then ``calculate_delta`` returns 0.
        """
        fake_policy = {'desiredCapacity': 3}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = self.get_state({}, dict.fromkeys(range(5)))

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_no_change_or_percent_or_desired_fails(self):
        """
        If 'change' or 'changePercent' or 'desiredCapacity' is not there in
        scaling policy, then ``calculate_delta`` doesn't know how to handle the
        policy and raises a ValueError
        """
        fake_policy = {'changeNone': 5}
        fake_config = {'minEntities': 0, 'maxEntities': 10}
        fake_state = self.get_state({}, {})

        self.assertRaises(AttributeError,
                          controller.calculate_delta,
                          self.mock_log, fake_state, fake_config, fake_policy)

    def test_zero_change_within_min_max(self):
        """
        If 'change' is zero, but the current active + pending is within the min
        and max, then ``calculate_delta`` returns 0
        """
        fake_policy = {'change': 0}
        fake_config = {'minEntities': 1, 'maxEntities': 10}
        fake_state = self.get_state(dict.fromkeys(range(5)), {})

        self.assertEqual(0, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_zero_change_below_min(self):
        """
        If 'change' is zero, but the current active + pending is below the min,
        then ``calculate_delta`` returns the difference between
        current + pending and the min
        """
        fake_policy = {'change': 0}
        fake_config = {'minEntities': 5, 'maxEntities': 10}
        fake_state = self.get_state({}, {})

        self.assertEqual(5, controller.calculate_delta(self.mock_log,
                                                       fake_state,
                                                       fake_config,
                                                       fake_policy))
        self.assertEqual(fake_state.desired, 5)

    def test_zero_change_above_max(self):
        """
        If 'change' is zero, but the current active + pending is above the max,
        then ``calculate_delta`` returns the negative difference between the
        current + pending and the max
        """
        fake_policy = {'change': 0}
        fake_config = {'minEntities': 0, 'maxEntities': 2}
        fake_state = self.get_state(dict.fromkeys(range(5)), {})

        self.assertEqual(-3, controller.calculate_delta(self.mock_log,
                                                        fake_state,
                                                        fake_config,
                                                        fake_policy))
        self.assertEqual(fake_state.desired, 2)

    def test_logs_relevant_information(self):
        """
        Log is called with at least the constrained desired capacity and the
        delta
        """
        fake_policy = {'change': 0}
        fake_config = {'minEntities': 1, 'maxEntities': 10}
        fake_state = self.get_state({}, {})
        controller.calculate_delta(self.mock_log, fake_state, fake_config,
                                   fake_policy)
        args, kwargs = self.mock_log.msg.call_args
        self.assertEqual(fake_state.desired, 1)
        self.assertEqual(
            args, (('calculating delta {current_active} + {current_pending}'
                    ' -> {constrained_desired_capacity}'),))
        self.assertEqual(kwargs, matches(ContainsDict({
            'server_delta': Equals(1),
            'constrained_desired_capacity': Equals(1)})))


class CheckCooldownsTestCase(SynchronousTestCase):
    """
    Tests for :func:`otter.controller.check_cooldowns`
    """

    def setUp(self):
        """
        Generate a mock log
        """
        self.mock_log = mock.MagicMock()

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

    def get_state(self, group_touched, policy_touched):
        """
        Only care about the group_touched and policy_touched values, so
        generate a whole :class:`GroupState` with other fake info
        """
        return GroupState(1, 1, "test", {}, {}, group_touched, policy_touched, False)

    def test_check_cooldowns_global_cooldown_and_policy_cooldown_pass(self):
        """
        If both the global cooldown and policy cooldown are sufficiently long
        ago, ``check_cooldowns`` returns True.
        """
        self.mock_now(30)
        fake_config = fake_policy = {'cooldown': 0}
        fake_state = self.get_state(MIN, {'pol': MIN})
        self.assertTrue(controller.check_cooldowns(self.mock_log, fake_state,
                                                   fake_config, fake_policy,
                                                   'pol'))

    def test_check_cooldowns_global_cooldown_passes_policy_never_touched(self):
        """
        If the global cooldown was sufficiently long ago and the policy has
        never been executed (hence there is no touched time for the policy),
        ``check_cooldowns`` returns True.
        """
        self.mock_now(30)
        fake_config = {'cooldown': 0}
        fake_policy = {'cooldown': 10000000}
        fake_state = self.get_state(MIN, {})
        self.assertTrue(controller.check_cooldowns(self.mock_log, fake_state,
                                                   fake_config, fake_policy,
                                                   'pol'))

    def test_check_cooldowns_no_policy_ever_executed(self):
        """
        If no policy has ever been executed (hence there is no global touch
        time), ``check_cooldowns`` returns True.
        """
        self.mock_now(10000)
        fake_config = {'cooldown': 1000}
        fake_policy = {'cooldown': 100}
        fake_state = self.get_state(None, {})
        self.assertTrue(controller.check_cooldowns(self.mock_log, fake_state,
                                                   fake_config, fake_policy,
                                                   'pol'))

    def test_check_cooldowns_global_cooldown_fails(self):
        """
        If the last time a (any) policy was executed is too recent,
        ``check_cooldowns`` returns False.
        """
        self.mock_now(1)
        fake_config = {'cooldown': 30}
        fake_policy = {'cooldown': 1000000000}
        fake_state = self.get_state(MIN, {})
        self.assertFalse(controller.check_cooldowns(self.mock_log, fake_state,
                                                    fake_config, fake_policy,
                                                    'pol'))

    def test_check_cooldowns_policy_cooldown_fails(self):
        """
        If the last time THIS policy was executed is too recent,
        ``check_cooldowns`` returns False.
        """
        self.mock_now(1)
        fake_config = {'cooldown': 1000000000}
        fake_policy = {'cooldown': 30}
        fake_state = self.get_state(MIN, {'pol': MIN})
        self.assertFalse(controller.check_cooldowns(self.mock_log, fake_state,
                                                    fake_config, fake_policy,
                                                    'pol'))


class ObeyConfigChangeTestCase(SynchronousTestCase):
    """
    Tests for :func:`otter.controller.obey_config_change`
    """

    def setUp(self):
        """
        Mock execute_launch_config and calculate_delta
        """
        self.calculate_delta = patch(self, 'otter.controller.calculate_delta')
        self.execute_launch_config = patch(
            self, 'otter.controller.execute_launch_config',
            return_value=defer.succeed(None))
        self.exec_scale_down = patch(
            self, 'otter.controller.exec_scale_down',
            return_value=defer.succeed(None))

        self.log = mock.MagicMock()
        self.state = mock.MagicMock(spec=['get_capacity'])
        self.state.get_capacity.return_value = {
            'desired_capacity': 5,
            'pending_capacity': 2,
            'active_capacity': 3
        }

        self.group = iMock(IScalingGroup, tenant_id='tenant', uuid='group')

    def test_parameters_bound_to_log(self):
        """
        Relevant values are bound to the log.
        """
        self.calculate_delta.return_value = 0
        controller.obey_config_change(self.log, 'transaction-id',
                                      'config', self.group, self.state, 'launch')
        self.log.bind.assert_called_once_with(scaling_group_id=self.group.uuid)

    def test_zero_delta_nothing_happens_state_is_returned(self):
        """
        If the delta is zero, ``execute_launch_config`` is not called and
        ``obey_config_change`` returns the current state
        """
        self.calculate_delta.return_value = 0
        d = controller.obey_config_change(self.log, 'transaction-id',
                                          'config', self.group, self.state, 'launch')
        self.assertIs(self.successResultOf(d), self.state)
        self.assertEqual(self.execute_launch_config.call_count, 0)

    def test_positive_delta_state_is_returned_if_execute_successful(self):
        """
        If the delta is positive, ``execute_launch_config`` is called and if
        it is successful, ``obey_config_change`` returns the current state
        """
        self.calculate_delta.return_value = 5
        d = controller.obey_config_change(self.log, 'transaction-id',
                                          'config', self.group, self.state,
                                          'launch')
        self.assertIs(self.successResultOf(d), self.state)
        self.execute_launch_config.assert_called_once_with(
            self.log.bind.return_value.bind.return_value,
            'transaction-id', self.state, 'launch',
            self.group, 5)

    def test_nonzero_delta_execute_errors_propagated(self):
        """
        ``obey_config_change`` propagates any errors ``execute_launch_config``
        raises
        """
        self.calculate_delta.return_value = 5
        self.execute_launch_config.return_value = defer.fail(Exception('meh'))
        d = controller.obey_config_change(self.log, 'transaction-id',
                                          'config', self.group, self.state,
                                          'launch')
        f = self.failureResultOf(d)
        self.assertTrue(f.check(Exception))
        self.execute_launch_config.assert_called_once_with(
            self.log.bind.return_value.bind.return_value,
            'transaction-id', self.state, 'launch',
            self.group, 5)

    def test_negative_delta_state_is_returned_if_execute_successful(self):
        """
        If the delta is negative, ``exec_scale_down`` is called and if
        it is successful, ``obey_config_change`` returns the current state
        """
        self.calculate_delta.return_value = -5
        d = controller.obey_config_change(self.log, 'transaction-id',
                                          'config', self.group, self.state, 'launch')
        self.assertIs(self.successResultOf(d), self.state)
        self.exec_scale_down.assert_called_once_with(
            self.log.bind.return_value.bind.return_value,
            'transaction-id', self.state,
            self.group, 5)

    def test_negative_delta_execute_errors_propagated(self):
        """
        ``obey_config_change`` propagates any errors ``exec_scale_down`` raises
        """
        self.calculate_delta.return_value = -5
        self.exec_scale_down.return_value = defer.fail(Exception('meh'))
        d = controller.obey_config_change(self.log, 'transaction-id',
                                          'config', self.group, self.state, 'launch')
        f = self.failureResultOf(d)
        self.assertTrue(f.check(Exception))
        self.exec_scale_down.assert_called_once_with(
            self.log.bind.return_value.bind.return_value,
            'transaction-id', self.state,
            self.group, 5)

    def test_audit_log_events_logged_on_positive_delta(self):
        """
        ``obey_config_change`` makes the correct audit log upon scale up
        """
        log = mock_log()
        self.calculate_delta.return_value = 5
        d = controller.obey_config_change(log, 'transaction-id',
                                          'config', self.group, self.state,
                                          'launch')
        self.assertIs(self.successResultOf(d), self.state)
        log.msg.assert_called_with(
            'Starting {convergence_delta} new servers to satisfy desired capacity',
            scaling_group_id=self.group.uuid, event_type="convergence.scale_up",
            convergence_delta=5, desired_capacity=5, pending_capacity=2,
            active_capacity=3, audit_log=True, policy_id=None,
            webhook_id=None)

    def test_audit_log_events_logged_on_negative_delta(self):
        """
        ``obey_config_change`` makes the correct audit log upon scale down
        """
        log = mock_log()
        self.calculate_delta.return_value = -5
        d = controller.obey_config_change(log, 'transaction-id',
                                          'config', self.group, self.state, 'launch')
        self.assertIs(self.successResultOf(d), self.state)
        log.msg.assert_called_with(
            'Deleting 5 servers to satisfy desired capacity',
            scaling_group_id=self.group.uuid, event_type="convergence.scale_down",
            convergence_delta=-5, desired_capacity=5, pending_capacity=2,
            active_capacity=3, audit_log=True, policy_id=None,
            webhook_id=None)


def mock_controller_utilities(test_case):
    """
    Mock out the following functions in the controller module, in order to simplify
    testing of scaling up and down.

        - check_cooldowns (returns True)
        - calculate_delta (return 1)
        - exec_scale_down (return a dummy success)
        - execute_launch_config (return a dummy success)
    """
    mocks = {}
    things_and_return_vals = {
        'check_cooldowns': True,
        'calculate_delta': 1,
        'exec_scale_down': defer.succeed("scaled down"),
        'execute_launch_config': defer.succeed("scaled up")
    }

    for thing, return_val in things_and_return_vals.iteritems():
        mocks[thing] = patch(test_case, 'otter.controller.{0}'.format(thing),
                             return_value=return_val)

    return mocks


def mock_group_state():
    """
    Create a mocked GroupState.
    """
    mock_state = mock.MagicMock(GroupState)
    mock_state.get_capacity.return_value = {
        'desired_capacity': 5,
        'pending_capacity': 2,
        'active_capacity': 3
    }
    return mock_state


def mock_group():
    """
    Create a mocked ScalingGroup.
    """
    group = iMock(IScalingGroup, tenant_id='tenant', uuid='group')
    group.view_config.return_value = defer.succeed("config")
    group.get_policy.return_value = defer.succeed("policy")
    group.view_launch_config.return_value = defer.succeed("launch")
    return group


class MaybeExecuteScalingPolicyTestCase(SynchronousTestCase):
    """
    Tests for :func:`otter.controller.maybe_execute_scaling_policy`
    """

    def setUp(self):
        """
        Mock relevant controller methods.
        """
        self.mocks = mock_controller_utilities(self)
        self.mock_log = mock.MagicMock()
        self.mock_state = mock_group_state()
        self.group = mock_group()

    def test_maybe_execute_scaling_policy_no_such_policy(self):
        """
        If there is no such scaling policy, the whole thing fails and
        ``NoSuchScalingPolicy`` gets propagated up.  No other model access
        happens, and the lock is still released.
        """
        self.group.get_policy.return_value = defer.fail(
            NoSuchPolicyError('1', '1', '1'))

        d = controller.maybe_execute_scaling_policy(self.mock_log,
                                                    'transaction',
                                                    self.group,
                                                    self.mock_state,
                                                    'pol1')
        self.failureResultOf(d, NoSuchPolicyError)

        self.assertEqual(len(self.group.view_config.mock_calls), 0)
        self.assertEqual(len(self.group.view_launch_config.mock_calls), 0)

    def test_execute_launch_config_success_on_positive_delta(self):
        """
        If lock is acquired, all cooldowns are all fine, ``calculate_delta``
        returns positive delta then ``execute_launch_config`` gets called
        and if does not fail, return value is the updated state.
        """
        self.mocks['execute_launch_config'].return_value = defer.succeed(
            'this should be returned')

        d = controller.maybe_execute_scaling_policy(self.mock_log,
                                                    'transaction',
                                                    self.group,
                                                    self.mock_state,
                                                    'pol1')

        result = self.successResultOf(d)
        self.assertEqual(result, self.mock_state)

        # log should have been updated
        self.mock_log.bind.assert_called_once_with(
            scaling_group_id=self.group.uuid, policy_id='pol1')

        self.mocks['check_cooldowns'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy", 'pol1')
        self.mocks['calculate_delta'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy")
        self.mocks['execute_launch_config'].assert_called_once_with(
            self.mock_log.bind.return_value.bind.return_value,
            'transaction', self.mock_state, "launch", self.group,
            self.mocks['calculate_delta'].return_value)

        # state should have been updated
        self.mock_state.mark_executed.assert_called_once_with('pol1')

    def test_execute_launch_config_failure_on_positive_delta(self):
        """
        If ``execute_launch_config`` fails for some reason, then state should
        not be marked as executed
        """
        expected = ValueError('some failure')
        self.mocks['execute_launch_config'].return_value = defer.fail(expected)

        d = controller.maybe_execute_scaling_policy(self.mock_log,
                                                    'transaction',
                                                    self.group,
                                                    self.mock_state,
                                                    'pol1')
        failure = self.failureResultOf(d)
        self.assertEqual(failure.value, expected)

        # log should have been updated
        self.mock_log.bind.assert_called_once_with(
            scaling_group_id=self.group.uuid, policy_id='pol1')

        self.mocks['check_cooldowns'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy", 'pol1')
        self.mocks['calculate_delta'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy")
        self.mocks['execute_launch_config'].assert_called_once_with(
            self.mock_log.bind.return_value.bind.return_value,
            'transaction', self.mock_state, "launch", self.group,
            self.mocks['calculate_delta'].return_value)

        # state should not have been updated
        self.assertEqual(self.mock_state.mark_executed.call_count, 0)

    def test_maybe_execute_scaling_policy_cooldown_failure(self):
        """
        If cooldowns are not fine, ``maybe_execute_scaling_policy`` raises a
        ``CannotExecutePolicyError`` exception.  Release lock still happens.
        """
        self.mocks['check_cooldowns'].return_value = False

        d = controller.maybe_execute_scaling_policy(self.mock_log,
                                                    'transaction',
                                                    self.group,
                                                    self.mock_state,
                                                    'pol1')
        f = self.failureResultOf(d, controller.CannotExecutePolicyError)
        self.assertIn("Cooldowns not met", str(f.value))

        self.mocks['check_cooldowns'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy", 'pol1')
        self.assertEqual(self.mocks['calculate_delta'].call_count, 0)
        self.assertEqual(self.mocks['execute_launch_config'].call_count, 0)

        # state should not have been updated
        self.assertEqual(self.mock_state.mark_executed.call_count, 0)

    def test_maybe_execute_scaling_policy_zero_delta(self):
        """
        If cooldowns are fine, but delta is zero,
        ``maybe_execute_scaling_policy`` raises a ``CannotExecutePolicyError``
        exception.  Release lock still happens.
        """
        self.mocks['calculate_delta'].return_value = 0

        d = controller.maybe_execute_scaling_policy(self.mock_log,
                                                    'transaction',
                                                    self.group,
                                                    self.mock_state,
                                                    'pol1')
        f = self.failureResultOf(d, controller.CannotExecutePolicyError)
        self.assertIn("No change in servers", str(f.value))

        self.mocks['check_cooldowns'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy", 'pol1')
        self.mocks['calculate_delta'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy")
        self.assertEqual(
            len(self.mocks['execute_launch_config'].mock_calls), 0)

    def test_exec_scale_down_success_when_delta_negative(self):
        """
        ``exec_scale_down`` gets called when ``calculate_delta`` returns value
        < 0. The state is marked as executed
        """
        self.mocks['calculate_delta'].return_value = -3

        controller.maybe_execute_scaling_policy(self.mock_log, 'transaction',
                                                self.group, self.mock_state,
                                                'pol1')
        self.mocks['exec_scale_down'].assert_called_once_with(
            self.mock_log.bind.return_value.bind.return_value, 'transaction',
            self.mock_state, self.group, 3)
        self.mocks['check_cooldowns'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy", 'pol1')
        self.mocks['calculate_delta'].assert_called_once_with(
            self.mock_log.bind.return_value, self.mock_state, "config",
            "policy")
        self.assertEqual(
            len(self.mocks['execute_launch_config'].mock_calls), 0)

        # state should have been updated
        self.mock_state.mark_executed.assert_called_once_with('pol1')

    def test_audit_log_events_logged_on_positive_delta(self):
        """
        ``obey_config_change`` makes the correct audit log upon scale up
        """
        log = mock_log()
        self.mocks['calculate_delta'].return_value = 5
        d = controller.maybe_execute_scaling_policy(log, 'transaction',
                                                    self.group, self.mock_state,
                                                    'pol1')
        self.assertEqual(self.successResultOf(d), self.mock_state)
        log.msg.assert_called_with(
            'Starting {convergence_delta} new servers to satisfy desired capacity',
            scaling_group_id=self.group.uuid, event_type="convergence.scale_up",
            convergence_delta=5, desired_capacity=5, pending_capacity=2,
            active_capacity=3, audit_log=True, policy_id=None,
            webhook_id=None)

    def test_audit_log_events_logged_on_negative_delta(self):
        """
        ``obey_config_change`` makes the correct audit log upon scale down
        """
        log = mock_log()
        self.mocks['calculate_delta'].return_value = -5
        d = controller.maybe_execute_scaling_policy(log, 'transaction',
                                                    self.group, self.mock_state,
                                                    'pol1')
        self.assertEqual(self.successResultOf(d), self.mock_state)
        log.msg.assert_called_with(
            'Deleting 5 servers to satisfy desired capacity',
            scaling_group_id=self.group.uuid, event_type="convergence.scale_down",
            convergence_delta=-5, desired_capacity=5, pending_capacity=2,
            active_capacity=3, audit_log=True, policy_id=None,
            webhook_id=None)


class ConvergeTestCase(SynchronousTestCase):
    """
    Tests for :func:`otter.controller.converge`, using both the Otter
    launch_server backend and the real convergence backend.
    """

    def setUp(self):
        """
        Mock relevant controller methods. Also build a mock model that can be
        used for testing.
        """
        self.mocks = mock_controller_utilities(self)
        self.mock_log = mock.MagicMock()
        self.mock_state = mock_group_state()
        self.group = mock_group()
        self.cvg_starter_mock = mock.Mock()
        self.addCleanup(set_convergence_starter, get_convergence_starter())
        set_convergence_starter(self.cvg_starter_mock)

    def test_no_change_returns_none(self):
        """
        converge returns None when there are no changes to make.
        """
        log = mock_log()
        self.mocks['calculate_delta'].return_value = 0
        result = controller.converge(
            log, 'transaction', 'config', self.group,
            self.mock_state, 'launch', 'policy')
        self.assertIs(result, None)
        log.msg.assert_called_once_with('no change in servers', server_delta=0)

    def test_scale_up_execute_launch_config(self):
        """
        Converge will invoke execute_launch_config when the delta is positive.
        """
        self.mocks['calculate_delta'].return_value = 5
        result = controller.converge(
            self.mock_log, 'transaction', 'config', self.group,
            self.mock_state, 'launch', 'policy')

        self.mock_log.bind.assert_any_call(server_delta=5)
        bound_log = self.mock_log.bind.return_value
        self.assertIs(self.successResultOf(result), self.mock_state)
        self.mocks['execute_launch_config'].assert_called_once_with(
            bound_log, 'transaction', self.mock_state, 'launch', self.group, 5)
        bound_log.msg.assert_any_call('executing launch configs')

        # And converger service is _not_ called
        self.assertFalse(self.cvg_starter_mock.start_convergence.called)

    def test_scale_down_exec_scale_down(self):
        """
        Converge will invoke exec_scale_down when the delta is negative.
        """
        self.mocks['calculate_delta'].return_value = -5
        result = controller.converge(
            self.mock_log, 'transaction', 'config', self.group,
            self.mock_state, 'launch', 'policy')

        self.mock_log.bind.assert_any_call(server_delta=-5)
        bound_log = self.mock_log.bind.return_value
        self.assertIs(self.successResultOf(result), self.mock_state)
        self.mocks['exec_scale_down'].assert_called_once_with(
            bound_log, 'transaction', self.mock_state, self.group, 5)
        bound_log.msg.assert_any_call('scaling down')
        # And converger service is _not_ called
        self.assertFalse(self.cvg_starter_mock.start_convergence.called)

    def test_audit_log_scale_up(self):
        """
        When converge scales up, an audit log is emitted.
        """
        log = mock_log()
        self.mocks['calculate_delta'].return_value = 1
        controller.converge(
            log, 'transaction', 'config', self.group,
            self.mock_state, 'launch', 'policy')

        log.msg.assert_any_call(
            "Starting {convergence_delta} new servers to satisfy desired "
            "capacity", event_type="convergence.scale_up", convergence_delta=1,
            policy_id=None, webhook_id=None,
            active_capacity=3, desired_capacity=5, pending_capacity=2,
            audit_log=True)

    def test_audit_log_scale_down(self):
        """
        When converge scales down, an audit log is emitted.
        """
        log = mock_log()
        self.mocks['calculate_delta'].return_value = -1
        controller.converge(
            log, 'transaction', 'config', self.group,
            self.mock_state, 'launch', 'policy')

        log.msg.assert_any_call(
            "Deleting 1 servers to satisfy desired capacity",
            event_type="convergence.scale_down",
            convergence_delta=-1,
            policy_id=None, webhook_id=None,
            active_capacity=3, desired_capacity=5, pending_capacity=2,
            audit_log=True)

    def test_real_convergence_nonzero_delta(self):
        """
        When a tenant is configured to for convergence, if the delta is
        non-zero, the Convergence service's ``converge`` method is invoked and
        a Deferred that fires with `None` is returned.
        """
        log = mock_log()
        state = GroupState('tenant', 'group', "test", [], [], None, {},
                           False)
        group_config = {'maxEntities': 100, 'minEntities': 0}
        policy = {'change': 5}
        config_data = {'convergence-tenants': ['tenant']}

        start_convergence = self.cvg_starter_mock.start_convergence
        start_convergence.return_value = defer.succeed("ignored")

        result = controller.converge(log, 'txn-id', group_config, self.group,
                                     state, 'launch', policy,
                                     config_value=config_data.get)
        self.assertEqual(self.successResultOf(result), state)
        start_convergence.assert_called_once_with(log, 'tenant', 'group')

        # And execute_launch_config is _not_ called
        self.assertFalse(self.mocks['execute_launch_config'].called)

    def test_real_convergence_zero_delta(self):
        """
        When a tenant is configured for convergence, if the delta is zero, the
        ConvergenceStarter service's ``start_convergence`` method is still
        invoked.
        """
        log = mock_log()
        state = GroupState('tenant', 'group-id', "test", [], [], None, {},
                           False)
        group_config = {'maxEntities': 100, 'minEntities': 0}
        policy = {'change': 0}
        config_data = {'convergence-tenants': ['tenant']}

        start_convergence = self.cvg_starter_mock.start_convergence
        start_convergence.return_value = defer.succeed("ignored")

        result = controller.converge(log, 'txn-id', group_config, self.group,
                                     state, 'launch', policy,
                                     config_value=config_data.get)
        self.assertIs(self.successResultOf(result), state)
        start_convergence.assert_called_once_with(log, 'tenant', 'group')

        # And execute_launch_config is _not_ called
        self.assertFalse(self.mocks['execute_launch_config'].called)


class ConvergenceRemoveServerTests(SynchronousTestCase):
    """
    Tests for :func:`otter.controller.convergence_remove_server_from_group`
    """
    def setUp(self):
        """
        Fake supervisor, group and state
        """
        self.config_data = {'convergence-tenants': ['tenant_id']}

        self.trans_id = 'trans_id'
        self.log = mock_log()
        self.state = GroupState('tenant_id', 'group_id', 'group_name',
                                active={'s0': {'id': 's0'}},
                                pending={},
                                group_touched=None,
                                policy_touched=None,
                                paused=None,
                                desired=1)
        self.group = iMock(IScalingGroup, tenant_id='tenant_id',
                           uuid='group_id')
        self.server_details = {
            'server': {
                'id': 'server_id',
                'metadata': {
                    'rax:autoscale:group:id': 'group_id',
                    'rax:auto_scaling_group_id': 'group_id'
                }
            }
        }
        self.group_manifest_info = {
            'groupConfiguration': {'minEntities': 0},
            'launchConfiguration': {'this is not used': 'here'},
            'state': self.state
        }

    def _remove(self, replace, purge, dispatcher):
        # Retry intents can't really be compared if the effect inside
        # has callbacks, so just unpack and assert something about the retry
        # params, and perform the wrapped effect.
        expected_retry_params = ShouldDelayAndRetry(
            can_retry=retry_times(3),
            next_interval=exponential_backoff_interval(2))

        @sync_performer
        def handle_retry(_disp, retry_intent):
            self.assertEqual(retry_intent.should_retry, expected_retry_params)
            return sync_perform(_disp, retry_intent.effect)

        full_dispatcher = ComposedDispatcher([
            test_dispatcher(),
            TypeDispatcher({Retry: handle_retry}),
            dispatcher])

        eff = controller.convergence_remove_server_from_group(
            self.group, self.state, 'server_id', replace, purge)
        return sync_perform(full_dispatcher, eff)

    def test_no_such_server_replace_true(self):
        """
        If there is no such server at all in Nova, a
        :class:`ServerNotFoundError` is raised.  No additional checking
        for config is needed because replace is set True.
        """
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: raise_(NoSuchServerError(server_id=u'server_id')))
        ])
        self.assertRaises(
            ServerNotFoundError, self._remove, True, False, dispatcher)
        self.assertEqual(dispatcher.sequence, [])

    def test_server_not_autoscale_server_replace_true(self):
        """
        If there is such a server in Nova, but it does not have
        autoscale-specific metadata, then :class:`ServerNotFoundError` is
        raised.  No additional checking for config is needed because
        replace is set True.
        """
        self.server_details['server'].pop('metadata')
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details)
        ])
        self.assertRaises(
            ServerNotFoundError, self._remove, True, False, dispatcher)
        self.assertEqual(dispatcher.sequence, [])

    def test_server_in_wrong_group_replace_true(self):
        """
        If there is such a server in Nova, but it belongs to a different group,
        then :class:`ServerNotFoundError` is raised.  No additional checking
        for config is needed because replace is set True.
        """
        self.server_details['server']['metadata'] = {
            'rax:autoscale:group:id': 'other_group_id',
            'rax:auto_scaling_group_id': 'other_group_id'
        }

        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details)
        ])
        self.assertRaises(
            ServerNotFoundError, self._remove, True, False, dispatcher)
        self.assertEqual(dispatcher.sequence, [])

    def test_server_in_group_cannot_scale_down(self):
        """
        If ``replace=False`` a check is done to see if the group can be scaled
        down by 1.  If not, then removing fails with a
        :class:`CannotExecutePolicyError` even if the server is in the group.
        """
        self.state.desired = 1
        self.group_manifest_info['groupConfiguration']['minEntities'] = 1
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (GetScalingGroupInfo(tenant_id='tenant_id', group_id='group_id'),
                lambda _: self.group_manifest_info)
        ])
        self.assertRaises(
            CannotDeleteServerBelowMinError, self._remove, False, False,
            dispatcher)
        self.assertEqual(dispatcher.sequence, [])

    def test_server_not_in_group_cannot_scale_down(self):
        """
        If both the server check and the scaling down check fail,
        the exception that gets raised is :class:`ServerNotFoundError`.
        """
        self.server_details['server'].pop('metadata')
        self.state.desired = 1
        self.group_manifest_info['groupConfiguration']['minEntities'] = 1
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (GetScalingGroupInfo(tenant_id='tenant_id', group_id='group_id'),
                lambda _: self.group_manifest_info)
        ])
        self.assertRaises(
            ServerNotFoundError, self._remove, False, False, dispatcher)
        self.assertEqual(dispatcher.sequence, [])

    def test_checks_pass_replace_true_purge_success(self):
        """
        If all the checks pass, and purge is true, then "DRAINING" is added
        to the server's metadata.  If this is successful, then the whole
        effect is a success, and returns same state the function was called
        with.
        """
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (set_nova_metadata_item('server_id', *DRAINING_METADATA).intent,
                lambda _: None)
        ])
        result = self._remove(True, True, dispatcher)
        self.assertEqual(result, self.state)
        self.assertEqual(dispatcher.sequence, [])

    def test_checks_pass_replace_false_purge_success(self):
        """
        If all the checks pass, and purge is true and replace is false, then
        "DRAINING" is added to the server's metadata.  If this is successful,
        then the whole effect is a success, and returns the state the function
        was called with, with the desired value decremented.
        """
        old_desired = self.state.desired = 2
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (GetScalingGroupInfo(tenant_id='tenant_id', group_id='group_id'),
                lambda _: self.group_manifest_info),
            (set_nova_metadata_item('server_id', *DRAINING_METADATA).intent,
                lambda _: None)
        ])
        result = self._remove(False, True, dispatcher)
        self.assertEqual(result, self.state)
        self.assertEqual(result.desired, old_desired - 1)
        self.assertEqual(dispatcher.sequence, [])

    def test_checks_pass_replace_true_purge_failure(self):
        """
        If all the checks pass, and purge is true, then "DRAINING" is added
        to the server's metadata.  If this fails, then the failure is
        propagated and the state is not returned.
        """
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (set_nova_metadata_item('server_id', *DRAINING_METADATA).intent,
                lambda _: raise_(ValueError('oops!')))
        ])
        self.assertRaises(ValueError, self._remove, True, True, dispatcher)
        self.assertEqual(dispatcher.sequence, [])

    def test_checks_pass_replace_true_no_purge_success(self):
        """
        If all the checks pass, and purge is false, then autoscale-specific
        metadata is removed from the server's metadata.  If this is successful,
        then the whole effect is a success, and returns same state the function
        was called with.
        """
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (EvictServerFromScalingGroup(scaling_group=self.group,
                                         server_id='server_id'),
                lambda _: None)
        ])
        result = self._remove(True, False, dispatcher)
        self.assertEqual(result, self.state)
        self.assertEqual(dispatcher.sequence, [])

    def test_checks_pass_replace_false_no_purge_success(self):
        """
        If all the checks pass, and purge is true and replace is false, then
        "DRAINING" is added to the server's metadata.  If this is successful,
        then the whole effect is a success, and returns the state the function
        was called with, with the desired value decremented.
        """
        old_desired = self.state.desired = 2
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (GetScalingGroupInfo(tenant_id='tenant_id', group_id='group_id'),
                lambda _: self.group_manifest_info),
            (EvictServerFromScalingGroup(scaling_group=self.group,
                                         server_id='server_id'),
                lambda _: None)
        ])
        result = self._remove(False, False, dispatcher)
        self.assertEqual(result, self.state)
        self.assertEqual(result.desired, old_desired - 1)
        self.assertEqual(dispatcher.sequence, [])

    def test_checks_pass_replace_true_no_purge_failure(self):
        """
        If all the checks pass, and purge is true, then "DRAINING" is added
        to the server's metadata.  If this fails, then the failure is
        propagated and the state is not returned.
        """
        dispatcher = SequenceDispatcher([
            (get_server_details('server_id').intent,
                lambda _: self.server_details),
            (EvictServerFromScalingGroup(scaling_group=self.group,
                                         server_id='server_id'),
                lambda _: raise_(ValueError('oops')))
        ])
        self.assertRaises(ValueError, self._remove, True, False, dispatcher)
        self.assertEqual(dispatcher.sequence, [])
