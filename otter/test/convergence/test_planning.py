"""Tests for convergence planning."""

from pyrsistent import b, pbag, pmap, pset, s

from toolz import groupby

from twisted.trial.unittest import SynchronousTestCase

from otter.convergence.model import (
    CLBDescription,
    CLBNode,
    CLBNodeCondition,
    CLBNodeType,
    DesiredGroupState,
    NovaServer,
    RCv3Description,
    RCv3Node,
    ServerState)
from otter.convergence.planning import (
    _default_limit_step_count,
    _limit_step_count,
    converge,
    optimize_steps,
    plan)
from otter.convergence.steps import (
    AddNodesToCLB,
    BulkAddToRCv3,
    BulkRemoveFromRCv3,
    ChangeCLBNode,
    CreateServer,
    DeleteServer,
    RemoveNodesFromCLB,
    SetMetadataItemOnServer)


def copy_clb_desc(clb_desc, condition=CLBNodeCondition.ENABLED, weight=1):
    """
    Produce a :class:`CLBDescription` from another, but with the given
    condition changed to the one given

    :param clb_desc: the :class:`CLBDescription` to copy
    :param condition: the :class:`CLBNodeCondition` to use
    """
    return CLBDescription(lb_id=clb_desc.lb_id, port=clb_desc.port,
                          condition=condition, weight=weight)


