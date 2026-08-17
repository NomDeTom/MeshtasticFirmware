"""Pins the transport's MAC and routing to what the firmware in this tree actually does.

Every expected value here is computed by hand from the C++ named in the test, never from a previous
run of this simulator. A test written against the simulator's own output would pass whether or not
the firmware was read correctly, and so would pin nothing.

Run from `sim/`:  python3 -m unittest sfpp.test_mesh -v
"""

import random
import unittest

from . import mesh as M


def small_mesh(profile="2.8", nodes=12, seed=11, area=None, **kwargs):
    rng = random.Random(seed)
    conf = M.make_config()
    # Keep density roughly constant as the node count grows, or placement cannot converge.
    area = area if area is not None else max(4000.0, 400.0 * nodes**0.5 * 2)
    return M.build(conf, nodes, area, rng, hop_limit=3, profile=profile, **kwargs)


def heard(mesh, rx, peer, hops_away=0, at=None):
    """Put `peer` in `rx`'s hot store, as receiving a packet from it would.

    Routing can only see what the store holds, so a test that skips this is testing a node that has
    never heard of anyone - which is a real state, but rarely the one under test.
    """
    if at is None:
        return mesh.note_heard(rx, peer, hops_away=hops_away)
    was, mesh.now = mesh.now, at
    try:
        return mesh.note_heard(rx, peer, hops_away=hops_away)
    finally:
        mesh.now = was


class ArduinoMap(unittest.TestCase):
    """RadioInterface::getCWsize runs map() over long, which truncates toward zero and never clamps."""

    def test_endpoints(self):
        self.assertEqual(M.arduino_map(-20, -20, 10, 3, 8), 3)
        self.assertEqual(M.arduino_map(10, -20, 10, 3, 8), 8)

    def test_float_input_truncates_toward_zero(self):
        # The parameter is long, so -5.7 dB enters the map as -5: (15 * 5) / 30 + 3 = 5.
        self.assertEqual(M.arduino_map(-5.7, -20, 10, 3, 8), 5)

    def test_negative_division_truncates_not_floors(self):
        # (-5 * 5) / 30 is 0 in C and -1 under Python's //. Getting this wrong shifts the whole
        # window by one for every SNR below the floor.
        self.assertEqual(M.arduino_map(-25, -20, 10, 3, 8), 3)
        self.assertEqual(M.arduino_map(-30, -20, 10, 3, 8), 2)

    def test_does_not_clamp_above_snr_max(self):
        # (40 * 5) / 30 + 3 = 9. The firmware takes this into a uint8_t without constraining it.
        self.assertEqual(M.arduino_map(20, -20, 10, 3, 8), 9)


class ContentionWindow(unittest.TestCase):
    def test_constants_match_radiointerface_header(self):
        self.assertEqual((M.CW_MIN, M.CW_MAX), (3, 8))
        self.assertEqual((M.SNR_MIN_DB, M.SNR_MAX_DB), (-20.0, 10.0))

    def test_non_router_waits_out_the_router_window(self):
        """getTxDelayMsecWeighted: (2 * CWmax * slot) + random(0, 2^CWsize) * slot."""
        mesh = small_mesh()
        slot = mesh.slot_time_ms()
        floor = 2 * M.CW_MAX * slot
        mesh.nodes[0].role = M.CLIENT
        for _ in range(50):
            self.assertGreaterEqual(mesh.tx_delay_weighted(0, 0.0), floor)

    def test_router_draws_from_the_bottom_of_the_window(self):
        """A ROUTER's whole draw fits below the offset every other role starts at."""
        mesh = small_mesh()
        slot = mesh.slot_time_ms()
        mesh.nodes[0].role = M.ROUTER
        cw = mesh.cw_size(0, 0.0)
        for _ in range(50):
            delay = mesh.tx_delay_weighted(0, 0.0)
            self.assertLess(delay, 2 * cw * slot)
            self.assertLess(delay, 2 * M.CW_MAX * slot)

    def test_router_late_gets_no_early_window(self):
        """ROUTER_LATE relays like a router but is not one of shouldRebroadcastEarlyLikeRouter's."""
        mesh = small_mesh()
        mesh.nodes[0].role = M.ROUTER_LATE
        floor = 2 * M.CW_MAX * mesh.slot_time_ms()
        self.assertGreaterEqual(mesh.tx_delay_weighted(0, 5.0), floor)

    def test_delays_are_whole_slots(self):
        """random(0, N) is integer, so a delay is always a whole number of slot times."""
        mesh = small_mesh()
        slot = mesh.slot_time_ms()
        for _ in range(30):
            delay = mesh.tx_delay_weighted(0, -3.0) - 2 * M.CW_MAX * slot
            self.assertAlmostEqual(delay / slot, round(delay / slot), places=9)

    def test_worst_case_is_the_far_end_of_the_window(self):
        """getTxDelayMsecWeightedWorst: (2 * CWmax + 2^CWsize) * slot."""
        mesh = small_mesh()
        slot = mesh.slot_time_ms()
        cw = mesh.cw_size(0, -2.0)
        self.assertAlmostEqual(
            mesh.tx_delay_weighted_worst(0, -2.0), (2 * M.CW_MAX + 2**cw) * slot
        )

    def test_retransmission_timer_matches_the_formula(self):
        """getRetransmissionMsec: 2*airtime + (2^CW + 2*CWmax + 2^((CWmax+CWmin)/2))*slot + 4500."""
        mesh = small_mesh()
        packet = M.Packet(1, 0, 70, 40)
        slot = mesh.slot_time_ms()
        cw = M.arduino_map(
            0, 0, 100, M.CW_MIN, M.CW_MAX
        )  # idle mesh, so utilisation is zero
        expected = (
            2 * int(mesh.airtime_ms(40))
            + (2**cw + 2 * M.CW_MAX + 2 ** ((M.CW_MAX + M.CW_MIN) // 2)) * slot
            + M.PROCESSING_TIME_MSEC
        )
        self.assertAlmostEqual(mesh.retransmission_msec(0, packet), expected)

    def test_legacy_profile_keeps_the_old_window(self):
        mesh = small_mesh(profile="legacy")
        slot = mesh.slot_time_ms()
        mesh.nodes[0].role = M.CLIENT
        # No router offset at all, and the window is capped by the clamped CW.
        self.assertLess(mesh.tx_delay_weighted(0, 15.0), 2**8 * slot)
        self.assertTrue(
            any(mesh.tx_delay_weighted(0, 0.0) < 2 * 8 * slot for _ in range(50))
        )


class ChannelUtilisation(unittest.TestCase):
    """AirTime::channelUtilizationPercent - 6 x 10 s of channel-busy milliseconds."""

    def test_full_window_is_one_hundred_percent(self):
        node = M.Node(0, 0.0, 0.0)
        for bucket in range(6):
            node.log_airtime(bucket * 10000.0, 10000.0)
        self.assertAlmostEqual(node.channel_utilization_percent(50000.0), 100.0)

    def test_ring_forgets_beyond_sixty_seconds(self):
        node = M.Node(0, 0.0, 0.0)
        node.log_airtime(0.0, 10000.0)
        self.assertAlmostEqual(node.channel_utilization_percent(0.0), 100.0 / 6)
        self.assertAlmostEqual(node.channel_utilization_percent(70000.0), 0.0)

    def test_receiving_counts_toward_utilisation(self):
        """logAirtime is charged for RX too, which is what sizes our own backoff."""
        mesh = small_mesh(nodes=6)
        mesh.originate(0, 70, 60, kind="t")
        mesh.run(20000.0)
        listeners = [
            n for n in mesh.nodes if n.index != 0 and n.index in mesh.neighbours[0]
        ]
        self.assertTrue(listeners, "test needs at least one neighbour")
        self.assertTrue(
            any(n.channel_utilization_percent(mesh.now) > 0 for n in listeners)
        )


class QueueOrder(unittest.TestCase):
    """MeshPacketQueue::enqueue - deferred behind ready, priority within ready, deadline within late."""

    def setUp(self):
        self.radio = M.Node(0, 0.0, 0.0)

    def _add(self, priority, tx_after=0.0, packet_id=0):
        entry = M.QueueEntry(
            M.Packet(packet_id, 0, 70, 40, priority=priority), tx_after=tx_after
        )
        M.Mesh._enqueue(self.radio, entry)
        return entry

    def test_higher_priority_goes_first(self):
        low = self._add(M.PRIORITY_BACKGROUND, packet_id=1)
        high = self._add(M.PRIORITY_ACK, packet_id=2)
        self.assertIs(self.radio.queue[0], high)
        self.assertIs(self.radio.queue[1], low)

    def test_equal_priority_is_first_in_first_out(self):
        first = self._add(M.PRIORITY_DEFAULT, packet_id=1)
        second = self._add(M.PRIORITY_DEFAULT, packet_id=2)
        self.assertIs(self.radio.queue[0], first)
        self.assertIs(self.radio.queue[1], second)

    def test_deferred_packets_sort_behind_everything_ready(self):
        late = self._add(M.PRIORITY_ACK, tx_after=5000.0, packet_id=1)
        ready = self._add(M.PRIORITY_BACKGROUND, packet_id=2)
        self.assertIs(self.radio.queue[0], ready)
        self.assertIs(self.radio.queue[1], late)

    def test_a_full_queue_gives_up_its_cheapest_ready_packet(self):
        """replaceLowerPriorityPacket branch 1: the back is ready and worth less."""
        mesh = small_mesh(nodes=6)
        radio = mesh.nodes[0]
        radio.busy_until = 1e9
        for packet_id in range(M.QUEUE_DEPTH):
            mesh.send(0, M.Packet(packet_id, 0, 70, 40, priority=M.PRIORITY_BACKGROUND))
        mesh.send(0, M.Packet(99, 0, 70, 40, priority=M.PRIORITY_ACK))
        self.assertEqual(len(radio.queue), M.QUEUE_DEPTH)
        self.assertEqual(radio.queue[0].packet.id, 99, "the ACK should be at the front")
        self.assertNotIn(
            M.QUEUE_DEPTH - 1,
            [e.packet.id for e in radio.queue],
            "the last background packet should have been the one evicted",
        )

    def test_a_full_queue_refuses_when_nothing_is_cheaper(self):
        mesh = small_mesh(nodes=6)
        radio = mesh.nodes[0]
        radio.busy_until = 1e9
        for packet_id in range(M.QUEUE_DEPTH):
            mesh.send(0, M.Packet(packet_id, 0, 70, 40, priority=M.PRIORITY_ACK))
        self.assertIsNone(
            mesh.send(0, M.Packet(99, 0, 70, 40, priority=M.PRIORITY_BACKGROUND))
        )
        self.assertNotIn(99, [e.packet.id for e in radio.queue])

    def test_a_ready_packet_displaces_a_deferred_one(self):
        """Branch 3: ready always beats deferred once the deferred packet is overdue.

        This is the case ROUTER_LATE creates, and the reason the eviction rule matters to
        R-routerlate: a mesh with late relays queued is a mesh with a mixed queue.
        """
        mesh = small_mesh(nodes=6)
        radio = mesh.nodes[0]
        radio.busy_until = 1e9
        mesh.now = 10000.0
        for packet_id in range(M.QUEUE_DEPTH):
            entry = M.QueueEntry(
                M.Packet(packet_id, 0, 70, 40, priority=M.PRIORITY_ACK),
                tx_after=5000.0,  # already overdue
            )
            M.Mesh._enqueue(radio, entry)
        # Lowest priority there is, but it is ready, and every incumbent is deferred and overdue.
        self.assertIsNotNone(
            mesh.send(0, M.Packet(99, 0, 70, 40, priority=M.PRIORITY_BACKGROUND))
        )
        self.assertEqual(radio.queue[0].packet.id, 99)

    def test_a_deferred_packet_does_not_displace_a_pending_one(self):
        """A deferred newcomer cannot evict a deferred incumbent whose deadline has not passed."""
        mesh = small_mesh(nodes=6)
        radio = mesh.nodes[0]
        radio.busy_until = 1e9
        mesh.now = 1000.0
        for packet_id in range(M.QUEUE_DEPTH):
            M.Mesh._enqueue(
                radio,
                M.QueueEntry(M.Packet(packet_id, 0, 70, 40), tx_after=50000.0),
            )
        newcomer = M.QueueEntry(M.Packet(99, 0, 70, 40), tx_after=60000.0)
        self.assertFalse(mesh._replace_lower_priority(radio, newcomer))

    def test_the_backoff_cap_exists_only_under_legacy(self):
        """The firmware has no backoff cap - setTransmitDelay reschedules indefinitely - so no
        release series carries one, and only `legacy` does.

        The rate at which it fired is a separate question: see
        test_the_cap_alone_does_not_reproduce_pre_fold_in_drops.
        """
        self.assertIsNone(M.Profile("2.8").max_backoffs)
        self.assertEqual(M.Profile("legacy").max_backoffs, 400)

    def test_legacy_gives_up_on_a_packet_the_channel_never_clears_for(self):
        mesh = small_mesh(nodes=6, profile="legacy")
        mesh.nodes[0].busy_until = 1e9  # never clears
        mesh.send(0, M.Packet(1, 0, 70, 40))
        mesh.run(6_000_000.0)
        self.assertEqual(mesh.stats["dropped_to_backoff_cap"], 1)
        self.assertEqual(len(mesh.nodes[0].queue), 0)

    def test_the_modern_profile_waits_forever_instead(self):
        mesh = small_mesh(nodes=6, profile="2.8")
        mesh.nodes[0].busy_until = 1e9
        mesh.send(0, M.Packet(1, 0, 70, 40))
        mesh.run(6_000_000.0)
        self.assertEqual(mesh.stats["dropped_to_backoff_cap"], 0)
        self.assertEqual(len(mesh.nodes[0].queue), 1)

    def test_overflow_is_the_only_drop(self):
        """RadioLibInterface::send drops on a full queue and nowhere else.

        A blocked packet is rescheduled indefinitely by setTransmitDelay - there is no backoff cap
        in the firmware, so congestion has to surface as a full queue and as latency rather than as
        packets that quietly evaporate.
        """
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].busy_until = 1e9  # the radio never frees up
        for packet_id in range(M.QUEUE_DEPTH + 4):
            mesh.send(0, M.Packet(packet_id, 0, 70, 40))
        self.assertEqual(len(mesh.nodes[0].queue), M.QUEUE_DEPTH)
        self.assertEqual(mesh.stats["queue_drops"], 4)

    def test_a_blocked_packet_is_never_abandoned(self):
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].busy_until = 120000.0
        mesh.send(0, M.Packet(1, 0, 70, 40))
        mesh.run(60000.0)
        self.assertEqual(len(mesh.nodes[0].queue), 1, "still waiting, not dropped")
        self.assertEqual(mesh.stats["queue_drops"], 0)
        self.assertGreater(mesh.stats["deferrals"], 0)

    def test_deferred_packets_sort_by_deadline(self):
        later = self._add(M.PRIORITY_DEFAULT, tx_after=9000.0, packet_id=1)
        sooner = self._add(M.PRIORITY_DEFAULT, tx_after=4000.0, packet_id=2)
        self.assertIs(self.radio.queue[0], sooner)
        self.assertIs(self.radio.queue[1], later)


