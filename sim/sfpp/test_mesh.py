"""Pins the transport's MAC and routing to what the firmware in this tree actually does.

Every expected value here was computed by hand from the C++ named in the test, not from a previous
run of this simulator. That is the only way a test like this is worth anything: a regression test
against my own output would pass just as happily if I had read RadioInterface wrong.

Run from `sim/`:  python3 -m unittest sfpp.test_mesh -v
"""

import random
import unittest

from . import mesh as M


def small_mesh(profile="2.8", nodes=12, seed=11, **kwargs):
    rng = random.Random(seed)
    conf = M.make_config()
    return M.build(conf, nodes, 4000.0, rng, hop_limit=3, profile=profile, **kwargs)


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
        cw = mesh.cw_size(0.0)
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
        cw = mesh.cw_size(-2.0)
        self.assertAlmostEqual(
            mesh.tx_delay_weighted_worst(-2.0), (2 * M.CW_MAX + 2**cw) * slot
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
            entry.tx_after, mesh.now + mesh.tx_delay_weighted_worst(5.0)
        )
        self.assertEqual(mesh.stats["late_window_clamps"], 1)


class HopLimit(unittest.TestCase):
    """Router::shouldDecrementHopLimit."""

    def _favourite_pair(self):
        mesh = small_mesh()
        mesh.nodes[0].role = M.ROUTER
        mesh.nodes[1].role = M.ROUTER
        mesh.nodes[0].favourites = {1}
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
        """Two nodes sharing a last byte means the safe branch: decrement."""
        mesh, packet = self._favourite_pair()
        byte = mesh.nodes[1].relay_byte
        mesh._by_relay_byte[byte] = [1, 2]
        self.assertTrue(mesh.should_decrement_hop_limit(0, packet))

    def test_legacy_profile_always_pays(self):
        mesh = small_mesh(profile="legacy")
        mesh.nodes[0].role = M.ROUTER
        mesh.nodes[0].favourites = {1}
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
        mesh.nodes[0].known_origins.add(5)
        self.assertTrue(mesh.is_rebroadcaster(0, M.Packet(1, 5, 70, 40)))


class LastByteResolution(unittest.TestCase):
    """NodeDB::resolveLastByte - unique, ambiguous, or unknown."""

    def test_unique_byte_resolves(self):
        mesh = small_mesh()
        target = next(
            i
            for i in range(len(mesh.nodes))
            if len(mesh._by_relay_byte[mesh.nodes[i].relay_byte]) == 1
        )
        self.assertEqual(
            mesh.resolve_unique_last_byte(0, mesh.nodes[target].relay_byte), target
        )

    def test_shared_byte_is_ambiguous(self):
        mesh = small_mesh()
        byte = mesh.nodes[3].relay_byte
        mesh._by_relay_byte[byte] = [3, 4]
        self.assertIsNone(mesh.resolve_unique_last_byte(0, byte))

    def test_unknown_byte_resolves_to_nothing(self):
        mesh = small_mesh()
        used = {n.relay_byte for n in mesh.nodes}
        spare = next(b for b in range(1, 256) if b not in used)
        self.assertIsNone(mesh.resolve_unique_last_byte(0, spare))

    def test_zero_is_no_preference_not_a_node(self):
        mesh = small_mesh()
        self.assertIsNone(mesh.resolve_unique_last_byte(0, M.NO_NEXT_HOP_PREFERENCE))


class NextHop(unittest.TestCase):
    """NextHopRouter::getNextHop and its decay back to flooding."""

    def _routed(self):
        mesh = small_mesh()
        peer = mesh.neighbours[0][0]
        dest = 7 if 7 not in (0, peer) else 8
        mesh.nodes[0].routes[dest] = mesh.nodes[peer].relay_byte
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
        self.assertNotIn(dest, mesh.nodes[0].routes)
        self.assertEqual(mesh.stats["route_health_expiries"], 1)

    def test_three_failures_kill_the_route(self):
        mesh, dest, _ = self._routed()
        for _ in range(M.ROUTE_FAILURE_THRESHOLD):
            mesh.note_route_failure(0, dest)
        self.assertIsNone(mesh.get_next_hop(0, dest, 0))
        self.assertNotIn(dest, mesh.nodes[0].routes)

    def test_legacy_profile_has_no_unicast_routing(self):
        mesh = small_mesh(profile="legacy")
        mesh.nodes[0].routes[7] = mesh.nodes[1].relay_byte
        self.assertIsNone(mesh.get_next_hop(0, 7, 0))

    def test_relay_gate_ignores_a_packet_addressed_to_another_hop(self):
        mesh = small_mesh()
        packet = M.Packet(5, 9, 70, 40, hop_limit=3, destination=6)
        packet.rx_rssi, packet.rx_snr = -100.0, 2.0
        used = {n.relay_byte for n in mesh.nodes}
        packet.next_hop = next(b for b in range(1, 256) if b not in used)
        self.assertFalse(mesh.perhaps_rebroadcast(0, packet))


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
        mesh.nodes[0].routes[dest] = mesh.nodes[peer].relay_byte
        mesh.note_route_learned(0, dest, mesh.nodes[peer].relay_byte)
        mesh.neighbours[0] = []  # the route is dead; nothing comes back
        mesh.originate(0, 70, 40, destination=dest, want_ack=True)
        mesh.run(1800000.0)
        self.assertGreater(mesh.stats["next_hop_fallbacks"], 0)
        self.assertNotIn(dest, mesh.nodes[0].routes)


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


class EndToEnd(unittest.TestCase):
    def test_a_flood_reaches_the_mesh_and_the_counters_add_up(self):
        mesh = small_mesh(nodes=25, seed=7)
        for _ in range(5):
            mesh.originate(0, 70, 40, kind="advert")
            mesh.run(mesh.now + 30000.0)
        stats = mesh.stats
        self.assertGreater(stats["receptions"], 0)
        self.assertGreater(stats["transmissions"], 5)
        # Every relay that reached the air was queued first, and everything queued either flew,
        # was cancelled, was dropped, or is still sitting there.
        self.assertLessEqual(stats["rebroadcasts"], stats["rebroadcasts_queued"])
        still_queued = sum(len(n.queue) for n in mesh.nodes)
        self.assertEqual(
            stats["rebroadcasts_queued"],
            stats["rebroadcasts"] + stats["rebroadcasts_cancelled"] + still_queued,
        )

    def test_both_profiles_run(self):
        for name in ("2.8", "legacy"):
            mesh = small_mesh(nodes=20, seed=3, profile=name, router_fraction=0.15)
            for _ in range(4):
                mesh.originate(0, 70, 40, kind="advert")
                mesh.run(mesh.now + 30000.0)
            self.assertGreater(mesh.stats["receptions"], 0, name)


if __name__ == "__main__":
    unittest.main()