class RemoveFromLBWithDrainingTests(SynchronousTestCase):
    """
    Tests for :func:`converge` with regards to draining a server on a load
    balanacer and removing them from the load balancer when finished draining.
    (:func:`_remove_from_lb_with_draining`).
    """
    LB_STEPS = (AddNodesToCLB, RemoveNodesFromCLB, ChangeCLBNode,
                BulkAddToRCv3, BulkRemoveFromRCv3)

    address = '1.1.1.1'

    def assert_converge_clb_steps(self, clb_descs, clb_nodes, clb_steps,
                                  draining_timeout, now):
        """
        Run the converge function on the given a server with the given
        :class:`CLBDescription`s  and :class:`CLBNode`s, the given
        draining timeout, and the given time.

        Assert that the LB steps produced are equivalent to the given
        CLB steps.

        Run the converge function again, this time with a default
        :class:`RCv3Description` and a default :class:`RCv3Node` added, and
        assert that the LB steps produced are equivalent to the given
        CLB steps plus a RCv3 node removal, because RCv3 nodes are not
        drainable and are hence unaffected by timeouts.
        """
        without_rcv3_steps = converge(
            DesiredGroupState(server_config={}, capacity=0,
                              draining_timeout=draining_timeout),
            s(server('abc',
                     ServerState.ACTIVE,
                     servicenet_address=self.address,
                     desired_lbs=s(*clb_descs))),
            s(*clb_nodes),
            now=now)

        self.assertEqual(self._filter_only_lb_steps(without_rcv3_steps),
                         b(*clb_steps))

        rcv3_desc = RCv3Description(
            lb_id='e762e42a-8a4e-4ffb-be17-f9dc672729b2')
        rcv3_step = BulkRemoveFromRCv3(
            lb_node_pairs=s(('e762e42a-8a4e-4ffb-be17-f9dc672729b2', 'abc')))

        with_rcv3_steps = converge(
            DesiredGroupState(server_config={}, capacity=0,
                              draining_timeout=draining_timeout),
            s(server('abc',
                     ServerState.ACTIVE,
                     servicenet_address=self.address,
                     desired_lbs=s(rcv3_desc, *clb_descs))),
            s(RCv3Node(node_id='43a39c18-8cad-4bb1-808e-450d950be289',
                       cloud_server_id='abc', description=rcv3_desc),
              *clb_nodes),
            now=now)

        self.assertEqual(self._filter_only_lb_steps(with_rcv3_steps),
                         b(rcv3_step, *clb_steps))

    def _filter_only_lb_steps(self, steps):
        """
        Converge may do other things to a server depending on its draining
        state.  This suite of tests is only testing how it handles the load
        balancer, so ignore steps that are not load-balancer related.
        """
        return pbag([step for step in steps if type(step) in self.LB_STEPS])

    def test_zero_timeout_remove_from_lb(self):
        """
        If the timeout is zero, all nodes are just removed.
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=clb_desc)
        clb_step = RemoveNodesFromCLB(lb_id='5', node_ids=s('123'))

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[clb_step],
            draining_timeout=0.0,
            now=0)

    def test_disabled_state_is_removed(self):
        """
        Drainable nodes in disabled state are just removed from the load
        balancer even if the timeout is positive.
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=copy_clb_desc(
                               clb_desc, condition=CLBNodeCondition.DISABLED))
        clb_step = RemoveNodesFromCLB(lb_id='5', node_ids=s('123'))

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[clb_step],
            draining_timeout=10.0,
            now=0)

    def test_drainable_enabled_state_is_drained(self):
        """
        Drainable nodes in enabled state are put into draining, while
        undrainable nodes are just removed.
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=clb_desc)
        clb_step = ChangeCLBNode(lb_id='5', node_id='123', weight=1,
                                 condition=CLBNodeCondition.DRAINING,
                                 type=CLBNodeType.PRIMARY)

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[clb_step],
            draining_timeout=10.0,
            now=0)

    def test_draining_state_is_ignored_if_connections_before_timeout(self):
        """
        Nodes in draining state will be ignored if they still have connections
        and the timeout is not yet expired.
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=copy_clb_desc(
                               clb_desc, condition=CLBNodeCondition.DRAINING),
                           drained_at=0.0, connections=1)

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[],
            draining_timeout=10.0,
            now=5)

    def test_draining_state_removed_if_no_connections_before_timeout(self):
        """
        Nodes in draining state will be removed if they have no more
        connections, even if the timeout is not yet expired
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=copy_clb_desc(
                               clb_desc, condition=CLBNodeCondition.DRAINING),
                           drained_at=0.0, connections=0)
        clb_step = RemoveNodesFromCLB(lb_id='5', node_ids=s('123'))

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[clb_step],
            draining_timeout=10.0,
            now=5)

    def test_draining_state_remains_if_connections_none_before_timeout(self):
        """
        Nodes in draining state will be ignored if timeout has not yet expired
        and the number of active connections are not provided
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=copy_clb_desc(
                               clb_desc, condition=CLBNodeCondition.DRAINING),
                           drained_at=0.0)

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[],
            draining_timeout=10.0,
            now=5)

    def test_draining_state_removed_if_connections_none_after_timeout(self):
        """
        Nodes in draining state will be removed when the timeout expires if
        the number of active connections are not provided
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=copy_clb_desc(
                               clb_desc, condition=CLBNodeCondition.DRAINING),
                           drained_at=0.0)
        clb_step = RemoveNodesFromCLB(lb_id='5', node_ids=s('123'))

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[clb_step],
            draining_timeout=10.0,
            now=15)

    def test_draining_state_removed_if_connections_and_timeout_expired(self):
        """
        Nodes in draining state will be removed when the timeout expires even
        if they still have active connections
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        clb_node = CLBNode(node_id='123', address=self.address,
                           description=copy_clb_desc(
                               clb_desc, condition=CLBNodeCondition.DRAINING),
                           drained_at=0.0, connections=10)
        clb_step = RemoveNodesFromCLB(lb_id='5', node_ids=s('123'))

        self.assert_converge_clb_steps(
            clb_descs=[clb_desc],
            clb_nodes=[clb_node],
            clb_steps=[clb_step],
            draining_timeout=10.0,
            now=15)

    def test_all_clb_changes_together(self):
        """
        Given all possible combination of clb load balancer states and
        timeouts, ensure function produces the right set of step for all of
        them.
        """
        clb_descs = [CLBDescription(lb_id='1', port=80),
                     CLBDescription(lb_id='2', port=80),
                     CLBDescription(lb_id='3', port=80),
                     CLBDescription(lb_id='4', port=80),
                     CLBDescription(lb_id='5', port=80)]

        clb_nodes = [
            # enabled, should be drained
            CLBNode(node_id='1', address=self.address,
                    description=clb_descs[0]),
            # disabled, should be removed
            CLBNode(node_id='2', address=self.address,
                    description=copy_clb_desc(
                        clb_descs[1], condition=CLBNodeCondition.DISABLED)),
            # draining, still connections, should be ignored
            CLBNode(node_id='3', address='1.1.1.1',
                    description=copy_clb_desc(
                        clb_descs[2], condition=CLBNodeCondition.DRAINING),
                    connections=3, drained_at=5.0),
            # draining, no connections, should be removed
            CLBNode(node_id='4', address='1.1.1.1',
                    description=copy_clb_desc(
                        clb_descs[3], condition=CLBNodeCondition.DRAINING),
                    connections=0, drained_at=5.0),
            # draining, timeout exired, should be removed
            CLBNode(node_id='5', address='1.1.1.1',
                    description=copy_clb_desc(
                        clb_descs[4], condition=CLBNodeCondition.DRAINING),
                    connections=10, drained_at=0.0)]

        clb_steps = [
            ChangeCLBNode(lb_id='1', node_id='1', weight=1,
                          condition=CLBNodeCondition.DRAINING,
                          type=CLBNodeType.PRIMARY),
            RemoveNodesFromCLB(lb_id='2', node_ids=s('2')),
            RemoveNodesFromCLB(lb_id='4', node_ids=s('4')),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('5')),
        ]

        self.assert_converge_clb_steps(
            clb_descs=clb_descs,
            clb_nodes=clb_nodes,
            clb_steps=clb_steps,
            draining_timeout=10.0,
            now=10)