class DupeCancellation(unittest.TestCase):
    """FloodingRouter::roleAllowsCancelingDupe."""

    def test_router_never_cancels(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.ROUTER
        self.assertFalse(mesh.role_allows_canceling_dupe(0, M.Packet(1, 4, 70, 40)))

    def test_router_late_never_cancels(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.ROUTER_LATE
        self.assertFalse(mesh.role_allows_canceling_dupe(0, M.Packet(1, 4, 70, 40)))

    def test_client_cancels(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.CLIENT
        self.assertTrue(mesh.role_allows_canceling_dupe(0, M.Packet(1, 4, 70, 40)))

    def test_client_base_cancels_only_for_strangers(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.CLIENT_BASE
        mesh.nodes[0].favourites = {4}
        self.assertFalse(mesh.role_allows_canceling_dupe(0, M.Packet(1, 4, 70, 40)))
        self.assertTrue(mesh.role_allows_canceling_dupe(0, M.Packet(2, 7, 70, 40)))

    def test_legacy_profile_cancels_for_every_role(self):
        mesh = small_mesh(profile="legacy")
        mesh.nodes[0].role = M.ROUTER
        self.assertTrue(mesh.role_allows_canceling_dupe(0, M.Packet(1, 4, 70, 40)))

    def test_router_keeps_its_queued_relay_on_a_dupe(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.ROUTER
        packet = M.Packet(99, 5, 70, 40, hop_limit=3)
        packet.rx_rssi, packet.rx_snr = -100.0, 5.0
        packet.hop_start = (
            4  # one hop already taken, so this is not a repeat from the source
        )
        mesh.nodes[0].history[99] = M.SeenRecord(5, 3, 0, 0.0)
        mesh.perhaps_rebroadcast(0, packet)
        self.assertEqual(len(mesh.nodes[0].queue), 1)
        mesh._handle_dupe(0, packet, we_were_next_hop=False)
        self.assertEqual(
            len(mesh.nodes[0].queue), 1, "a ROUTER must not drop its relay"
        )
        self.assertEqual(mesh.stats["cancel_refused_by_role"], 1)

    def test_client_drops_its_queued_relay_on_a_dupe(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.CLIENT
        packet = M.Packet(99, 5, 70, 40, hop_limit=3)
        packet.rx_rssi, packet.rx_snr = -100.0, 5.0
        packet.hop_start = 4
        mesh.nodes[0].history[99] = M.SeenRecord(5, 3, 0, 0.0)
        mesh.perhaps_rebroadcast(0, packet)
        mesh._handle_dupe(0, packet, we_were_next_hop=False)
        self.assertEqual(len(mesh.nodes[0].queue), 0)
        self.assertEqual(mesh.stats["rebroadcasts_cancelled"], 1)


class LateWindow(unittest.TestCase):
    """RadioLibInterface::clampToLateRebroadcastWindow."""

    def test_router_late_moves_its_relay_to_the_back(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.ROUTER_LATE
        packet = M.Packet(99, 5, 70, 40, hop_limit=3)
        packet.rx_rssi, packet.rx_snr = -100.0, 5.0
        packet.hop_start = 4
        mesh.nodes[0].history[99] = M.SeenRecord(5, 3, 0, 0.0)
        mesh.perhaps_rebroadcast(0, packet)
        mesh._handle_dupe(0, packet, we_were_next_hop=False)
        self.assertEqual(len(mesh.nodes[0].queue), 1)
        entry = mesh.nodes[0].queue[0]
        self.assertAlmostEqual(
            entry.tx_after, mesh.now + mesh.tx_delay_weighted_worst(0, 5.0)
        )
        self.assertEqual(mesh.stats["late_window_clamps"], 1)


class HopLimit(unittest.TestCase):
    """Router::shouldDecrementHopLimit."""

    def _favourite_pair(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.ROUTER
        mesh.nodes[1].role = M.ROUTER
        mesh.nodes[0].favourites = {1}
        heard(mesh, 0, 1)  # hop preservation can only see peers in the hot store
        packet = M.Packet(1, 5, 70, 40, hop_limit=2)
        packet.hop_start = 3  # one hop taken already
        packet.relay_node = mesh.nodes[1].relay_byte
        return mesh, packet

    def test_first_hop_always_pays(self):
        mesh, packet = self._favourite_pair()
        packet.hop_start = packet.hop_limit  # nothing taken yet
        self.assertTrue(mesh.should_decrement_hop_limit(0, packet))

    def test_favourite_router_to_router_is_free(self):
        mesh, packet = self._favourite_pair()
        self.assertFalse(mesh.should_decrement_hop_limit(0, packet))
        self.assertEqual(mesh.stats["hop_limit_preserved"], 1)

    def test_non_favourite_relay_pays(self):
        mesh, packet = self._favourite_pair()
        mesh.nodes[0].favourites = set()
        self.assertTrue(mesh.should_decrement_hop_limit(0, packet))

    def test_client_always_pays(self):
        mesh, packet = self._favourite_pair()
        mesh.nodes[0].role = M.CLIENT
        self.assertTrue(mesh.should_decrement_hop_limit(0, packet))

    def test_ambiguous_relay_byte_pays(self):
        """Two known nodes sharing a last byte means the safe branch: decrement."""
        mesh, packet = self._favourite_pair()
        mesh.nodes[2].node_num = (mesh.nodes[2].node_num & ~0xFF) | mesh.nodes[
            1
        ].relay_byte
        mesh.nodes[2].role = M.ROUTER  # relevant, so it counts as a rival candidate
        heard(mesh, 0, 2)
        self.assertTrue(mesh.should_decrement_hop_limit(0, packet))

    def test_legacy_profile_always_pays(self):
        mesh = small_mesh(profile="legacy")
        mesh.nodes[0].role = M.ROUTER
        mesh.nodes[0].favourites = {1}
        heard(mesh, 0, 1)
        packet = M.Packet(1, 5, 70, 40, hop_limit=2)
        packet.hop_start = 3
        packet.relay_node = mesh.nodes[1].relay_byte
        self.assertTrue(mesh.should_decrement_hop_limit(0, packet))


class HopLimitUpgrade(unittest.TestCase):
    """FloodingRouter::perhapsHandleUpgradedPacket."""

    def test_a_longer_lived_copy_replaces_the_queued_one(self):
        mesh = small_mesh()
        first = M.Packet(42, 5, 70, 40, hop_limit=1)
        first.rx_rssi, first.rx_snr = -100.0, 3.0
        first.hop_start = 3
        mesh._receive(0, first, -100.0)
        self.assertEqual(len(mesh.nodes[0].queue), 1)
        self.assertEqual(mesh.nodes[0].queue[0].packet.hop_limit, 0)

        better = M.Packet(42, 5, 70, 40, hop_limit=3)
        better.hop_start = 3
        mesh._receive(0, better, -100.0)
        self.assertEqual(mesh.stats["hop_upgrades"], 1)
        self.assertEqual(len(mesh.nodes[0].queue), 1)
        self.assertEqual(mesh.nodes[0].queue[0].packet.hop_limit, 2)

    def test_legacy_profile_keeps_the_first_copy(self):
        mesh = small_mesh(profile="legacy")
        first = M.Packet(42, 5, 70, 40, hop_limit=1)
        first.hop_start = 3
        mesh._receive(0, first, -100.0)
        better = M.Packet(42, 5, 70, 40, hop_limit=3)
        better.hop_start = 3
        mesh._receive(0, better, -100.0)
        self.assertEqual(mesh.stats["hop_upgrades"], 0)
        self.assertEqual(mesh.nodes[0].queue[0].packet.hop_limit, 0)


class RebroadcastMode(unittest.TestCase):
    """FloodingRouter::isRebroadcaster and the modes it consults."""

    def test_none_never_relays(self):
        mesh = small_mesh()
        mesh.nodes[0].rebroadcast_mode = M.REBROADCAST_NONE
        self.assertFalse(mesh.is_rebroadcaster(0))

    def test_client_mute_never_relays(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.CLIENT_MUTE
        self.assertFalse(mesh.is_rebroadcaster(0))

    def test_core_portnums_only_drops_an_sr_advert(self):
        mesh = small_mesh()
        mesh.nodes[0].rebroadcast_mode = M.REBROADCAST_CORE_PORTNUMS_ONLY
        self.assertTrue(mesh.is_rebroadcaster(0, M.Packet(1, 5, 1, 40)))
        self.assertFalse(mesh.is_rebroadcaster(0, M.Packet(2, 5, 250, 40)))

    def test_known_only_needs_the_originator_in_the_database(self):
        mesh = small_mesh()
        mesh.nodes[0].rebroadcast_mode = M.REBROADCAST_KNOWN_ONLY
        self.assertFalse(mesh.is_rebroadcaster(0, M.Packet(1, 5, 70, 40)))
        heard(mesh, 0, 5)
        self.assertTrue(mesh.is_rebroadcaster(0, M.Packet(1, 5, 70, 40)))


class LastByteResolution(unittest.TestCase):
    """NodeDB::resolveLastByte - unique, ambiguous, or unknown."""

    def test_a_zero_low_byte_is_sent_as_ff(self):
        """getLastByteOfNodeNum: `(num & 0xFF) ? (num & 0xFF) : 0xFF`, because 0 is the sentinel."""
        node = M.Node(0, 0.0, 0.0, node_num=0x1234AB00)
        self.assertEqual(node.relay_byte, 0xFF)
        self.assertEqual(M.Node(0, 0.0, 0.0, node_num=0x1234AB07).relay_byte, 0x07)

    def test_the_three_outcomes_are_distinguished(self):
        """resolveLastByte returns a status, not just a node: NONE and AMBIGUOUS differ."""
        mesh = small_mesh()
        byte = mesh.nodes[3].relay_byte
        self.assertEqual(
            mesh.resolve_last_byte(0, byte), (M.RESOLUTION_NONE, None)
        )
        heard(mesh, 0, 3)
        self.assertEqual(mesh.resolve_last_byte(0, byte), (M.RESOLUTION_UNIQUE, 3))
        mesh.nodes[4].node_num = (mesh.nodes[4].node_num & ~0xFF) | byte
        heard(mesh, 0, 4)
        self.assertEqual(
            mesh.resolve_last_byte(0, byte), (M.RESOLUTION_AMBIGUOUS, None)
        )
        self.assertEqual(mesh.stats["next_hop_ambiguous"], 1)
        self.assertEqual(mesh.stats["next_hop_unresolved"], 1)

    def test_the_sentinel_byte_resolves_to_nothing(self):
        """0 is NO_RELAY_NODE, and getLastByteOfNodeNum never yields it."""
        mesh = small_mesh()
        heard(mesh, 0, 3)
        self.assertEqual(mesh.resolve_last_byte(0, 0), (M.RESOLUTION_NONE, None))

    def test_an_ignored_node_is_not_a_candidate(self):
        """The candidate gate drops ignored nodes, so they cannot collide with anyone."""
        mesh = small_mesh()
        byte = mesh.nodes[3].relay_byte
        mesh.nodes[4].node_num = (mesh.nodes[4].node_num & ~0xFF) | byte
        heard(mesh, 0, 3)
        heard(mesh, 0, 4)
        self.assertIsNone(mesh.resolve_unique_last_byte(0, byte))
        mesh.nodes[0].nodedb[4].is_ignored = True
        self.assertEqual(mesh.resolve_unique_last_byte(0, byte), 3)

    def test_pre_2_8_takes_the_first_match_without_checking(self):
        """resolveLastByte is new here; 2.6 and 2.7 resolve a colliding byte to whoever comes first."""
        mesh = small_mesh(profile="2.7")
        byte = mesh.nodes[3].relay_byte
        mesh.nodes[4].node_num = (mesh.nodes[4].node_num & ~0xFF) | byte
        heard(mesh, 0, 3)
        heard(mesh, 0, 4)
        status, peer = mesh.resolve_last_byte(0, byte)
        self.assertEqual(status, M.RESOLUTION_UNIQUE)
        self.assertIn(peer, (3, 4))
        self.assertEqual(mesh.stats["next_hop_ambiguous"], 0)

    def test_unique_byte_resolves(self):
        mesh = small_mesh()
        heard(mesh, 0, 3)
        self.assertEqual(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte), 3)

    def test_shared_byte_is_ambiguous(self):
        mesh = small_mesh()
        mesh.nodes[4].node_num = (mesh.nodes[4].node_num & ~0xFF) | mesh.nodes[
            3
        ].relay_byte
        heard(mesh, 0, 3)
        heard(mesh, 0, 4)
        self.assertIsNone(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte))

    def test_a_byte_we_have_not_heard_resolves_to_nothing(self):
        """The candidate gate is the hot store, so an unheard peer is not a candidate."""
        mesh = small_mesh()
        self.assertIsNone(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte))

    def test_evicting_a_peer_forgets_how_to_resolve_it(self):
        mesh = small_mesh()
        heard(mesh, 0, 3)
        self.assertEqual(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte), 3)
        del mesh.nodes[0].nodedb[3]
        self.assertIsNone(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte))

    def test_a_collision_outside_the_store_is_not_a_collision(self):
        """The reason a small store makes resolution *better*, stated as a test.

        Two nodes share a byte, but only one is in our store. A model that resolved against the
        whole mesh would call this ambiguous and fall back to flooding; the firmware resolves it.
        """
        mesh = small_mesh()
        mesh.nodes[4].node_num = (mesh.nodes[4].node_num & ~0xFF) | mesh.nodes[
            3
        ].relay_byte
        heard(mesh, 0, 3)
        self.assertEqual(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte), 3)

    def test_the_send_path_needs_a_fresh_direct_neighbour(self):
        """requireDirectNeighbor: hops_away 0 and heard inside NEXTHOP_NEIGHBOR_FRESH_SECS."""
        mesh = small_mesh()
        mesh.now = 0.0
        heard(mesh, 0, 3, hops_away=1)
        byte = mesh.nodes[3].relay_byte
        self.assertIsNone(
            mesh.resolve_unique_last_byte(0, byte, require_direct_neighbour=True)
        )
        heard(mesh, 0, 3, hops_away=0)
        self.assertEqual(
            mesh.resolve_unique_last_byte(0, byte, require_direct_neighbour=True), 3
        )
        mesh.now = M.NEXTHOP_NEIGHBOR_FRESH_MSEC + 1
        self.assertIsNone(
            mesh.resolve_unique_last_byte(0, byte, require_direct_neighbour=True),
            "a neighbour not heard for two hours is not a usable next hop",
        )

    def test_the_relay_path_accepts_a_router_that_is_not_a_neighbour(self):
        """Without requireDirectNeighbor the gate widens to favourites and router-like nodes."""
        mesh = small_mesh()
        mesh.nodes[3].role = M.ROUTER
        heard(mesh, 0, 3, hops_away=2)
        self.assertEqual(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte), 3)

    def test_a_distant_client_is_not_a_relevant_candidate(self):
        mesh = small_mesh()
        mesh.nodes[3].role = M.CLIENT
        heard(mesh, 0, 3, hops_away=2)
        self.assertIsNone(mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte))

    def test_zero_is_no_preference_not_a_node(self):
        mesh = small_mesh()
        self.assertIsNone(mesh.resolve_unique_last_byte(0, M.NO_NEXT_HOP_PREFERENCE))


class NextHop(unittest.TestCase):
    """NextHopRouter::getNextHop and its decay back to flooding."""

    def _routed(self):
        mesh = small_mesh()
        peer = mesh.neighbours[0][0]
        dest = 7 if 7 not in (0, peer) else 8
        heard(mesh, 0, peer)
        heard(mesh, 0, dest, hops_away=2)
        mesh.nodes[0].nodedb[dest].next_hop = mesh.nodes[peer].relay_byte
        mesh.note_route_learned(0, dest, mesh.nodes[peer].relay_byte)
        return mesh, dest, peer

    def test_broadcast_never_gets_a_next_hop(self):
        mesh = small_mesh()
        self.assertIsNone(mesh.get_next_hop(0, M.BROADCAST, 0))

    def test_a_fresh_route_is_used(self):
        mesh, dest, peer = self._routed()
        self.assertEqual(mesh.get_next_hop(0, dest, 0), mesh.nodes[peer].relay_byte)

    def test_never_hands_the_packet_back_to_its_relay(self):
        mesh, dest, peer = self._routed()
        self.assertIsNone(mesh.get_next_hop(0, dest, mesh.nodes[peer].relay_byte))

    def test_a_stale_route_floods_and_is_cleared(self):
        mesh, dest, _ = self._routed()
        mesh.now = M.ROUTE_TTL_MSEC + 1
        self.assertIsNone(mesh.get_next_hop(0, dest, 0))
        self.assertEqual(mesh.nodes[0].nodedb[dest].next_hop, M.NO_NEXT_HOP_PREFERENCE)
        self.assertEqual(mesh.stats["route_expired_ttl"], 1)
        self.assertEqual(mesh.stats["route_expired_failures"], 0)

    def test_three_failures_kill_the_route(self):
        mesh, dest, _ = self._routed()
        for _ in range(M.ROUTE_FAILURE_THRESHOLD):
            mesh.note_route_failure(0, dest)
        self.assertIsNone(mesh.get_next_hop(0, dest, 0))
        self.assertEqual(mesh.nodes[0].nodedb[dest].next_hop, M.NO_NEXT_HOP_PREFERENCE)

    def test_legacy_profile_has_no_unicast_routing(self):
        mesh = small_mesh(profile="legacy")
        heard(mesh, 0, 7)
        mesh.nodes[0].nodedb[7].next_hop = mesh.nodes[1].relay_byte
        self.assertIsNone(mesh.get_next_hop(0, 7, 0))

    def test_relay_gate_ignores_a_packet_addressed_to_another_hop(self):
        mesh = small_mesh()
        packet = M.Packet(5, 9, 70, 40, hop_limit=3, destination=6)
        packet.rx_rssi, packet.rx_snr = -100.0, 2.0
        used = {n.relay_byte for n in mesh.nodes}
        packet.next_hop = next(b for b in range(1, 256) if b not in used)
        self.assertFalse(mesh.perhaps_rebroadcast(0, packet))


class HotStore(unittest.TestCase):
    """NodeDB as a bounded store, and the four separate ways a learned next hop dies."""

    def test_the_store_is_capped_and_drops_the_stalest(self):
        mesh = small_mesh(nodes=12, max_num_nodes=4)
        for peer in range(1, 6):
            heard(mesh, 0, peer, at=float(peer))
        store = mesh.nodes[0].nodedb
        self.assertEqual(len(store), 4)
        self.assertNotIn(1, store, "the least-recently-heard record goes first")
        self.assertIn(5, store)

    def test_a_favourite_outranks_recency(self):
        """demoteOldestHotNodesToWarm: protection beats recency, always."""
        mesh = small_mesh(nodes=12, max_num_nodes=3)
        mesh.nodes[0].favourites = {1}
        heard(mesh, 0, 1, at=1.0)  # oldest, but protected
        for peer in (2, 3, 4):
            heard(mesh, 0, peer, at=float(peer) * 10)
        self.assertIn(1, mesh.nodes[0].nodedb)

    def test_eviction_forgets_the_route_with_no_expiry_involved(self):
        """The quietest of the four deaths: no TTL, no failure, no fallback - just gone."""
        mesh = small_mesh(nodes=12, max_num_nodes=3)
        heard(mesh, 0, 1, at=1.0)
        heard(mesh, 0, 9, at=2.0)
        mesh.nodes[0].nodedb[9].next_hop = mesh.nodes[1].relay_byte
        mesh.note_route_learned(0, 9, mesh.nodes[1].relay_byte)
        for peer in (2, 3, 4):
            heard(mesh, 0, peer, at=float(peer) * 10)
        self.assertNotIn(9, mesh.nodes[0].nodedb)
        self.assertEqual(mesh.stats["routes_lost_to_eviction"], 1)
        self.assertIsNone(mesh.get_next_hop(0, 9, 0))
        self.assertEqual(mesh.stats["route_expired_ttl"], 0)
        self.assertEqual(mesh.stats["route_expired_failures"], 0)

    def test_the_two_health_expiries_are_told_apart(self):
        mesh = small_mesh()
        peer = mesh.neighbours[0][0]
        dest = next(i for i in range(len(mesh.nodes)) if i not in (0, peer))
        heard(mesh, 0, peer)
        heard(mesh, 0, dest, hops_away=2)
        mesh.nodes[0].nodedb[dest].next_hop = mesh.nodes[peer].relay_byte
        mesh.note_route_learned(0, dest, mesh.nodes[peer].relay_byte)
        for _ in range(M.ROUTE_FAILURE_THRESHOLD):
            mesh.note_route_failure(0, dest)
        self.assertIsNone(mesh.get_next_hop(0, dest, 0))
        self.assertEqual(mesh.stats["route_expired_failures"], 1)
        self.assertEqual(mesh.stats["route_expired_ttl"], 0)

    def test_a_fresh_route_dies_when_its_neighbour_goes_quiet(self):
        """The fourth death, and the one with the longest clock: resolution freshness.

        The route is inside its 30-minute TTL and has never failed, but the neighbour it points at
        has not been heard for two hours, so the byte no longer resolves on the send path.
        """
        mesh = small_mesh()
        peer = mesh.neighbours[0][0]
        dest = next(i for i in range(len(mesh.nodes)) if i not in (0, peer))
        mesh.now = 0.0
        heard(mesh, 0, peer, hops_away=0)
        heard(mesh, 0, dest, hops_away=2)
        mesh.nodes[0].nodedb[dest].next_hop = mesh.nodes[peer].relay_byte
        mesh.note_route_learned(0, dest, mesh.nodes[peer].relay_byte)

        mesh.now = M.ROUTE_TTL_MSEC - 1  # still inside the route's own TTL
        self.assertIsNotNone(mesh.get_next_hop(0, dest, 0))

        # Re-learn so the TTL cannot be what expires, then let the neighbour go quiet.
        mesh.now = M.NEXTHOP_NEIGHBOR_FRESH_MSEC + 1
        mesh.note_route_learned(0, dest, mesh.nodes[peer].relay_byte)
        self.assertIsNone(mesh.get_next_hop(0, dest, 0))
        self.assertEqual(
            mesh.stats["route_expired_ttl"], 0, "not an expiry - a resolution failure"
        )

    def test_packet_history_is_a_ring(self):
        """PACKETHISTORY_MAX: twice the hot store, floored at 100, oldest evicted."""
        mesh = small_mesh(nodes=6, max_num_nodes=10)
        node = mesh.nodes[0]
        self.assertEqual(node.history_max, 100)
        for packet_id in range(node.history_max + 5):
            node.remember(packet_id, M.SeenRecord(1, 3, 0, float(packet_id)))
        self.assertEqual(len(node.history), node.history_max)
        self.assertNotIn(0, node.history)
        self.assertNotIn(0, node.seen, "seen is the same ring, not a second one")
        self.assertIn(node.history_max + 4, node.history)

    def test_a_forgotten_packet_can_be_relayed_again(self):
        """The consequence of the ring: eviction restores a node's willingness to relay."""
        mesh = small_mesh(nodes=6, max_num_nodes=10)
        node = mesh.nodes[0]
        packet = M.Packet(1, 5, 70, 40, hop_limit=3)
        packet.hop_start = (
            4  # one hop already taken, so this is not an originator retry
        )
        mesh._receive(0, packet, -100.0)
        self.assertEqual(len(node.queue), 1)
        node.queue.clear()
        mesh._receive(0, packet, -100.0)
        self.assertEqual(len(node.queue), 0, "still remembered, so still suppressed")

        for packet_id in range(2, node.history_max + 3):
            node.remember(packet_id, M.SeenRecord(1, 3, 0, float(packet_id) * 1000))
        self.assertNotIn(1, node.history)
        mesh._receive(0, packet, -100.0)
        self.assertEqual(len(node.queue), 1, "forgotten, so relayed as if new")


class Platforms(unittest.TestCase):
    def test_store_sizes_match_mesh_pb_constants(self):
        self.assertEqual(M.PLATFORM_HOT_STORE["stm32wl"], 10)
        self.assertEqual(M.PLATFORM_HOT_STORE["nrf52840"], 120)
        self.assertEqual(M.PLATFORM_HOT_STORE["esp32s3_16mb"], 250)

    def test_a_uniform_mesh_is_all_one_board(self):
        mesh = small_mesh(nodes=20, platform_mix="uniform")
        self.assertEqual({n.platform for n in mesh.nodes}, {"nrf52840"})
        self.assertEqual({n.max_num_nodes for n in mesh.nodes}, {120})

    def test_a_mixed_mesh_has_nodes_with_different_stores(self):
        mesh = small_mesh(nodes=60, platform_mix="baymesh-2026-08", seed=4)
        sizes = {n.max_num_nodes for n in mesh.nodes}
        self.assertGreater(len(sizes), 1, "the point of a mix is that nodes differ")
        for node in mesh.nodes:
            self.assertEqual(node.max_num_nodes, M.PLATFORM_HOT_STORE[node.platform])
            self.assertEqual(node.history_max, M.packet_history_max(node.max_num_nodes))

    def test_a_single_board_can_be_named_directly(self):
        mesh = small_mesh(nodes=12, platform_mix="stm32wl")
        self.assertEqual({n.max_num_nodes for n in mesh.nodes}, {10})
        self.assertEqual({n.history_max for n in mesh.nodes}, {100})

    def test_an_unknown_mix_is_refused(self):
        with self.assertRaises(ValueError):
            small_mesh(nodes=6, platform_mix="pentium")

    def test_the_board_table_is_derived_from_this_tree(self):
        """Spot-checks against variants/*/platformio.ini, which is where these numbers come from.

        Heltec V3 is the one worth pinning: it is an 8 MB ESP32-S3, so it gets 200 slots, not the
        120 that an "nRF52840-ish default" assumption hands it.
        """
        self.assertEqual(M.HARDWARE_STORE["HELTEC_V3"], 200)
        self.assertEqual(M.HARDWARE_STORE["HELTEC_V4"], 250)
        self.assertEqual(M.HARDWARE_STORE["RAK4631"], 120)
        self.assertEqual(M.HARDWARE_STORE["STATION_G2"], 250)
        self.assertEqual(M.HARDWARE_STORE["T_DECK"], 250)
        self.assertEqual(M.HARDWARE_STORE["TRACKER_T1000_E"], 120)
        self.assertEqual(M.HARDWARE_STORE["TLORA_T3_S3"], 100)

    def test_a_census_converts_to_a_mix(self):
        mix = M.census_to_mix({"RAK4631": 421, "HELTEC_V3": 233, "T_DECK": 32})
        self.assertAlmostEqual(sum(mix.values()), 1.0)
        self.assertAlmostEqual(mix["nrf52840"], 421 / 686, places=3)
        self.assertAlmostEqual(mix["esp32s3_8mb"], 233 / 686, places=3)

    def test_a_census_normalises_names(self):
        self.assertEqual(
            M.census_to_mix({"heltec-v3": 1}), M.census_to_mix({"HELTEC_V3": 1})
        )

    def test_an_unknown_model_is_not_silently_bucketed(self):
        """A census that is 30% 'unrecognised' must not quietly become a census of the default."""
        with self.assertRaises(ValueError):
            M.census_to_mix({"RAK4631": 10, "TOTALLY_MADE_UP": 5})

    def test_an_empty_census_is_refused(self):
        with self.assertRaises(ValueError):
            M.census_to_mix({"RAK4631": 0})

    def test_the_measured_mix_matches_the_census_it_came_from(self):
        """The published mix must be reproducible from the raw counts, not hand-tuned afterwards."""
        census = {
            "RAK4631": 421,
            "HELTEC_V3": 233,
            "HELTEC_V4": 180,
            "TRACKER_T1000_E": 135,
            "SEEED_SOLAR_NODE": 98,
            "STATION_G2": 84,
            "SEEED_WIO_TRACKER_L1": 77,
            "HELTEC_MESH_NODE_T114": 62,
            "T_DECK": 32,
            "T_ECHO": 28,
            "HELTEC_MESH_POCKET": 28,
            "RAK3401": 27,
            "WISMESH_TAG": 27,
            "LILYGO_TBEAM_S3_CORE": 26,
            "XIAO_NRF52_KIT": 23,
            "TBEAM": 22,
            "SEEED_XIAO_S3": 19,
            "HELTEC_WIRELESS_TRACKER": 17,
        }
        derived = M.census_to_mix(census)
        published = M.PLATFORM_MIXES["baymesh-2026-08"]
        self.assertEqual(set(derived), set(published))
        for platform, share in published.items():
            self.assertAlmostEqual(derived[platform], share, places=2)


class RoleCensus(unittest.TestCase):
    """Role shares from the same 1769-node census."""

    def test_the_measured_shares_are_what_gets_assigned(self):
        mesh = small_mesh(nodes=200, seed=5, role_mix="baymesh-2026-08")
        counts = {}
        for node in mesh.nodes:
            counts[node.role] = counts.get(node.role, 0) + 1
        self.assertEqual(counts[M.ROUTER], 8)  # 4% of 200
        self.assertEqual(counts[M.ROUTER_LATE], 6)  # 3%
        self.assertEqual(counts[M.CLIENT_BASE], 32)  # 16%
        self.assertEqual(counts[M.CLIENT_MUTE], 36)  # 18%

    def test_the_census_has_far_fewer_routers_than_the_old_default(self):
        """4% measured against the 10% the simulator assumed."""
        self.assertLess(M.ROLE_MIXES["baymesh-2026-08"][M.ROUTER], 0.05)
        self.assertEqual(M.ROLE_MIXES["legacy-default"][M.ROUTER], 0.10)

    def test_muted_nodes_never_relay(self):
        """18% of the real mesh, and none of it was modelled before the census."""
        mesh = small_mesh(nodes=60, seed=5, role_mix="baymesh-2026-08")
        muted = [n.index for n in mesh.nodes if n.role == M.CLIENT_MUTE]
        self.assertTrue(muted)
        for index in muted:
            self.assertFalse(mesh.is_rebroadcaster(index))

    def test_router_like_roles_go_to_the_best_sited_nodes(self):
        mesh = small_mesh(nodes=100, seed=5, role_mix="baymesh-2026-08")
        degrees = [len(mesh.neighbours[i]) for i in range(100)]
        router_like = [i for i in range(100) if mesh.nodes[i].is_router_like()]
        others = [i for i in range(100) if not mesh.nodes[i].is_router_like()]
        best_other = max(degrees[i] for i in others)
        self.assertTrue(all(degrees[i] >= best_other for i in router_like))

    def test_a_role_mix_can_be_passed_directly(self):
        mesh = small_mesh(nodes=100, seed=5, role_mix={M.ROUTER: 0.5, M.CLIENT: 0.5})
        self.assertEqual(sum(1 for n in mesh.nodes if n.role == M.ROUTER), 50)


class Reliable(unittest.TestCase):
    """ReliableRouter / NextHopRouter::doRetransmissions."""

    def test_attempt_counts_match_the_header(self):
        self.assertEqual(M.NUM_RELIABLE_RETX, 3)
        self.assertEqual(M.NUM_RELIABLE_UNICAST_ATTEMPTS, 5)

    def test_hearing_a_relay_is_an_implicit_ack_and_stops_the_retries(self):
        """ReliableRouter::perhapsGenerateImplicitAckForOwnOverheard.

        The point of this optimisation is airtime, so it is worth pinning: on a mesh with any
        neighbour at all, the first relay we overhear ends the retransmission schedule outright.
        """
        mesh = small_mesh()
        mesh.originate(0, 70, 40, want_ack=True)
        self.assertEqual(len(mesh.nodes[0].reliable), 1)
        mesh.run(600000.0)
        self.assertEqual(mesh.stats["reliable_retx"], 0)
        self.assertEqual(mesh.nodes[0].reliable, {})

    def test_an_unheard_broadcast_retries(self):
        mesh = small_mesh()
        mesh.neighbours[0] = []  # nothing hears us, so no implicit ACK ever comes back
        mesh.originate(0, 70, 40, want_ack=True)
        mesh.run(600000.0)
        self.assertEqual(mesh.stats["reliable_retx"], M.NUM_RELIABLE_RETX - 1)
        self.assertEqual(mesh.stats["reliable_failures"], 1)

    def test_the_last_directed_try_falls_back_to_flooding(self):
        mesh = small_mesh()
        peer = mesh.neighbours[0][0]
        dest = next(i for i in range(len(mesh.nodes)) if i not in (0, peer))
        heard(mesh, 0, peer)
        heard(mesh, 0, dest, hops_away=2)
        mesh.nodes[0].nodedb[dest].next_hop = mesh.nodes[peer].relay_byte
        mesh.note_route_learned(0, dest, mesh.nodes[peer].relay_byte)
        mesh.neighbours[0] = []  # the route is dead; nothing comes back
        mesh.originate(0, 70, 40, destination=dest, want_ack=True)
        mesh.run(1800000.0)
        self.assertGreater(mesh.stats["next_hop_fallbacks"], 0)
        self.assertEqual(mesh.nodes[0].nodedb[dest].next_hop, M.NO_NEXT_HOP_PREFERENCE)


class Opaque(unittest.TestCase):
    """NextHopRouter::relayOpaquePacket - relayed, but never seen."""

    def test_an_undecodable_packet_is_relayed_from_the_header(self):
        mesh = small_mesh()
        packet = M.Packet(77, 5, 70, 40, hop_limit=3, opaque=True)
        mesh._receive(0, packet, -100.0)
        self.assertEqual(mesh.stats["opaque_relays"], 1)
        self.assertEqual(len(mesh.nodes[0].queue), 1)
        self.assertEqual(mesh.nodes[0].queue[0].packet.hop_limit, 2)

    def test_it_never_enters_history_or_the_app_layer(self):
        mesh = small_mesh()
        heard = []
        mesh.on_receive = lambda *args: heard.append(args)
        mesh._receive(0, M.Packet(77, 5, 70, 40, hop_limit=3, opaque=True), -100.0)
        self.assertEqual(heard, [])
        self.assertNotIn(77, mesh.nodes[0].history)

    def test_rebroadcast_mode_none_blocks_it(self):
        mesh = small_mesh()
        mesh.nodes[0].rebroadcast_mode = M.REBROADCAST_NONE
        mesh._receive(0, M.Packet(77, 5, 70, 40, hop_limit=3, opaque=True), -100.0)
        self.assertEqual(mesh.stats["opaque_relays"], 0)


class ForkExtras(unittest.TestCase):
    def test_hop_exhaustion_relays_once_with_nothing_left(self):
        """TrafficManagementModule::shouldExhaustHops."""
        mesh = small_mesh(profile=M.Profile("2.8", exhaust_hops=True))
        mesh.should_exhaust_hops = lambda packet: True
        packet = M.Packet(3, 5, 70, 40, hop_limit=3)
        packet.rx_rssi, packet.rx_snr = -100.0, 2.0
        self.assertTrue(mesh.perhaps_rebroadcast(0, packet))
        self.assertEqual(mesh.nodes[0].queue[0].packet.hop_limit, 0)
        self.assertEqual(mesh.stats["hops_exhausted"], 1)

    def test_event_mode_caps_what_a_relay_passes_on(self):
        """NextHopRouter::capEventRelayHops."""
        mesh = small_mesh(profile=M.Profile("2.8", event_relay_hop_limit=2))
        packet = M.Packet(3, 5, 70, 40, hop_limit=7)
        packet.rx_rssi, packet.rx_snr = -100.0, 2.0
        mesh.perhaps_rebroadcast(0, packet)
        self.assertEqual(mesh.nodes[0].queue[0].packet.hop_limit, 2)


class WarmTier(unittest.TestCase):
    """WarmNodeStore - what an evicted node keeps, and what it loses."""

    def small(self, slots=3, warm=4, profile="2.8"):
        mesh = small_mesh(nodes=10, profile=profile)
        for node in mesh.nodes:
            node.max_num_nodes = slots
            node.warm_num_nodes = warm
            node.cold_cache_size = 0
        return mesh

    def test_eviction_demotes_rather_than_forgetting(self):
        mesh = self.small()
        for peer in (1, 2, 3, 4):
            heard(mesh, 0, peer, at=peer * 1000.0)
        node = mesh.nodes[0]
        self.assertEqual(len(node.nodedb), 3)
        self.assertIn(1, node.warm, "the stalest record is the one demoted")
        self.assertEqual(mesh.stats["warm_demotions"], 1)

    def test_re_admission_empties_the_warm_slot(self):
        """A node lives in hot or warm, never both."""
        mesh = self.small()
        for peer in (1, 2, 3, 4):
            heard(mesh, 0, peer, at=peer * 1000.0)
        node = mesh.nodes[0]
        self.assertIn(1, node.warm)
        heard(mesh, 0, 1, at=9000.0)
        self.assertIn(1, node.nodedb)
        self.assertNotIn(1, node.warm)
        self.assertEqual(mesh.stats["warm_promotions"], 1)
        for peer in node.nodedb:
            self.assertNotIn(peer, node.warm)

    def test_the_key_survives_demotion_but_the_route_does_not(self):
        """The tier exists for the key; next_hop and hops_away are hot-store fields."""
        mesh = self.small()
        node = mesh.nodes[0]
        record = heard(mesh, 0, 1, hops_away=0, at=1000.0)
        record.has_key = True
        record.next_hop = 0x42
        for peer in (2, 3, 4):
            heard(mesh, 0, peer, at=peer * 1000.0)
        self.assertNotIn(1, node.nodedb)
        self.assertTrue(node.warm[1].has_key)
        self.assertTrue(node.knows_key(1), "a warm key is still authoritative")
        # Re-admitted without a usable hop count, so nothing but the key comes back: the route and
        # the hop distance start again from what the next packets show.
        mesh.now = 9000.0
        readmitted = mesh.note_heard(0, 1, hops_away=None)
        self.assertTrue(readmitted.has_key)
        self.assertEqual(readmitted.next_hop, M.NO_NEXT_HOP_PREFERENCE)
        self.assertIsNone(readmitted.hops_away)

    def test_a_keyless_entry_never_displaces_a_keyed_one(self):
        """absorb(): keyless candidates never displace keyed entries."""
        mesh = self.small(slots=2, warm=1)
        node = mesh.nodes[0]
        keyed = heard(mesh, 0, 1, at=1000.0)
        keyed.has_key = True
        heard(mesh, 0, 2, at=2000.0)
        heard(mesh, 0, 3, at=3000.0)  # evicts 1, which is keyed, into the warm slot
        self.assertTrue(node.warm[1].has_key)
        heard(mesh, 0, 4, at=4000.0)  # evicts 2, keyless, against a full keyed tier
        self.assertIn(1, node.warm, "the keyed identity is kept")
        self.assertNotIn(2, node.warm)
        self.assertEqual(mesh.stats["warm_evictions"], 0)

    def test_last_heard_is_quantised_to_128_seconds(self):
        """The low seven bits of last_heard carry role, protection and the signed flag."""
        self.assertEqual(M.warm_quantise(0.0), 0.0)
        self.assertEqual(M.warm_quantise(127_999.0), 0.0)
        self.assertEqual(M.warm_quantise(128_000.0), 128_000.0)
        self.assertEqual(M.warm_quantise(200_000.0), 128_000.0)
        entry = M.WarmEntry(200_000.0)
        self.assertEqual(entry.last_heard, 128_000.0)

    def test_no_warm_tier_before_this_tree_or_on_the_smallest_board(self):
        mesh = self.small(profile="2.7")
        for node in mesh.nodes:
            node.warm_num_nodes = 0
        for peer in (1, 2, 3, 4):
            heard(mesh, 0, peer, at=peer * 1000.0)
        self.assertEqual(mesh.nodes[0].warm, {})
        self.assertEqual(mesh.stats["warm_demotions"], 0)
        self.assertEqual(M.PLATFORM_WARM_STORE["stm32wl"], 0)
        self.assertFalse(M.Profile("2.7").warm_store)


class PacketSigning(unittest.TestCase):
    """Router.cpp: a 64-byte XEdDSA signature, the size gate, and the three receive policies."""

    def test_the_size_gate_is_the_frame_budget(self):
        """signedDataFits: payload + 66 + 16 <= 255, so 173 bytes is the last that signs."""
        self.assertTrue(M.signed_data_fits(173))
        self.assertFalse(M.signed_data_fits(174))

    def test_a_broadcast_carries_the_signature_in_its_airtime(self):
        mesh = small_mesh(nodes=6)
        packet = mesh.originate(0, 1, 60)
        self.assertTrue(packet.xeddsa_signed)
        self.assertEqual(packet.length, 60 + M.XEDDSA_SIGNATURE_FIELD_BYTES)
        self.assertEqual(mesh.stats["packets_signed"], 1)

    def test_an_oversized_payload_goes_unsigned_rather_than_undelivered(self):
        """The gate exists so a packet that would not fit signed is sent as it is."""
        mesh = small_mesh(nodes=6)
        packet = mesh.originate(0, 1, 200)
        self.assertFalse(packet.xeddsa_signed)
        self.assertEqual(packet.length, 200)
        self.assertEqual(mesh.stats["packets_too_large_to_sign"], 1)

    def test_a_dm_is_not_signed(self):
        """Signing covers unencrypted broadcasts; a unicast only when the operator is licensed."""
        mesh = small_mesh(nodes=6)
        heard(mesh, 0, 1).has_key = True
        packet = mesh.originate(0, 1, 40, destination=1, pki=True)
        self.assertFalse(packet.xeddsa_signed)
        self.assertTrue(packet.pki_encrypted)

    def test_no_series_before_this_tree_signs(self):
        for version in ("2.4", "2.5", "2.6", "2.7"):
            mesh = small_mesh(nodes=6, profile=version)
            packet = mesh.originate(0, 1, 60)
            self.assertFalse(packet.xeddsa_signed, version)
            self.assertEqual(packet.length, 60, version)

    def _packet(self, mesh, signed=True, length=40, portnum=1):
        packet = M.Packet(7, 3, portnum, length, hop_limit=3)
        packet.xeddsa_signed = signed
        return packet

    def test_strict_drops_what_it_cannot_verify(self):
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].signature_policy = M.SIGNATURE_POLICY_STRICT
        signed = self._packet(mesh)
        self.assertFalse(mesh._signature_policy_admits(0, signed))
        self.assertEqual(mesh.stats["dropped_unverifiable"], 1)
        heard(mesh, 0, 3).has_key = True
        self.assertTrue(mesh._signature_policy_admits(0, signed))

    def test_strict_drops_unsigned_traffic_outright(self):
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].signature_policy = M.SIGNATURE_POLICY_STRICT
        self.assertFalse(mesh._signature_policy_admits(0, self._packet(mesh, signed=False)))
        self.assertEqual(mesh.stats["dropped_unsigned_strict"], 1)

    def test_a_signed_nodeinfo_bootstraps_its_own_key(self):
        """verifyFirstContactNodeInfo: the packet carries the key its node number is derived from."""
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].signature_policy = M.SIGNATURE_POLICY_STRICT
        info = self._packet(mesh, portnum=M.NODEINFO_PORTNUM)
        self.assertTrue(mesh._signature_policy_admits(0, info))
        self.assertTrue(mesh.nodes[0].nodedb[3].has_key)
        self.assertEqual(mesh.stats["signature_bootstraps"], 1)

    def test_balanced_drops_only_a_downgrade_from_a_known_signer(self):
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].signature_policy = M.SIGNATURE_POLICY_BALANCED
        plain = self._packet(mesh, signed=False)
        self.assertTrue(mesh._signature_policy_admits(0, plain), "not a known signer yet")
        record = heard(mesh, 0, 3)
        record.has_key = True
        self.assertTrue(mesh._signature_policy_admits(0, self._packet(mesh)))
        self.assertTrue(record.xeddsa_signed, "verifying marks the sender as a signer")
        self.assertFalse(mesh._signature_policy_admits(0, plain))
        self.assertEqual(mesh.stats["dropped_downgrade"], 1)

    def test_a_payload_too_big_to_sign_escapes_the_downgrade_rule(self):
        """The gate an attacker inflates past, and the reason a growing signable type breaks."""
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].signature_policy = M.SIGNATURE_POLICY_BALANCED
        record = heard(mesh, 0, 3)
        record.has_key = True
        record.xeddsa_signed = True
        self.assertFalse(mesh._signature_policy_admits(0, self._packet(mesh, signed=False)))
        big = self._packet(mesh, signed=False, length=200)
        self.assertTrue(mesh._signature_policy_admits(0, big))

    def test_compatible_takes_everything(self):
        mesh = small_mesh(nodes=6)
        record = heard(mesh, 0, 3)
        record.has_key = True
        record.xeddsa_signed = True
        self.assertTrue(mesh._signature_policy_admits(0, self._packet(mesh, signed=False)))
        self.assertEqual(mesh.stats["dropped_downgrade"], 0)

    def test_a_pki_dm_passes_every_policy_unread(self):
        mesh = small_mesh(nodes=6)
        mesh.nodes[0].signature_policy = M.SIGNATURE_POLICY_STRICT
        packet = self._packet(mesh, signed=False)
        packet.pki_encrypted = True
        self.assertTrue(mesh._signature_policy_admits(0, packet))