class ConvergeLBStateTests(SynchronousTestCase):
    """
    Tests for :func:`converge` with regards to converging the load balancer
    state on active servers.  (:func:`_converge_lb_state`)
    """
    def test_add_to_lb(self):
        """
        If a desired LB config is not in the set of current configs,
        `converge_lb_state` returns the relevant adding-to-load-balancer
        steps (:class:`AddNodesToCLB` in the case of CLB,
        :class:`BulkAddToRCv3` in the case of RCv3).
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        rcv3_desc = RCv3Description(
            lb_id='c6fe49fa-114a-4ea4-9425-0af8b30ff1e7')

        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(clb_desc, rcv3_desc))]),
                set(),
                0),
            pbag([
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(('1.1.1.1', clb_desc))),
                BulkAddToRCv3(
                    lb_node_pairs=s(
                        ('c6fe49fa-114a-4ea4-9425-0af8b30ff1e7', 'abc')))
            ]))

    def test_change_lb_node(self):
        """
        If a desired CLB mapping is in the set of current configs,
        but the configuration is wrong, `converge_lb_state` returns a
        :class:`ChangeCLBNode` object.  RCv3 nodes cannot be changed - they are
        either right or wrong.
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        rcv3_desc = RCv3Description(
            lb_id='c6fe49fa-114a-4ea4-9425-0af8b30ff1e7')

        current = [CLBNode(node_id='123', address='1.1.1.1',
                           description=copy_clb_desc(clb_desc, weight=5)),
                   RCv3Node(node_id='234', cloud_server_id='abc',
                            description=rcv3_desc)]
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(clb_desc, rcv3_desc))]),
                set(current),
                0),
            pbag([
                ChangeCLBNode(lb_id='5', node_id='123', weight=1,
                              condition=CLBNodeCondition.ENABLED,
                              type=CLBNodeType.PRIMARY)]))

    def test_remove_lb_node(self):
        """
        If a current lb config is not in the desired set of lb configs,
        `converge_lb_state` returns a :class:`RemoveFromCLB` object for CLBs
        and a :class:`BulkRemoveFromRCv3` for RCv3 load balancers.
        """
        current = [CLBNode(node_id='123', address='1.1.1.1',
                           description=CLBDescription(
                               lb_id='5', port=80, weight=5)),
                   RCv3Node(node_id='234', cloud_server_id='abc',
                            description=RCv3Description(
                                lb_id='c6fe49fa-114a-4ea4-9425-0af8b30ff1e7'))]

        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=pset())]),
                set(current),
                0),
            pbag([RemoveNodesFromCLB(lb_id='5', node_ids=s('123')),
                  BulkRemoveFromRCv3(lb_node_pairs=s(
                      ('c6fe49fa-114a-4ea4-9425-0af8b30ff1e7', 'abc')))]))

    def test_do_nothing(self):
        """
        If the desired lb state matches the current lb state,
        `converge_lb_state` returns nothing
        """
        clb_desc = CLBDescription(lb_id='5', port=80)
        rcv3_desc = RCv3Description(
            lb_id='c6fe49fa-114a-4ea4-9425-0af8b30ff1e7')

        current = [
            CLBNode(node_id='123', address='1.1.1.1', description=clb_desc),
            RCv3Node(node_id='234', cloud_server_id='abc',
                     description=rcv3_desc)]

        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(clb_desc, rcv3_desc))]),
                set(current),
                0),
            pbag([]))

    def test_all_changes(self):
        """
        Remove, change, and add a node to a load balancer all together
        """
        descs = [CLBDescription(lb_id='5', port=80),
                 CLBDescription(lb_id='6', port=80, weight=2),
                 RCv3Description(lb_id='c6fe49fa-114a-4ea4-9425-0af8b30ff1e7')]

        current = [
            CLBNode(node_id='123', address='1.1.1.1',
                    description=CLBDescription(lb_id='5', port=8080)),
            CLBNode(node_id='234', address='1.1.1.1',
                    description=copy_clb_desc(descs[1], weight=1)),
            RCv3Node(node_id='345', cloud_server_id='abc',
                     description=RCv3Description(
                         lb_id='e762e42a-8a4e-4ffb-be17-f9dc672729b2'))
        ]

        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=pset(descs))]),
                set(current),
                0),
            pbag([
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(('1.1.1.1',
                                       CLBDescription(lb_id='5', port=80)))),
                ChangeCLBNode(lb_id='6', node_id='234', weight=2,
                              condition=CLBNodeCondition.ENABLED,
                              type=CLBNodeType.PRIMARY),
                RemoveNodesFromCLB(lb_id='5', node_ids=s('123')),
                BulkAddToRCv3(lb_node_pairs=s(
                    ('c6fe49fa-114a-4ea4-9425-0af8b30ff1e7', 'abc'))),
                BulkRemoveFromRCv3(lb_node_pairs=s(
                    ('e762e42a-8a4e-4ffb-be17-f9dc672729b2', 'abc')))
            ]))

    def test_same_clb_multiple_ports(self):
        """
        It's possible to have the same cloud load balancer using multiple ports
        on the host.

        (use case: running multiple single-threaded server processes on a
        machine)
        """
        desired = s(CLBDescription(lb_id='5', port=8080),
                    CLBDescription(lb_id='5', port=8081))
        current = []
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=desired)]),
                set(current),
                0),
            pbag([
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(('1.1.1.1',
                                       CLBDescription(lb_id='5', port=8080)))),
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(('1.1.1.1',
                                       CLBDescription(lb_id='5', port=8081))))
                ]))


def server(id, state, created=0, image_id='image', flavor_id='flavor',
           **kwargs):
    """Convenience for creating a :obj:`NovaServer`."""
    return NovaServer(id=id, state=state, created=created, image_id=image_id,
                      flavor_id=flavor_id, **kwargs)


class DrainAndDeleteServerTests(SynchronousTestCase):
    """
    Tests for :func:`converge` having to do with deleting draining servers,
    or servers that don't need to be drained. (:func:`_drain_and_delete`)
    """
    clb_desc = CLBDescription(lb_id='1', port=80)
    rcv3_desc = RCv3Description(lb_id='c6fe49fa-114a-4ea4-9425-0af8b30ff1e7')

    def test_active_server_without_load_balancers_can_be_deleted(self):
        """
        If an active server to be scaled down is not attached to any load
        balancers, even if it should be, it can be deleted.
        It is not first put into draining state.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0,
                                  draining_timeout=10.0),
                set([server('abc', state=ServerState.ACTIVE,
                            desired_lbs=s(self.clb_desc, self.rcv3_desc))]),
                set(),
                0),
            pbag([DeleteServer(server_id='abc')]))

    def test_active_server_can_be_deleted_if_all_lbs_can_be_removed(self):
        """
        If an active server to be scaled down can be removed from all the load
        balancers, the server can be deleted.  It is not first put into
        draining state.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0),
                set([server('abc', state=ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(self.clb_desc, self.rcv3_desc))]),
                set([CLBNode(node_id='1', address='1.1.1.1',
                             description=self.clb_desc),
                     RCv3Node(node_id='2', cloud_server_id='abc',
                              description=self.rcv3_desc)]),
                0),
            pbag([
                DeleteServer(server_id='abc'),
                RemoveNodesFromCLB(lb_id='1', node_ids=s('1')),
                BulkRemoveFromRCv3(lb_node_pairs=s(
                    (self.rcv3_desc.lb_id, 'abc')))
            ]))

    def test_draining_server_can_be_deleted_if_all_lbs_can_be_removed(self):
        """
        If draining server can be removed from all the load balancers, the
        server can be deleted.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0),
                set([server('abc', state=ServerState.DRAINING,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(self.clb_desc, self.rcv3_desc))]),
                set([CLBNode(node_id='1', address='1.1.1.1',
                             description=copy_clb_desc(
                                 self.clb_desc,
                                 condition=CLBNodeCondition.DRAINING)),
                     RCv3Node(node_id='2', cloud_server_id='abc',
                              description=self.rcv3_desc)]),
                0),
            pbag([
                DeleteServer(server_id='abc'),
                RemoveNodesFromCLB(lb_id='1', node_ids=s('1')),
                BulkRemoveFromRCv3(lb_node_pairs=s(
                    (self.rcv3_desc.lb_id, 'abc')))
            ]))

    def test_draining_server_ignored_if_waiting_for_timeout(self):
        """
        If the server already in draining state is waiting for the draining
        timeout on some load balancers, and no further load balancers can be
        removed, nothing is done to it.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0,
                                  draining_timeout=10.0),
                set([server('abc', state=ServerState.DRAINING,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(self.clb_desc, self.rcv3_desc))]),
                set([CLBNode(node_id='1', address='1.1.1.1',
                             description=CLBDescription(
                                 lb_id='1', port=80,
                                 condition=CLBNodeCondition.DRAINING),
                             drained_at=1.0, connections=1)]),
                2),
            pbag([]))

    def test_draining_server_waiting_for_timeout_some_lbs_removed(self):
        """
        Load balancers that can be removed are removed, even if the server is
        already in draining state is waiting for the draining timeout on some
        load balancers.
        """
        other_clb_desc = CLBDescription(lb_id='9', port=80)

        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0,
                                  draining_timeout=2.0),
                set([server('abc', state=ServerState.DRAINING,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(self.clb_desc, self.rcv3_desc,
                                          other_clb_desc))]),
                set([
                    # This node is in draining - nothing will be done to it
                    CLBNode(node_id='1', address='1.1.1.1',
                            description=copy_clb_desc(
                                self.clb_desc,
                                condition=CLBNodeCondition.DRAINING),
                            drained_at=1.0, connections=1),
                    # This node is done draining, it can be removed
                    CLBNode(node_id='2', address='1.1.1.1',
                            description=copy_clb_desc(
                                other_clb_desc,
                                condition=CLBNodeCondition.DRAINING),
                            drained_at=0.0),
                    # This node is not drainable, it can be removed
                    RCv3Node(node_id='3', cloud_server_id='abc',
                             description=self.rcv3_desc)]),
                2),
            pbag([
                RemoveNodesFromCLB(lb_id='9', node_ids=s('2')),
                BulkRemoveFromRCv3(lb_node_pairs=s(
                    (self.rcv3_desc.lb_id, 'abc')))
            ]))

    def test_active_server_is_drained_if_not_all_lbs_can_be_removed(self):
        """
        If an active server to be deleted cannot be removed from all the load
        balancers, it is set to draining state and all the nodes are set to
        draining condition.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0,
                                  draining_timeout=10.0),
                set([server('abc', state=ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(self.clb_desc, self.rcv3_desc))]),
                set([CLBNode(node_id='1', address='1.1.1.1',
                             description=self.clb_desc),
                     RCv3Node(node_id='2', cloud_server_id='abc',
                              description=self.rcv3_desc)]),
                0),
            pbag([
                ChangeCLBNode(lb_id='1', node_id='1', weight=1,
                              condition=CLBNodeCondition.DRAINING,
                              type=CLBNodeType.PRIMARY),
                SetMetadataItemOnServer(server_id='abc',
                                        key='rax:auto_scaling_draining',
                                        value='draining'),
                BulkRemoveFromRCv3(lb_node_pairs=s(
                    (self.rcv3_desc.lb_id, 'abc')))
            ]))

    def test_active_server_is_drained_even_if_all_already_in_draining(self):
        """
        If an active server already has all of its load balancers in draining,
        but it cannot be removed from all of them yet, it is set to draining
        state even though no load balancer actions need to be performed.

        This can happen for instance if the server was supposed to be deleted
        in a previous convergence run, and the load balancers were set to
        draining but setting the server metadata failed.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0,
                                  draining_timeout=10.0),
                set([server('abc', state=ServerState.ACTIVE,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(self.clb_desc, self.rcv3_desc))]),
                set([CLBNode(node_id='1', address='1.1.1.1',
                             description=copy_clb_desc(
                                 self.clb_desc,
                                 condition=CLBNodeCondition.DRAINING),
                             connections=1, drained_at=0.0)]),
                1),
            pbag([
                SetMetadataItemOnServer(server_id='abc',
                                        key='rax:auto_scaling_draining',
                                        value='draining')
            ]))

    def test_draining_server_has_all_enabled_lb_set_to_draining(self):
        """
        If a draining server is enabled on any load balancers, it is set to
        draining on those load balancers and it is not deleted.  The metadata
        is not re-set to draining.

        This can happen for instance if the server was supposed to be deleted
        in a previous convergence run, and the server metadata was set but
        the load balancers update failed.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=0,
                                  draining_timeout=10.0),
                set([server('abc', state=ServerState.DRAINING,
                            servicenet_address='1.1.1.1',
                            desired_lbs=s(self.clb_desc, self.rcv3_desc))]),
                set([CLBNode(node_id='1', address='1.1.1.1',
                             description=self.clb_desc)]),
                1),
            pbag([
                ChangeCLBNode(lb_id='1', node_id='1', weight=1,
                              condition=CLBNodeCondition.DRAINING,
                              type=CLBNodeType.PRIMARY)
            ]))


class ConvergeTests(SynchronousTestCase):
    """
    Tests for :func:`converge` that do not specifically cover load balancers,
    although some load balancer information may be included.
    """

    def test_converge_give_me_a_server(self):
        """
        A server is added if there are not enough servers to meet
        the desired capacity.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set(),
                set(),
                0),
            pbag([CreateServer(server_config=pmap())]))

    def test_converge_give_me_multiple_servers(self):
        """
        Multiple servers are added at a time if there are not enough servers to
        meet the desired capacity.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=2),
                set(),
                set(),
                0),
            pbag([
                CreateServer(server_config=pmap()),
                CreateServer(server_config=pmap())]))

    def test_count_building_as_meeting_capacity(self):
        """
        No servers are created if there are building servers that sum with
        active servers to meet capacity.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.BUILD)]),
                set(),
                0),
            pbag([]))

    def test_delete_nodes_in_error_state(self):
        """
        If a server we created enters error state, it will be deleted if
        necessary, and replaced.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ERROR)]),
                set(),
                0),
            pbag([
                DeleteServer(server_id='abc'),
                CreateServer(server_config=pmap()),
            ]))

    def test_delete_error_state_servers_with_lb_nodes(self):
        """
        If a server we created enters error state and it is attached to one
        or more load balancers, it will be removed from its load balancers
        as well as get deleted.  (Tests that error state servers are not
        excluded from converging load balancer state.)
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ERROR,
                            servicenet_address='1.1.1.1')]),
                set([CLBNode(address='1.1.1.1', node_id='3',
                             description=CLBDescription(lb_id='5',
                                                        port=80)),
                     CLBNode(address='1.1.1.1', node_id='5',
                             description=CLBDescription(lb_id='5',
                                                        port=8080))]),
                0),
            pbag([
                DeleteServer(server_id='abc'),
                RemoveNodesFromCLB(lb_id='5', node_ids=s('3')),
                RemoveNodesFromCLB(lb_id='5', node_ids=s('5')),
                CreateServer(server_config=pmap()),
            ]))

    def test_scale_down(self):
        """If we have more servers than desired, we delete the oldest."""
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1),
                set([server('abc', ServerState.ACTIVE, created=0),
                     server('def', ServerState.ACTIVE, created=1)]),
                set(),
                0),
            pbag([DeleteServer(server_id='abc')]))

    def test_scale_down_building_first(self):
        """
        When scaling down, first we delete building servers, in preference
        to older server.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=2),
                set([server('abc', ServerState.ACTIVE, created=0),
                     server('def', ServerState.BUILD, created=1),
                     server('ghi', ServerState.ACTIVE, created=2)]),
                set(),
                0),
            pbag([DeleteServer(server_id='def')]))

    def test_timeout_building(self):
        """
        Servers that have been building for too long will be deleted and
        replaced.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=2),
                set([server('slowpoke', ServerState.BUILD, created=0),
                     server('ok', ServerState.ACTIVE, created=0)]),
                set(),
                3600),
            pbag([
                DeleteServer(server_id='slowpoke'),
                CreateServer(server_config=pmap())]))

    def test_timeout_replace_only_when_necessary(self):
        """
        If a server is timing out *and* we're over capacity, it will be
        deleted without replacement.
        """
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=2),
                set([server('slowpoke', ServerState.BUILD, created=0),
                     server('old-ok', ServerState.ACTIVE, created=0),
                     server('new-ok', ServerState.ACTIVE, created=3600)]),
                set(),
                3600),
            pbag([DeleteServer(server_id='slowpoke')]))

    def test_converge_active_servers_ignores_servers_to_be_deleted(self):
        """
        Only servers in active that are not being deleted will have their
        load balancers converged.
        """
        desc = CLBDescription(lb_id='5', port=80)
        desired_lbs = s(desc)
        self.assertEqual(
            converge(
                DesiredGroupState(server_config={}, capacity=1,
                                  desired_lbs=desired_lbs),
                set([server('abc', ServerState.ACTIVE,
                            servicenet_address='1.1.1.1', created=0,
                            desired_lbs=desired_lbs),
                     server('bcd', ServerState.ACTIVE,
                            servicenet_address='2.2.2.2', created=1,
                            desired_lbs=desired_lbs)]),
                set(),
                0),
            pbag([
                DeleteServer(server_id='abc'),
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(('2.2.2.2', desc)))
            ]))