class AdaptiveCongestion(unittest.TestCase):
    """Default::getConfiguredOrDefaultMsScaled - each node throttles on what it has heard."""

    def test_the_coefficient_comes_from_this_node_s_own_store(self):
        import random

        from . import traffic as T

        mesh = small_mesh(nodes=60, seed=2)
        gen = T.Generator(mesh, random.Random(1), bytes(range(16)))
        self.assertEqual(gen.node_congestion(0), 1.0, "a node that has heard nobody")
        for peer in range(1, 60):
            heard(mesh, 0, peer)
        self.assertGreater(
            gen.node_congestion(0),
            1.0,
            "having heard the mesh, the same node throttles",
        )
        self.assertEqual(gen.node_congestion(1), 1.0, "and node 1 has still heard nobody")

    def test_the_two_hour_window_bounds_the_input(self):
        import random

        from . import traffic as T

        mesh = small_mesh(nodes=60, seed=2)
        gen = T.Generator(mesh, random.Random(1), bytes(range(16)))
        for peer in range(1, 60):
            heard(mesh, 0, peer)
        self.assertGreater(gen.node_congestion(0), 1.0)
        mesh.now = M.NUM_ONLINE_SECS * 1000.0 + 1
        self.assertEqual(
            gen.node_congestion(0), 1.0, "nothing heard inside the window is online"
        )

    def test_static_mode_keeps_one_coefficient_for_the_whole_mesh(self):
        import random

        from . import traffic as T

        mesh = small_mesh(nodes=60, seed=2)
        gen = T.Generator(
            mesh, random.Random(1), bytes(range(16)), congestion_mode="static"
        )
        self.assertEqual(gen.node_congestion(0), gen.congestion)
        self.assertGreater(gen.congestion, 1.0)


class KeyEconomics(unittest.TestCase):
    """What eviction costs: not a worse route, but no conversation until NodeInfo is heard again."""

    def test_a_pki_dm_needs_a_key_from_some_tier(self):
        mesh = small_mesh(nodes=6)
        self.assertIsNone(
            mesh.originate(0, 1, 40, destination=1, pki=True),
            "no key in any tier, so nothing is composed",
        )
        self.assertEqual(mesh.stats["dm_blocked_no_key"], 1)
        heard(mesh, 0, 1).has_key = True
        self.assertIsNotNone(mesh.originate(0, 1, 40, destination=1, pki=True))

    def test_nodeinfo_is_what_teaches_a_key(self):
        mesh = small_mesh(nodes=8, seed=5)
        peer = next(iter(mesh.neighbours[0]))
        mesh.originate(peer, M.NODEINFO_PORTNUM, 40, kind="nodeinfo")
        mesh.run(30000.0)
        self.assertTrue(mesh.nodes[0].nodedb[peer].has_key)
        self.assertTrue(mesh.nodes[0].knows_key(peer))

    def test_the_cold_cache_answers_when_both_other_tiers_have_dropped_the_peer(self):
        """A cold key is usable on the decrypt path and is never authoritative."""
        mesh = small_mesh(nodes=6)
        node = mesh.nodes[0]
        node.cold_cache_size = 8
        node.warm_num_nodes = 0
        mesh._cache_cold_key(0, 3)
        self.assertNotIn(3, node.nodedb)
        self.assertTrue(node.knows_key(3))
        self.assertFalse(node.warm_key(3), "the cold tier is not authoritative")
        self.assertIsNone(
            mesh.resolve_unique_last_byte(0, mesh.nodes[3].relay_byte),
            "and nothing resolves from it",
        )