class OptimizerTests(SynchronousTestCase):
    """Tests for :func:`optimize_steps`."""

    def test_optimize_clb_adds(self):
        """
        Multiple :class:`AddNodesToCLB` steps for the same LB
        are merged into one.
        """
        steps = pbag([
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='5', port=80)))),
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.2.3.4',
                                   CLBDescription(lb_id='5', port=80))))])
        self.assertEqual(
            optimize_steps(steps),
            pbag([
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(
                        ('1.1.1.1', CLBDescription(lb_id='5', port=80)),
                        ('1.2.3.4', CLBDescription(lb_id='5', port=80)))
                )]))

    def test_optimize_clb_adds_maintain_unique_ports(self):
        """
        Multiple ports can be specified for the same address and LB ID when
        adding to a CLB.
        """
        steps = pbag([
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='5', port=80)))),
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='5', port=8080))))])

        self.assertEqual(
            optimize_steps(steps),
            pbag([
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(
                        ('1.1.1.1',
                         CLBDescription(lb_id='5', port=80)),
                        ('1.1.1.1',
                         CLBDescription(lb_id='5', port=8080))))]))

    def test_clb_adds_multiple_load_balancers(self):
        """
        Aggregation is done on a per-load-balancer basis when adding to a CLB.
        """
        steps = pbag([
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='5', port=80)))),
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.2',
                                   CLBDescription(lb_id='5', port=80)))),
            AddNodesToCLB(
                lb_id='6',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='6', port=80)))),
            AddNodesToCLB(
                lb_id='6',
                address_configs=s(('1.1.1.2',
                                   CLBDescription(lb_id='6', port=80)))),
        ])
        self.assertEqual(
            optimize_steps(steps),
            pbag([
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(
                        ('1.1.1.1', CLBDescription(lb_id='5', port=80)),
                        ('1.1.1.2', CLBDescription(lb_id='5', port=80)))),
                AddNodesToCLB(
                    lb_id='6',
                    address_configs=s(
                        ('1.1.1.1', CLBDescription(lb_id='6', port=80)),
                        ('1.1.1.2', CLBDescription(lb_id='6', port=80)))),
            ]))

    def test_optimize_clb_removes(self):
        """
        Aggregation is done on a per-load-balancer basis when remove nodes from
        a CLB.
        """
        steps = pbag([
            RemoveNodesFromCLB(lb_id='5', node_ids=s('1')),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('2')),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('3')),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('4'))])

        self.assertEqual(
            optimize_steps(steps),
            pbag([
                RemoveNodesFromCLB(lb_id='5', node_ids=s('1', '2', '3', '4'))
            ]))

    def test_clb_remove_multiple_load_balancers(self):
        """
        Multiple :class:`RemoveNodesFromCLB` steps for the same LB
        are merged into one.
        """
        steps = pbag([
            RemoveNodesFromCLB(lb_id='5', node_ids=s('1')),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('2')),
            RemoveNodesFromCLB(lb_id='6', node_ids=s('3')),
            RemoveNodesFromCLB(lb_id='6', node_ids=s('4'))])

        self.assertEqual(
            optimize_steps(steps),
            pbag([
                RemoveNodesFromCLB(lb_id='5', node_ids=s('1', '2')),
                RemoveNodesFromCLB(lb_id='6', node_ids=s('3', '4'))
            ]))

    def test_optimize_leaves_other_steps(self):
        """
        Unoptimizable steps pass the optimizer unchanged.
        """
        steps = pbag([
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='5', port=80)))),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('1')),
            CreateServer(server_config=pmap({})),
            BulkRemoveFromRCv3(lb_node_pairs=pset([("lb-1", "node-a")])),
            BulkAddToRCv3(lb_node_pairs=pset([("lb-2", "node-b")]))
            # Note that the add & remove pair should not be the same;
            # the optimizer might reasonably optimize opposite
            # operations away in the future.
        ])
        self.assertEqual(
            optimize_steps(steps),
            steps)

    def test_mixed_optimization(self):
        """
        Mixes of optimizable and unoptimizable steps still get optimized
        correctly.
        """
        steps = pbag([
            # CLB adds
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='5', port=80)))),
            AddNodesToCLB(
                lb_id='5',
                address_configs=s(('1.1.1.2',
                                   CLBDescription(lb_id='5', port=80)))),
            AddNodesToCLB(
                lb_id='6',
                address_configs=s(('1.1.1.1',
                                   CLBDescription(lb_id='6', port=80)))),
            AddNodesToCLB(
                lb_id='6',
                address_configs=s(('1.1.1.2',
                                   CLBDescription(lb_id='6', port=80)))),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('1')),
            RemoveNodesFromCLB(lb_id='5', node_ids=s('2')),
            RemoveNodesFromCLB(lb_id='6', node_ids=s('3')),
            RemoveNodesFromCLB(lb_id='6', node_ids=s('4')),

            # Unoptimizable steps
            CreateServer(server_config=pmap({})),
        ])

        self.assertEqual(
            optimize_steps(steps),
            pbag([
                # Optimized CLB adds
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(('1.1.1.1',
                                       CLBDescription(lb_id='5', port=80)),
                                      ('1.1.1.2',
                                       CLBDescription(lb_id='5', port=80)))),
                AddNodesToCLB(
                    lb_id='6',
                    address_configs=s(('1.1.1.1',
                                       CLBDescription(lb_id='6', port=80)),
                                      ('1.1.1.2',
                                       CLBDescription(lb_id='6', port=80)))),
                RemoveNodesFromCLB(lb_id='5', node_ids=s('1', '2')),
                RemoveNodesFromCLB(lb_id='6', node_ids=s('3', '4')),

                # Unoptimizable steps
                CreateServer(server_config=pmap({}))
            ]))