class FirmwareVersions(unittest.TestCase):
    """Pins each release series' rules to the tags in this repository.

    A series profile is that series' final release: 2.4 = v2.4.3, 2.5 = v2.5.23, 2.6 = v2.6.13,
    2.7 = v2.7.21, 2.8 = this tree. Every expectation below was read off the named file at that tag.
    """

    def test_contention_window_constants_per_series(self):
        """RadioInterface.h CWmin/CWmax, and the SNR range getCWsize maps onto them."""
        expected = {
            "2.4": (2, 8, 15.0),
            "2.5": (2, 7, 15.0),
            "2.6": (3, 8, 10.0),
            "2.7": (3, 8, 10.0),
            "2.8": (3, 8, 10.0),
        }
        for version, (cw_min, cw_max, snr_max) in expected.items():
            profile = M.Profile(version)
            self.assertEqual((profile.cw_min, profile.cw_max), (cw_min, cw_max), version)
            self.assertEqual(profile.snr_min, -20.0, version)
            self.assertEqual(profile.snr_max, snr_max, version)

    def test_cw_size_at_zero_snr_differs_by_series(self):
        """getCWsize(0) under each series' own map() arguments, worked through by hand.

        2.4: (0+20)*(8-2)//(15+20) + 2 = 120//35 + 2 = 5.
        2.5: (0+20)*(7-2)//(15+20) + 2 = 100//35 + 2 = 4.
        2.6+: (0+20)*(8-3)//(10+20) + 3 = 100//30 + 3 = 6.
        """
        for version, expected in (("2.4", 5), ("2.5", 4), ("2.6", 6), ("2.8", 6)):
            mesh = small_mesh(profile=version)
            self.assertEqual(mesh.cw_size(0, 0.0), expected, version)

    def test_the_router_offset_is_in_every_series(self):
        """The 2 * CWmax * slot a non-early rebroadcaster waits is in 2.4 already.

        It was attributed to 2.8 when the fold-in landed. getTxDelayMsecWeighted has carried it since
        before 2.4, so only `legacy` - this transport's own earlier model - is missing it.
        """
        for version in M.VERSIONS:
            mesh = small_mesh(profile=version)
            mesh.nodes[0].role = M.CLIENT
            floor = 2 * mesh.nodes[0].profile.cw_max * mesh.slot_time_ms()
            for _ in range(20):
                self.assertGreaterEqual(mesh.tx_delay_weighted(0, 0.0), floor, version)
        self.assertFalse(M.Profile("legacy").router_offset)

    def test_repeater_rebroadcasts_early_until_2_8(self):
        """shouldRebroadcastEarlyLikeRouter dropped REPEATER; up to 2.7 the test admitted it."""
        for version, early in (("2.4", True), ("2.6", True), ("2.7", True), ("2.8", False)):
            mesh = small_mesh(profile=version)
            mesh.nodes[0].role = M.REPEATER
            self.assertEqual(mesh._rebroadcasts_early(0), early, version)

    def test_client_base_rebroadcasts_early_only_in_2_7_and_only_for_favourites(self):
        """v2.7.9's CLIENT_BASE branch returns nodeDB->isFromOrToFavoritedNode(p)."""
        mesh = small_mesh(profile="2.7", nodes=6)
        mesh.nodes[0].role = M.CLIENT_BASE
        mine = M.Packet(1, 2, 1, 40, hop_limit=3)
        self.assertFalse(mesh._rebroadcasts_early(0, mine))
        mesh.nodes[0].favourites = {2}
        self.assertTrue(mesh._rebroadcasts_early(0, mine))
        # 2.8 took the branch out, so a CLIENT_BASE waits behind the offset whoever sent it.
        modern = small_mesh(profile="2.8", nodes=6)
        modern.nodes[0].role = M.CLIENT_BASE
        modern.nodes[0].favourites = {2}
        self.assertFalse(modern._rebroadcasts_early(0, mine))

    def test_roles_fall_back_when_the_series_lacks_them(self):
        """ROUTER_LATE arrived in v2.5.18 and CLIENT_BASE in v2.7.9."""
        for version, late, base in (
            ("2.4", False, False),
            ("2.5", True, False),
            ("2.6", True, False),
            ("2.7", True, True),
            ("2.8", True, True),
        ):
            profile = M.Profile(version)
            self.assertEqual(profile.router_late_role, late, version)
            self.assertEqual(profile.client_base_role, base, version)
            mesh = small_mesh(
                profile=version,
                nodes=10,
                router_late_fraction=0.2,
                client_base_fraction=0.2,
            )
            roles = {n.role for n in mesh.nodes}
            self.assertEqual(M.ROUTER_LATE in roles, late, version)
            self.assertEqual(M.CLIENT_BASE in roles, base, version)

    def test_queue_orders_by_priority_and_id_before_2_5(self):
        """2.4's CompareMeshPacketFunc: priority alone, ties to the lower id, no late group."""
        mesh = small_mesh(profile="2.4", nodes=4)
        radio = mesh.nodes[0]
        for packet_id, priority in ((5, M.PRIORITY_DEFAULT), (3, M.PRIORITY_DEFAULT)):
            packet = M.Packet(packet_id, 1, 1, 40, hop_limit=3)
            packet.priority = priority
            mesh._enqueue(radio, M.QueueEntry(packet))
        self.assertEqual([e.packet.id for e in radio.queue], [3, 5])

    def test_a_relayed_packet_outranks_our_own_from_2_5(self):
        """2.5's tie-break at equal priority: !isFromUs(p1) && isFromUs(p2)."""
        mesh = small_mesh(profile="2.5", nodes=4)
        radio = mesh.nodes[0]
        ours = M.Packet(1, 0, 1, 40, hop_limit=3)
        relayed = M.Packet(2, 3, 1, 40, hop_limit=3)
        mesh._enqueue(radio, M.QueueEntry(ours))
        mesh._enqueue(radio, M.QueueEntry(relayed))
        self.assertEqual([e.packet.id for e in radio.queue], [2, 1])
        # 2.4 has no such rule, so the second packet simply queues behind the first.
        old = small_mesh(profile="2.4", nodes=4)
        mesh._enqueue(old.nodes[0], M.QueueEntry(M.Packet(1, 0, 1, 40, hop_limit=3)))
        mesh._enqueue(old.nodes[0], M.QueueEntry(M.Packet(2, 3, 1, 40, hop_limit=3)))
        self.assertEqual([e.packet.id for e in old.nodes[0].queue], [1, 2])

    def test_hop_preservation_starts_at_2_7_and_gains_ambiguity_checking_in_2_8(self):
        """Router::shouldDecrementHopLimit arrived in v2.7.11 and resolves uniquely only here.

        2.7 walks its store for favourited router-like nodes and preserves the hop on the first
        matching last byte. This tree resolves the byte first and charges the hop when a second node
        answers to it.
        """
        for version in ("2.4", "2.5", "2.6"):
            self.assertFalse(M.Profile(version).preserve_hops, version)

        for version, preserved in (("2.7", True), ("2.8", False)):
            mesh = small_mesh(profile=version, nodes=6)
            # Two favourited routers sharing a last byte: the relay byte cannot say which relayed.
            mesh.nodes[1].node_num = 0x0000AA11
            mesh.nodes[2].node_num = 0x0000BB11
            for peer in (1, 2):
                mesh.nodes[peer].role = M.ROUTER
                heard(mesh, 0, peer)
            mesh.nodes[0].role = M.ROUTER
            mesh.nodes[0].favourites = {1, 2}
            packet = M.Packet(9, 3, 1, 40, hop_limit=2)
            packet.hop_start = 3  # one hop taken already, so the first-hop rule does not apply
            packet.relay_node = 0x11
            self.assertEqual(
                mesh.should_decrement_hop_limit(0, packet), not preserved, version
            )

    def test_unicast_gets_five_attempts_only_in_this_tree(self):
        """NUM_RELIABLE_UNICAST_ATTEMPTS is new; before it a DM had the broadcast count of 3."""
        for version in ("2.4", "2.5", "2.6", "2.7"):
            self.assertEqual(M.Profile(version).unicast_attempts, 3, version)
        self.assertEqual(M.Profile("2.8").unicast_attempts, 5)

    def test_next_hop_routing_starts_at_2_6(self):
        """NextHopRouter is v2.6.0; learning a route from relay_node is v2.7.13."""
        expected = {
            "2.4": (False, False),
            "2.5": (False, False),
            "2.6": (True, False),
            "2.7": (True, True),
            "2.8": (True, True),
        }
        for version, (routing, learning) in expected.items():
            profile = M.Profile(version)
            self.assertEqual(profile.next_hop_routing, routing, version)
            self.assertEqual(profile.next_hop_learning, learning, version)

    def test_hot_store_size_per_series(self):
        """mesh-pb-constants.h: a flat 100 until 2.6, nRF52 at 80 in 2.6 and 2.7, 120 here."""
        expected = {"2.4": 100, "2.5": 100, "2.6": 80, "2.7": 80, "2.8": 120}
        for version, slots in expected.items():
            table = M.PLATFORM_HOT_STORE_BY_VERSION[M.Profile(version).hot_store_model]
            self.assertEqual(table["nrf52840"], slots, version)
            self.assertEqual(table["stm32wl"], 100 if version in ("2.4", "2.5") else 10)

    def test_legacy_is_not_a_firmware_version(self):
        profile = M.Profile("legacy")
        self.assertIsNone(profile.version)
        for version in M.VERSIONS:
            self.assertFalse(profile.at_least(version), version)
        self.assertTrue(M.Profile("2.7").at_least("2.6"))
        self.assertFalse(M.Profile("2.6").at_least("2.7"))
        with self.assertRaises(ValueError):
            M.Profile("2.9")


class EndToEnd(unittest.TestCase):
    def test_a_flood_reaches_the_mesh_and_the_counters_add_up(self):
        mesh = small_mesh(nodes=25, seed=7)
        for _ in range(5):
            mesh.originate(0, 70, 40, kind="advert")
            mesh.run(mesh.now + 30000.0)
        stats = mesh.stats
        self.assertGreater(stats["receptions"], 0)
        self.assertGreater(stats["transmissions"], 5)
        # Every relay that reached the air was queued first, and everything queued either flew, was
        # cancelled, was swapped out by a hop-limit upgrade, or is still sitting there. The upgrade
        # term is the one that is easy to miss: perhapsHandleUpgradedPacket pops a queued copy that
        # neither flew nor was cancelled, then queues the better copy in its place.
        #
        # This accounting is only exact while nothing overflows, because a queue-full drop counts
        # the refused newcomer and the evicted incumbent under one counter.
        self.assertLessEqual(stats["rebroadcasts"], stats["rebroadcasts_queued"])
        self.assertEqual(stats["queue_drops"], 0)
        still_queued = sum(len(n.queue) for n in mesh.nodes)
        self.assertEqual(
            stats["rebroadcasts_queued"],
            stats["rebroadcasts"]
            + stats["rebroadcasts_cancelled"]
            + stats["hop_upgrades"]
            + still_queued,
        )

    def test_every_profile_runs(self):
        for name in M.VERSIONS + ("legacy",):
            mesh = small_mesh(nodes=20, seed=3, profile=name, router_fraction=0.15)
            for _ in range(4):
                mesh.originate(0, 70, 40, kind="advert")
                mesh.run(mesh.now + 30000.0)
            self.assertGreater(mesh.stats["receptions"], 0, name)


if __name__ == "__main__":
    unittest.main()