class LimitStepCount(SynchronousTestCase):
    """
    Tests for limiting step counts.
    """

    def _create_some_steps(self, counts={}):
        """
        Creates some steps for testing.

        :param counts: A mapping of supported step classes to the number of
            those steps to create. If unspecified, assumed to be zero.
        :return: A pbag of steps.
        """
        create_servers = [CreateServer(server_config=pmap({"sentinel": i}))
                          for i in xrange(counts.get(CreateServer, 0))]
        delete_servers = [DeleteServer(server_id='abc-' + str(i))
                          for i in xrange(counts.get(DeleteServer, 0))]
        remove_from_clbs = [RemoveNodesFromCLB(lb_id='1', node_ids=(str(i),))
                            for i in xrange(counts.get(RemoveNodesFromCLB, 0))]

        return pbag(create_servers + delete_servers + remove_from_clbs)

    def _test_limit_step_count(self, in_step_counts, step_limits):
        """
        Create some steps, limit them, assert they were limited.
        """
        in_steps = self._create_some_steps(in_step_counts)
        out_steps = _limit_step_count(in_steps, step_limits)
        expected_step_counts = {
            cls: step_limits.get(cls, in_step_count)
            for (cls, in_step_count)
            in in_step_counts.iteritems()
        }
        actual_step_counts = {
            cls: len(steps_of_this_type)
            for (cls, steps_of_this_type)
            in groupby(type, out_steps).iteritems()
        }
        self.assertEqual(expected_step_counts, actual_step_counts)

    def test_limit_step_count(self):
        """
        The steps are limited so that there are at most as many of each
        type as specified,. If no limit is specified for a type, any
        number of them are allowed.
        """
        in_step_counts = {
            CreateServer: 10,
            DeleteServer: 10
        }
        step_limits = {
            CreateServer: 3,
            DeleteServer: 10
        }
        self._test_limit_step_count(in_step_counts, step_limits)

    def test_default_step_limit(self):
        """
        The default limit limits server creation to up to 3 steps.
        """
        limits = _default_limit_step_count.keywords["step_limits"]
        self.assertEqual(limits, pmap({CreateServer: 3}))


class PlanTests(SynchronousTestCase):
    """Tests for :func:`plan`."""

    def test_plan(self):
        """An optimized plan is returned. Steps are limited."""
        desc = CLBDescription(lb_id='5', port=80)
        desired_lbs = s(desc)
        desired_group_state = DesiredGroupState(
            server_config={}, capacity=8, desired_lbs=desired_lbs)

        result = plan(
            desired_group_state,
            set([server('server1', state=ServerState.ACTIVE,
                        servicenet_address='1.1.1.1',
                        desired_lbs=desired_lbs),
                 server('server2', state=ServerState.ACTIVE,
                        servicenet_address='1.2.3.4',
                        desired_lbs=desired_lbs)]),
            set(),
            0)

        self.assertEqual(
            result,
            pbag([
                AddNodesToCLB(
                    lb_id='5',
                    address_configs=s(('1.1.1.1', desc), ('1.2.3.4', desc))
                )] + [CreateServer(server_config=pmap({}))] * 3))
